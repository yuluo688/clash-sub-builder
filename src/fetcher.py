"""订阅源下载与格式自动识别。"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import yaml

from src.models import Node
from src.parsers.base64_subscription import parse_base64_subscription
from src.parsers.clash import parse_clash_yaml
from src.utils import mask_url

logger = logging.getLogger(__name__)


def detect_and_parse(content: str, source: str = "") -> list[Node]:
    """自动识别 Clash YAML / Base64 / URI 列表。"""
    if not content or not content.strip():
        return []

    text = content.strip()

    # 1) 尝试 Clash YAML（含 proxies 键）
    if "proxies:" in text or "Proxy:" in text or text.lstrip().startswith("{"):
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and (
                "proxies" in data or "Proxy" in data or "proxy-groups" in data
            ):
                nodes = parse_clash_yaml(text, source=source)
                if nodes:
                    return nodes
            # 纯 proxy list
            if isinstance(data, list) and data and isinstance(data[0], dict):
                nodes = parse_clash_yaml(text, source=source)
                if nodes:
                    return nodes
        except Exception:
            pass

    # 2) Base64 / 分享链接
    nodes = parse_base64_subscription(text, source=source)
    if nodes:
        return nodes

    # 3) 再试一次 YAML（宽松）
    try:
        nodes = parse_clash_yaml(text, source=source)
        if nodes:
            return nodes
    except Exception:
        pass

    logger.warning("Unable to parse content from %s", source or "unknown")
    return []


def fetch_source(
    name: str,
    url: str,
    timeout: float = 30.0,
    max_retries: int = 2,
    user_agent: str = "clash-sub-builder/1.0",
) -> tuple[list[Node], bool]:
    """下载并解析单个源。返回 (nodes, downloaded_ok)。"""
    safe = mask_url(url)
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
    }
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
                verify=True,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                # 优先文本
                content = resp.text
                if not content and resp.content:
                    content = resp.content.decode("utf-8", errors="ignore")
            nodes = detect_and_parse(content, source=name)
            logger.info(
                "Source %s downloaded OK (%s bytes, %d nodes) url=%s",
                name,
                len(content),
                len(nodes),
                safe,
            )
            return nodes, True
        except Exception as e:
            last_err = e
            logger.warning(
                "Source %s attempt %d failed: %s url=%s",
                name,
                attempt + 1,
                e,
                safe,
            )
    logger.error("Source %s failed permanently: %s url=%s", name, last_err, safe)
    return [], False


def fetch_all_sources(
    sources: list[dict[str, Any]],
    timeout: float = 30.0,
    max_retries: int = 2,
    user_agent: str = "clash-sub-builder/1.0",
) -> tuple[list[Node], int, int]:
    """
    拉取全部启用源。
    返回: (all_nodes, enabled_count, downloaded_count)
    """
    enabled = [s for s in sources if s.get("enabled", True)]
    all_nodes: list[Node] = []
    downloaded = 0
    for src in enabled:
        name = str(src.get("name") or "unnamed")
        url = str(src.get("url") or "").strip()
        if not url:
            logger.warning("Source %s has empty url, skip", name)
            continue
        nodes, ok = fetch_source(
            name=name,
            url=url,
            timeout=timeout,
            max_retries=max_retries,
            user_agent=user_agent,
        )
        if ok:
            downloaded += 1
        all_nodes.extend(nodes)
    return all_nodes, len(enabled), downloaded