"""账号级限速：请求最小间隔 + 抖动，避免触发腾讯反封号。

参考 Tom6814 的 RateLimiter：默认最小间隔 1.5s + ±0.3s 随机抖动。
单用户少量账号，够用即可。
"""
from __future__ import annotations

import asyncio
import random
import threading
import time


class AccountRateLimiter:
    def __init__(self, min_interval: float = 1.5, jitter: float = 0.3):
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._last = 0.0

    def wait_if_needed(self) -> None:
        """若距上次请求不足间隔，则等待补齐（异步友好：仅计算，调用方决定 await）。"""
        with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                # 加抖动
                wait += random.uniform(-self.jitter, self.jitter)
                wait = max(wait, 0.1)
            else:
                wait = 0.0
            self._last = time.time() + wait if wait > 0 else now
        if wait > 0:
            # 阻塞式等待（同步客户端场景够用）
            time.sleep(wait)


class AsyncAccountRateLimiter:
    """异步版本，用于 httpx.AsyncClient 场景。"""

    def __init__(self, min_interval: float = 1.5, jitter: float = 0.3):
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait_if_needed(self) -> None:
        async with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                wait += random.uniform(-self.jitter, self.jitter)
                wait = max(wait, 0.1)
            else:
                wait = 0.0
            self._last = now + wait if wait > 0 else now
        if wait > 0:
            await asyncio.sleep(wait)
