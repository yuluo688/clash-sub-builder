"""ss:// URI 解析 → Clash 风格 Node。"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote

from src.models import Node
from src.parsers.common import node_from_clash_dict

logger = logging.getLogger(__name__)


def _b64decode_str(data: str) -> str:
    s = data.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s).decode("utf-8", errors="ignore")


def parse_ss(uri: str, source: str = "") -> Node | None:
    """支持 SIP002 与 legacy ss://base64 格式。"""
    try:
        if not uri.lower().startswith("ss://"):
            return None

        body = uri[5:]
        name = None
        if "#" in body:
            body, frag = body.split("#", 1)
            name = unquote(frag)

        # SIP002: ss://base64(method:password)@host:port 或 method:password@host:port
        if "@" in body:
            userinfo, hostinfo = body.rsplit("@", 1)
            host_part = hostinfo
            query = ""
            if "?" in hostinfo:
                host_part, query = hostinfo.split("?", 1)

            decoded = userinfo
            if ":" not in userinfo:
                try:
                    decoded = _b64decode_str(userinfo)
                except Exception:
                    decoded = userinfo

            if ":" not in decoded:
                return None
            method, password = decoded.split(":", 1)
            method = unquote(method)
            password = unquote(password)

            if host_part.startswith("["):
                m = re.match(r"^\[([^\]]+)\]:(\d+)$", host_part)
                if not m:
                    return None
                host, port_s = m.group(1), m.group(2)
            else:
                if ":" not in host_part:
                    return None
                host, port_s = host_part.rsplit(":", 1)
            port = int(port_s)

            qs = parse_qs(query) if query else {}
            plugin = ""
            if "plugin" in qs:
                plugin = unquote(qs["plugin"][0])

            clash: dict[str, Any] = {
                "name": name or f"ss-{host}",
                "type": "ss",
                "server": host,
                "port": port,
                "cipher": method,
                "password": password,
                "udp": True,
            }
            if plugin:
                parts = plugin.split(";")
                plugin_name = parts[0]
                opts: dict[str, str] = {}
                for p in parts[1:]:
                    if "=" in p:
                        k, v = p.split("=", 1)
                        opts[k] = v
                if "obfs" in plugin_name or opts.get("obfs"):
                    clash["plugin"] = "obfs"
                    clash["plugin-opts"] = {
                        "mode": opts.get("obfs", "http"),
                        "host": opts.get("obfs-host", ""),
                    }
                elif "v2ray" in plugin_name:
                    clash["plugin"] = "v2ray-plugin"
                    clash["plugin-opts"] = {
                        "mode": opts.get("mode", "websocket"),
                        "tls": opts.get("tls") == "true" or "tls" in opts,
                        "host": opts.get("host", ""),
                        "path": opts.get("path", "/"),
                    }

            return node_from_clash_dict(clash, source=source)

        # legacy: ss://base64(method:password@host:port)
        decoded = _b64decode_str(body)
        if "@" not in decoded:
            return None
        userinfo, hostinfo = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        if hostinfo.startswith("["):
            m = re.match(r"^\[([^\]]+)\]:(\d+)$", hostinfo)
            if not m:
                return None
            host, port_s = m.group(1), m.group(2)
        else:
            host, port_s = hostinfo.rsplit(":", 1)
        clash = {
            "name": name or f"ss-{host}",
            "type": "ss",
            "server": host,
            "port": int(port_s),
            "cipher": method,
            "password": password,
            "udp": True,
        }
        return node_from_clash_dict(clash, source=source)
    except Exception as e:
        logger.debug("ss parse failed: %s", e)
        return None