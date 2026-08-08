"""Proxy delay checking via Mihomo core."""

from .delay import check_nodes_delay
from .mihomo import MihomoRunner

__all__ = ["MihomoRunner", "check_nodes_delay"]