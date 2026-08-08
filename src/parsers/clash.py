"""Clash / Clash Meta YAML 解析。"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from src.models import Node
from src.parsers.common import node_from_clash_dict

logger = logging.getLogger(__name__)


def parse_clash_yaml(content: str, source: str = "") -> list[Node]:
    """解析 Clash/Clash Meta YAML 文本中的 proxies。"""
    if not content or not content.strip():
        return []

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        logger.warning("Clash YAML parse error from %s: %s", source or "unknown", e)
        return []

    return parse_clash_data(data, source=source)


def parse_clash_data(data: Any, source: str = "") -> list[Node]:
    if data is None:
        return []

    proxies: list[Any] = []
    if isinstance(data, dict):
        proxies = data.get("proxies") or data.get("Proxy") or []
    elif isinstance(data, list):
        # 少数源直接给 proxy 列表
        proxies = data
    else:
        return []

    if not isinstance(proxies, list):
        logger.warning("proxies is not a list from %s", source or "unknown")
        return []

    nodes: list[Node] = []
    for item in proxies:
        try:
            node = node_from_clash_dict(item, source=source)
            if node:
                nodes.append(node)
        except Exception as e:
            logger.debug("skip proxy item from %s: %s", source, e)
    return nodes