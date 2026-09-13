"""错误响应构造：上游错误包装（防泄原文）、SSE 错误事件、usage 归一化。"""
from __future__ import annotations

import json


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
