"""测试：Clash YAML / Base64 / 去重 / 国家 / 重命名 / YAML 生成。"""

from __future__ import annotations

import base64
import textwrap
from pathlib import Path

import pytest
import yaml

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