"""推理端点：三协议（Chat / Responses / Anthropic Messages）+ count_tokens + /v1/models。

三协议统一流程：协议适配器转 OpenAI Chat → enhance_body 规整（别名/裁剪/思考/脱敏）
→ 预取上游首行（失败返回正确 HTTP 状态码，可换号重试）→ 流式转换回原协议或非流式聚合。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import config
from ..adapters.anthropic import anthropic_request_to_chat, AnthropicStreamConverter
from ..adapters.responses import responses_request_to_chat, ResponsesStreamConverter
from ..gateway import inference
from ..gateway.attachments import extract_input_text
from ..gateway.errors import conv_usage, err_anthropic, json_error, safe_err
from ..gateway.sse import delta_parts, sanitize_chat_sse, with_keepalive
from ..reasoning import estimate_tokens, parse_model_aliases
from ..upstream import build_upstream_body, collect_upstream, UpstreamError


def _usage_logger(ctx, app_name):
    # 每请求最多启动一次落库。取消 to_thread 不会停止线程，不能再补写 aborted。
    started = False

    async def write(*args, **kwargs):
        nonlocal started
        if started:
            return
        started = True
        await asyncio.to_thread(inference.log_usage, ctx, *args, app_name=app_name, **kwargs)
    return write


def register(app: FastAPI, ctx) -> None:
    db, models = ctx.db, ctx.models

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(default=None),
                    x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        inference.check_api_key(ctx, authorization, x_api_key)
        data = models.list()
        # 别名条目：复用真实模型的元数据（模态/上下文等），id 换成别名——
        # 客户端模型下拉里直接出现熟名字（gpt-4o 等），选中即路由到真实模型
        aliases = parse_model_aliases(db.get_settings().get("model_aliases") or "")
        if aliases:
            seen = {m["id"] for m in data}
            base_by_id = {m["id"]: m for m in data}
            for alias, real in aliases.items():
                if alias in seen or real not in base_by_id:
                    continue
                entry = dict(base_by_id[real])
                entry["id"] = alias
                entry["name"] = f"{alias}（{real} 别名）"
                data.append(entry)
                seen.add(alias)
        return {"object": "list", "data": data}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request,
                           authorization: str | None = Header(default=None),
                           x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        """Anthropic 兼容：估算输入 token 数（Claude Code / SDK 会调用）。

        返回 {input_tokens: int}。本地加权估算（CJK ~1.5 字符/token，其它 /4），
        刻意不调用上游——这是发正式请求前的预检，打上游又慢又耗配额；
        实际 token 数以模型返回的 usage 为准。
        """
        await asyncio.to_thread(inference.check_api_key, ctx, authorization, x_api_key)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
        texts: list[str] = []
        n_msg = 0
        for m in body.get("messages") or []:
            n_msg += 1
            c = m.get("content")
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        texts.append(p.get("text", "") or "")
        system = body.get("system")
        if isinstance(system, str):
            texts.append(system)
        elif isinstance(system, list):
            for p in system:
                if isinstance(p, dict):
                    texts.append(p.get("text", "") or "")
        return {"input_tokens": estimate_tokens(" ".join(t for t in texts if t), n_msg)}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               authorization: str | None = Header(default=None),
                               x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        app_name = await asyncio.to_thread(inference.check_api_key, ctx, authorization, x_api_key)
        log_usage = _usage_logger(ctx, app_name)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        if not payload.get("messages"):
            raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

        client_wants_stream = bool(payload.get("stream"))
        body = await asyncio.to_thread(inference.enhance_body, ctx, build_upstream_body(payload))
        # 选号（含会话粘性）：会话键从原始 payload 提取（白名单会剥掉
        # prompt_cache_key/metadata 等粘性键来源）
        account, session_key = inference.acquire_account(ctx, body, raw=payload)
        # 记账用解析后的真实模型名（别名请求按真实模型归因，避免按模型统计被打碎）
        model_name = body.get("model", "auto")
        input_text = await extract_input_text(body)
        t0 = time.time()

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
        # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
        try:
            it, first, account = await inference.open_upstream(ctx, account, body)
            # 换号重试后把会话重绑到实际服务的账号（刷新 TTL）
            if session_key:
                ctx.session_router.bind(session_key, account.uid)
        except UpstreamError as e:
            account = getattr(e, "account", account)
            # open_upstream 内部已按错误分类表罚过号（含禁用/模型级冷却），
            # 这里只记账，**不能**再 update_pool——重复施加会把已经设好的
            # 冷却用 cooldown_for_error 的返回值覆盖掉（对 CLIENT 类是 0，
            # 等于把刚设的冷却清掉）。
            await log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                      update_pool=False)
            raise HTTPException(status_code=e.status_code, detail=safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            account = getattr(e, "account", account)
            await log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=inference.COOLDOWN_SOFT, update_pool=not getattr(e, "policy_applied", False))
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        if client_wants_stream:
            async def gen():
                _usage = None
                _out_parts, _reason_parts, terminal = [], [], []
                finish_reason = None
                completed = False
                async def lines():
                    yield first
                    async for line in it:
                        yield line
                try:
                    async for line in lines():
                        if line.startswith("data:") and line[5:].strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line[5:].strip()) if line.startswith("data:") else {}
                        except json.JSONDecodeError:
                            chunk = {}
                        if chunk.get("usage"):
                            _usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                        _c, _r = delta_parts(line)
                        if _c: _out_parts.append(_c)
                        if _r: _reason_parts.append(_r)
                        clean = sanitize_chat_sse(line)
                        if clean:
                            # 完成块和后续 usage 一起暂存，确保终态发出前记账。
                            if finish_reason is not None:
                                terminal.append(clean + "\n\n")
                            else:
                                yield clean + "\n\n"
                    if finish_reason is None:
                        raise UpstreamError(502, b'{"error":{"message":"upstream stream ended without a finish reason","type":"upstream_error"}}')
                    status = "incomplete" if finish_reason in ("length", "content_filter") else "ok"
                    await log_usage("chat", model_name, account, t0, status, usage=_usage,
                                    input_content=input_text, output_content="".join(_out_parts),
                                    reasoning_content="".join(_reason_parts))
                    completed = True
                except (asyncio.CancelledError, GeneratorExit):
                    await log_usage("chat", model_name, account, t0, "aborted", "client disconnected",
                                    usage=_usage, input_content=input_text,
                                    output_content="".join(_out_parts), reasoning_content="".join(_reason_parts),
                                    update_pool=False)
                    raise
                except UpstreamError as e:
                    act = await asyncio.to_thread(inference.penalize, ctx, account, e.status_code, e.raw, e.headers, model_name)
                    await log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}",
                                    usage=_usage, input_content=input_text, output_content="".join(_out_parts),
                                    reasoning_content="".join(_reason_parts), update_pool=False)
                    yield f'data: {json_error(e.status_code, e.raw.decode("utf-8", "replace"))}\n\n'.encode()
                    yield b"data: [DONE]\n\n"
                except Exception as e:
                    await log_usage("chat", model_name, account, t0, "error", str(e), usage=_usage,
                                    input_content=input_text, output_content="".join(_out_parts),
                                    reasoning_content="".join(_reason_parts), cooldown=inference.COOLDOWN_SOFT)
                    yield f'data: {json_error(502, str(e))}\n\n'.encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    await it.aclose()
                if completed:
                    for chunk in terminal:
                        yield chunk
                    yield b"data: [DONE]\n\n"
            return StreamingResponse(with_keepalive(gen(), 15.0), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        try:
            async def prefetched():
                yield first
                async for line in it:
                    yield line
            try:
                collected = await collect_upstream({}, body, lines=prefetched())
            finally:
                await it.aclose()
            _msg = (collected.get("choices") or [{}])[0].get("message", {})
            # 工具调用摘要并入输出记录（非流式 agent 回复常只有 tool_calls）
            _tc_summary = "".join(
                f"<tool_call:{(tc.get('function') or {}).get('name', '?')} {(tc.get('function') or {}).get('arguments', '')}>"
                for tc in _msg.get("tool_calls") or [] if isinstance(tc, dict))
            status = "incomplete" if (collected.get("choices") or [{}])[0].get("finish_reason") in ("length", "content_filter") else "ok"
            await log_usage("chat", model_name, account, t0, status, usage=collected.get("usage"),
                       input_content=input_text,
                       output_content=(_msg.get("content") or "") + _tc_summary,
                       reasoning_content=_msg.get("reasoning_content") or "")
            return JSONResponse(content=collected)
        except UpstreamError as e:
            account = getattr(e, "account", account)
            # 首字节后的聚合错误不在 open_upstream 的处置范围内，这里补上
            act = await asyncio.to_thread(inference.penalize, ctx, account, e.status_code, e.raw, e.headers, model_name)
            await log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}",
                      input_content=input_text, cooldown=act.cooldown, update_pool=False)
            raise HTTPException(status_code=e.status_code, detail=safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            account = getattr(e, "account", account)
            await log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=inference.COOLDOWN_SOFT, update_pool=not getattr(e, "policy_applied", False))
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    @app.post("/v1/messages")
    async def messages(request: Request,
                       authorization: str | None = Header(default=None),
                       x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
                       anthropic_version: str | None = Header(default=None, alias="anthropic-version")):
        app_name = await asyncio.to_thread(inference.check_api_key, ctx, authorization, x_api_key)
        log_usage = _usage_logger(ctx, app_name)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        chat_body = await asyncio.to_thread(inference.enhance_body, ctx, anthropic_request_to_chat(payload))
        model_name = chat_body.get("model", "auto")
        input_text = await extract_input_text(chat_body)
        account, session_key = inference.acquire_account(ctx, chat_body, raw=payload)
        t0 = time.time()

        # Anthropic 默认流式；客户端可用 stream=false 请求非流式
        converter = AnthropicStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码（流式与非流式一致）
        # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
        try:
            it, first, account = await inference.open_upstream(ctx, account, chat_body)
            # 换号重试后把会话重绑到实际服务的账号（刷新 TTL）
            if session_key:
                ctx.session_router.bind(session_key, account.uid)
        except UpstreamError as e:
            account = getattr(e, "account", account)
            # open_upstream 已罚过号 → 只记账（同 chat 流式路径的注释）
            await log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}",
                      input_content=input_text, update_pool=False)
            raise HTTPException(status_code=e.status_code, detail=safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            account = getattr(e, "account", account)
            await log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=inference.COOLDOWN_SOFT, update_pool=not getattr(e, "policy_applied", False))
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        # 闭包错误标志：gen 捕获 UpstreamError 后只 yield 错误事件不抛出（流式路径需要），
        # 非流式路径靠它区分"正常结束"与"中途出错"，避免把半截内容当 200 成功返回
        errored = {"flag": False, "status": 0, "msg": ""}

        async def gen():
            _reason_parts: list[str] = []
            try:
                evt = converter.feed_line(first)
                _c, _r = delta_parts(first)
                if _r: _reason_parts.append(_r)
                if evt:
                    yield evt.encode()
                async for line in it:
                    evt = converter.feed_line(line)
                    _c, _r = delta_parts(line)
                    if _r: _reason_parts.append(_r)
                    if evt:
                        yield evt.encode()
                terminal = converter.finish().encode()
                status = "incomplete" if converter._finish_reason in ("length", "content_filter") else "ok"
                await log_usage("anthropic", model_name, account, t0, status, usage=conv_usage(converter._usage),
                           input_content=input_text,
                           output_content=(getattr(converter, "_text_content", "") or "") + converter.tools_summary(),
                           reasoning_content="".join(_reason_parts))
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端中途断开：补记已产生的输出（CancelledError 不走 except Exception）；
                # 断开非账号过错，不计入失败/冷却
                await log_usage("anthropic", model_name, account, t0, "aborted", "client disconnected",
                          usage=conv_usage(converter._usage),
                          input_content=input_text,
                          output_content=(getattr(converter, "_text_content", "") or "") + converter.tools_summary(),
                          reasoning_content="".join(_reason_parts), update_pool=False)
                raise
            except UpstreamError as e:
                errored["flag"] = True
                errored["status"] = e.status_code
                errored["msg"] = str(e.raw.decode("utf-8", "replace"))
                # 流中途断开：不在 open_upstream 处置范围内 → 走分类表补罚
                act = await asyncio.to_thread(inference.penalize, ctx, account, e.status_code, e.raw, e.headers, model_name)
                await log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}",
                          input_content=input_text, cooldown=act.cooldown, update_pool=False)
                yield err_anthropic(e.status_code, errored["msg"]).encode()
            except Exception as e:  # noqa: BLE001
                errored["flag"] = True
                errored["status"] = 502
                errored["msg"] = str(e)
                await log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text,
                          cooldown=inference.COOLDOWN_SOFT)
                yield err_anthropic(502, str(e)).encode()

            finally:
                await it.aclose()

            if not errored["flag"]:
                yield terminal

        if client_wants_stream:
            return StreamingResponse(with_keepalive(gen(), 15.0), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 非流式：消费流并聚合（gen 内的 _log_usage 会记录，这里不再重复）；
        # 中途出错时返回上游真实状态码而非 200+半截内容
        async for _ in gen():
            pass
        if errored["flag"]:
            raise HTTPException(status_code=errored["status"] or 502,
                                detail={"error": {"message": errored["msg"], "type": "upstream_error"}})
        return JSONResponse(content=converter.get_nonstream_response())

    @app.post("/v1/responses")
    async def responses(request: Request,
                        authorization: str | None = Header(default=None),
                        x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        app_name = await asyncio.to_thread(inference.check_api_key, ctx, authorization, x_api_key)
        log_usage = _usage_logger(ctx, app_name)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        chat_body = await asyncio.to_thread(inference.enhance_body, ctx, responses_request_to_chat(payload))
        model_name = chat_body.get("model", "auto")
        input_text = await extract_input_text(chat_body)
        account, session_key = inference.acquire_account(ctx, chat_body, raw=payload)
        t0 = time.time()

        converter = ResponsesStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
        # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
        try:
            it, first, account = await inference.open_upstream(ctx, account, chat_body)
            # 换号重试后把会话重绑到实际服务的账号（刷新 TTL）
            if session_key:
                ctx.session_router.bind(session_key, account.uid)
        except UpstreamError as e:
            account = getattr(e, "account", account)
            # open_upstream 已罚过号 → 只记账（同 chat 流式路径的注释）
            await log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}",
                      input_content=input_text, update_pool=False)
            raise HTTPException(status_code=e.status_code, detail=safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            account = getattr(e, "account", account)
            await log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=inference.COOLDOWN_SOFT, update_pool=not getattr(e, "policy_applied", False))
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        # 闭包错误标志：用途同 /v1/messages——非流式路径区分正常结束与中途出错
        errored = {"flag": False, "status": 0, "msg": ""}

        async def gen():
            _reason_parts: list[str] = []
            try:
                evt = converter.feed_line(first)
                _c, _r = delta_parts(first)
                if _r: _reason_parts.append(_r)
                if evt:
                    yield evt.encode()
                async for line in it:
                    evt = converter.feed_line(line)
                    _c, _r = delta_parts(line)
                    if _r: _reason_parts.append(_r)
                    if evt:
                        yield evt.encode()
                terminal = converter.finish().encode()
                status = "incomplete" if converter.terminal_status() == "incomplete" else "ok"
                await log_usage("responses", model_name, account, t0, status, usage=conv_usage(converter._usage),
                           input_content=input_text,
                           output_content=(getattr(converter, "_content", "") or "") + converter.tools_summary(),
                           reasoning_content="".join(_reason_parts))
                # 上游已明确结束且用量已入库；客户端读到终态后关连接不能再记一条 aborted。
                # 把终态 yield 放在 try 之外，关闭生成器也不会重走取消补记分支。
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端中途断开：补记已产生的输出（CancelledError 不走 except Exception）；
                # 断开非账号过错，不计入失败/冷却
                await log_usage("responses", model_name, account, t0, "aborted", "client disconnected",
                          usage=conv_usage(converter._usage),
                          input_content=input_text,
                          output_content=(getattr(converter, "_content", "") or "") + converter.tools_summary(),
                          reasoning_content="".join(_reason_parts), update_pool=False)
                raise
            except UpstreamError as e:
                errored["flag"] = True
                errored["status"] = e.status_code
                errored["msg"] = str(e.raw.decode("utf-8", "replace"))
                # 流中途断开：不在 open_upstream 处置范围内 → 走分类表补罚
                act = await asyncio.to_thread(inference.penalize, ctx, account, e.status_code, e.raw, e.headers, model_name)
                await log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}",
                          input_content=input_text, cooldown=act.cooldown, update_pool=False)
                yield f'data: {json_error(e.status_code, errored["msg"])}\n\n'.encode()
                yield b"data: [DONE]\n\n"
            except Exception as e:  # noqa: BLE001
                errored["flag"] = True
                errored["status"] = 502
                errored["msg"] = str(e)
                await log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text,
                          cooldown=inference.COOLDOWN_SOFT)
                yield f'data: {json_error(502, str(e))}\n\n'.encode()
                yield b"data: [DONE]\n\n"

            finally:
                await it.aclose()

            if not errored["flag"]:
                yield terminal

        if client_wants_stream:
            return StreamingResponse(with_keepalive(gen(), 15.0), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 非流式：中途出错时返回上游真实状态码而非 200+半截内容
        async for _ in gen():
            pass
        if errored["flag"]:
            raise HTTPException(status_code=errored["status"] or 502,
                                detail={"error": {"message": errored["msg"], "type": "upstream_error"}})
        return JSONResponse(content=converter.get_nonstream_response())
