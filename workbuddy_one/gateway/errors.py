"""错误响应构造：上游错误包装（防泄原文）、SSE 错误事件、usage 归一化。

另含两块：
1. 限流判定与重置时间解析（参考 Sliverkiss #28/#31）：
   上游在非 429 状态码下也会返回限流语义（200 + code 11140 "rate-limiting"、
   400 + "rate limit" 等），不识别则账号既不冷却也不换号，反复撞同一堵墙；
   429/6004 的 msg 里带「将在 YYYY-MM-DD HH:MM:SS 重置」墙钟，可用于精确冷却。
2. **错误分类表 + 处置策略**（`classify` / `action_for`，2026-09-21 吸收
   Sliverkiss 的 `Classify`）。此前只有「限流 / 其他」两档，会把**请求自身的问题**
   误判成**账号的问题**：prompt 超上下文（11115）会白轮一遍健康号并把好号打进冷却；
   WAF 403 落"其他"只换号不罚 → 连环 403。分类表把「罚不罚号 / 换不换号 / 罚多久」
   拆成三个正交决策，见 `action_for` 的策略表。
"""
from __future__ import annotations

import enum
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# 限流/节流关键词（小写比较 + 中文原文双通道）。宁缺毋滥：只收录明确指向
# "请求速率/模型用量被节流"的措辞。连字符形式需单列——子串匹配不跨 '-'。
# "frequency limit" 是 6004 英文文案（usage exceeds frequency limit）的形态。
_RATE_MARKERS = (
    "rate limit",       # rate limit / rate limits / rate limiting
    "rate-limiting",
    "rate-limited",
    "frequency limit",  # usage exceeds frequency limit（6004）
    "too many requests",
    "usage limit",
    "请求过于频繁",
    "使用频率",
    "限流",
)

# 限流重置墙钟：中文「将在 …重置」/英文 "reset at …"，时间固定 UTC+8
_RESET_RE = re.compile(
    r"(?:将在|reset at)\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.IGNORECASE)
_RESET_TZ = timezone(timedelta(hours=8))  # 上游文案固定按 UTC+8 表述


def is_rate_limit_body(raw: bytes | str) -> bool:
    """按响应体文案判定是否限流（与状态码无关，配合 cooldown_for_error 使用）。"""
    try:
        text = raw.decode("utf-8", "replace").lower() if isinstance(raw, (bytes, bytearray)) else str(raw).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(m in text for m in _RATE_MARKERS)


def parse_rate_reset(raw: bytes | str) -> float | None:
    """从限流响应体提取上游明示的重置墙钟（epoch 秒，按 UTC+8 解释）。

    没有时间文案返回 None——调用方退回有界冷却，绝不臆造时间。
    """
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:  # noqa: BLE001
        return None
    m = _RESET_RE.search(text)
    if not m:
        return None
    try:
        # 上游文案的时间固定是 UTC+8 墙钟，显式按该时区解释（与部署机本地时区无关）
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_RESET_TZ).timestamp()
    except ValueError:
        return None


def rate_limit_cooldown(raw: bytes | str, base: float = 300.0, cap: float = 7200.0) -> float:
    """限流场景的冷却时长：能解析出上游重置墙钟则对齐之（夹在 [60, cap]），
    否则用 base（默认 5 分钟）。

    上游明说"何时恢复"时按它冷却（不多罚也不少罚）；解析失败或文案无时间
    （如 11140 通用限流）退回固定 base。
    """
    reset = parse_rate_reset(raw)
    if reset is None:
        return base
    return max(60.0, min(reset - time.time(), cap))


# ===========================================================================
# 错误分类表 + 处置策略（2026-09-21 吸收 Sliverkiss `Classify`）
# ===========================================================================
#
# 为什么需要它：原先只有「限流 / 其他」两档，**把「请求自身的问题」误判成
# 「账号的问题」**，具体后果：
#   - prompt 超上下文（11115，400）→ 当账号故障 → 白轮一遍健康号 + 把好号打进冷却
#   - 图片无效（11135，400）→ 同上
#   - 积分耗尽（402 / 429+14018）→ 按普通失败处理，冷却远短于"次日 4 点签到才恢复"
#   - WAF 403（body 无业务信封）→ 落"其他"→ 只换号不罚 → 连环 403
#   - 模型不存在（11102）→ 换号，但下次还会选中同一个坏号（没有负缓存）
#
# 分类把三件事拆开：**罚不罚号**（cooldown）、**换不换号**（rotate）、
# **罚多久**（cooldown 的数值 + Retry-After 头族）。见 `action_for`。


