"""通过 Mihomo External Controller 并发测速（真实代理延迟）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import httpx

from src.checker.mihomo import MihomoRunner, make_temp_proxy_name, resolve_mihomo_path
from src.filter import is_complete
from src.models import Node

logger = logging.getLogger(__name__)


def select_cn_dialer(
    candidates: Sequence[Node],
    *,
    mihomo_path: str | None = None,
    api_host: str = "127.0.0.1",
    api_port: int = 9090,
    timeout: int = 5000,
    max_candidates: int = 5,
    geo_url: str = "https://ipinfo.io/json",
) -> Node | None:
    """Only trust a candidate as a CN dialer after its HTTPS exit check returns CN."""
    if not geo_url.startswith("https://"):
        raise ValueError("checker.cn_chain.geo_url must use HTTPS")
    binary = resolve_mihomo_path(mihomo_path)
    if not binary:
        logger.warning("Mihomo binary missing; CN dialer verification unavailable")
        return None

    for index, node in enumerate((n for n in candidates if is_complete(n)), start=1):
        if index > max_candidates:
            break
        runner = MihomoRunner(binary=binary, api_host=api_host, api_port=api_port)
        try:
            proxy = node.to_clash_proxy()
            proxy.pop("dialer-proxy", None)
            runner.start([proxy])
            with httpx.Client(
                proxy=f"http://127.0.0.1:{runner.mixed_port}",
                timeout=timeout / 1000.0 + 3.0,
                trust_env=False,
            ) as client:
                response = client.get(geo_url)
                response.raise_for_status()
                country = response.json().get("country")
            if isinstance(country, str) and country.upper() == "CN":
                logger.info("CN dialer verified (candidate %d)", index)
                return node
            logger.info("CN dialer candidate %d has non-CN or unknown exit", index)
        except Exception as exc:
            logger.warning(
                "CN dialer candidate %d verification failed (%s)", index, type(exc).__name__
            )
        finally:
            runner.stop()
    logger.warning("No verified CN dialer; leaving candidates for client-side checks")
    return None


def _build_test_urls(
    test_url: str,
    fallback_test_urls: Sequence[str] | str | None,
) -> tuple[str, ...]:
    """Build a stable, de-duplicated list of delay test targets."""
    candidates: list[object] = [test_url]
    if isinstance(fallback_test_urls, str):
        candidates.append(fallback_test_urls)
    elif fallback_test_urls:
        candidates.extend(fallback_test_urls)

    urls: list[str] = []
    for candidate in candidates:
        url = str(candidate).strip()
        if url and url not in urls:
            urls.append(url)

    if not urls:
        raise ValueError("at least one delay test URL is required")
    return tuple(urls)


async def _delay_async(
    client: httpx.AsyncClient,
    base_url: str,
    proxy_name: str,
    test_urls: tuple[str, ...],
    timeout_ms: int,
    retries: int,
) -> int | None:
    encoded = quote(proxy_name, safe="")
    url = f"{base_url}/proxies/{encoded}/delay"
    # httpx timeout 需略大于测速 timeout。每次重试轮换目标，避免单一
    # 204 站点被节点策略或临时网络故障误判为节点不可用。
    req_timeout = (timeout_ms / 1000.0) + 3.0
    for attempt in range(retries + 1):
        test_url = test_urls[attempt % len(test_urls)]
        try:
            r = await client.get(
                url,
                params={"url": test_url, "timeout": timeout_ms},
                timeout=req_timeout,
            )
            if r.status_code != 200:
                raise RuntimeError(f"delay API returned {r.status_code}")
            delay = r.json().get("delay")
            if delay is not None:
                measured = int(delay)
                if measured > 0:
                    return measured
        except Exception:
            pass
        if attempt < retries:
            await asyncio.sleep(0.2)
    return None


async def _test_batch_async(
    base_url: str,
    count: int,
    test_urls: tuple[str, ...],
    timeout_ms: int,
    concurrency: int,
    retries: int,
) -> list[int | None]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[int | None] = [None] * count

    # 本地 External Controller 必须绕过系统代理
    async with httpx.AsyncClient(trust_env=False) as client:

        async def one(i: int) -> None:
            name = make_temp_proxy_name(i)
            async with sem:
                results[i] = await _delay_async(
                    client,
                    base_url,
                    name,
                    test_urls,
                    timeout_ms,
                    retries,
                )

        await asyncio.gather(*(one(i) for i in range(count)))
    return results


def check_nodes_delay(
    nodes: list[Node],
    *,
    mihomo_path: str | None = None,
    timeout: int = 5000,
    concurrency: int = 20,
    retries: int = 1,
    test_url: str = "https://www.gstatic.com/generate_204",
    fallback_test_urls: Sequence[str] | str | None = None,
    api_host: str = "127.0.0.1",
    api_port: int = 9090,
    batch_size: int = 100,
    enabled: bool = True,
    dialer_proxy: dict[str, Any] | None = None,
) -> list[Node]:
    """
    使用 Mihomo 核心对节点做真实 delay 测试。
    - 按 batch 启动 mihomo，写入临时配置
    - 并发调用 /proxies/{name}/delay
    - 每次重试轮换检测地址，任一地址成功即保留节点
    - 可选通过 dialer_proxy 链接到大陆出口后再测候选节点
    - 单节点失败不中断
    """
    if not enabled:
        logger.warning("Checker disabled; marking all nodes untested (latency=None)")
        for n in nodes:
            n.latency = None
        return nodes

    if not nodes:
        return nodes

    test_urls = _build_test_urls(test_url, fallback_test_urls)
    binary = resolve_mihomo_path(mihomo_path)
    if not binary:
        logger.error(
            "Mihomo binary not found. Skip live check; all nodes marked failed. "
            "Run: python scripts/download_mihomo.py"
        )
        for n in nodes:
            n.latency = -1
        return nodes

    # 为避免名称冲突，测速时使用临时名；原始 node.name 保留
    total = len(nodes)
    logger.info(
        "Delay check start: %d nodes, concurrency=%d, timeout=%dms, batch=%d, targets=%s",
        total,
        concurrency,
        timeout,
        batch_size,
        ", ".join(test_urls),
    )

    alive = 0
    for start in range(0, total, max(1, batch_size)):
        batch = nodes[start : start + batch_size]
        proxies = [n.to_clash_proxy() for n in batch]
        # 强制临时名
        for i, p in enumerate(proxies):
            p["name"] = make_temp_proxy_name(i)

        runner = MihomoRunner(
            binary=binary,
            api_host=api_host,
            api_port=api_port + (start // max(1, batch_size)) % 50,
            dialer_proxy=dialer_proxy,
        )
        # 每批使用不同端口，避免 TIME_WAIT 冲突
        try:
            runner.start(proxies)
            results = asyncio.run(
                _test_batch_async(
                    base_url=runner.base_url,
                    count=len(batch),
                    test_urls=test_urls,
                    timeout_ms=timeout,
                    concurrency=concurrency,
                    retries=retries,
                )
            )
            for node, delay in zip(batch, results):
                if delay is not None and delay > 0:
                    node.latency = int(delay)
                    node.score = 1.0 / (1.0 + node.latency)
                    alive += 1
                else:
                    node.latency = -1
                    node.score = 0.0
        except Exception as e:
            logger.error("Batch %d-%d mihomo check failed: %s", start, start + len(batch), e)
            for node in batch:
                node.latency = -1
                node.score = 0.0
        finally:
            runner.stop()

        logger.info(
            "Batch progress: %d/%d tested, alive so far ~%d",
            min(start + len(batch), total),
            total,
            alive,
        )

    logger.info("Delay check done: alive=%d / tested=%d", alive, total)
    return nodes


def check_nodes_delay_sync_fallback(
    nodes: list[Node],
    runner: MihomoRunner,
    test_url: str,
    timeout_ms: int,
    concurrency: int,
    retries: int,
) -> None:
    """线程池同步兜底（一般不走这里）。"""
    def work(i: int) -> tuple[int, int | None]:
        name = make_temp_proxy_name(i)
        d: int | None = None
        for _ in range(retries + 1):
            d = runner.delay(name, test_url, timeout_ms)
            if d is not None:
                break
        return i, d

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = [ex.submit(work, i) for i in range(len(nodes))]
        for fut in futs:
            try:
                i, d = fut.result()
                if d is not None and d > 0:
                    nodes[i].latency = d
                else:
                    nodes[i].latency = -1
            except Exception:
                pass
