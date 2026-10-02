"""推理链路：应用 Key 鉴权、选号、限速、请求体增强、上游连接与换号重试、用量记账。

所有函数接收 GatewayContext（含 db/pool/models/limiters 等运行时状态），
保持与 FastAPI 路由解耦、可独立单测。
"""
from __future__ import annotations

import hashlib
import asyncio
import time
import logging

import httpx
from fastapi import HTTPException

from ..config import config
from ..desensitize import desensitize_body
from ..region import region_of_account
from .errors import (  # noqa: F401（is_rate_limit_body re-export 供 routes 使用）
    ErrKind, action_for, classify, is_rate_limit_body,
)
from . import session
from ..ratelimit import AsyncAccountRateLimiter
from ..reasoning import sanitize_body
from ..upstream import stream_upstream, UpstreamError

logger = logging.getLogger("workbuddy_one.gateway.inference")

# 不同错误对应的冷却时长（秒）
COOLDOWN_NONE = 0.0
COOLDOWN_SOFT = 60.0       # 通用失败
COOLDOWN_HARD = 1800.0     # 认证/限流（401/403/429）


def cooldown_for(status_code: int) -> float:
    """按上游 HTTP 状态码选择冷却时长（**已不参与主链路**，保留给旧调用方/测试）。

    主链路改用 `errors.classify` + `errors.action_for`（错误分类表）。本函数只在
    "分类器也判不出语义"的极端兜底场景有意义，目前无生产调用方。
    """
    if status_code == 429:
        return 300.0
    if status_code in (401, 403):
        return COOLDOWN_HARD
    if status_code >= 500:
        return 120.0
    return COOLDOWN_SOFT


def cooldown_for_error(status_code: int, raw: bytes, headers=None) -> float:
    """错误 → 冷却秒数（错误分类表的薄封装，保留向后兼容的签名）。

    真正的语义在 `errors.classify`（这是什么错）+ `errors.action_for`（怎么处置）。
    """
    from .errors import classify as _classify
    return action_for(_classify(status_code, raw, headers), raw, headers).cooldown


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


def model_prices(ctx, model: str) -> dict:
    models = getattr(ctx, 'models', None)
    return models.prices_for(model) if models is not None and hasattr(models, 'prices_for') else {}


def pick_account(ctx, regions: set[str] | None = None, model: str = "", exclude=None):
    """从池中选择一个账号，返回 Account；无账号则 503。

    regions: 可选，限定区域（见 pool.pick 与 models.regions_for）。混池下按请求的
    模型把候选账号收敛到「确实提供该模型」的区域，避免被上游 400（11102）拒绝。
    model: 可选，请求的模型名。用于跳过「该 (账号,模型) 已被冷却」的组合
    （6004 模型级限流 / 11102 负缓存），否则换号时会原地重试同一个坏组合。
    """
    kwargs = {"exclude": exclude} if exclude else {}
    acc = ctx.pool.pick(regions=regions, model=model, prices=model_prices(ctx, model), **kwargs)
    if acc is None:
        if model:
            raise HTTPException(status_code=503, detail={"error": {
                "message": f"模型 {model} 在全部账号上均不可用（已被限流或该账号无此模型），请稍后重试或更换模型",
                "type": "model_unavailable"}})
        raise HTTPException(status_code=503, detail={"error": {"message": "无可用账号（全部冷却或额度耗尽），请检查账号状态", "type": "auth_error"}})
    # fallback 顶班账号（无健康账号时选出的最早冷却到期者）若还要冷却 30 秒以上，
    # 不硬打——把请求送上去只会再吃一个 429，形成 429 风暴；直接明确拒绝
    if acc.cooldown_until - time.time() > 30:
        raise HTTPException(status_code=503, detail={
            "error": {"message": "上游限流中，所有账号均在冷却，请稍后重试", "type": "rate_limit_error"}})
    return acc


def model_regions(ctx, body: dict) -> set[str] | None:
    """请求模型可用的区域集合；返回 None 表示「不限制区域」。

    混池下国内外模型集大部分不重叠（实测国际版 18 个 / 国内版 16 个，交集仅 5 个），
    把区域专属模型发给另一个区域的账号会被上游 400（code=11102 service info not found）。

    目录里没有该模型时（别名、新模型、目录尚未拉到）返回 None —— **不限制**，
    保持原有随机轮换，避免把未知模型直接打成不可用。
    """
    models = getattr(ctx, "models", None)
    if models is None or not hasattr(models, "regions_for"):
        return None  # 无模型目录（部分测试用的精简 ctx）：不限制区域
    model = str(body.get("model") or "").strip()
    if not model:
        return None
    return models.regions_for(model) or None