class ErrKind(str, enum.Enum):
    """上游错误的语义分类。继承 str 便于日志/DB 直接落库。"""
    NONE = "none"                       # 成功 / 非错误
    HARD_CREDIT = "hard_credit"         # 积分耗尽（402 / 429+14018）→ 冷却到次日 04:00
    SOFT_RATE = "soft_rate"             # 限流（429 / 限流文案）→ 短冷却
    SESSION_DEAD = "session_dead"       # 登录态失效（401+12153）→ 禁用，需重新登录
    NOT_FOUND = "not_found"             # 404 偶发 → 短冷却
    SERVER = "server"                   # 上游 5xx
    CONTENT_BLOCKED = "content_blocked"  # 内容策略拦截 → 不罚号，透传
    BAD_PARAMS = "bad_params"           # 请求体畸形（11101）→ 不罚号，但仍轮转
    ACCOUNT_FAULT = "account_fault"     # 账号级授权/配额故障（11140 / 14017）
    MODEL_BLOCKED = "model_blocked"     # 11102 该后端无此模型 → (账号,模型) 负缓存避让
    WAF_BLOCK = "waf_block"             # 403 且无业务信封（WAF 拦截页）→ 软冷却 + 抖动
    PROMPT_TOO_LONG = "prompt_too_long"  # 11115 超上下文 → 不罚号不轮转，透传原文
    IMAGE_INVALID = "image_invalid"     # 11135 图片无效 → 不罚号不轮转，fail-fast
    CLIENT = "client"                   # 其他 4xx → 只换号不罚（喂连败计数）


# ---- 词表：按匹配通道分组（fold = 小写比较 + 原文比较双通道）----

# 余额不足（matchFold）。"quota exceeded" 语义跨计费/限流两界，只在**非 429** 上判
# （429 已由状态码层前置接管），否则会把限流误归硬冷却到次日 04:00，白扔一个号约 12h。
_HARD_CREDIT_MARKERS = (
    "insufficient credit", "no credit", "credit exhausted", "credits exhausted",
    "out of credit", "quota exceeded", "quota exhaust", "payment required",
    "credit not enough", "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
)

# 登录态失效（matchExact，大小写敏感）：必须与 12153 精确匹配，宽泛子串会误伤。
_SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")

# 账号级授权/配额故障（matchFold）。
# 注意 11140 **不能**按 code 判：该 code 也承载模型级限流文案
# （"The model provider is rate-limiting requests."），那种场景必须保持 SOFT_RATE
# （限流文案层先命中）。所以这里只收 msg 关键词 "request illegal"。
_ACCOUNT_FAULT_MARKERS = (
    "request illegal", "trial not activated", "trial version is not yet activated",
)

# 超上下文（matchFold）。只认业务码与窄短语；业务码经 `_code_marker` 判定
# （字面量 marker 覆盖不了 `"code": 11115` 这种带空白的形态），文案走窄短语。
# 429/5xx 上不判（限流/服务端语义优先）。
_PROMPT_TOO_LONG_MARKERS = ('"code":11115', '"code":"11115"', "prompt is too long")

# 内容策略拦截（matchLower，词表已小写）。合法流量被逐字指纹审核误杀，
# 非账号问题 → 不罚号、不轮转，透传。
_CONTENT_BLOCKED_MARKERS = (
    "blocked by security policy", "unapproved channel", "illegal api invocation",
)

# 请求体解析失败（matchExact）。换了账号照样 400，不罚号，但仍轮转
# （不同账号可能有不同的模型权限，值得再试一次）。业务码 11101 同样走
# `_code_marker` 以容忍 JSON 空白。
_BAD_PARAMS_MARKERS = ("Unmarshal chat params failed",)

# 图片格式/数据无效（matchFold）的**文案**形态；业务码 11135 走 _code_marker
# （字面量 marker 覆盖不了 `"code": 11135` 这种带空白的形态）。
_INVALID_IMAGE_MARKERS = (
    "invalid image_url content", "invalid_image_data", "replace the image",
)

# 11115 只在请求级 4xx 上判（与 11102 的 400/404 口径同理）。
_PROMPT_TOO_LONG_STATUSES = (400, 404, 413)

