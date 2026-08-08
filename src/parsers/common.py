"""从 Clash 风格 dict 构建 Node，并抽取传输摘要字段。"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Node

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {
    "ss",
    "ssr",
    "vmess",
    "vless",
    "trojan",
    "hysteria",
    "hysteria2",
    "tuic",
    "wireguard",
    "snell",
    "http",
    "socks5",
    "mieru",
    "anytls",
}


def _as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return None


def extract_transport(raw: dict[str, Any]) -> dict[str, Any]:
    """从 Clash proxy dict 提取 network / sni / ws / grpc 摘要。"""
    network = raw.get("network") or raw.get("net")
    if isinstance(network, str):
        network = network.lower()
    else:
        network = None

    tls = _as_bool(raw.get("tls"))
    sni = raw.get("sni") or raw.get("servername") or raw.get("server-name")
    if not sni:
        opts = raw.get("reality-opts") or raw.get("reality_opts") or {}
        if isinstance(opts, dict):
            sni = opts.get("server-name") or opts.get("servername")
    if not sni and isinstance(raw.get("tls"), dict):
        sni = raw["tls"].get("server_name") or raw["tls"].get("sni")

    ws_path = None
    ws_headers: dict[str, str] | None = None
    grpc_service_name = None

    ws_opts = raw.get("ws-opts") or raw.get("ws_opts") or {}
    if isinstance(ws_opts, dict) and ws_opts:
        network = network or "ws"
        ws_path = ws_opts.get("path")
        headers = ws_opts.get("headers")
        if isinstance(headers, dict):
            ws_headers = {str(k): str(v) for k, v in headers.items()}

    # 旧字段
    if not ws_path and raw.get("ws-path"):
        ws_path = raw.get("ws-path")
        network = network or "ws"
    if not ws_headers and isinstance(raw.get("ws-headers"), dict):
        ws_headers = {str(k): str(v) for k, v in raw["ws-headers"].items()}

    grpc_opts = raw.get("grpc-opts") or raw.get("grpc_opts") or {}
    if isinstance(grpc_opts, dict) and grpc_opts:
        network = network or "grpc"
        grpc_service_name = grpc_opts.get("grpc-service-name") or grpc_opts.get(
            "serviceName"
        )

    h2_opts = raw.get("h2-opts") or raw.get("h2_opts") or {}
    if isinstance(h2_opts, dict) and h2_opts and not network:
        network = "h2"

    return {
        "tls": tls,
        "network": network,
        "sni": str(sni) if sni else None,
        "ws_path": str(ws_path) if ws_path else None,
        "ws_headers": ws_headers,
        "grpc_service_name": str(grpc_service_name) if grpc_service_name else None,
    }


def node_from_clash_dict(
    item: dict[str, Any],
    source: str = "",
) -> Node | None:
    """将单个 Clash proxy 映射为 Node；字段不完整则返回 None。"""
    if not isinstance(item, dict):
        return None

    ptype = str(item.get("type") or "").lower().strip()
    if not ptype or ptype not in SUPPORTED_TYPES:
        return None

    server = item.get("server")
    port = item.get("port")
    name = item.get("name") or f"{ptype}-{server}-{port}"

    if not server or port is None:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    if port_i <= 0 or port_i > 65535:
        return None

    uuid = item.get("uuid")
    password = item.get("password")
    # ss 的 cipher+password 组合
    if ptype == "ss" and not password:
        password = item.get("password")

    # 必须保留完整 raw，重命名时只改 name
    raw = dict(item)
    raw["name"] = str(name)
    raw["type"] = ptype
    raw["server"] = str(server)
    raw["port"] = port_i

    transport = extract_transport(raw)

    return Node(
        name=str(name),
        type=ptype,
        server=str(server).strip(),
        port=port_i,
        raw=raw,
        uuid=str(uuid) if uuid else None,
        password=str(password) if password else None,
        tls=transport["tls"],
        network=transport["network"],
        sni=transport["sni"],
        ws_path=transport["ws_path"],
        ws_headers=transport["ws_headers"],
        grpc_service_name=transport["grpc_service_name"],
        original_source=source,
    )