def acquire_account(ctx, body: dict, raw: dict | None = None):
    """选号（含会话粘性 + 按模型限定区域）。返回 (account, session_key)。

    会话键从 raw（客户端原始 payload）提取——build_upstream_body 的白名单
    会剥掉 prompt_cache_key/metadata 等非透传字段，从过滤后的 body 提取会
    丢失这两个键来源（只剩 user 字段与消息指纹兜底）。raw 缺省回落 body。
    粘性（多账号场景）：同一会话键此前绑定的账号若仍在当前低价优选池，
    直接复用——保住上游 prompt cache 命中；出现更低价可用账号或原账号不可用则解粘，
    回池按权重重选并重绑新账号。单账号场景键照常提取，行为不变。
    """
    key = session.extract_session_key(raw if raw is not None else body)
    regions = model_regions(ctx, body)
    model = str(body.get("model") or "").strip()
    sticky_uid = ctx.session_router.lookup(key, ctx.pool) if key else None
    if sticky_uid and not ctx.pool.can_reuse(sticky_uid, regions=regions, model=model,
                                             prices=model_prices(ctx, model)):
        ctx.session_router.unbind(key)
        sticky_uid = None
    if sticky_uid:
        for a in ctx.pool.accounts:
            if a.uid == sticky_uid:
                if regions and a.region_id not in regions:
                    # 粘住的账号所在区域不提供该模型：解粘，让下面按区域重选
                    ctx.session_router.unbind(key)
                    break
                if a.model_cooling(time.time(), model):
                    # 粘住的账号刚好把这个模型冷却了（6004/11102）：解粘重选，
                    # 否则会一路粘着撞同一堵墙
                    ctx.session_router.unbind(key)
                    break
                if a.cooldown_until - time.time() <= 30:  # 与 pick_account 同一冷却兜底
                    return a, key
                # 粘住的账号在冷却：解粘走正常轮换
                ctx.session_router.unbind(key)
                break
    acc = pick_account(ctx, regions=regions, model=model)
    if key:
        ctx.session_router.bind(key, acc.uid)
    return acc, key


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
    """对单个账号建立上游连接并预取首个 SSE 行，成功返回 (iterator, 首行)。

    在途名额只在这段窗口里占用（P1-1）：上游风控看的是**同时打到它的连接数**，
    首字节到达后流已建立，再计数只会引入"流没读完就泄漏名额"的故障模式。
    选号后的竞争由原子占位约束；满载最多等待 10 秒，不发送未获得名额的请求。
    """
    if config.ratelimit:
        await limiter(ctx, account.uid).wait_if_needed()
    # 可能刷新 token（同步网络）或等待凭据锁，必须让出事件循环给管理端。
    headers = await asyncio.to_thread(get_headers, ctx, account)
    deadline = time.monotonic() + 10.0
    while not ctx.pool.acquire_slot(account.uid):
        if time.monotonic() >= deadline:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "账号连接名额繁忙，请稍后重试", "type": "capacity_error"}})
        await asyncio.sleep(0.05)
    it = None
    try:
        it = stream_upstream(headers, body).__aiter__()
        first = await it.__anext__()   # 首次 anext 会真正发起上游请求；失败抛 UpstreamError
    except StopAsyncIteration:
        raise UpstreamError(502, b'{"error":{"message":"upstream returned an empty stream","type":"upstream_error"}}')
    except BaseException:
        if it is not None:
            await it.aclose()
        raise
    finally:
        ctx.pool.release_slot(account.uid)
    return it, first


def apply_error_policy(ctx, account, kind: ErrKind, act, model: str):
    """把一条处置策略落到账号池（罚号 / 禁用 / (账号,模型) 冷却 / 连败计数）。

    与 `action_for` 分开的理由：策略是"该怎么反应"（纯函数、好测），这里是
    "落到哪个对象上"（副作用）。测试策略表不用造账号池。
    """
    uid = account.uid
    if act.disable:
        # 终态：session 失效 / 被上游封禁。用**自动禁用位**而不是手动停用位，
        # 否则用户点一下"启用"就会把它放回池子，下一个请求立刻再吃一次同样的错。
        reason = act.reason or "上游要求重新登录"
        ctx.pool.disable_auto(uid, reason)
        try:
            ctx.db.set_account_state(uid, auto_disabled_reason=reason)
        except Exception:  # noqa: BLE001  落库失败不该让请求本身失败
            logger.warning("自动禁用落库失败 uid=%s", uid[:8], exc_info=True)
        logger.warning("账号 %s 已自动禁用：%s", uid[:8], reason)
        return
    if act.model_scoped and model:
        ctx.pool.cooldown_model(uid, model, act.cooldown, neg_cache=act.neg_cache)
        # 落库：11102 的负缓存 TTL 是 6~24 小时，重启就丢等于每次重启都要重新
        # 去上游碰一次钉子。落库失败不影响本次请求（只是重启后会重新试探）。
        try:
            snap = ctx.pool.model_block_state(uid, model)
            if snap:
                ctx.db.save_model_block(uid, model, snap["until"], snap["streak"], act.reason)
        except Exception:  # noqa: BLE001
            logger.warning("模型冷却落库失败 uid=%s model=%s", uid[:8], model, exc_info=True)
        logger.info("账号 %s 的模型 %s 已冷却 %.0fs（%s）", uid[:8], model, act.cooldown, act.reason)
        return
    if act.cooldown > 0:
        ctx.pool.on_failure(uid, act.cooldown)
        return
    if kind is ErrKind.CLIENT:
        # 无权威分类的失败：单次不罚，连成串才降权（P1-2）
        ctx.pool.note_failures(uid, config.fail_streak_threshold, config.fail_degrade_seconds)