# 冷却时长常量（秒）。与 Sliverkiss 对齐：软冷却 60s、404 60s、WAF 抖动基 60s、
# 5xx 120s、软冷却封顶 2h；模型级负缓存 6h 起、封顶 24h。
SOFT_COOLDOWN = 60.0
NOT_FOUND_COOLDOWN = 60.0
WAF_COOLDOWN_BASE = 60.0
SERVER_COOLDOWN = 120.0
SOFT_RATE_MAX = 7200.0
MODEL_BLOCK_BASE_TTL = 6 * 3600.0
MODEL_BLOCK_MAX_TTL = 24 * 3600.0

# Retry-After 头族（P1-2）：上游明示等待时长的三个候选头，按权威度排序。
# 大小写不敏感（httpx.Headers 已归一；普通 dict 走 _header_get 兜底）。
_RETRY_AFTER_HEADERS = ("retry-after", "retry-after-ms", "x-ratelimit-reset")
# 解析结果上限：超过视为上游异常值丢弃（回落本地计算），与软冷却封顶同量级。
_RETRY_AFTER_SANITY = 7200.0


def _decode(raw: bytes | str | None) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _hit_fold(text: str, lower: str, patterns) -> bool:
    """大小写不敏感匹配：小写子串 或 原文子串（中文无大小写，原文通道即其入口）。"""
    return any(p.lower() in lower or p in text for p in patterns)


def _hit_lower(lower: str, patterns) -> bool:
    return any(p in lower for p in patterns)


def _json_root(text: str):
    try:
        root = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return root if isinstance(root, dict) else None


def _has_business_code(text: str, want: str) -> bool:
    """递归查找任意层级（含嵌套 error/data）里 code 字段等于 want 的节点。

    上游信封在顶层与嵌套对象之间摇摆，只看顶层会漏判（对齐 Sliverkiss
    hasBusinessCode 的遍历口径）。
    """
    root = _json_root(text)
    if root is None:
        return False

    def walk(v) -> bool:
        if isinstance(v, dict):
            c = v.get("code")
            if c is not None and str(c).strip() == want:
                return True
            return any(walk(x) for x in v.values())
        if isinstance(v, list):
            return any(walk(x) for x in v)
        return False

    return walk(root)


_CODE_MARKER_RE: dict[str, re.Pattern] = {}


def _code_marker(lower: str, code: str) -> bool:
    """在（已小写的）原文里找 `"code": <code>` 形态，容忍 JSON 空白与字符串形态。"""
    pat = _CODE_MARKER_RE.get(code)
    if pat is None:
        pat = _CODE_MARKER_RE[code] = re.compile(r'"code"\s*:\s*"?' + re.escape(code) + r'"?')
    return bool(pat.search(lower))


def is_model_blocked(status: int, text: str, lower: str | None = None) -> bool:
    """11102「该后端无此模型」的确定性答复。

    只比对 code/msg **独立字段**，绝不做整段文本子串匹配——错误体还带 requestId
    等字段，拿整段文本匹配会把 "11102" 撞在 ID 上、误避让一个本来能用的模型。
    只认 400/404：429 带 11102 属限流语义（走 SOFT_RATE）。
    """
    if status not in (400, 404) or not text:
        return False
    lower = text.lower() if lower is None else lower
    # 轻量预检：既无 code 又无文案时直接返回（大多数 4xx 零解析开销）
    if "11102" not in text and "service info not found" not in lower:
        return False
    root = _json_root(text)
    if root is None:
        return False
    nodes = [root]
    inner = root.get("error")
    if isinstance(inner, dict):
        nodes.append(inner)
    code = ""
    msg = ""
    for node in nodes:
        for k in ("code", "errCode", "error_code"):
            v = node.get(k)
            if v is not None and str(v).strip() and not code:
                code = str(v).strip()
        for k in ("msg", "message"):
            v = node.get(k)
            if isinstance(v, str) and v.strip() and not msg:
                msg = v.strip()
    if code == "11102":
        return True
    return "service info not found" in msg.lower()


def has_business_envelope(text: str) -> bool:
    """错误体是否携带上游业务信封（含 `"code":` 或 `"msg":` 字段）。

    刻意不做 JSON 解析：信封存在性只需字段名命中——畸形 JSON 但含 `"msg":` 字样
    仍按业务响应保守处理（宁漏判 WAF 也不误罚业务 403，后者有各自的权威分类）。
    """
    return '"code":' in text or '"msg":' in text


def is_waf_blocked(status: int, text: str) -> bool:
    """403 且无业务信封（HTML 拦截页 / 空体 / 纯文本）= APISIX WAF 拦截形态。

    带信封的 403（11140 request illegal / 11128 等）走既有分类链，不受影响。
    """
    return status == 403 and not has_business_envelope(text)


def classify(status: int, raw: bytes | str | None, headers=None) -> ErrKind:
    """把上游响应分类为 ErrKind。**判定顺序即语义优先级**，不要随手调换。

    顺序理由（每条都有实测依据，改前先读）：
      1. 11102 最先——「模型在后端不存在」是最具体的确定性答复，语义比计费/限流都强；
         不先判会落 4xx 兜底（只换号不避让），坏号留在池内反复被选中。
      2. 402——真正的计费余额耗尽状态码，最严、最不可自愈。
      3. sessionDead / accountFault 先于 `status == 429`——账号级终态等不来自愈，
         限流状态码不得掩盖它们（429+14017 必须 accountFault；401+12153 混排
         "rate limit" 必须 sessionDead）。
      4. `429 + 14018`——结构化的「账号积分耗尽」业务码，必须先于通用 429 兜底，
         否则会被误判为可自愈的软限流。
      5. `status == 429` **先于余额关键词**——429 的 body 高频携带
         `quota exceeded` / `额度不足` 这类**跨计费与限流两界**的措辞；关键词先判会把
         限流误归硬冷却到次日 04:00，**白扔一个号约 12 小时**。状态码比关键词权威。
         非 429 的 quota 措辞仍走下面的余额层（历史语义不变）。
      6. 余额关键词——非 429 响应（200 业务信封 / 403 信封等）携带计费措辞。
      7. 限流文案——非 429 状态码携带限流文案（200+11140、400+"rate limit" 等）。
      8. 11115 **先于 404/5xx**——404 上打的 11115 若落 NOT_FOUND 会去冷却一个
         无辜的账号（上下文超限与账号无关）。
      9. 404 / 5xx——常规分类。
     10. WAF 403 **先于 4xx 兜底**——WAF 空体/HTML 永远不会命中内容/参数层的关键词，
         落 CLIENT 的代价是「只换号不罚」，正是连环 403 的根因。
     11. 图片无效（11135）——确定性的请求级错误，fail-fast。
     12. 内容策略 / 参数错误 / 其他 4xx——通用兜底。
    """
    text = _decode(raw)
    lower = text.lower()

    if is_model_blocked(status, text, lower):
        return ErrKind.MODEL_BLOCKED
    if status == 402:
        return ErrKind.HARD_CREDIT
    if _hit_fold(text, lower, _SESSION_DEAD_MARKERS):
        return ErrKind.SESSION_DEAD
    if _hit_fold(text, lower, _ACCOUNT_FAULT_MARKERS):
        return ErrKind.ACCOUNT_FAULT
    if status == 429 and _has_business_code(text, "14018"):
        return ErrKind.HARD_CREDIT
    if status == 429:
        return ErrKind.SOFT_RATE
    if _hit_fold(text, lower, _HARD_CREDIT_MARKERS):
        return ErrKind.HARD_CREDIT
    if _hit_fold(text, lower, _RATE_MARKERS):
        return ErrKind.SOFT_RATE
    if status in _PROMPT_TOO_LONG_STATUSES and (
            _hit_fold(text, lower, _PROMPT_TOO_LONG_MARKERS)
            or _code_marker(lower, "11115")):
        return ErrKind.PROMPT_TOO_LONG
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if is_waf_blocked(status, text):
        return ErrKind.WAF_BLOCK
    if status == 400 and (_hit_fold(text, lower, _INVALID_IMAGE_MARKERS)
                          or _code_marker(lower, "11135")):
        return ErrKind.IMAGE_INVALID
    if status >= 400:
        if _hit_lower(lower, _CONTENT_BLOCKED_MARKERS):
            return ErrKind.CONTENT_BLOCKED
        if _hit_fold(text, lower, _BAD_PARAMS_MARKERS) or _code_marker(lower, "11101"):
            return ErrKind.BAD_PARAMS
        return ErrKind.CLIENT
    return ErrKind.NONE


def _header_get(headers, name: str) -> str | None:
    """大小写不敏感地取响应头。httpx.Headers.get 已归一，普通 dict 走兜底遍历。"""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        try:
            v = getter(name)
        except Exception:  # noqa: BLE001
            v = None
        if v is not None:
            return v
    try:
        items = headers.items()
    except AttributeError:
        return None
    for k, v in items:
        if str(k).lower() == name:
            return v
    return None


def parse_retry_after(headers, now: float | None = None) -> float | None:
    """从响应头族解析上游明示的等待秒数（P1-2）。

    依次尝试 `Retry-After`（秒）→ `Retry-After-Ms`（毫秒）→
    `X-Ratelimit-Reset`（epoch 秒或毫秒，取 now+ 剩余量）。任一头缺失/非纯数字
    （如 HTTP-Date 形态）/非正/超 2h 上限则尝试下一头；全部不可用返回 None
    ——调用方回落本地计算，**绝不臆造等待时长**。
    """
    now = time.time() if now is None else now
    for name in _RETRY_AFTER_HEADERS:
        raw = _header_get(headers, name)
        if raw is None:
            continue
        v = str(raw).strip()
        if not v or not v.isdigit():
            continue  # 非纯数字（HTTP-Date 等）不解析，宁缺毋滥
        n = float(v)
        if name == "retry-after-ms":
            n /= 1000.0
        elif name == "x-ratelimit-reset":
            # 毫秒级 epoch 约 1.7e12，秒级约 1.7e9；1e11 是安全的判别阈值
            if n > 1e11:
                n /= 1000.0
            n -= now
        if n <= 0 or n > _RETRY_AFTER_SANITY:
            continue
        return n
    return None


def next_day_4am(now: float | None = None) -> float:
    """now 之后最近的一个 04:00（本地时区）的 epoch 秒。

    04:00 是上游签到任务恢复积分的时间点（签到时间默认 9/21 点，额度在 04:00
    结算）。余额耗尽的账号冷却到这里，而不是随便给个固定值。
    """
    n = datetime.fromtimestamp(time.time() if now is None else now)
    target = n.replace(hour=4, minute=0, second=0, microsecond=0)
    if n.hour >= 4:
        target += timedelta(days=1)
    return target.timestamp()


def _soft_rate_cooldown(raw, headers, now: float) -> float:
    """限流冷却：body 重置墙钟 > Retry-After 头 > 有界退避（固定软冷却）。

    优先级有依据：body 里的「将在 … 重置」是上游更权威的口径；Retry-After 头只在
    无重置文案时兜底（与官方 CLI 同序）。
    """
    reset = parse_rate_reset(raw)
    if reset is not None:
        return max(60.0, min(reset - now, SOFT_RATE_MAX))
    ra = parse_retry_after(headers, now)
    if ra is not None:
        return max(1.0, min(ra, SOFT_RATE_MAX))
    return SOFT_COOLDOWN


def _waf_cooldown(headers, now: float) -> float:
    """WAF 403 冷却：有 Retry-After 头按头，否则基 60s + ±25% 抖动。

    抖动是为了让多个账号/多个请求的退避不落在同一时刻，避免退避到期后齐刷刷
    再撞同一堵 WAF（惊群）。**不 Disable**——WAF 拦截是临时风控，禁用账号会
    让用户误以为账号坏了。
    """
    ra = parse_retry_after(headers, now)
    if ra is not None:
        return max(1.0, min(ra, SOFT_RATE_MAX))
    return random.uniform(WAF_COOLDOWN_BASE * 0.75, WAF_COOLDOWN_BASE * 1.25)


class Action(NamedTuple):
    """一条处置策略。

    rotate       换不换号（请求级错误不换号：换了也一样错）
    cooldown     账号冷却秒数（0 = 不罚；model_scoped 时表示该模型的冷却 TTL）
    reason       冷却/禁用原因（WebUI 展示"为什么不可用"）
    disable      是否把账号彻底摘出去（终态：需人工重新登录）
    fail_fast    不轮转、把上游原文透传给客户端
    model_scoped 冷却只作用于该 (账号, 模型)，切模型即可用
    neg_cache    写 (账号, 模型) 负缓存（指数 TTL）
    """
    rotate: bool = True
    cooldown: float = 0.0
    reason: str = ""
    disable: bool = False
    fail_fast: bool = False
    model_scoped: bool = False
    neg_cache: bool = False


