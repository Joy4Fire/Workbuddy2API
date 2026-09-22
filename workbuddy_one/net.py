"""出站 HTTP 客户端工厂：统一代理策略、`trust_env` 行为与连接层参数。

为什么需要这一层
----------------
项目里有 11 处直接 `httpx.Client(...)`，每处都重复写 `trust_env=False`。
代理策略一旦要调整就得改 11 个地方，且很容易漏——收敛到这里。
超时/连接池/keepalive 同理：以前全是 httpx 默认值，聊天流式被 read 超时
掐断的问题只能一处处改（见 DEFAULT_TIMEOUT 注释）。

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

# ---- 连接层加固（P1-1，2026-09-21）----
#
# 背景：以前这里只统一了代理策略，超时/连接池/keepalive 全用 httpx 默认值。
# 默认值有两个问题：
#   1) `Timeout` 默认是 5s 全阶段（connect/read/write/pool），对**聊天流式**来说
#      read 超时太短——模型思考 10 秒就被掐断；而 connect 5s 又偏长（上游不可达
#      时要干等 5 秒才失败）。所以改成细粒度：连接快失败、读放大。
#   2) 连接池无上限，突发并发会开出大量 TCP 连接，正好撞上游风控。
#
# 这些是**默认值**，调用方仍可用 `net.client(timeout=...)` 覆盖（extra 优先）。
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
# 连接池上限：keepalive 复用 + 有界并发。max_connections 与单账号在途上限
# （config.max_in_flight，默认 3）不在一个量级——池是**进程级**的，要容纳
# 多个账号 × 各自的并发，同时又不至于无限膨胀。
DEFAULT_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16,
                              keepalive_expiry=30.0)
# 显式禁用 HTTP/2：上游（APISIX 网关）对 h2 的支持不稳，曾观察到 h2 下的
# 偶发 RST_STREAM。httpx 的 `http2=False` 是默认值，这里显式写出来是为了
# 表明"这是刻意的决定，不是没配"。


def client_kwargs(**extra) -> dict:
    """构造 httpx 客户端的公共参数（显式代理 + 连接层加固；`extra` 可覆盖）。"""
    kwargs: dict = {
        "trust_env": False,
        "timeout": DEFAULT_TIMEOUT,
        "limits": DEFAULT_LIMITS,
        "http2": False,
        # 连接建立阶段的额外兜底：httpx 的 connect 超时只覆盖 TCP+TLS 握手，
        # 上游网关偶发"接受连接但不回响应头"，靠 read 超时兜（见 DEFAULT_TIMEOUT）
    }
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
