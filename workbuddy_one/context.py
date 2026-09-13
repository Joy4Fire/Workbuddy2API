"""网关运行时上下文：create_app 装配后传给各路由模块的共享依赖。

routes/ 下的注册函数签名统一为 `register(app, ctx)`；需要共享状态的
gateway 函数也以 ctx 为第一参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .benchmarks import AABenchmarks
from .credentials import CredentialManager
from .db import Database
from .models import ModelRegistry
from .pool import AccountPool
from .ratelimit import AsyncAccountRateLimiter
from .scheduler import Scheduler


@dataclass
class GatewayContext:
    db: Database
    pool: AccountPool
    models: ModelRegistry
    benchmarks: AABenchmarks
    scheduler: Scheduler
    managers: dict[str, CredentialManager]
    limiters: dict[str, AsyncAccountRateLimiter] = field(default_factory=dict)
    auth_files_count: int = 0
