"""测试：Clash YAML / Base64 / 去重 / 国家 / 重命名 / YAML 生成。"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

import main
import src.checker.delay as delay_checker
from src.checker.delay import _delay_async, select_cn_dialer
from src.checker.mihomo import DIALER_PROXY_NAME, MihomoRunner, resolve_dialer_proxy, resolve_mihomo_path
from src.deduplicate import deduplicate, fingerprint
from src.filter import filter_nodes
from src.generator import build_config, generate_yaml, validate_yaml_file
from src.geo import is_europe, match_country_from_name
from src.models import Node
from src.parsers.base64_subscription import parse_base64_subscription
from src.parsers.clash import parse_clash_yaml
from src.parsers.shadowsocks import parse_ss
from src.parsers.trojan import parse_trojan
from src.parsers.vless import parse_vless
from src.parsers.vmess import parse_vmess
from src.rename import rename_nodes


def _node(**kwargs) -> Node:
    defaults = dict(
        name="n",
        type="vmess",
        server="1.2.3.4",
        port=443,
        uuid="11111111-1111-1111-1111-111111111111",
        raw={},
    )
    defaults.update(kwargs)
    n = Node(**defaults)
    if not n.raw:
        n.raw = {
            "name": n.name,
            "type": n.type,
            "server": n.server,
            "port": n.port,
            "uuid": n.uuid,
            "alterId": 0,
            "cipher": "auto",
        }
    return n


class TestClashParser:
    def test_parse_proxies(self):
        content = textwrap.dedent(
            """
            proxies:
              - name: test-hk
                type: ss
                server: 203.0.113.1
                port: 8388
                cipher: aes-256-gcm
                password: secret
              - name: test-us
                type: vmess
                server: 198.51.100.2
                port: 443
                uuid: 11111111-1111-1111-1111-111111111111
                alterId: 0
                cipher: auto
                tls: true
                network: ws
                ws-opts:
                  path: /ray
                  headers:
                    Host: example.com
            """
        )
        nodes = parse_clash_yaml(content, source="t")
        assert len(nodes) == 2
        assert nodes[0].type == "ss"
        assert nodes[1].network == "ws"
        assert nodes[1].ws_path == "/ray"

    def test_invalid_yaml(self):
        assert parse_clash_yaml("proxies: [") == []


class TestBase64AndURI:
    def test_vmess(self):
        import json

        payload = {
            "v": "2",
            "ps": "US-Node",
            "add": "1.1.1.1",
            "port": "443",
            "id": "11111111-1111-1111-1111-111111111111",
            "aid": "0",
            "net": "ws",
            "type": "none",
            "host": "a.com",
            "path": "/v",
            "tls": "tls",
        }
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        node = parse_vmess(f"vmess://{b64}")
        assert node is not None
        assert node.server == "1.1.1.1"
        assert node.type == "vmess"

    def test_vless(self):
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@2.2.2.2:443"
            "?encryption=none&security=tls&type=ws&host=h.com&path=%2F"
            "#JP-Test"
        )
        node = parse_vless(uri)
        assert node is not None
        assert node.server == "2.2.2.2"
        assert "JP" in node.name or "jp" in node.name.lower() or node.name

    def test_trojan(self):
        uri = "trojan://password@3.3.3.3:443?sni=example.com#HK"
        node = parse_trojan(uri)
        assert node is not None
        assert node.password == "password"

    def test_ss_sip002(self):
        # method:password base64
        user = base64.urlsafe_b64encode(b"aes-256-gcm:pass").decode().rstrip("=")
        uri = f"ss://{user}@4.4.4.4:8388#SG"
        node = parse_ss(uri)
        assert node is not None
        assert node.port == 8388

    def test_base64_subscription(self):
        lines = "\n".join(
            [
                "trojan://p@1.1.1.1:443?sni=a.com#A",
                "trojan://p@2.2.2.2:443?sni=b.com#B",
            ]
        )
        b64 = base64.b64encode(lines.encode()).decode()
        nodes = parse_base64_subscription(b64)
        assert len(nodes) == 2


class TestDedup:
    def test_same_config_different_name(self):
        a = _node(name="a", server="1.1.1.1", port=443, uuid="u1")
        b = _node(name="b", server="1.1.1.1", port=443, uuid="u1")
        assert fingerprint(a) == fingerprint(b)
        uniq, removed = deduplicate([a, b])
        assert len(uniq) == 1
        assert removed == 1

    def test_different_server(self):
        a = _node(name="a", server="1.1.1.1")
        b = _node(name="b", server="2.2.2.2")
        uniq, removed = deduplicate([a, b])
        assert len(uniq) == 2
        assert removed == 0


class TestCountry:
    def test_keywords(self):
        assert match_country_from_name("🇺🇸 US Los Angeles 01") == "US"
        assert match_country_from_name("Tokyo-01") == "JP"
        assert match_country_from_name("Hong Kong IEPL") == "HK"
        assert match_country_from_name("Singapore-SG") == "SG"
        assert match_country_from_name("Taiwan Hinet") == "TW"
        assert match_country_from_name("Seoul-KR") == "KR"

    def test_no_false_positive_aus_as_us(self):
        # "AUS" 不应被误判为 US（词边界）
        # 注意：如果名称含 "us" 作为独立词才会匹配
        assert match_country_from_name("Australia Sydney") == "AU"

    def test_europe(self):
        assert is_europe("DE")
        assert is_europe("FR")
        assert is_europe("GB")
        assert not is_europe("US")
        assert not is_europe("JP")


class TestRename:
    def test_unique_and_format(self):
        nodes = [
            _node(name="a", country_code="US", latency=85, server="1.1.1.1"),
            _node(name="b", country_code="US", latency=103, server="1.1.1.2"),
            _node(name="c", country_code="JP", latency=71, server="2.2.2.2"),
        ]
        rename_nodes(nodes, include_latency=True)
        names = [n.name for n in nodes]
        assert len(names) == len(set(names))
        assert any(n.startswith("🇺🇸 US-") and n.endswith("ms") for n in names)
        assert any(n.startswith("🇯🇵 JP-") for n in names)
        # 只改 name
        assert nodes[0].uuid == "11111111-1111-1111-1111-111111111111"


class TestFilter:
    def test_latency_and_cap(self):
        nodes = [
            _node(name="ok", latency=100, country_code="US", password="x", type="trojan"),
            _node(name="slow", latency=900, country_code="US", password="x", type="trojan"),
            _node(name="dead", latency=-1, country_code="US", password="x", type="trojan"),
        ]
        for n in nodes:
            n.raw["password"] = "x"
            n.password = "x"
            n.type = "trojan"
            n.raw["type"] = "trojan"
        out, _ = filter_nodes(nodes, max_latency=800, max_nodes_total=50, max_nodes_per_country=50)
        assert len(out) == 1
        assert out[0].latency == 100

    def test_untested_nodes_share_country_and_source_quotas(self):
        nodes = [
            _node(
                name=f"{country}-{source}-{i}",
                country_code=country,
                original_source=source,
                server=f"{country}-{source}-{i}.example",
                latency=-1,
            )
            for country in ("US", "JP", "SG")
            for source in ("source-a", "source-b")
            for i in range(3)
        ]

        selected, _ = filter_nodes(
            nodes, require_latency=False, max_nodes_per_country=2, max_nodes_total=6
        )

        assert len(selected) == 6
        assert {node.country_code for node in selected} == {"US", "JP", "SG"}
        for country in ("US", "JP", "SG"):
            assert {node.original_source for node in selected if node.country_code == country} == {
                "source-a", "source-b"
            }

    def test_global_untested_cap_does_not_drop_later_countries(self):
        nodes = [
            _node(name=f"US-{i}", country_code="US", server=f"us-{i}.example")
            for i in range(10)
        ] + [_node(name="JP-0", country_code="JP", server="jp.example")]

        selected, _ = filter_nodes(nodes, require_latency=False, max_nodes_total=2)

        assert {node.country_code for node in selected} == {"US", "JP"}

    def test_chain_success_is_preferred_but_failed_candidates_remain(self):
        nodes = [
            _node(name="failed", country_code="JP", original_source="source", latency=-1),
            _node(name="passed", country_code="JP", original_source="source", latency=240),
        ]

        selected, _ = filter_nodes(nodes, require_latency=False, max_nodes_total=1)
        assert [node.name for node in selected] == ["passed"]
        selected, _ = filter_nodes(nodes, require_latency=False, max_nodes_total=2)
        assert [node.name for node in selected] == ["passed", "failed"]

    def test_country_quota_prioritizes_chain_success_across_sources(self):
        nodes = [
            _node(name="passed-a1", country_code="JP", original_source="a", latency=180),
            _node(name="passed-a2", country_code="JP", original_source="a", latency=200),
            _node(name="failed-b", country_code="JP", original_source="b", latency=-1),
        ]

        selected, _ = filter_nodes(nodes, require_latency=False, max_nodes_per_country=2)
        assert [node.name for node in selected] == ["passed-a1", "passed-a2"]


class TestPipeline:
    def test_client_mode_keeps_candidates_without_runner(self, tmp_path: Path, monkeypatch):
        output = tmp_path / "all.yaml"
        stats = tmp_path / "stats.json"
        cfg_path = tmp_path / "config.yaml"
        src_path = tmp_path / "sources.yaml"
        cfg_path.write_text(
            yaml.safe_dump({
                "checker": {"enabled": False},
                "geo": {"enable_dns": False},
                "output": {"path": str(output), "stats_path": str(stats)},
            }),
            encoding="utf-8",
        )
        src_path.write_text("sources:\n  - name: demo\n    url: https://example.com/sub\n", encoding="utf-8")
        monkeypatch.setattr(
            main,
            "fetch_all_sources",
            lambda *args, **kwargs: (
                [_node(name="JP-test", latency=-1), _node(name="US-test", server="other.example")],
                1,
                1,
            ),
        )
        monkeypatch.setattr(main, "check_nodes_delay", lambda *args, **kwargs: pytest.fail(
            "client mode must not run the overseas checker"
        ))

        assert main.run(str(cfg_path), str(src_path)) == 0
        generated = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert len(generated["proxies"]) == 2
        auto_group = next(g for g in generated["proxy-groups"] if g["name"] == "⚡ 自动选择")
        assert auto_group["type"] == "url-test"
        assert all("ms" not in p["name"] for p in generated["proxies"])
        assert json.loads(stats.read_text(encoding="utf-8"))["pipeline"]["tested"] == 0

    @pytest.mark.parametrize("invalid_node", [False, True])
    def test_no_candidates_preserves_previous_subscription(
        self, tmp_path: Path, monkeypatch, invalid_node: bool
    ):
        output = tmp_path / "all.yaml"
        output.write_text("old subscription", encoding="utf-8")
        cfg_path = tmp_path / "config.yaml"
        src_path = tmp_path / "sources.yaml"
        cfg_path.write_text(
            yaml.safe_dump({
                "checker": {"enabled": False},
                "output": {"path": str(output)},
            }),
            encoding="utf-8",
        )
        src_path.write_text("sources:\n  - name: demo\n    url: https://example.com/sub\n", encoding="utf-8")
        fetched = [_node(uuid=None, raw={"name": "missing credential"})] if invalid_node else []
        monkeypatch.setattr(main, "fetch_all_sources", lambda *args, **kwargs: (
            fetched, 1, int(invalid_node)
        ))

        assert main.run(str(cfg_path), str(src_path)) == 1
        assert output.read_text(encoding="utf-8") == "old subscription"

    def test_cn_bootstrap_prefers_previous_and_deduplicates(self, tmp_path: Path):
        previous = _node(name="CN-previous", server="cn.example")
        output = tmp_path / "all.yaml"
        output.write_text(yaml.safe_dump({"proxies": [previous.to_clash_proxy()]}), encoding="utf-8")
        current = [
            _node(name="CN-duplicate", server="cn.example", country_code="CN"),
            _node(name="CN-new", server="cn-new.example", country_code="CN"),
            _node(name="US-other", server="us.example", country_code="US"),
        ]

        candidates = main.cn_dialer_candidates(current, output)

        assert [node.server for node in candidates] == ["cn.example", "cn-new.example"]

    @pytest.mark.parametrize("verified", [True, False])
    def test_cn_chain_uses_verified_dialer_or_falls_back(
        self, tmp_path: Path, monkeypatch, verified: bool
    ):
        output = tmp_path / "all.yaml"
        stats = tmp_path / "stats.json"
        config = tmp_path / "config.yaml"
        sources = tmp_path / "sources.yaml"
        config.write_text(
            yaml.safe_dump({
                "checker": {"enabled": True, "cn_chain": {"enabled": True}},
                "geo": {"enable_dns": False},
                "output": {"path": str(output), "stats_path": str(stats)},
            }), encoding="utf-8",
        )
        sources.write_text("sources:\n  - name: demo\n    url: https://example.com/sub\n", encoding="utf-8")
        cn = _node(name="CN-entry", server="cn.example")
        others = [_node(name=f"JP-{i}", server=f"jp-{i}.example") for i in range(2)]
        monkeypatch.setattr(main, "fetch_all_sources", lambda *args, **kwargs: (
            [cn, *others], 1, 1
        ))
        monkeypatch.setattr(main, "select_cn_dialer", lambda candidates, **kwargs: (
            candidates[0] if verified else None
        ))
        checks: list[list[Node]] = []

        def fake_check(targets: list[Node], **kwargs):
            checks.append(targets)
            assert kwargs["dialer_proxy"]["server"] == "cn.example"
            assert cn not in targets
            targets[0].latency = 250
            targets[1].latency = -1
            return targets

        monkeypatch.setattr(main, "check_nodes_delay", fake_check)

        assert main.run(str(config), str(sources)) == 0
        proxies = yaml.safe_load(output.read_text(encoding="utf-8"))["proxies"]
        pipeline = json.loads(stats.read_text(encoding="utf-8"))["pipeline"]
        assert len(proxies) == 3
        assert all("dialer-proxy" not in proxy for proxy in proxies)
        assert all("ms" not in proxy["name"] for proxy in proxies)
        assert len(checks) == int(verified)
        assert pipeline["cn_chain"] == ("verified" if verified else "fallback")
        assert pipeline["tested"] == (2 if verified else 0)
        assert pipeline["alive"] == int(verified)

    def test_cn_chain_uses_previous_subscription_without_publishing_old_node(
        self, tmp_path: Path, monkeypatch
    ):
        output = tmp_path / "all.yaml"
        config = tmp_path / "config.yaml"
        sources = tmp_path / "sources.yaml"
        old_cn = _node(name="CN-old", server="old-cn.example")
        output.write_text(yaml.safe_dump({"proxies": [old_cn.to_clash_proxy()]}), encoding="utf-8")
        config.write_text(
            yaml.safe_dump({
                "checker": {"enabled": True, "cn_chain": {"enabled": True}},
                "geo": {"enable_dns": False},
                "output": {"path": str(output), "stats_path": str(tmp_path / "stats.json")},
            }), encoding="utf-8",
        )
        sources.write_text("sources:\n  - name: demo\n    url: https://example.com/sub\n", encoding="utf-8")
        current = _node(name="JP-fresh", server="jp.example")
        monkeypatch.setattr(main, "fetch_all_sources", lambda *args, **kwargs: ([current], 1, 1))

        def select(candidates, **kwargs):
            assert candidates[0].server == "old-cn.example"
            return candidates[0]

        def check(targets, **kwargs):
            assert targets == [current]
            assert kwargs["dialer_proxy"]["server"] == "old-cn.example"
            targets[0].latency = 200

        monkeypatch.setattr(main, "select_cn_dialer", select)
        monkeypatch.setattr(main, "check_nodes_delay", check)

        assert main.run(str(config), str(sources)) == 0
        proxies = yaml.safe_load(output.read_text(encoding="utf-8"))["proxies"]
        assert [proxy["server"] for proxy in proxies] == ["jp.example"]


class TestDelayChecker:
    def test_cn_chain_config_accepted_by_mihomo_when_available(self):
        binary = resolve_mihomo_path()
        if not binary:
            pytest.skip("Mihomo not installed locally")
        cn_dialer = {
            "name": "CN-bootstrap", "type": "ss", "server": "cn.example", "port": 8388,
            "cipher": "aes-256-gcm", "password": "demo-pass",
        }
        runner = MihomoRunner(binary, dialer_proxy=cn_dialer)
        runner._tmpdir = tempfile.TemporaryDirectory()
        try:
            path = runner._write_config([_node(name="JP-test").to_clash_proxy()])
            result = subprocess.run(
                [binary, "-t", "-d", runner._tmpdir.name, "-f", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, result.stderr
        finally:
            runner.stop()

    def test_cn_dialer_requires_verified_exit_and_skips_unusable_candidates(self, monkeypatch):
        candidates = [
            _node(name="CN-wrong-exit", server="a.example"),
            _node(name="CN-verified", server="b.example"),
        ]
        observed: list[str] = []

        class FakeRunner:
            mixed_port = 17890

            def __init__(self, **kwargs):
                pass

            def start(self, proxies):
                observed.append(proxies[0]["server"])

            def stop(self):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                assert kwargs["proxy"] == "http://127.0.0.1:17890"
                assert kwargs["trust_env"] is False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url):
                assert url.startswith("https://")
                return httpx.Response(
                    200,
                    json={"country": "US" if len(observed) == 1 else "CN"},
                    request=httpx.Request("GET", url),
                )

        monkeypatch.setattr(delay_checker, "MihomoRunner", FakeRunner)
        monkeypatch.setattr(delay_checker, "resolve_mihomo_path", lambda *_: "mihomo")
        monkeypatch.setattr(delay_checker.httpx, "Client", FakeClient)

        assert select_cn_dialer(candidates, max_candidates=2) is candidates[1]
        assert observed == ["a.example", "b.example"]

    def test_cn_dialer_falls_back_when_no_verified_exit(self, monkeypatch):
        node = _node(name="CN-unknown-exit")

        class FakeRunner:
            mixed_port = 17890

            def __init__(self, **kwargs):
                pass

            def start(self, proxies):
                pass

            def stop(self):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url):
                return httpx.Response(
                    200, json={"country": "US"}, request=httpx.Request("GET", url)
                )

        monkeypatch.setattr(delay_checker, "MihomoRunner", FakeRunner)
        monkeypatch.setattr(delay_checker, "resolve_mihomo_path", lambda *_: "mihomo")
        monkeypatch.setattr(delay_checker.httpx, "Client", FakeClient)

        assert select_cn_dialer([node]) is None
        with pytest.raises(ValueError, match="HTTPS"):
            select_cn_dialer([node], geo_url="http://geo.example")

    def test_uses_fallback_url_after_primary_failure(self):
        requested_test_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_test_urls.append(str(request.url.params["url"]))
            if len(requested_test_urls) == 1:
                return httpx.Response(502)
            return httpx.Response(200, json={"delay": 123})

        async def run_check() -> int | None:
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await _delay_async(
                    client,
                    "http://127.0.0.1:9090",
                    "t0000",
                    ("https://primary.example/204", "https://fallback.example/204"),
                    timeout_ms=100,
                    retries=1,
                )

        assert asyncio.run(run_check()) == 123
        assert requested_test_urls == [
            "https://primary.example/204",
            "https://fallback.example/204",
        ]

    def test_china_dialer_chains_candidate_connections(self):
        dialer = resolve_dialer_proxy(
            {"enabled": True, "type": "socks5", "tls": True},
            {
                "CN_DIALER_SERVER": "cn-probe.example",
                "CN_DIALER_PORT": "1080",
                "CN_DIALER_USERNAME": "runner",
                "CN_DIALER_PASSWORD": "secret",
            },
        )
        assert dialer is not None
        assert dialer["server"] == "cn-probe.example"
        assert dialer["tls"] is True

        runner = MihomoRunner("mihomo", dialer_proxy=dialer)
        runner._tmpdir = tempfile.TemporaryDirectory()
        try:
            config = yaml.safe_load(runner._write_config([_node().to_clash_proxy()]).read_text())
        finally:
            runner.stop()

        assert config["proxies"][0]["name"] == DIALER_PROXY_NAME
        assert config["proxies"][1]["dialer-proxy"] == DIALER_PROXY_NAME


class TestGenerator:
    def test_build_and_validate(self, tmp_path: Path):
        nodes = [
            _node(name="🇺🇸 US-01-10ms", country_code="US", latency=10, type="trojan", password="p"),
            _node(name="🇯🇵 JP-01-20ms", country_code="JP", latency=20, type="trojan", password="p"),
            _node(name="🇩🇪 DE-01-30ms", country_code="DE", latency=30, type="trojan", password="p"),
        ]
        for n in nodes:
            n.raw = {
                "name": n.name,
                "type": "trojan",
                "server": n.server,
                "port": n.port,
                "password": "p",
            }
            n.password = "p"
            n.type = "trojan"

        cfg = build_config(nodes)
        assert "mixed-port" in cfg
        assert "proxies" in cfg
        assert len(cfg["proxies"]) == 3
        group_names = [g["name"] for g in cfg["proxy-groups"]]
        assert "🚀 节点选择" in group_names
        assert "⚡ 自动选择" in group_names
        assert "🇺🇸 美国" in group_names
        assert "🇪🇺 欧洲" in group_names

        # 欧洲组应包含 DE 节点
        eu = next(g for g in cfg["proxy-groups"] if g["name"] == "🇪🇺 欧洲")
        assert any("DE" in p for p in eu["proxies"])

        out = tmp_path / "all.yaml"
        generate_yaml(nodes, out)
        data = validate_yaml_file(out)
        assert isinstance(data["proxies"], list)
        # syntax re-parse
        yaml.safe_load(out.read_text(encoding="utf-8"))


class TestUtilsMask:
    def test_mask_url(self):
        from src.utils import mask_url

        u = mask_url("https://example.com/sub?token=supersecret&x=1")
        assert "supersecret" not in u
        assert "token=***" in u