async def open_upstream(ctx, account, body: dict):
    """在返回 StreamingResponse 前，先建立上游连接并预取首个 SSE 行。

    这样上游在流真正开始前失败时，能返回正确的 HTTP 状态码（而非 200+SSE 错误），
    客户端可以正确识别错误而不是卡住等待。
    返回 (iterator, 首行, 实际使用的账号)——可换号的错误会自动换健康账号重试一次
    （仅一次，不递归），换号后调用方必须用返回的账号记账，而不是最初的账号。

    **换不换号由错误分类决定**（`errors.action_for`）：
      - 请求级错误（11115 超上下文 / 11135 图片无效 / 内容拦截）→ `fail_fast`：
        不换号、不罚号，原样抛给调用方透传。同一份 body 换任何账号结果都一样，
        换号只会白烧健康号的请求配额。
      - 账号级错误 → 按策略罚号（冷却/禁用/(账号,模型) 冷却）后换号重试一次。
    """
    # 脱敏在选号后、发上游前施加：只在此处做一次，避免换号重试时重复注入零宽字符
    body = apply_desensitize(body, account)
    model = str(body.get("model") or "").strip()
    try:
        it, first = await open_upstream_once(ctx, account, body)
        return it, first, account
    except UpstreamError as e:
        e.account = account
        e.policy_applied = True
        kind = classify(e.status_code, e.raw, e.headers)
        act = action_for(kind, e.raw, e.headers)
        logger.info("上游错误 %s（HTTP %s）→ %s，换号=%s 冷却=%.0fs",
                    kind.value, e.status_code, act.reason or "-", act.rotate, act.cooldown)
        await asyncio.to_thread(apply_error_policy, ctx, account, kind, act, model)
        if act.fail_fast:
            # 请求自身的问题：不轮转，把上游原文交给调用方透传
            raise
        if act.disable or not act.rotate:
            raise
        if ctx.pool.healthy_count() < 1:
            # 没有其它健康账号：放弃重试，按原错误交给调用方记录/返回
            #（后续请求会被 pick_account 的冷却兜底挡下并得到 503）
            raise
        try:
            alt = pick_account(ctx, regions=model_regions(ctx, body), model=model, exclude={account.uid})
        except HTTPException:
            # 没有可重试组合时保留最初的上游错误，不把它覆盖为选号失败。
            raise e
        try:
            it, first = await open_upstream_once(ctx, alt, body)
        except UpstreamError as e2:
            e2.account = alt
            e2.policy_applied = True
            kind2 = classify(e2.status_code, e2.raw, e2.headers)
            act2 = action_for(kind2, e2.raw, e2.headers)
            await asyncio.to_thread(apply_error_policy, ctx, alt, kind2, act2, model)
            raise
        except httpx.HTTPError as e2:
            ctx.pool.on_failure(alt.uid, COOLDOWN_SOFT)
            e2.account = alt
            e2.policy_applied = True
            raise
        return it, first, alt


def penalize(ctx, account, status_code: int, raw, headers=None, model: str = ""):
    """按错误分类表对账号施加处置，返回本次的 Action。

    给**路由层**用：`open_upstream` 内部已经施过处置，路由在它之外捕获到上游错误时
    （流中途断开、`collect_upstream` 直连）才调这个，避免同一个错误被罚两次。

    为什么必须由路由层走这里而不是自己算个冷却秒数：`log_usage` 的 `cooldown` 参数
    只能表达"冷却多久"，表达不了"这个错误该不该罚号"（11115/11135 罚号就是错的）
    和"要不要禁用"（12153/11140）。走分类表才能和主链路同一口径。
    """
    kind = classify(status_code, raw, headers)
    act = action_for(kind, raw, headers)
    apply_error_policy(ctx, account, kind, act, model)
    return act