def action_for(kind: ErrKind, raw: bytes | str | None = b"", headers=None,
               now: float | None = None) -> Action:
    """把 ErrKind 映射为处置策略。

    与分类分开的理由：分类是**协议知识**（上游说了什么），策略是**运营决策**
    （我们怎么反应）。分开后调策略不用碰分类表，测试也能各自独立断言。

    三个正交维度：
      - rotate    换不换号（请求级错误不换号：换了也一样错）
      - cooldown  罚不罚号 / 罚多久（账号级错误才罚）
      - disable   要不要把账号彻底摘出去（终态：需人工重新登录）
    """
    now = time.time() if now is None else now
    if kind is ErrKind.HARD_CREDIT:
        # 402 / 429+14018：冷却到次日 04:00（签到结算），冷却中不参与选号
        return Action(True, max(60.0, next_day_4am(now) - now), "余额不足")
    if kind is ErrKind.SOFT_RATE:
        return Action(True, _soft_rate_cooldown(raw, headers, now), "429 rate limit")
    if kind is ErrKind.WAF_BLOCK:
        return Action(True, _waf_cooldown(headers, now), "waf 403 block")
    if kind is ErrKind.SESSION_DEAD:
        # 12153：session 失效，短冷却救不活，必须人工重新扫码登录
        return Action(True, 0.0, "登录态失效（12153），请重新扫码登录", disable=True)
    if kind is ErrKind.NOT_FOUND:
        # 固定 60s，不随限流退避升级：偶发路径缺失不是限流信号
        return Action(True, NOT_FOUND_COOLDOWN, "upstream 404")
    if kind is ErrKind.ACCOUNT_FAULT:
        if "request illegal" in _decode(raw).lower():
            # 11140 账号级授权封禁：软冷却到期也不会自愈，必须重新登录 → 禁用
            return Action(True, 0.0, "账号被上游封禁（11140 request illegal），请重新扫码登录",
                          disable=True)
        # 14017 试用未激活：补完 register 后可能自愈 → 软冷却（禁用会让用户补完也用不了）
        return Action(True, SOFT_COOLDOWN, "账号级故障（试用未激活）")
    if kind is ErrKind.SERVER:
        return Action(True, SERVER_COOLDOWN, "上游 5xx")
    if kind in (ErrKind.CONTENT_BLOCKED, ErrKind.PROMPT_TOO_LONG, ErrKind.IMAGE_INVALID):
        # 请求自身的问题：同一 body 换任何账号结果都一样 → 不罚号、不轮转，透传原文
        return Action(False, 0.0, "", fail_fast=True)
    if kind is ErrKind.MODEL_BLOCKED:
        # (账号, 模型) 负缓存：只避让这个组合，切模型/切账号即可用
        return Action(True, MODEL_BLOCK_BASE_TTL, "模型在该账号不可用（11102）",
                      model_scoped=True, neg_cache=True)
    if kind is ErrKind.BAD_PARAMS:
        # 发给上游的 body 有问题：不罚号，但**仍然轮转**（不同账号模型权限可能不同）
        return Action(True, 0.0, "请求体畸形（不罚号）")
    # CLIENT / NONE：只换号不罚（防雪崩）。CLIENT 由调用方喂连败计数（见 P1-2）
    return Action(True, 0.0, "")


def safe_err(raw: bytes, status: int) -> dict:
    """把上游错误响应体包装成安全的 detail（JSON 可解析则透传 error 结构，否则包一层）。"""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("error"):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"error": {"message": raw.decode("utf-8", "replace"), "type": "upstream_error"}}


def json_error(status: int, message: str) -> str:
    return json.dumps({"error": {"message": message, "type": "upstream_error"}})


def err_anthropic(status: int, message: str) -> str:
    # Anthropic SSE 规范要求错误事件带 `event: error` 头，只发裸 data 行时
    # Claude Code 等客户端可能不识别而一直挂起等待
    return ("event: error\ndata: " +
            json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}) + "\n\n")


def conv_usage(usage: dict | None) -> dict:
    """统一 token 名称并保留积分；丢 credit 会把付费调用错误记为免费。"""
    from ..adapters.usage import normalize_chat_usage
    return normalize_chat_usage(usage)
