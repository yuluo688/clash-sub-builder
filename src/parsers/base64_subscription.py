"""Base64 订阅 / 纯文本 URI 列表解析。"""

from __future__ import annotations

import base64
import logging
import re

from src.models import Node
from src.parsers.shadowsocks import parse_ss
from src.parsers.trojan import parse_trojan
from src.parsers.vless import parse_vless
from src.parsers.vmess import parse_vmess

logger = logging.getLogger(__name__)

_URI_RE = re.compile(
    r"(?P<uri>(?:vmess|vless|trojan|ss|ssr)://[^\s]+)",
    re.IGNORECASE,
)


def _try_b64_decode(text: str) -> str | None:
    s = text.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if not s:
        return None
    # 明显不是 base64 则跳过
    if "://" in s[:20]:
        return None
    try:
        pad = (-len(s)) % 4
        if pad:
            s += "=" * pad
        raw = base64.b64decode(s, validate=False)
        decoded = raw.decode("utf-8", errors="ignore")
        if not decoded.strip():
            return None
        return decoded
    except Exception:
        return None


def parse_share_uri(uri: str, source: str = "") -> Node | None:
    """解析单条分享链接。"""
    u = uri.strip()
    if not u:
        return None
    # 去掉可能的引号
    u = u.strip("\"'")
    lower = u.lower()
    try:
        if lower.startswith("vmess://"):
            return parse_vmess(u, source=source)
        if lower.startswith("vless://"):
            return parse_vless(u, source=source)
        if lower.startswith("trojan://"):
            return parse_trojan(u, source=source)
        if lower.startswith("ss://"):
            return parse_ss(u, source=source)
        # ssr 可选：复杂度高，跳过并记 debug
        if lower.startswith("ssr://"):
            logger.debug("ssr URI skipped (optional): %s", source)
            return None
    except Exception as e:
        logger.debug("URI parse error: %s", e)
    return None


def parse_base64_subscription(content: str, source: str = "") -> list[Node]:
    """解析 Base64 订阅或纯文本多行 URI。"""
    if not content:
        return []

    text = content.strip()
    decoded = _try_b64_decode(text)
    if decoded:
        text = decoded

    nodes: list[Node] = []
    seen_lines: set[str] = set()

    # 优先按行
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen_lines:
            continue
        seen_lines.add(line)
        node = parse_share_uri(line, source=source)
        if node:
            nodes.append(node)
            continue
        # 行内可能混有多条
        for m in _URI_RE.finditer(line):
            uri = m.group("uri").rstrip("`,\"'")
            n = parse_share_uri(uri, source=source)
            if n:
                nodes.append(n)

    # 若按行无结果，全文扫 URI
    if not nodes:
        for m in _URI_RE.finditer(text):
            uri = m.group("uri").rstrip("`,\"'")
            n = parse_share_uri(uri, source=source)
            if n:
                nodes.append(n)

    return nodes