"""节点筛选：延迟 / 完整性 / 每国上限 / 总数上限。"""

from __future__ import annotations

import logging
from collections import defaultdict

from src.models import Node

logger = logging.getLogger(__name__)


def is_complete(node: Node) -> bool:
    if not node.type or not node.server or not node.port:
        return False
    if node.port <= 0 or node.port > 65535:
        return False
    # 常见协议需要凭证
    t = node.type.lower()
    if t in ("vmess", "vless") and not (node.uuid or node.raw.get("uuid")):
        return False
    if t in ("trojan", "ss", "ssr") and not (node.password or node.raw.get("password")):
        return False
    return True


def filter_nodes(
    nodes: list[Node],
    max_latency: int = 800,
    max_nodes_total: int = 500,
    max_nodes_per_country: int = 50,
    require_latency: bool = True,
) -> tuple[list[Node], int]:
    """
    筛选可用节点。
    require_latency=True 时丢弃未测速或失败节点。
    返回 (filtered, removed_count)。
    """
    before = len(nodes)
    alive: list[Node] = []
    for n in nodes:
        if not is_complete(n):
            continue
        if require_latency:
            if n.latency is None or n.latency <= 0:
                continue
            if n.latency > max_latency:
                continue
        alive.append(n)

    # 按国家分组，延迟升序
    by_cc: dict[str, list[Node]] = defaultdict(list)
    for n in alive:
        cc = (n.country_code or "OTHER").upper()
        by_cc[cc].append(n)

    selected: list[Node] = []
    for cc, group in by_cc.items():
        group.sort(key=lambda x: (x.latency if x.latency is not None else 10**9, x.name))
        selected.extend(group[: max_nodes_per_country if max_nodes_per_country > 0 else len(group)])

    # 全局按延迟排序后截断
    selected.sort(key=lambda x: (x.latency if x.latency is not None else 10**9, x.country_code, x.name))
    if max_nodes_total > 0:
        selected = selected[:max_nodes_total]

    removed = before - len(selected)
    logger.info(
        "Filter: %d -> %d (removed %d, max_latency=%s, per_country=%s, total=%s)",
        before,
        len(selected),
        removed,
        max_latency,
        max_nodes_per_country,
        max_nodes_total,
    )
    return selected, removed