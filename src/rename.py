"""统一重命名：emoji 国家代码[-城市]-编号[-延迟]。只改 name。"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from src.geo import country_emoji
from src.models import Node

logger = logging.getLogger(__name__)

_CITY_SAFE = re.compile(r"[^A-Za-z0-9]+")


def _city_slug(city: str | None) -> str | None:
    if not city:
        return None
    s = _CITY_SAFE.sub("", city.strip())
    if not s:
        return None
    return s[:12]


def rename_nodes(
    nodes: list[Node],
    include_latency: bool = True,
    include_city: bool = False,
) -> list[Node]:
    """
    按国家分组重新编号，保证名称唯一。
    只修改 name / raw['name']，不改认证字段。
    """
    groups: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        cc = (n.country_code or "OTHER").upper()
        groups[cc].append(n)

    # 各国内按延迟排序再编号
    for cc, group in groups.items():
        group.sort(
            key=lambda x: (
                x.latency if x.latency is not None and x.latency > 0 else 10**9,
                x.server,
                x.port,
            )
        )
        width = max(2, len(str(len(group))))
        emoji = country_emoji(cc)
        used: set[str] = set()
        for idx, node in enumerate(group, start=1):
            num = str(idx).zfill(width)
            parts = [f"{emoji} {cc}"]
            if include_city:
                city = _city_slug(node.city)
                if city:
                    parts.append(city)
            parts.append(num)
            if include_latency and node.latency is not None and node.latency > 0:
                parts.append(f"{int(node.latency)}ms")
            name = "-".join(parts)
            # 极端情况下保证唯一
            base = name
            suffix = 2
            while name in used:
                name = f"{base}-{suffix}"
                suffix += 1
            used.add(name)
            node.name = name
            if node.raw is not None:
                node.raw["name"] = name

    logger.info("Renamed %d nodes", len(nodes))
    return nodes