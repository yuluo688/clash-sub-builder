"""通过 Mihomo External Controller 并发测速（真实代理延迟）。"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

from src.checker.mihomo import MihomoRunner, make_temp_proxy_name, resolve_mihomo_path
from src.models import Node

logger = logging.getLogger(__name__)


async def _delay_async(
    client: httpx.AsyncClient,
    base_url: str,
    proxy_name: str,
    test_url: str,
    timeout_ms: int,
    retries: int,
) -> int | None:
    encoded = quote(proxy_name, safe="")
    url = f"{base_url}/proxies/{encoded}/delay"
    params = {"url": test_url, "timeout": timeout_ms}
    # httpx timeout 需略大于测速 timeout
    req_timeout = (timeout_ms / 1000.0) + 3.0
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, params=params, timeout=req_timeout)
            if r.status_code != 200:
                continue
            data = r.json()
            delay = data.get("delay")
            if delay is None:
                continue
            d = int(delay)
            if d > 0:
                return d
        except Exception:
            if attempt >= retries:
                return None
            await asyncio.sleep(0.2)
    return None


async def _test_batch_async(
    base_url: str,
    count: int,
    test_url: str,
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
                    test_url,
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
    api_host: str = "127.0.0.1",
    api_port: int = 9090,
    batch_size: int = 100,
    enabled: bool = True,
) -> list[Node]:
    """
    使用 Mihomo 核心对节点做真实 delay 测试。
    - 按 batch 启动 mihomo，写入临时配置
    - 并发调用 /proxies/{name}/delay
    - 单节点失败不中断
    """
    if not enabled:
        logger.warning("Checker disabled; marking all nodes untested (latency=None)")
        for n in nodes:
            n.latency = None
        return nodes

    if not nodes:
        return nodes

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
        "Delay check start: %d nodes, concurrency=%d, timeout=%dms, batch=%d",
        total,
        concurrency,
        timeout,
        batch_size,
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
        )
        # 每批使用不同端口，避免 TIME_WAIT 冲突
        try:
            runner.start(proxies)
            results = asyncio.run(
                _test_batch_async(
                    base_url=runner.base_url,
                    count=len(batch),
                    test_url=test_url,
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