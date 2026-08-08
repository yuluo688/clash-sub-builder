"""统一节点数据模型与运行统计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    """内部统一节点模型。

    raw: 保留 Clash Meta 所需的完整代理字典（除 name 外尽量不改）。
    重命名只改 name / raw['name']，不碰认证字段。
    """

    name: str
    type: str
    server: str
    port: int
    raw: dict[str, Any] = field(default_factory=dict)

    # 认证相关（便于 fingerprint）
    uuid: str | None = None
    password: str | None = None

    # 传输相关摘要
    tls: bool | None = None
    network: str | None = None
    sni: str | None = None
    ws_path: str | None = None
    ws_headers: dict[str, str] | None = None
    grpc_service_name: str | None = None

    # 元数据
    original_source: str = ""
    country: str = "Other"
    country_code: str = "OTHER"
    city: str | None = None
    latency: int | None = None  # ms，None=未测，-1=失败
    score: float = 0.0

    def credential(self) -> str:
        """用于去重的凭证字段。"""
        if self.uuid:
            return self.uuid
        if self.password:
            return self.password
        # 回退：从 raw 取常见字段
        for key in ("uuid", "password", "psk"):
            val = self.raw.get(key)
            if val:
                return str(val)
        return ""

    def to_clash_proxy(self) -> dict[str, Any]:
        """输出 Clash Meta proxy 字典，只覆盖 name。"""
        proxy = dict(self.raw) if self.raw else {}
        proxy["name"] = self.name
        # 保证基础字段存在
        proxy.setdefault("type", self.type)
        proxy.setdefault("server", self.server)
        proxy.setdefault("port", self.port)
        return proxy


@dataclass
class Stats:
    """流水线统计。"""

    sources: int = 0
    downloaded: int = 0
    raw_nodes: int = 0
    parsed: int = 0
    duplicates_removed: int = 0
    tested: int = 0
    alive: int = 0
    filtered: int = 0
    countries: int = 0
    errors: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return [
            f"Sources: {self.sources}",
            f"Downloaded: {self.downloaded}",
            f"Raw Nodes: {self.raw_nodes}",
            f"Parsed: {self.parsed}",
            f"Duplicates Removed: {self.duplicates_removed}",
            f"Tested: {self.tested}",
            f"Alive: {self.alive}",
            f"Filtered: {self.filtered}",
            f"Countries: {self.countries}",
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "downloaded": self.downloaded,
            "raw_nodes": self.raw_nodes,
            "parsed": self.parsed,
            "duplicates_removed": self.duplicates_removed,
            "tested": self.tested,
            "alive": self.alive,
            "filtered": self.filtered,
            "countries": self.countries,
        }