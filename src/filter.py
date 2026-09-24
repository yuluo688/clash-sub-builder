"""节点筛选：延迟 / 完整性 / 每国上限 / 总数上限。"""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import zip_longest

from src.models import Node

logger = logging.getLogger(__name__)


def _round_robin(groups: list[list[Node]]) -> list[Node]:
    return [node for row in zip_longest(*groups) for node in row if node is not None]


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

    # 经 Runner 测速时按延迟筛选；未测速时跨来源、国家交错取样，避免
    # 输入顺序或国家名排序将配额全部分给少数来源/地区。
    by_cc: dict[str, list[Node]] = defaultdict(list)
    for n in alive:
        cc = (n.country_code or "OTHER").upper()
        by_cc[cc].append(n)

    if require_latency:
        selected: list[Node] = []
        for group in by_cc.values():
            group.sort(key=lambda x: (x.latency, x.name))
            selected.extend(group[:max_nodes_per_country] if max_nodes_per_country > 0 else group)
        selected.sort(key=lambda x: (x.latency, x.country_code, x.name))
    else:
        per_country: list[list[Node]] = []
        for group in by_cc.values():
            by_source: dict[str, list[Node]] = defaultdict(list)
            for node in group:
                by_source[node.original_source].append(node)
            for source_nodes in by_source.values():
                source_nodes.sort(
                    key=lambda node: (
                        node.latency is None or node.latency <= 0,
                        node.latency if node.latency is not None and node.latency > 0 else float("inf"),
                    )
                )
            balanced = _round_robin(list(by_source.values()))
            balanced.sort(key=lambda node: node.latency is None or node.latency <= 0)
            per_country.append(balanced[:max_nodes_per_country] if max_nodes_per_country > 0 else balanced)
        selected = _round_robin(per_country)

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
