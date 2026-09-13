"""推理链路：应用 Key 鉴权、选号、限速、请求体增强、上游连接与换号重试、用量记账。

所有函数接收 GatewayContext（含 db/pool/models/limiters 等运行时状态），
保持与 FastAPI 路由解耦、可独立单测。
"""
from __future__ import annotations

import hashlib
import time
import logging

import httpx
from fastapi import HTTPException

from ..config import config
from ..desensitize import desensitize_body
from ..ratelimit import AsyncAccountRateLimiter
from ..reasoning import sanitize_body
from ..upstream import stream_upstream, UpstreamError

logger = logging.getLogger("workbuddy_one.gateway.inference")

# 不同错误对应的冷却时长（秒）
COOLDOWN_NONE = 0.0
COOLDOWN_SOFT = 60.0       # 通用失败
COOLDOWN_HARD = 1800.0     # 认证/限流（401/403/429）


def cooldown_for(status_code: int) -> float:
    """按上游 HTTP 状态码选择冷却时长。

    429 限流若只用 60s 软冷却，账号会反复被限流、UI 在健康/不可用间抖动，
    故限流给 5 分钟；认证类错误（401/403）用 30 分钟；上游服务错误 2 分钟；
    其余走 60s 软冷却。
    """
    if status_code == 429:
        return 300.0
    if status_code in (401, 403):
        return COOLDOWN_HARD
    if status_code >= 500:
        return 120.0
    return COOLDOWN_SOFT


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def check_api_key(ctx, authorization, x_api_key) -> str:
    """校验应用 API Key，返回对应应用名；无效则 401。

    不再使用单一的 API_KEY 主密钥——每个请求必须属于某个命名应用，
    以便使用记录能追溯来源。
    """
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if not token:
        raise HTTPException(status_code=401, detail={"error": {"message": "missing api key", "type": "auth_error"}})
    app = ctx.db.find_app_by_key(hash_key(token))
    if app is None:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})
    return app["name"]


def limiter(ctx, uid: str) -> AsyncAccountRateLimiter:
    """按账号取限速器（惰性创建）。"""
    if uid not in ctx.limiters:
        ctx.limiters[uid] = AsyncAccountRateLimiter(min_interval=config.ratelimit_interval)
    return ctx.limiters[uid]


def pick_account(ctx):
    """从池中选择一个账号，返回 Account；无账号则 503。"""
    acc = ctx.pool.pick()
    if acc is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "无可用账号（全部冷却或额度耗尽），请检查账号状态", "type": "auth_error"}})
    # fallback 顶班账号（无健康账号时选出的最早冷却到期者）若还要冷却 30 秒以上，
    # 不硬打——把请求送上去只会再吃一个 429，形成 429 风暴；直接明确拒绝
    if acc.cooldown_until - time.time() > 30:
        raise HTTPException(status_code=503, detail={
            "error": {"message": "上游限流中，所有账号均在冷却，请稍后重试", "type": "rate_limit_error"}})
    return acc


def get_headers(ctx, account):
    """取账号请求头（token 过期时自动刷新）；刷新失败转 503 并冷却该账号。

    RuntimeError 直接漏到 FastAPI 会变成裸 500，客户端无从得知该做什么。
    """
    try:
        return account.mgr.get_headers()
    except RuntimeError as e:
        ctx.pool.on_failure(account.uid, COOLDOWN_HARD)
        raise HTTPException(status_code=503, detail={
            "error": {"message": f"账号 token 刷新失败，请到 WebUI 重新扫码登录（{e}）",
                      "type": "auth_error"}})


async def open_upstream_once(ctx, account, body: dict):
    """对单个账号建立上游连接并预取首个 SSE 行，成功返回 (iterator, 首行)。"""
    if config.ratelimit:
        await limiter(ctx, account.uid).wait_if_needed()
    headers = get_headers(ctx, account)
    it = stream_upstream(headers, body).__aiter__()
    first = await it.__anext__()   # 首次 anext 会真正发起上游请求；失败抛 UpstreamError
    return it, first


