"""国家 / 地区识别：GeoIP（可选）→ DNS+GeoIP → 名称关键词 → OTHER。"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from src.models import Node

logger = logging.getLogger(__name__)

# 欧洲国家（节点保留真实国旗/代码，同时进欧洲组）
EUROPE_CODES = frozenset(
    {
        "DE",
        "FR",
        "GB",
        "UK",
        "NL",
        "IT",
        "ES",
        "CH",
        "SE",
        "FI",
        "NO",
        "PL",
        "AT",
        "BE",
        "IE",
        "CZ",
        "DK",
        "PT",
        "RO",
        "HU",
        "GR",
        "BG",
        "HR",
        "SK",
        "SI",
        "LT",
        "LV",
        "EE",
        "LU",
        "MT",
        "CY",
        "IS",
        "UA",
        "MD",
        "RS",
        "BA",
        "AL",
        "MK",
        "ME",
    }
)

# code -> (country name, emoji)
COUNTRY_META: dict[str, tuple[str, str]] = {
    "US": ("United States", "🇺🇸"),
    "JP": ("Japan", "🇯🇵"),
    "SG": ("Singapore", "🇸🇬"),
    "HK": ("Hong Kong", "🇭🇰"),
    "TW": ("Taiwan", "🇹🇼"),
    "KR": ("Korea", "🇰🇷"),
    "CN": ("China", "🇨🇳"),
    "DE": ("Germany", "🇩🇪"),
    "FR": ("France", "🇫🇷"),
    "GB": ("United Kingdom", "🇬🇧"),
    "UK": ("United Kingdom", "🇬🇧"),
    "NL": ("Netherlands", "🇳🇱"),
    "IT": ("Italy", "🇮🇹"),
    "ES": ("Spain", "🇪🇸"),
    "CH": ("Switzerland", "🇨🇭"),
    "SE": ("Sweden", "🇸🇪"),
    "FI": ("Finland", "🇫🇮"),
    "NO": ("Norway", "🇳🇴"),
    "PL": ("Poland", "🇵🇱"),
    "AT": ("Austria", "🇦🇹"),
    "BE": ("Belgium", "🇧🇪"),
    "IE": ("Ireland", "🇮🇪"),
    "CZ": ("Czechia", "🇨🇿"),
    "DK": ("Denmark", "🇩🇰"),
    "PT": ("Portugal", "🇵🇹"),
    "CA": ("Canada", "🇨🇦"),
    "AU": ("Australia", "🇦🇺"),
    "RU": ("Russia", "🇷🇺"),
    "IN": ("India", "🇮🇳"),
    "BR": ("Brazil", "🇧🇷"),
    "TR": ("Turkey", "🇹🇷"),
    "MY": ("Malaysia", "🇲🇾"),
    "TH": ("Thailand", "🇹🇭"),
    "VN": ("Vietnam", "🇻🇳"),
    "PH": ("Philippines", "🇵🇭"),
    "ID": ("Indonesia", "🇮🇩"),
    "MO": ("Macao", "🇲🇴"),
    "AE": ("UAE", "🇦🇪"),
    "AR": ("Argentina", "🇦🇷"),
    "MX": ("Mexico", "🇲🇽"),
    "NZ": ("New Zealand", "🇳🇿"),
    "OTHER": ("Other", "🌎"),
}


@dataclass(frozen=True)
class KeywordRule:
    code: str
    # 词边界匹配的短语（大小写不敏感）
    phrases: tuple[str, ...]


# 避免简单 substring 误判：使用词边界 / 明确短语
KEYWORD_RULES: list[KeywordRule] = [
    KeywordRule(
        "US",
        (
            "united states",
            "los angeles",
            "san jose",
            "san francisco",
            "new york",
            "las vegas",
            "seattle",
            "chicago",
            "dallas",
            "miami",
            "america",
            "usa",
            "us",
        ),
    ),
    KeywordRule("JP", ("japan", "tokyo", "osaka", "nagoya", "jp")),
    KeywordRule("SG", ("singapore", "sg")),
    KeywordRule("HK", ("hong kong", "hongkong", "hk")),
    KeywordRule("TW", ("taiwan", "taipei", "tw")),
    KeywordRule("KR", ("korea", "seoul", "busan", "kr")),
    KeywordRule("GB", ("united kingdom", "london", "britain", "england", "uk", "gb")),
    KeywordRule("DE", ("germany", "frankfurt", "berlin", "munich", "de")),
    KeywordRule("FR", ("france", "paris", "fr")),
    KeywordRule("NL", ("netherlands", "amsterdam", "nl")),
    KeywordRule("CA", ("canada", "toronto", "vancouver", "montreal", "ca")),
    KeywordRule("AU", ("australia", "sydney", "melbourne", "au")),
    KeywordRule("RU", ("russia", "moscow", "ru")),
    KeywordRule("IN", ("india", "mumbai", "in")),
    KeywordRule("TR", ("turkey", "istanbul", "tr")),
    KeywordRule("MY", ("malaysia", "kuala lumpur", "my")),
    KeywordRule("TH", ("thailand", "bangkok", "th")),
    KeywordRule("VN", ("vietnam", "hanoi", "vn")),
    KeywordRule("PH", ("philippines", "manila", "ph")),
    KeywordRule("ID", ("indonesia", "jakarta", "id")),
    KeywordRule("MO", ("macao", "macau", "mo")),
    KeywordRule("AE", ("dubai", "uae", "ae")),
    KeywordRule("BR", ("brazil", "sao paulo", "br")),
    KeywordRule("IT", ("italy", "milan", "rome", "it")),
    KeywordRule("ES", ("spain", "madrid", "es")),
    KeywordRule("CH", ("switzerland", "zurich", "ch")),
    KeywordRule("SE", ("sweden", "stockholm", "se")),
    KeywordRule("FI", ("finland", "helsinki", "fi")),
    KeywordRule("NO", ("norway", "oslo", "no")),
    KeywordRule("PL", ("poland", "warsaw", "pl")),
    KeywordRule("IE", ("ireland", "dublin", "ie")),
    KeywordRule("CN", ("china", "beijing", "shanghai", "shenzhen", "cn")),
]


def _normalize_code(code: str | None) -> str:
    if not code:
        return "OTHER"
    c = code.upper().strip()
    if c == "UK":
        return "GB"
    return c if c else "OTHER"


def country_emoji(code: str) -> str:
    code = _normalize_code(code)
    return COUNTRY_META.get(code, COUNTRY_META["OTHER"])[1]


def country_name(code: str) -> str:
    code = _normalize_code(code)
    return COUNTRY_META.get(code, COUNTRY_META["OTHER"])[0]


def is_europe(code: str) -> bool:
    return _normalize_code(code) in EUROPE_CODES or code.upper() == "UK"


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def match_country_from_name(name: str) -> str | None:
    """基于节点名称关键词识别国家，词边界匹配避免误判。"""
    if not name:
        return None
    # 统一分隔符，便于 \b 匹配
    text = name.replace("_", " ").replace("-", " ").replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip()
    lower = text.lower()

    # 优先匹配更长短语
    candidates: list[tuple[int, str]] = []
    for rule in KEYWORD_RULES:
        for phrase in rule.phrases:
            if " " in phrase:
                if phrase in lower:
                    candidates.append((len(phrase), rule.code))
            else:
                # 词边界：US 不匹配 AUS 中的误匹配由短语表控制；
                # 对 2 字母代码要求两侧非字母数字
                pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
                if re.search(pattern, lower, re.IGNORECASE):
                    candidates.append((len(phrase), rule.code))

    if not candidates:
        # emoji 国旗粗检
        for code, (_, emoji) in COUNTRY_META.items():
            if code == "OTHER":
                continue
            if emoji and emoji in name:
                return _normalize_code(code)
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return _normalize_code(candidates[0][1])


class GeoResolver:
    """多级国家识别。"""

    def __init__(
        self,
        mmdb_path: str | None = None,
        enable_dns: bool = True,
    ) -> None:
        self.enable_dns = enable_dns
        self._reader = None
        self._dns_cache: dict[str, str | None] = {}
        self._ip_cache: dict[str, str | None] = {}

        if mmdb_path and Path(mmdb_path).is_file():
            try:
                import maxminddb  # type: ignore

                self._reader = maxminddb.open_database(mmdb_path)
                logger.info("GeoIP MMDB loaded: %s", mmdb_path)
            except Exception as e:
                logger.warning("GeoIP MMDB unavailable (%s), using keyword fallback", e)
                self._reader = None
        else:
            if mmdb_path:
                logger.info(
                    "GeoIP MMDB not found at %s, keyword fallback enabled", mmdb_path
                )

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None

    def _lookup_ip(self, ip: str) -> str | None:
        if ip in self._ip_cache:
            return self._ip_cache[ip]
        code: str | None = None
        if self._reader is not None:
            try:
                rec = self._reader.get(ip)
                if isinstance(rec, dict):
                    country = rec.get("country") or rec.get("registered_country") or {}
                    if isinstance(country, dict):
                        code = country.get("iso_code")
            except Exception as e:
                logger.debug("GeoIP lookup failed for %s: %s", ip, e)
        code = _normalize_code(code) if code else None
        if code == "OTHER":
            code = None
        self._ip_cache[ip] = code
        return code

    def _resolve_host_ip(self, host: str) -> str | None:
        if host in self._dns_cache:
            return self._dns_cache[host]
        if not self.enable_dns:
            self._dns_cache[host] = None
            return None
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            for info in infos:
                ip = info[4][0]
                if is_ip_address(ip):
                    self._dns_cache[host] = ip
                    return ip
        except Exception as e:
            logger.debug("DNS resolve failed for %s: %s", host, e)
        self._dns_cache[host] = None
        return None

    def resolve_code(self, node: Node) -> str:
        server = (node.server or "").strip()
        # 1) server 是 IP
        if server and is_ip_address(server):
            code = self._lookup_ip(server)
            if code:
                return code
        # 2) 域名 DNS + GeoIP
        elif server and self._reader is not None:
            ip = self._resolve_host_ip(server)
            if ip:
                code = self._lookup_ip(ip)
                if code:
                    return code
        # 3) 名称关键词
        code = match_country_from_name(node.name)
        if code:
            return code
        # 4) OTHER
        return "OTHER"

    def enrich(self, nodes: list[Node]) -> list[Node]:
        for n in nodes:
            try:
                code = self.resolve_code(n)
            except Exception:
                code = "OTHER"
            n.country_code = code
            n.country = country_name(code)
        return nodes


def annotate_countries(
    nodes: list[Node],
    mmdb_path: str | None = None,
    enable_dns: bool = True,
) -> list[Node]:
    resolver = GeoResolver(mmdb_path=mmdb_path, enable_dns=enable_dns)
    try:
        return resolver.enrich(nodes)
    finally:
        resolver.close()