def enhance_body(ctx, body: dict) -> dict:
    """统一规整，构造最终上游请求体。

    刻意不含反审核脱敏：脱敏必须按「最终选中的账号属于哪个区域」决定（见
    apply_desensitize），而本函数在选号之前调用，此时账号还未知。
    """
    from ..reasoning import parse_model_aliases, resolve_model_alias
    # 设置只读一次：别名解析与系统提示词模式都要用（get_settings 会打 DB）
    settings = ctx.db.get_settings()
    # 模型别名解析：客户端用熟名字（gpt-4o 等）也能路由到真实模型
    if body.get("model"):
        aliases = parse_model_aliases(settings.get("model_aliases") or "")
        body["model"] = resolve_model_alias(str(body["model"]), aliases)
    # 输出上限按模型实际能力裁剪：Claude Code 常发 32000+，超过部分模型上限会被
    # 上游拒绝（目录未知时不裁剪）。max_tokens 与 max_completion_tokens 都要管——
    # OpenAI 新客户端（Responses 系）发的是后者，只裁前者会漏掉。
    max_out = ctx.models.max_output_tokens(str(body.get("model") or ""))
    if max_out:
        for key in ("max_tokens", "max_completion_tokens"):
            value = body.get(key)
            # 只裁显式给的正整数；None/字符串等交给上游自己判
            if type(value) is int and value > max_out:
                logger.info("%s %s 超过模型 %s 上限，裁剪为 %s",
                            key, value, body.get("model"), max_out)
                body[key] = max_out
    # 动态思考强度表：来自模型目录（动态获取），无则交给内置静态表
    model_id = body.get("model")
    dyn_efforts = None
    if model_id:
        efforts = ctx.models.reasoning_efforts(model_id)
        if efforts:
            dyn_efforts = {model_id: efforts}
    # 系统提示词三模式（P2-1）：默认 passthrough = 零改动。custom/append 需要
    # 用户配了 prompt_text 才生效（空文本一律视为 passthrough，见 apply_prompt_mode）。
    body = sanitize_body(body, efforts=dyn_efforts,
                         prompt_mode=str(settings.get("prompt_mode") or ""),
                         prompt_text=str(settings.get("prompt_text") or ""))
    return body


def apply_desensitize(body: dict, account) -> dict:
    """按账号所属区域决定是否做反审核脱敏。

    脱敏（system/developer 里插零宽空格）是为绕国内内容审核而做的，国际版没有
    这层审核，注入零宽字符只会白白降低 system prompt 的保真度。所以只在国内版
    账号上施加；config.desensitize 关掉时两个区域都不做。

    混池场景下按「本次实际选中的账号」判定，同一份请求发给国内版账号会被脱敏、
    发给国际版账号则保持原文。
    """
    if not config.desensitize:
        return body
    if region_of_account(account).id == "global":
        return body
    return desensitize_body(body, roles=("system", "developer"))


def log_usage(ctx, protocol, model_name, account, t0, status, err="", usage=None,
              cooldown=COOLDOWN_NONE, input_content="", output_content="",
              reasoning_content="", app_name="", update_pool=True):
    usage = usage or {}
    # update_pool=False 用于"客户端断开补记"等非账号过错的场景：
    # 断开不是账号的失败，不应计入失败数/冷却（否则 Claude Code 常见的主动中断
    # 会把健康账号打成"冷却中"）
    if update_pool:
        if status in ("ok", "incomplete"):
            ctx.pool.on_success(account.uid)
            # 成功即证明该 (账号,模型) 可用：清掉可能存在的负缓存/模型冷却。
            # 不清的话，一条错误的 11102 判断会让这个组合被避让最长 24 小时。
            if model_name and ctx.pool.clear_model_cooldown(account.uid, model_name):
                try:
                    ctx.db.delete_model_block(account.uid, model_name)
                except Exception:  # noqa: BLE001
                    logger.warning("清除模型负缓存落库失败", exc_info=True)
            # 成本台账（P2-3）：用本次的 credits 与 token 数更新 (账号,模型) 单价。
            # credits=0 是**有效观测**（免费额度包），会被记成 tier0。
            if model_name and usage.get('credit') is not None:
                tokens = (usage.get("prompt_tokens") or usage.get("input_tokens") or 0) + \
                         (usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                snap = ctx.pool.record_cost(account.uid, model_name,
                                            usage.get("credit") or 0, tokens)
                if snap:
                    try:
                        ctx.db.save_model_cost(snap["uid"], snap["model"], snap["cost_per_1k"],
                                               snap["samples"], snap["updated_at"])
                    except Exception:  # noqa: BLE001
                        logger.warning("成本台账落库失败", exc_info=True)
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
