#!/usr/bin/env python3
"""
clash-sub-builder 主入口

流水线：
  拉取公开订阅 → 解析 → 去重 → (可选) Mihomo 真实测速
  → 国家识别 → 筛选 → 重命名 → 生成 output/all.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.checker.delay import check_nodes_delay, select_cn_dialer
from src.checker.mihomo import resolve_dialer_proxy
from src.deduplicate import deduplicate, fingerprint
from src.fetcher import fetch_all_sources
from src.filter import filter_nodes, is_complete
from src.generator import country_stats, generate_yaml, validate_yaml_file
from src.geo import annotate_countries, match_country_from_name
from src.models import Node, Stats
from src.parsers.clash import parse_clash_yaml
from src.rename import rename_nodes
from src.utils import deep_get, load_yaml, setup_logging

logger = logging.getLogger("main")

ROOT = Path(__file__).resolve().parent


def load_config(config_path: Path, sources_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = load_yaml(str(config_path))
    src_data = load_yaml(str(sources_path))
    sources = src_data.get("sources") or []
    if not isinstance(sources, list):
        sources = []
    return cfg, sources


def cn_dialer_candidates(nodes: list[Node], previous_output: Path) -> list[Node]:
    """Prefer last run's CN nodes, then newly fetched CN nodes, without duplicates."""
    previous: list[Node] = []
    if previous_output.is_file():
        try:
            previous = parse_clash_yaml(previous_output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            logger.warning("Cannot read previous subscription for CN bootstrap: %s", exc)

    # Reserve one attempt for an old known candidate; leave the rest for fresh sources.
    candidates = [
        n for n in previous if match_country_from_name(n.name) == "CN" and is_complete(n)
    ][:1]
    current, _ = filter_nodes(
        [n for n in nodes if n.country_code == "CN"],
        require_latency=False,
        max_nodes_total=0,
        max_nodes_per_country=0,
    )
    candidates.extend(current)
    seen: set[str] = set()
    unique: list[Node] = []
    for node in candidates:
        if not is_complete(node):
            continue
        key = fingerprint(node)
        if key not in seen:
            unique.append(node)
            seen.add(key)
    return unique


def run(config_path: str, sources_path: str, skip_check: bool = False) -> int:
    setup_logging()
    stats = Stats()

    cfg_path = Path(config_path)
    src_path = Path(sources_path)
    if not cfg_path.is_file():
        logger.error("config not found: %s", cfg_path)
        return 1
    if not src_path.is_file():
        logger.error("sources not found: %s", src_path)
        return 1

    cfg, sources = load_config(cfg_path, src_path)
    stats.sources = len([s for s in sources if s.get("enabled", True)])
    out_path = Path(str(deep_get(cfg, "output", "path", default="output/all.yaml")))
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    # --- fetch ---
    nodes, enabled_count, downloaded = fetch_all_sources(
        sources,
        timeout=float(deep_get(cfg, "fetcher", "timeout", default=30)),
        max_retries=int(deep_get(cfg, "fetcher", "max_retries", default=2)),
        user_agent=str(deep_get(cfg, "fetcher", "user_agent", default="clash-sub-builder/1.0")),
    )
    stats.sources = enabled_count
    stats.downloaded = downloaded
    stats.raw_nodes = len(nodes)
    stats.parsed = len(nodes)
    logger.info("Fetched raw nodes: %d from %d/%d sources", len(nodes), downloaded, enabled_count)

    if not nodes and enabled_count:
        logger.error("No nodes parsed from enabled sources; leaving existing subscription unchanged")
        return 1

    # --- dedupe ---
    nodes, removed = deduplicate(nodes)
    stats.duplicates_removed = removed

    # --- geo (CN bootstrap requires the country label before delay checking) ---
    nodes = annotate_countries(
        nodes,
        mmdb_path=str(deep_get(cfg, "geo", "mmdb_path", default="")) or None,
        enable_dns=bool(deep_get(cfg, "geo", "enable_dns", default=True)),
    )

    # --- delay check (Mihomo real proxy delay) ---
    checker_enabled = bool(deep_get(cfg, "checker", "enabled", default=True)) and not skip_check
    cn_chain_enabled = checker_enabled and bool(
        deep_get(cfg, "checker", "cn_chain", "enabled", default=False)
    )
    dialer_settings = deep_get(cfg, "checker", "dialer_proxy", default={})
    if cn_chain_enabled and isinstance(dialer_settings, Mapping) and dialer_settings.get("enabled"):
        raise ValueError("checker.cn_chain and checker.dialer_proxy cannot both be enabled")
    dialer_proxy = (
        resolve_dialer_proxy(dialer_settings)
        if checker_enabled and not cn_chain_enabled
        else None
    )
    max_nodes_total = int(deep_get(cfg, "filter", "max_nodes_total", default=500))
    max_nodes_per_country = int(deep_get(cfg, "filter", "max_nodes_per_country", default=50))
    check_options = dict(
        mihomo_path=str(deep_get(cfg, "checker", "mihomo_path", default="bin/mihomo")),
        timeout=int(deep_get(cfg, "checker", "timeout", default=5000)),
        concurrency=int(deep_get(cfg, "checker", "concurrency", default=20)),
        retries=int(deep_get(cfg, "checker", "retries", default=1)),
        test_url=str(
            deep_get(cfg, "checker", "test_url", default="https://www.gstatic.com/generate_204")
        ),
        fallback_test_urls=deep_get(cfg, "checker", "fallback_test_urls", default=[]),
        api_host=str(deep_get(cfg, "checker", "api_host", default="127.0.0.1")),
        api_port=int(deep_get(cfg, "checker", "api_port", default=9090)),
        batch_size=int(deep_get(cfg, "checker", "batch_size", default=100)),
    )
    chain_status = "disabled"
    if cn_chain_enabled:
        cn_candidates = cn_dialer_candidates(nodes, out_path)
        nodes, _ = filter_nodes(
            nodes,
            require_latency=False,
            max_nodes_total=max_nodes_total,
            max_nodes_per_country=max_nodes_per_country,
        )
        bootstrap = select_cn_dialer(
            cn_candidates,
            mihomo_path=check_options["mihomo_path"],
            api_host=check_options["api_host"],
            api_port=check_options["api_port"],
            timeout=check_options["timeout"],
            max_candidates=int(
                deep_get(cfg, "checker", "cn_chain", "max_candidates", default=5)
            ),
            geo_url=str(
                deep_get(cfg, "checker", "cn_chain", "geo_url", default="https://ipinfo.io/json")
            ),
        )
        if bootstrap:
            bootstrap_key = fingerprint(bootstrap)
            targets = [node for node in nodes if fingerprint(node) != bootstrap_key]
            check_nodes_delay(targets, dialer_proxy=bootstrap.to_clash_proxy(), **check_options)
            stats.tested = len(targets)
            chain_status = "verified"
        else:
            chain_status = "fallback"
    elif nodes and checker_enabled:
        check_nodes_delay(nodes, dialer_proxy=dialer_proxy, **check_options)
        stats.tested = len(nodes)
    else:
        logger.info("Runner delay check disabled; client will test candidate nodes locally")
        for node in nodes:
            node.latency = None
            node.score = 0.0
    stats.alive = sum(1 for n in nodes if n.latency is not None and n.latency > 0)

    # CN 链式测速只能判断从入口到候选节点的连通性；保留失败候选供客户端复测。
    require_latency = checker_enabled and not cn_chain_enabled

    # --- filter ---
    max_latency = int(deep_get(cfg, "checker", "max_latency", default=800))
    nodes, filt_removed = filter_nodes(
        nodes,
        max_latency=max_latency,
        max_nodes_total=max_nodes_total,
        max_nodes_per_country=max_nodes_per_country,
        require_latency=require_latency,
    )
    stats.filtered = len(nodes)
    if not nodes and enabled_count:
        logger.error("No valid nodes after filtering; leaving existing subscription unchanged")
        return 1

    # --- rename ---
    nodes = rename_nodes(
        nodes,
        include_latency=not cn_chain_enabled and bool(
            deep_get(cfg, "generator", "include_latency_in_name", default=True)
        ),
        include_city=bool(deep_get(cfg, "generator", "include_city_in_name", default=False)),
    )

    cstats = country_stats(nodes)
    stats.countries = len(cstats)

    # --- generate ---
    generate_yaml(
        nodes,
        out_path,
        mixed_port=int(deep_get(cfg, "generator", "mixed_port", default=7890)),
        allow_lan=bool(deep_get(cfg, "generator", "allow_lan", default=False)),
        mode=str(deep_get(cfg, "generator", "mode", default="rule")),
        log_level=str(deep_get(cfg, "generator", "log_level", default="info")),
        ipv6=bool(deep_get(cfg, "generator", "ipv6", default=True)),
        external_controller=str(
            deep_get(cfg, "generator", "external_controller", default="127.0.0.1:9090")
        ),
        url_test_url=str(
            deep_get(
                cfg,
                "generator",
                "url_test_url",
                default="https://www.gstatic.com/generate_204",
            )
        ),
        url_test_interval=int(deep_get(cfg, "generator", "url_test_interval", default=300)),
        url_test_tolerance=int(deep_get(cfg, "generator", "url_test_tolerance", default=50)),
    )

    # validate
    try:
        validate_yaml_file(out_path)
        logger.info("YAML validation OK: %s", out_path)
    except Exception as e:
        logger.error("YAML validation failed: %s", e)
        return 1

    # stats json（给 Worker /health /stats 用，模式 A 一并提交）
    stats_path = Path(str(deep_get(cfg, "output", "stats_path", default="output/stats.json")))
    if not stats_path.is_absolute():
        stats_path = ROOT / stats_path
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "ok",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": len(nodes),
        "total": len(nodes),
        "countries": cstats,
        "pipeline": {**stats.to_dict(), "cn_chain": chain_status},
    }
    stats_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # summary
    print("")
    print("========== SUMMARY ==========")
    for line in stats.summary_lines():
        print(line)
    print(f"Output: {out_path}")
    print(f"Country breakdown: {cstats}")
    print("=============================")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build aggregated Clash Meta subscription")
    parser.add_argument(
        "-c",
        "--config",
        default=str(ROOT / "config" / "config.yaml"),
        help="path to config.yaml",
    )
    parser.add_argument(
        "-s",
        "--sources",
        default=str(ROOT / "config" / "sources.yaml"),
        help="path to sources.yaml",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="skip Mihomo delay check (offline / unit-friendly)",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.config, args.sources, skip_check=args.skip_check)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
