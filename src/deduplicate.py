"""节点去重：基于协议指纹。"""

from __future__ import annotations

import hashlib
import logging

from src.models import Node

logger = logging.getLogger(__name__)


def fingerprint(node: Node) -> str:
    """
    SHA256(type + server + port + uuid/password + network + sni)
    名称不同但配置相同 → 同一指纹。
    """
    parts = [
        (node.type or "").lower().strip(),
        (node.server or "").lower().strip(),
        str(node.port or 0),
        node.credential(),
        (node.network or "").lower().strip(),
        (node.sni or "").lower().strip(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deduplicate(nodes: list[Node]) -> tuple[list[Node], int]:
    """去重，保留首次出现。返回 (unique_nodes, removed_count)。"""
    seen: set[str] = set()
    unique: list[Node] = []
    removed = 0
    for n in nodes:
        fp = fingerprint(n)
        if fp in seen:
            removed += 1
            continue
        seen.add(fp)
        unique.append(n)
    logger.info("Deduplicate: %d -> %d (removed %d)", len(nodes), len(unique), removed)
    return unique, removed