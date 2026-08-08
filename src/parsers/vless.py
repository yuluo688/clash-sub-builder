"""vless:// URI 解析 → Clash 风格 Node。"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from src.models import Node
from src.parsers.common import node_from_clash_dict

logger = logging.getLogger(__name__)


def parse_vless(uri: str, source: str = "") -> Node | None:
    try:
        if not uri.lower().startswith("vless://"):
            return None
        parsed = urlparse(uri)
        uuid = unquote(parsed.username or "")
        host = parsed.hostname
        port = parsed.port
        if not uuid or not host or not port:
            return None

        qs = parse_qs(parsed.query)

        def q(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return unquote(vals[0]) if vals else default

        name = unquote(parsed.fragment) if parsed.fragment else f"vless-{host}"
        network = (q("type") or q("net") or "tcp").lower()
        security = (q("security") or "").lower()
        sni = q("sni") or q("host") or q("peer")
        fp = q("fp") or q("fingerprint")
        flow = q("flow")
        path = q("path") or "/"
        host_header = q("host")
        service_name = q("serviceName") or q("service-name") or path
        pbk = q("pbk")
        sid = q("sid")
        spx = q("spx")

        clash: dict[str, Any] = {
            "name": name,
            "type": "vless",
            "server": host,
            "port": int(port),
            "uuid": uuid,
            "udp": True,
        }
        if flow:
            clash["flow"] = flow

        if security in ("tls", "reality"):
            clash["tls"] = True
            if sni:
                clash["servername"] = sni
            if fp:
                clash["client-fingerprint"] = fp
            if security == "reality":
                reality: dict[str, Any] = {}
                if pbk:
                    reality["public-key"] = pbk
                if sid:
                    reality["short-id"] = sid
                if spx:
                    reality["spider-x"] = spx
                if reality:
                    clash["reality-opts"] = reality

        if network and network != "tcp":
            clash["network"] = network
        if network == "ws":
            clash["ws-opts"] = {
                "path": path,
                "headers": {"Host": host_header} if host_header else {},
            }
        elif network == "grpc":
            clash["grpc-opts"] = {"grpc-service-name": service_name}
        elif network in ("h2", "http"):
            clash["network"] = "h2" if network == "h2" else network
            clash["h2-opts"] = {
                "path": path,
                "host": [host_header] if host_header else [],
            }

        return node_from_clash_dict(clash, source=source)
    except Exception as e:
        logger.debug("vless parse failed: %s", e)
        return None