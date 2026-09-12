"""上游转发层：直连腾讯 copilot.tencent.com/v2/chat/completions。

后端本身是标准 OpenAI Chat Completions 协议（含原生 tools/tool_calls/SSE 流式），
因此核心是：注入鉴权 header + 透传请求体 + 强制 stream=True。
非流式请求在本地聚合 SSE 成单个响应。
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from .config import config

# 透传 body 白名单字段
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
    "thinking",  # DeepSeek 思维链开关（reasoning.inject_thinking 注入/客户端显式传入）
}

# 模块级共享客户端：复用 TCP/TLS 连接，避免每次请求都重建连接造成握手开销。
# trust_env=False: 避免读取 HTTP_PROXY 等环境变量导致 httpx 解析出无效代理。
# 每个 stream/请求各自独立使用，互不阻塞；单进程内并发安全。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=300, trust_env=False, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _client


def build_upstream_body(payload: dict) -> dict:
    """从客户端请求体构造上游 body：白名单筛选 + 强制流式。"""
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


async def stream_upstream(headers: dict, body: dict) -> AsyncIterator[str]:
    """透传上游 SSE 流，逐行 yield 原始 data 行（含 [DONE]）。"""
    url = f"{config.backend}/v2/chat/completions"
    client = _get_client()
    async with client.stream("POST", url, headers=headers, json=body) as resp:
        if resp.status_code != 200:
            raw = await resp.aread()
            raise UpstreamError(resp.status_code, raw)
        async for line in resp.aiter_lines():
            line = line.strip()
            if line.startswith("data:"):
                yield line


async def collect_upstream(headers: dict, body: dict) -> dict:
    """消费上游 SSE，聚合成单个非流式 chat.completion 对象。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in stream_upstream(headers, body):
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"index": idx, "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    slot["function"]["name"] = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    slot["function"]["arguments"] += tc["function"]["arguments"]

    content = "".join(content_parts)
    message: dict = {"role": "assistant", "content": content}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    result = {
        "id": "chatcmpl-workbuddy",
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model or "auto",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
    }
    if usage:
        result["usage"] = usage
    return result


class UpstreamError(Exception):
    def __init__(self, status_code: int, raw: bytes):
        self.status_code = status_code
        self.raw = raw
        super().__init__(f"upstream HTTP {status_code}")
