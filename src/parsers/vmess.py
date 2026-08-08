"""vmess:// URI 解析 → Clash 风格 Node。"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import unquote

from src.models import Node
from src.parsers.common import node_from_clash_dict

logger = logging.getLogger(__name__)


def _b64decode(data: str) -> bytes:
    s = data.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s)


def parse_vmess(uri: str, source: str = "") -> Node | None:
    """解析 vmess://base64(json) 链接。"""
    try:
        if not uri.lower().startswith("vmess://"):
            return None
        payload = uri[8:].strip()
        # 部分链接 name 在 # 后
        if "#" in payload:
            payload, frag = payload.split("#", 1)
            remark = unquote(frag)
        else:
            remark = None

        raw_json = _b64decode(payload).decode("utf-8", errors="ignore")
        obj: dict[str, Any] = json.loads(raw_json)

        host = obj.get("add") or obj.get("host") or obj.get("server")
        port = obj.get("port")
        uuid = obj.get("id")
        name = remark or obj.get("ps") or obj.get("name") or f"vmess-{host}"
        net = (obj.get("net") or "tcp").lower()
        tls_flag = str(obj.get("tls") or "").lower() in ("tls", "1", "true")
        sni = obj.get("sni") or obj.get("host") or ""
        path = obj.get("path") or "/"
        host_header = obj.get("host") or ""
        aid = obj.get("aid", 0)
        cipher = obj.get("scy") or obj.get("security") or "auto"

        clash: dict[str, Any] = {
            "name": str(name),
            "type": "vmess",
            "server": str(host),
            "port": int(port),
            "uuid": str(uuid),
            "alterId": int(aid) if aid is not None else 0,
            "cipher": cipher,
            "udp": True,
        }
        if tls_flag:
            clash["tls"] = True
            if sni:
                clash["servername"] = sni
        if net and net != "tcp":
            clash["network"] = net
        if net == "ws":
            clash["ws-opts"] = {
                "path": path or "/",
                "headers": {"Host": host_header} if host_header else {},
            }
        elif net == "grpc":
            clash["grpc-opts"] = {
                "grpc-service-name": obj.get("path") or obj.get("serviceName") or ""
            }
        elif net == "h2":
            clash["h2-opts"] = {
                "path": path or "/",
                "host": [host_header] if host_header else [],
            }

        return node_from_clash_dict(clash, source=source)
    except Exception as e:
        logger.debug("vmess parse failed: %s", e)
        return None