"""日志与通用工具。"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse


SENSITIVE_KEYS = ("token", "key", "secret", "password", "passwd", "auth")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def mask_url(url: str) -> str:
    """对 URL 中含 token/key/secret 的 query 打码，避免日志泄露。"""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            # 路径中也可能带 secret
            lower = url.lower()
            for k in SENSITIVE_KEYS:
                if k in lower:
                    return re.sub(
                        rf"({k}[=:/_-])([^/&\s?#]+)",
                        r"\1***",
                        url,
                        flags=re.IGNORECASE,
                    )
            return url

        qs = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        for key in list(qs.keys()):
            if any(s in key.lower() for s in SENSITIVE_KEYS):
                qs[key] = ["***"]
                changed = True
        if not changed:
            return url

        # 重建 query
        parts = []
        for k, values in qs.items():
            for v in values:
                parts.append(f"{k}={v}")
        new_query = "&".join(parts)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )
    except Exception:
        return "[masked-url]"


def load_yaml(path: str) -> dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be mapping: {path}")
    return data


def deep_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur