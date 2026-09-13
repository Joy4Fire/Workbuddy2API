"""SSE 流处理：增量解析（记录用）、Chat 行清洗（透传用）、流式心跳（防断连）。"""
from __future__ import annotations

import asyncio
import json


def delta_parts(line: str) -> tuple[str, str]:
    """解析一行上游 SSE data，返回 (content 增量 + tool_calls 重组, reasoning_content 增量)。

    tool_calls 以 <tool_call:name args> 形式并入 content 侧：DSH/agent 类客户端
    的回复大多是工具调用，若不记录会出现「有 token 消耗但输出为空」的记录。
    """
    content, reasoning = "", ""
    if not (line.startswith("data:") and line[5:].strip() not in ("[DONE]", "")):
        return content, reasoning
    try:
        chunk = json.loads(line[5:].strip())
    except json.JSONDecodeError:
        return content, reasoning
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content += delta["content"]
        rc = delta.get("reasoning_content")
        if rc:
            reasoning += rc
        # 重组工具调用增量（OpenAI 流式格式：name 在首块、arguments 分片递增）
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name")
            if name:
                content += f"<tool_call:{name} "
            args = fn.get("arguments")
            if args:
                content += args
            if tc.get("id") and "arguments" not in fn:
                content += ">"
    # Anthropic 兼容层已单独处理；这里补充 message 的 tool_use（若上游混合返回）
    return content, reasoning


def sanitize_chat_sse(line: str) -> str:
    """清洗 OpenAI Chat 流式透传行：丢弃空 content/reasoning/refusal/tool_calls 等空 delta。

    WorkBuddy 上游每个 chunk 都带空 content/reasoning_content，直接透传给
    OpenAI 兼容客户端会渲染出空白思考块。返回清洗后的 SSE 行；若整行只剩空
    delta（应被丢弃），返回空字符串。
    """
    if not (line.startswith("data:") and line[5:].strip() not in ("[DONE]", "")):
        return line
    raw = line[5:].strip()
    try:
        chunk = json.loads(raw)
    except json.JSONDecodeError:
        return line
    if not isinstance(chunk, dict):
        return line
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return line
    kept_choices = []
    for choice in choices:
        if not isinstance(choice, dict):
            kept_choices.append(choice)
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            # 删除空/假值字段
            for k in ("content", "reasoning_content", "refusal"):
                if k in delta and not delta[k]:
                    delta.pop(k)
            if "function_call" in delta and not delta.get("function_call"):
                delta.pop("function_call")
            if "tool_calls" in delta and not delta.get("tool_calls"):
                delta.pop("tool_calls")
            if "reasoning" in delta and not delta.get("reasoning"):
                delta.pop("reasoning")
            if delta:
                choice["delta"] = delta
            else:
                # delta 全空：仅当无 finish_reason 时才丢弃，否则保留（结束标记）
                if choice.get("finish_reason"):
                    choice.pop("delta", None)
                else:
                    continue
        kept_choices.append(choice)
    if not kept_choices:
        return ""
    chunk["choices"] = kept_choices
    return "data: " + json.dumps(chunk, ensure_ascii=False)


async def with_keepalive(gen, interval: float = 15.0):
    """包一层 SSE 流：静默超过 interval 秒时插入 `: keepalive` 注释行。

    防止长思考模型（DeepSeek 等）输出间隙的静默被客户端/中间代理掐断。
    用 pump 任务 + 队列实现——不能对上游流迭代器本身做 wait_for 超时
    （超时取消会把上游连接读断掉），只能对队列读取做超时，取消队列读取
    不影响上游。客户端断开时 finally 取消 pump，连带触发内部 gen 的
    CancelledError 分支（断开补记逻辑照常工作）。
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def pump():
        try:
            async for chunk in gen:
                await queue.put(("chunk", chunk))
        except (asyncio.CancelledError, GeneratorExit):
            # pump 自身被取消（客户端断开触发 finally 的 task.cancel()）：
            # 内部 gen 的 CancelledError 分支已在之前的迭代点执行过补记，
            # 这里按"流结束"收场——不能把外部的 CancelledError 实例重新
            # raise 到消费方（Python 3.14 会把它当作对消费任务的取消请求）
            pass
        except BaseException as e:  # noqa: BLE001
            await queue.put(("error", e))
        await queue.put(("done", None))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            if kind == "chunk":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return
    finally:
        task.cancel()
