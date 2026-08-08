"""trojan:// URI 解析 → Clash 风格 Node。"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from src.models import Node
from src.parsers.common import node_from_clash_dict

logger = logging.getLogger(__name__)


def parse_trojan(uri: str, source: str = "") -> Node | None:
    try:
        if not uri.lower().startswith("trojan://"):
            return None
        parsed = urlparse(uri)
        password = unquote(parsed.username or "")
        host = parsed.hostname
        port = parsed.port or 443
        if not password or not host:
            return None

        qs = parse_qs(parsed.query)

        def q(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return unquote(vals[0]) if vals else default

        name = unquote(parsed.fragment) if parsed.fragment else f"trojan-{host}"
        sni = q("sni") or q("peer") or host
        network = (q("type") or q("net") or "tcp").lower()
        path = q("path") or "/"
        host_header = q("host")
        allow_insecure = q("allowInsecure") in ("1", "true") or q("insecure") in (
            "1",
            "true",
        )
        fp = q("fp") or q("fingerprint")

        clash: dict[str, Any] = {
            "name": name,
            "type": "trojan",
            "server": host,
            "port": int(port),
            "password": password,
            "udp": True,
            "sni": sni,
            "skip-cert-verify": allow_insecure,
        }
        if fp:
            clash["client-fingerprint"] = fp

        if network and network != "tcp":
            clash["network"] = network
        if network == "ws":
            clash["ws-opts"] = {
                "path": path,
                "headers": {"Host": host_header} if host_header else {},
            }
        elif network == "grpc":
            service = q("serviceName") or q("service-name") or path
            clash["grpc-opts"] = {"grpc-service-name": service}

        return node_from_clash_dict(clash, source=source)
    except Exception as e:
        logger.debug("trojan parse failed: %s", e)
        return None