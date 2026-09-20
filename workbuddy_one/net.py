"""出站 HTTP 客户端工厂：统一代理策略与 `trust_env` 行为。

为什么需要这一层
----------------
项目里有 11 处直接 `httpx.Client(...)`，每处都重复写 `trust_env=False`。
代理策略一旦要调整就得改 11 个地方，且很容易漏——收敛到这里。

为什么默认 `trust_env=False`
----------------------------
httpx 默认会读 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 环境变量。
在 Docker、CI、公司内网里这些变量经常是**无效或指向内网**的，会让本该直连的
上游请求解析出一个连不通的代理，症状是"莫名其妙全部超时"，排查成本极高。

为什么又要有 `PROXY`
--------------------
"完全不能配代理"同样不行——企业网、或需要走代理才能访问上游的场景下，用户没有
任何手段。所以提供一个**显式**开关（`config.proxy`），而不是放开 `trust_env`
去赌环境变量干不干净。

用法
----
    from . import net
    with net.client(timeout=15) as c:            # 同步
        ...
    async with net.async_client(timeout=20) as c:  # 异步
        ...

不要在新的出站调用里直接写 `httpx.Client(...)`。
"""
from __future__ import annotations

import httpx

from .config import config


def client_kwargs(**extra) -> dict:
    """构造 httpx 客户端的公共参数（含显式代理；`extra` 可覆盖）。"""
    kwargs: dict = {"trust_env": False}
    # 取 DB 覆盖 > 环境变量（见 config.proxy_effective）：WebUI 里改完即时生效，
    # 不必重启进程。
    proxy = config.proxy_effective
    if proxy:
        kwargs["proxy"] = proxy
    kwargs.update(extra)
    return kwargs


def client(**extra) -> httpx.Client:
    """同步客户端（已应用统一代理策略）。"""
    return httpx.Client(**client_kwargs(**extra))


def async_client(**extra) -> httpx.AsyncClient:
    """异步客户端（已应用统一代理策略）。"""
    return httpx.AsyncClient(**client_kwargs(**extra))
