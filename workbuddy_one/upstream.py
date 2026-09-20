"""上游转发层：直连 WorkBuddy 的 /v2/chat/completions。

后端本身是标准 OpenAI Chat Completions 协议（含原生 tools/tool_calls/SSE 流式），
因此核心是：注入鉴权 header + 透传请求体 + 强制 stream=True。
非流式请求在本地聚合 SSE 成单个响应。

host 按账号区域选择（国内版 copilot.tencent.com / 国际版 www.workbuddy.ai）：
区域信息由 credentials 注入到 header 的 X-Domain 里，此处据此还原，
因此调用方无需感知区域，签名保持不变。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from . import net, region
from .config import config

logger = logging.getLogger("workbuddy_one.upstream")

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
# 代理与 trust_env 策略统一走 net.py（默认直连；需要代理时由 config.proxy 显式指定）。
# 每个 stream/请求各自独立使用，互不阻塞；单进程内并发安全。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = net.async_client(
            timeout=300,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


def build_upstream_body(payload: dict) -> dict:
    """从客户端请求体构造上游 body：白名单筛选 + 强制流式。"""
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def _domain_from_headers(headers) -> str:
    """从请求头还原账号域名（credentials 注入的 X-Domain），用于选上游 host。"""
    for key in ("X-Domain", "x-domain"):
        try:
            v = headers.get(key)
        except AttributeError:
            break
        if v:
            return str(v)
    return config.domain


def _chat_url(headers) -> str:
    """上游 chat 端点：按账号区域选 host，路径两区域一致。"""
    return f"{region.chat_base(_domain_from_headers(headers))}/v2/chat/completions"


async def stream_upstream(headers: dict, body: dict) -> AsyncIterator[str]:
    """透传上游 SSE 流，逐行 yield 原始 data 行（含 [DONE]）。"""
    url = _chat_url(headers)
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
    """消费上游 SSE，聚合成单个非流式 chat.completion 对象。

    流完整性（吸收 Sliverkiss 空流/截断修复 + Buddy2api 完成标记校验）：
    - 空流哨兵：整个流没有任何 content/reasoning/tool_calls 增量时抛
      UpstreamError(502)——上游偶发返回空流，聚合成 200+空消息会把故障
      伪装成"模型回答为空"，误导调用方重试策略与观测；
    - sawDone 截断检测：上游未发 `data: [DONE]` 就断连（EOF）视为截断，
      丢弃 arguments 非法 JSON 的残缺 tool_calls（客户端解析会卡死会话）；
    - 完成标记校验：既无 `[DONE]` 也无 `finish_reason` 时直接 502，**不伪造
      `stop`**——否则截断的半截回答会看起来像正常完成（详见下方注释）。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    saw_done = False

    async for line in stream_upstream(headers, body):
        data = line[5:].strip()
        if data == "[DONE]":
            saw_done = True
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

    # 截断收尾（EOF 未发 [DONE]）：丢弃 arguments 解析失败的残缺 tool_calls
    # （正常 [DONE] 收尾零影响；finish_reason=length 的主动截断同此处理）
    if not saw_done and tool_calls:
        for idx in list(tool_calls):
            args = tool_calls[idx]["function"]["arguments"]
            try:
                json.loads(args or "{}")
            except json.JSONDecodeError:
                del tool_calls[idx]
                logger.warning("上游流截断：丢弃 arguments 残缺的 tool_call idx=%d", idx)

    # 完成标记校验：非流式聚合必须拿到明确结果，宁可报错也不伪造 "stop"。
    # 实测上游正常完成时**一定**同时发 finish_reason 与 [DONE]，缺任何一项都属异常。
    # 此前这里用 `finish_reason or "stop"` 兜底，会把「被截断的半截回答」伪装成正常
    # 完成——客户端的重试/降级策略因此永不触发，观测上也看不出上游出过问题
    # （对齐参考实现 Buddy2api v2.1.11 的同类修复）。注意：明确的 finish_reason
    # 之后直接 EOF 仍然接受（只认「有没有完成标记」，不强制要求 [DONE]）。
    if not saw_done and finish_reason is None:
        logger.warning("上游流截断：既无 [DONE] 也无 finish_reason，拒绝伪造完成")
        raise UpstreamError(
            502,
            b'{"error":{"message":"upstream stream ended without [DONE] or a finish reason",'
            b'"type":"upstream_error"}}',
        )

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    # 空流哨兵：无任何内容增量（含截断后 tool_calls 全被丢弃的场景）
    if not content and not reasoning and not tool_calls and not usage:
        raise UpstreamError(502, b'{"error":{"message":"upstream returned an empty stream","type":"upstream_error"}}')

    if tool_calls:
        # 有 tool_calls 却没给 finish_reason：按语义补 "tool_calls"（比 "stop" 准确）
        finish_reason = finish_reason or "tool_calls"
    elif finish_reason is None:
        # 发了 [DONE] 却始终没有 finish_reason：同样是异常流，不猜
        logger.warning("上游流异常：[DONE] 收尾但无 finish_reason")
        raise UpstreamError(
            502,
            b'{"error":{"message":"upstream stream ended before a finish reason",'
            b'"type":"upstream_error"}}',
        )

    message: dict = {"role": "assistant", "content": content}
    if reasoning_parts:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    result = {
        "id": "chatcmpl-workbuddy",
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model or "auto",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        result["usage"] = usage
    return result


class UpstreamError(Exception):
    def __init__(self, status_code: int, raw: bytes):
        self.status_code = status_code
        self.raw = raw
        super().__init__(f"upstream HTTP {status_code}")
