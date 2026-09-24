"""Mihomo / Clash Meta Core 进程管理与 External Controller 客户端。"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

logger = logging.getLogger(__name__)

DIALER_PROXY_NAME = "__china_dialer__"


def resolve_dialer_proxy(
    config: Any,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Build an optional SOCKS5 dialer from environment-backed settings."""
    if not isinstance(config, Mapping) or not bool(config.get("enabled", False)):
        return None

    proxy_type = str(config.get("type") or "socks5").strip().lower()
    if proxy_type not in {"socks", "socks5"}:
        raise ValueError("checker.dialer_proxy.type must be socks5")

    env = os.environ if environ is None else environ

    def required_env(key: str, default: str) -> str:
        env_name = str(config.get(key) or default).strip()
        value = env.get(env_name, "").strip()
        if not value:
            raise ValueError(
                f"checker.dialer_proxy is enabled but environment variable {env_name} is empty"
            )
        return value

    server = required_env("server_env", "CN_DIALER_SERVER")
    port_value = required_env("port_env", "CN_DIALER_PORT")
    try:
        port = int(port_value)
    except ValueError as e:
        raise ValueError("CN dialer port must be an integer") from e
    if not 1 <= port <= 65535:
        raise ValueError("CN dialer port must be between 1 and 65535")

    proxy: dict[str, Any] = {
        "name": DIALER_PROXY_NAME,
        "type": "socks5",
        "server": server,
        "port": port,
    }
    username = env.get(str(config.get("username_env") or "CN_DIALER_USERNAME"), "").strip()
    password = env.get(str(config.get("password_env") or "CN_DIALER_PASSWORD"), "").strip()
    if bool(username) != bool(password):
        raise ValueError("CN dialer username and password must be provided together")
    if username:
        proxy["username"] = username
        proxy["password"] = password
    if bool(config.get("tls", False)):
        proxy["tls"] = True

    return proxy


def resolve_mihomo_path(configured: str | None = None) -> str | None:
    """查找 mihomo 可执行文件。"""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
        # Windows 下补 .exe
        if not configured.endswith(".exe"):
            candidates.append(configured + ".exe")
    candidates.extend(
        [
            "bin/mihomo",
            "bin/mihomo.exe",
            "mihomo",
            "mihomo.exe",
            "clash-meta",
            "clash-meta.exe",
        ]
    )
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return str(p.resolve())
        found = shutil.which(c)
        if found:
            return found
    return None


class MihomoRunner:
    """启动临时 Mihomo 实例，通过 REST API 做真实代理延迟测试。"""

    def __init__(
        self,
        binary: str,
        api_host: str = "127.0.0.1",
        api_port: int = 9090,
        mixed_port: int = 17890,
        dialer_proxy: dict[str, Any] | None = None,
    ) -> None:
        self.binary = binary
        self.api_host = api_host
        self.api_port = api_port
        self.mixed_port = mixed_port
        self._proc: subprocess.Popen[bytes] | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._config_path: Path | None = None
        self.dialer_proxy = dict(dialer_proxy) if dialer_proxy else None

    @property
    def base_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"

    def _write_config(self, proxies: list[dict[str, Any]]) -> Path:
        assert self._tmpdir is not None
        # 使用安全临时名称，避免特殊字符导致 API 路径问题
        safe_proxies: list[dict[str, Any]] = []
        for i, p in enumerate(proxies):
            item = dict(p)
            item["name"] = f"t{i:04d}"
            safe_proxies.append(item)

        names = [p["name"] for p in safe_proxies]
        configured_proxies = safe_proxies
        if self.dialer_proxy:
            dialer = dict(self.dialer_proxy)
            dialer["name"] = DIALER_PROXY_NAME
            dialer.pop("dialer-proxy", None)
            for item in safe_proxies:
                item["dialer-proxy"] = DIALER_PROXY_NAME
            configured_proxies = [dialer, *safe_proxies]

        cfg = {
            "mixed-port": self.mixed_port,
            "allow-lan": False,
            "mode": "global",
            "log-level": "warning",
            "ipv6": True,
            "external-controller": f"{self.api_host}:{self.api_port}",
            "secret": "",
            "proxies": configured_proxies,
            "proxy-groups": [
                {
                    "name": "GLOBAL",
                    "type": "select",
                    "proxies": names + ["DIRECT"],
                }
            ],
            "rules": ["MATCH,GLOBAL"],
        }
        path = Path(self._tmpdir.name) / "config.yaml"
        path.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self._config_path = path
        return path

    def start(self, proxies: list[dict[str, Any]], ready_timeout: float = 30.0) -> None:
        if not proxies:
            raise ValueError("no proxies to test")
        self.stop()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="mihomo-check-")
        cfg_path = self._write_config(proxies)

        cmd = [self.binary, "-d", self._tmpdir.name, "-f", str(cfg_path)]
        logger.info(
            "Starting Mihomo: %s (%d proxies%s)",
            self.binary,
            len(proxies),
            " via configured dialer" if self.dialer_proxy else "",
        )
        # Windows 下创建新进程组，便于终止
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(cmd, **kwargs)
        deadline = time.time() + ready_timeout
        last_err = ""
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = ""
                try:
                    err = (self._proc.stderr.read() or b"").decode("utf-8", errors="ignore")
                except Exception:
                    pass
                raise RuntimeError(f"Mihomo exited early: {err[:500]}")
            try:
                # trust_env=False：避免系统 HTTP_PROXY 劫持 127.0.0.1 API
                with httpx.Client(timeout=2.0, trust_env=False) as client:
                    r = client.get(f"{self.base_url}/version")
                    if r.status_code == 200:
                        logger.info("Mihomo ready: %s", r.text[:200])
                        return
            except Exception as e:
                last_err = str(e)
            time.sleep(0.3)
        raise TimeoutError(f"Mihomo API not ready: {last_err}")

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            except Exception as e:
                logger.debug("stop mihomo: %s", e)
            self._proc = None
        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except Exception:
                pass
            self._tmpdir = None

    def __enter__(self) -> "MihomoRunner":
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def delay(
        self,
        proxy_name: str,
        test_url: str,
        timeout_ms: int,
    ) -> int | None:
        """
        调用 GET /proxies/{name}/delay?url=...&timeout=...
        成功返回 delay(ms)，失败返回 None。
        """
        # External Controller 需要对 name 做 URL 编码
        encoded = quote(proxy_name, safe="")
        url = f"{self.base_url}/proxies/{encoded}/delay"
        try:
            with httpx.Client(
                timeout=(timeout_ms / 1000.0) + 5.0, trust_env=False
            ) as client:
                r = client.get(
                    url,
                    params={"url": test_url, "timeout": timeout_ms},
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                delay = data.get("delay")
                if delay is None:
                    return None
                d = int(delay)
                return d if d > 0 else None
        except Exception:
            return None


def make_temp_proxy_name(index: int) -> str:
    return f"t{index:04d}"
