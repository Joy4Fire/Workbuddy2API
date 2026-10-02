"""手动检查本项目源仓库版本；只读取版本声明，绝不执行远端代码。"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from . import __version__, net

SOURCE_URL = 'https://raw.githubusercontent.com/Joy4Fire/Workbuddy2API/main/workbuddy_one/__init__.py'
PROJECT_URL = 'https://github.com/Joy4Fire/Workbuddy2API'
SUCCESS_TTL = 6 * 3600
FAIL_TTL = 60
MAX_SOURCE_BYTES = 32768


def parse_version(source: str) -> tuple[str, tuple[int, int, int]]:
    # 只接受稳定版的字面声明，不能用 exec/import 读取不受信任的网络内容。
    match = re.search(r'^__version__\s*=\s*[\"\'](\d+\.\d+\.\d+)[\"\']\s*(?:#.*)?$', source, re.MULTILINE)
    if not match:
        raise ValueError('版本声明无法识别')
    version = match.group(1)
    return version, tuple(int(p) for p in version.split('.'))


class UpdateChecker:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_attempt: float | None = None
        self._retry_after = 0
        self._snapshot = {
            'current_version': __version__, 'latest_version': None, 'status': 'unchecked',
            'checked_at': None, 'error': '', 'project_url': PROJECT_URL, 'source_url': SOURCE_URL,
        }

    def cached(self) -> dict:
        """高频 GET 仅取快照；页面打开与网关启动都不触发外部请求。"""
        return dict(self._snapshot)

    async def check(self) -> dict:
        # 按钮连点和并发调用合并，失败也短暂缓存，避免 GitHub 故障时持续轰炸。
        async with self._lock:
            if self._last_attempt is not None and time.monotonic() - self._last_attempt < self._retry_after:
                return self.cached()
            try:
                async with asyncio.timeout(12):
                    async with net.async_client(timeout=10) as client:
                        async with client.stream('GET', SOURCE_URL, headers={'Accept': 'text/plain'}) as response:
                            response.raise_for_status()
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > MAX_SOURCE_BYTES:
                                    raise ValueError('版本文件超出大小上限')
                latest, remote = parse_version(content.decode('utf-8'))
                _, current = parse_version(f'__version__ = "{__version__}"')
                status = 'update_available' if remote > current else 'local_ahead' if remote < current else 'up_to_date'
                self._snapshot.update(latest_version=latest, status=status, checked_at=time.time(), error='')
                self._retry_after = SUCCESS_TTL
            except (httpx.HTTPError, ValueError, TimeoutError):
                # 网络失败不能被展示成「已是最新」；保留上次版本但明确本次未核验。
                self._snapshot.update(status='unavailable', checked_at=time.time(),
                                      error='暂时无法核验源仓库版本，请检查网络或代理后重试。')
                self._retry_after = FAIL_TTL
            self._last_attempt = time.monotonic()
            return self.cached()
