"""错误响应构造：上游错误包装（防泄原文）、SSE 错误事件、usage 归一化。

另含限流判定与重置时间解析（参考 Sliverkiss #28/#31）：
上游在非 429 状态码下也会返回限流语义（200 + code 11140 "rate-limiting"、
400 + "rate limit" 等），不识别则账号既不冷却也不换号，反复撞同一堵墙；
429/6004 的 msg 里带「将在 YYYY-MM-DD HH:MM:SS 重置」墙钟，可用于精确冷却。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

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
    """把各协议转换器内部的 usage 归一化为 input/output tokens。"""
    if not usage:
        return {}
    return {
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
    }
