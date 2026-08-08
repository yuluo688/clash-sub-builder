# -*- coding: utf-8 -*-
"""本地端到端测试：本地 HTTP 订阅源 + 全流水线 + Mihomo 启动测速。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import yaml

from src.checker.delay import check_nodes_delay
from src.checker.mihomo import resolve_mihomo_path
from src.deduplicate import deduplicate
from src.fetcher import detect_and_parse
from src.filter import filter_nodes
from src.generator import (
    build_config,
    country_stats,
    generate_yaml,
    validate_yaml_file,
)
from src.geo import annotate_countries
from src.rename import rename_nodes

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tmp" / "fixture_sub.yaml"
OUT = ROOT / "tmp" / "e2e_all.yaml"
FIXTURE.parent.mkdir(parents=True, exist_ok=True)

fixture = {
    "proxies": [
        {
            "name": "HK-Node-A",
            "type": "ss",
            "server": "203.0.113.10",
            "port": 8388,
            "cipher": "aes-256-gcm",
            "password": "pass-a",
        },
        {
            "name": "Hong Kong Duplicate",
            "type": "ss",
            "server": "203.0.113.10",
            "port": 8388,
            "cipher": "aes-256-gcm",
            "password": "pass-a",
        },
        {
            "name": "US Los Angeles 01",
            "type": "trojan",
            "server": "203.0.113.20",
            "port": 443,
            "password": "pass-us",
            "sni": "example.com",
            "skip-cert-verify": True,
        },
        {
            "name": "Tokyo-JP-01",
            "type": "vmess",
            "server": "203.0.113.30",
            "port": 443,
            "uuid": "11111111-1111-1111-1111-111111111111",
            "alterId": 0,
            "cipher": "auto",
            "tls": True,
            "network": "ws",
            "ws-opts": {"path": "/ray", "headers": {"Host": "example.com"}},
        },
        {
            "name": "Singapore SG",
            "type": "vless",
            "server": "203.0.113.40",
            "port": 443,
            "uuid": "22222222-2222-2222-2222-222222222222",
            "tls": True,
            "servername": "example.com",
            "network": "ws",
            "ws-opts": {"path": "/", "headers": {"Host": "example.com"}},
        },
        {
            "name": "Frankfurt DE",
            "type": "trojan",
            "server": "203.0.113.50",
            "port": 443,
            "password": "pass-de",
            "sni": "example.com",
        },
        {
            "name": "Taiwan TW",
            "type": "ss",
            "server": "203.0.113.60",
            "port": 8388,
            "cipher": "chacha20-ietf-poly1305",
            "password": "pass-tw",
        },
        {
            "name": "Seoul KR",
            "type": "ss",
            "server": "203.0.113.70",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "pass-kr",
        },
    ]
}
FIXTURE.write_text(yaml.safe_dump(fixture, allow_unicode=True), encoding="utf-8")


def main() -> int:
    print("=== 1) Parse local fixture subscription ===")
    content = FIXTURE.read_text(encoding="utf-8")
    nodes = detect_and_parse(content, source="local_fixture")
    print(f"  parsed={len(nodes)}")
    assert len(nodes) == 8, f"expected 8 nodes, got {len(nodes)}"

    # 网络可达性抽检（不依赖本机代理对 127.0.0.1 的劫持）
    print("=== 1b) httpx public reachability smoke ===")
    try:
        with httpx.Client(timeout=10.0, trust_env=False, follow_redirects=True) as client:
            r = client.get("https://www.gstatic.com/generate_204")
            print(f"  gstatic status={r.status_code}")
    except Exception as e:
        print(f"  gstatic skip/fail: {e}")

    print("=== 2) Dedup ===")
    nodes, removed = deduplicate(nodes)
    print(f"  unique={len(nodes)} removed={removed}")
    assert removed == 1 and len(nodes) == 7

    print("=== 3) Mihomo binary ===")
    binary = resolve_mihomo_path(str(ROOT / "bin" / "mihomo"))
    print(f"  binary={binary}")
    assert binary, "mihomo not found"

    print("=== 4) Mihomo start + delay API (TEST-NET unreachable) ===")
    sample = nodes[:2]
    for n in sample:
        n.latency = None
    checked = check_nodes_delay(
        sample,
        mihomo_path=binary,
        timeout=2000,
        concurrency=2,
        retries=0,
        batch_size=10,
        api_port=19090,
        enabled=True,
    )
    for n in checked:
        print(f"  {n.name}: latency={n.latency}")
    assert all(n.latency == -1 for n in checked), "expected latency -1"
    print("  Mihomo delay path OK")

    print("=== 5) Country + filter + rename + generate ===")
    full = list(nodes)
    annotate_countries(full, mmdb_path=None, enable_dns=False)
    for i, n in enumerate(full):
        n.latency = 50 + i * 10
    filtered, _ = filter_nodes(
        full,
        max_latency=800,
        max_nodes_total=500,
        max_nodes_per_country=50,
        require_latency=True,
    )
    rename_nodes(filtered, include_latency=True)
    print("  renamed:")
    for n in filtered:
        print(f"    {n.name}  code={n.country_code}")

    cfg = build_config(filtered)
    group_names = [g["name"] for g in cfg["proxy-groups"]]
    required = [
        "🚀 节点选择",
        "⚡ 自动选择",
        "🇺🇸 美国",
        "🇯🇵 日本",
        "🇸🇬 新加坡",
        "🇭🇰 香港",
        "🇹🇼 台湾",
        "🇰🇷 韩国",
        "🇪🇺 欧洲",
        "🌎 其他地区",
    ]
    for g in required:
        assert g in group_names, f"missing group {g}"

    eu = next(g for g in cfg["proxy-groups"] if g["name"] == "🇪🇺 欧洲")
    assert any("DE" in p for p in eu["proxies"]), "DE should be in EU group"

    generate_yaml(filtered, OUT)
    data = validate_yaml_file(OUT)
    assert len(data["proxies"]) == len(filtered)
    assert "mixed-port" in data and "rules" in data
    print(
        f"  wrote {OUT} proxies={len(data['proxies'])} countries={country_stats(filtered)}"
    )

    print("=== 6) main.py --skip-check smoke ===")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--skip-check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    print((r.stdout or "")[-800:])
    if r.returncode != 0:
        print(r.stderr)
        return r.returncode
    assert (ROOT / "output" / "all.yaml").is_file()
    assert (ROOT / "output" / "stats.json").is_file()
    stats = json.loads((ROOT / "output" / "stats.json").read_text(encoding="utf-8"))
    print(f"  stats.json nodes={stats.get('nodes')} status={stats.get('status')}")

    print("")
    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT FAIL: {e}")
        raise SystemExit(1)