async def open_upstream(ctx, account, body: dict):
    """在返回 StreamingResponse 前，先建立上游连接并预取首个 SSE 行。

    这样上游在流真正开始前失败时，能返回正确的 HTTP 状态码（而非 200+SSE 错误），
    客户端可以正确识别错误而不是卡住等待。
    返回 (iterator, 首行, 实际使用的账号)——429/502/503 时会自动换健康账号重试一次
    （仅一次，不递归），换号后调用方必须用返回的账号记账，而不是最初的账号。
    """
    try:
        it, first = await open_upstream_once(ctx, account, body)
        return it, first, account
    except UpstreamError as e:
        if e.status_code not in (429, 502, 503):
            raise
        # 该账号已确认打不通：先上冷却，避免下面 pick() 又选中它原地重试
        ctx.pool.on_failure(account.uid, cooldown_for(e.status_code))
        if ctx.pool.healthy_count() < 1:
            # 没有其它健康账号：放弃重试，按原错误交给调用方记录/返回
            #（后续请求会被 pick_account 的冷却兜底挡下并得到 503）
            raise
        alt = pick_account(ctx)
        try:
            it, first = await open_upstream_once(ctx, alt, body)
        except (UpstreamError, httpx.HTTPError):
            ctx.pool.on_failure(alt.uid, COOLDOWN_SOFT)
            raise
        return it, first, alt


def enhance_body(ctx, body: dict) -> dict:
    """统一规整 + 可选脱敏，构造最终上游请求体。"""
    from ..reasoning import parse_model_aliases, resolve_model_alias
    # 模型别名解析：客户端用熟名字（gpt-4o 等）也能路由到真实模型
    if body.get("model"):
        aliases = parse_model_aliases(ctx.db.get_settings().get("model_aliases") or "")
        body["model"] = resolve_model_alias(str(body["model"]), aliases)
    # max_tokens 按模型实际上限裁剪：Claude Code 常发 32000+，
    # 超过部分模型上限会被上游拒绝（目录未知时不裁剪）
    max_out = ctx.models.max_output_tokens(str(body.get("model") or ""))
    if max_out and body.get("max_tokens") and body["max_tokens"] > max_out:
        logger.info("max_tokens %s 超过模型 %s 上限，裁剪为 %s",
                    body["max_tokens"], body.get("model"), max_out)
        body["max_tokens"] = max_out
    # 动态思考强度表：来自模型目录（动态获取），无则交给内置静态表
    model_id = body.get("model")
    dyn_efforts = None
    if model_id:
        efforts = ctx.models.reasoning_efforts(model_id)
        if efforts:
            dyn_efforts = {model_id: efforts}
    body = sanitize_body(body, efforts=dyn_efforts)
    if config.desensitize:
        body = desensitize_body(body, roles=("system", "developer"))
    return body


def log_usage(ctx, protocol, model_name, account, t0, status, err="", usage=None,
              cooldown=COOLDOWN_NONE, input_content="", output_content="",
              reasoning_content="", app_name="", update_pool=True):
    usage = usage or {}
    # update_pool=False 用于"客户端断开补记"等非账号过错的场景：
    # 断开不是账号的失败，不应计入失败数/冷却（否则 Claude Code 常见的主动中断
    # 会把健康账号打成"冷却中"）
    if update_pool:
        if status == "ok":
            ctx.pool.on_success(account.uid)
        else:
            ctx.pool.on_failure(account.uid, cooldown or COOLDOWN_SOFT)
    # 超长文本裁剪：思考链/输出也可能很长（实测 COT 单条 4 万+ 字符）
    ctx.db.log_usage(model=model_name, protocol=protocol, account_uid=account.uid,
                     input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                     output_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                     latency_ms=(time.time() - t0) * 1000, status=status, error=err,
                     input_content=input_content, output_content=output_content,
                     reasoning_content=reasoning_content,
                     credits=usage.get("credit") or 0,
                     app_name=app_name)
