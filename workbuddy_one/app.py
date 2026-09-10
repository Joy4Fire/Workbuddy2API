"""FastAPI 应用：/v1/chat/completions、/v1/responses、/v1/messages、/v1/models、/health。

三协议统一转换为 OpenAI Chat 请求发往腾讯 /v2/chat/completions，
响应侧再由各协议适配器转回对应协议的原生 SSE 事件流。
"""
from __future__ import annotations

import json
import io
import time
import asyncio
import hashlib
import secrets
import os
import re
from functools import partial
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import qrcode
from qrcode.image.svg import SvgPathImage

from .adapters.anthropic import anthropic_request_to_chat, AnthropicStreamConverter
from .adapters.responses import responses_request_to_chat, ResponsesStreamConverter
from .config import config
from .credentials import CredentialManager, find_auth_files
from .db import Database
from .desensitize import desensitize_body
from ._crypto import encrypt as encrypt_key, decrypt as decrypt_key
from .models import ModelRegistry
from .benchmarks import AABenchmarks
from .oauth import oauth_begin, oauth_poll, OAuthError
from .pool import AccountPool
from .ratelimit import AsyncAccountRateLimiter
from .reasoning import sanitize_body
from .scheduler import Scheduler
from .upstream import build_upstream_body, collect_upstream, stream_upstream, UpstreamError

import logging
logger = logging.getLogger("workbuddy_one.app")


# ---- DSH 附件归档 ----
# DSH 把图片/附件存在 ~/.dsh/attachments 下（无扩展名的对象文件），请求里只带
# 路径文本。若不归档，记录里只剩一个指向 DSH 私有目录的路径——附件随时可能被
# DSH 清理，记录就"没有保存"了。这里把引用到的附件复制进项目本地 data/attachments。
ATTACH_DIR = Path(__file__).resolve().parent.parent / "data" / "attachments"
_DSH_ATTACH_RE = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+[\\/]\.dsh[\\/]attachments[\\/][^\s\"'<>|]+", re.IGNORECASE)
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024  # 单附件归档上限 20MB


def _ext_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _archive_attachments(text: str) -> str:
    """扫描文本中的 DSH 附件路径，复制到项目本地并返回替换后的文本。

    同名内容（相同 sha256）只归档一次；失败（文件不存在/过大/无权限）保留原路径。
    """
    if not text or ".dsh" not in text.lower() or "attachments" not in text.lower():
        return text
    out = text
    for m in list(_DSH_ATTACH_RE.finditer(text)):
        src = Path(m.group(0))
        try:
            if not src.is_file():
                continue
            if src.stat().st_size > _MAX_ARCHIVE_BYTES:
                continue
            data = src.read_bytes()
            if not data:
                continue
            digest = hashlib.sha256(data).hexdigest()
            ext = _ext_of(data)
            dest = ATTACH_DIR / f"{digest[:16]}{ext}"
            if not dest.exists():
                ATTACH_DIR.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
            out = out.replace(m.group(0), str(dest))
        except OSError:
            continue
    return out

# 不同错误对应的冷却时长（秒）
COOLDOWN_NONE = 0.0
COOLDOWN_SOFT = 60.0       # 通用失败
COOLDOWN_HARD = 1800.0     # 认证/限流（401/403/429）


def create_app() -> FastAPI:
    # 关闭自动生成的 /docs、/redoc、/openapi.json：本地网关无需暴露 API 文档（减少攻击面）
    app = FastAPI(title="Workbuddy2API", version="0.4.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    db = Database(config.db_path)

    # 加载本地 auth 文件，注册账号
    managers: dict[str, CredentialManager] = {}
    auth_files = find_auth_files()
    for f in auth_files:
        try:
            mgr = CredentialManager(f)
            summary = mgr.summary()
            if summary.get("uid"):
                db.upsert_account({"path": str(f)}, summary)
                managers[summary["uid"]] = mgr
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 加载 auth 文件失败 {f}: {e}")

    pool = AccountPool(managers)
    # 从 DB 恢复上次额度的最近值：避免进程重启后额度盲区（冷启动即可按额度排除已耗尽账号）
    for _acc in pool.accounts:
        _row = db.get_account(_acc.uid)
        if _row:
            if _row.get("credits_remaining") is not None:
                pool.set_credits(_acc.uid, _row.get("credits_remaining"), _row.get("credits_total"),
                                 _row.get("credits_expire_at"))
            if _row.get("priority"):
                pool.set_priority(_acc.uid, _row.get("priority"))
    models = ModelRegistry(pool)
    benchmarks = AABenchmarks(db=db)
    scheduler = Scheduler(pool, db=db, models=models, benchmarks=benchmarks,
                          credit_interval_min=config.credit_refresh_min)

    def _register_account(path: Path) -> dict:
        """把一份 auth 文件注册进账号池（含 DB 落账）。返回账号摘要。"""
        mgr = CredentialManager(path)
        summary = mgr.summary()
        if not summary.get("uid"):
            raise HTTPException(status_code=400, detail={"error": {"message": "auth 文件中缺少 uid"}})
        uid = summary["uid"]
        added = pool.add_account(uid, mgr)
        db.upsert_account({"path": str(path)}, summary)
        managers[uid] = mgr
        return {"uid": uid, "added": added, **summary}

    def _auths_dir() -> Path:
        d = Path(__file__).resolve().parent.parent / "auths"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _remove_account(uid: str) -> dict:
        """从账号池、DB 移除账号；若其 auth 文件在项目 auths/ 下则一并删除。"""
        removed = pool.remove_account(uid)
        managers.pop(uid, None)
        db.delete_account(uid)
        # 删除项目内 auths/ 下的对应文件（本机 CodeBuddy 目录的保留，不删）
        d = _auths_dir()
        for f in d.glob(f"*{uid}*.info"):
            try:
                f.unlink()
            except OSError:
                pass
        return {"ok": True, "removed": removed, "uid": uid}

    @app.on_event("startup")
    async def _startup():
        await scheduler.start()

    @app.on_event("shutdown")
    async def _shutdown():
        await scheduler.stop()

    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _gen_api_key() -> str:
        return "sk-" + secrets.token_hex(16)

    def _check_api_key(authorization, x_api_key):
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
        app = db.find_app_by_key(_hash_key(token))
        if app is None:
            raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})
        return app["name"]

    def _pick_account():
        """从池中选择一个账号，返回 Account；无账号则 503。"""
        acc = pool.pick()
        if acc is None:
            raise HTTPException(status_code=503, detail={"error": {"message": "无可用账号（全部冷却或额度耗尽），请检查账号状态", "type": "auth_error"}})
        return acc

    # 账号级限速器
    _limiters: dict[str, AsyncAccountRateLimiter] = {}

    def _limiter(uid: str) -> AsyncAccountRateLimiter:
        if uid not in _limiters:
            _limiters[uid] = AsyncAccountRateLimiter(min_interval=config.ratelimit_interval)
        return _limiters[uid]

    async def _open_upstream(account, body: dict):
        """在返回 StreamingResponse 前，先建立上游连接并预取首个 SSE 行。

        这样上游在流真正开始前失败时，能返回正确的 HTTP 状态码（而非 200+SSE 错误），
        客户端可以正确识别错误而不是卡住等待。成功返回 (iterator, 首行)。
        """
        if config.ratelimit:
            await _limiter(account.uid).wait_if_needed()
        headers = account.mgr.get_headers()
        it = stream_upstream(headers, body).__aiter__()
        first = await it.__anext__()   # 首次 anext 会真正发起上游请求；失败抛 UpstreamError
        return it, first

    def _enhance_body(body: dict) -> dict:
        """统一规整 + 可选脱敏，构造最终上游请求体。"""
        # 动态思考强度表：来自模型目录（动态获取），无则交给内置静态表
        model_id = body.get("model")
        dyn_efforts = None
        if model_id:
            efforts = models.reasoning_efforts(model_id)
            if efforts:
                dyn_efforts = {model_id: efforts}
        body = sanitize_body(body, efforts=dyn_efforts)
        if config.desensitize:
            body = desensitize_body(body, roles=("system", "developer"))
        return body

    def _log_usage(protocol, model_name, account, t0, status, err="", usage=None, cooldown=COOLDOWN_NONE,
                   input_content="", output_content="", reasoning_content="", app_name=""):
        usage = usage or {}
        if status == "ok":
            pool.on_success(account.uid)
        else:
            pool.on_failure(account.uid, cooldown or COOLDOWN_SOFT)
        # 超长文本裁剪：思考链/输出也可能很长（实测 COT 单条 4 万+ 字符）
        db.log_usage(model=model_name, protocol=protocol, account_uid=account.uid,
                     input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                     output_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                     latency_ms=(time.time() - t0) * 1000, status=status, error=err,
                     input_content=input_content, output_content=output_content,
                     reasoning_content=reasoning_content,
                     credits=usage.get("credit") or 0,
                     app_name=app_name)

    def _extract_input_text(body: dict) -> str:
        """从上游请求体提取输入消息（角色: 内容，逐条）。

        保留策略：**完整无损入库**——用于后续训练自有模型，因此不截断消息条数、
        不限制长度，完整保留 system 提示、全部历史消息与模型输出。
        （发往上游的请求体本就完整，这里与之一致，仅从"记录"视角重组为可读文本。）

        多模态：图片部件以 `[图片: <media_type>|<data 或 base64>]` 形式**完整保留**
        base64 数据，供将来训练多模态模型；同一请求的图片也归档到项目本地
        data/attachments（若上游以 DSH 路径引用）。
        """
        msgs = body.get("messages") or []
        parts: list[str] = []
        for m in msgs:
            role = m.get("role", "user")
            c = m.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                segs: list[str] = []
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        segs.append(p.get("text", "") or "")
                    elif p.get("type") == "image_url":
                        url = (p.get("image_url") or {}).get("url", "") if isinstance(p.get("image_url"), dict) else ""
                        segs.append(f"[图片: image_url|{url}]")
                    elif p.get("type") == "image":
                        src = p.get("source") or {}
                        mt = src.get("media_type", "?") if isinstance(src, dict) else "?"
                        data = src.get("data", "") if isinstance(src, dict) else ""
                        segs.append(f"[图片: {mt}|{data}]")
                    elif p.get("type") == "input_image":
                        img = p.get("image") or {}
                        mt = img.get("media_type", "?") if isinstance(img, dict) else "?"
                        data = img.get("data", "") if isinstance(img, dict) else ""
                        segs.append(f"[图片: {mt}|{data}]")
                text = " ".join(s for s in segs if s)
            else:
                text = ""
            if text:
                parts.append(f"{role}: {text}")
        result = "\n".join(parts)
        # DSH 附件归档：若文本里引用了 ~/.dsh/attachments 下的对象（非 data-url 场景），
        # 复制进项目本地留存（按 sha256 去重）。完整保留文本，不做替换。
        _archive_attachments(result)
        return result

    def _delta_parts(line: str) -> tuple[str, str]:
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

    def _sanitize_chat_sse(line: str) -> str:
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

    @app.get("/health")
    def health():
        info = {"status": "ok", "auth_files": len(auth_files), "accounts": list(managers.keys())}
        return info

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(default=None),
                    x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        _check_api_key(authorization, x_api_key)
        return {"object": "list", "data": models.list()}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request,
                           authorization: str | None = Header(default=None),
                           x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        """Anthropic 兼容：估算输入 token 数（Claude Code / SDK 会调用）。

        返回 {input_tokens: int}。这里做轻量估算（字符数/4 + 每条消息固定开销），
        供客户端做上下文管理；实际 token 数以模型返回的 usage 为准。
        """
        _check_api_key(authorization, x_api_key)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
        text_len = 0
        n_msg = 0
        for m in body.get("messages") or []:
            n_msg += 1
            c = m.get("content")
            if isinstance(c, str):
                text_len += len(c)
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        text_len += len(p.get("text", "") or "")
        system = body.get("system")
        if isinstance(system, str):
            text_len += len(system)
        elif isinstance(system, list):
            for p in system:
                if isinstance(p, dict):
                    text_len += len(p.get("text", "") or "")
        # 粗略估算：英文约 4 字符/token，另加每条消息 ~4 token 的结构开销
        est = int(text_len / 4) + n_msg * 4 + 4
        return {"input_tokens": est}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               authorization: str | None = Header(default=None),
                               x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        app_name = _check_api_key(authorization, x_api_key)
        log_usage = partial(_log_usage, app_name=app_name)
        account = _pick_account()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        if not payload.get("messages"):
            raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

        client_wants_stream = bool(payload.get("stream"))
        body = _enhance_body(build_upstream_body(payload))
        headers = account.mgr.get_headers()
        model_name = payload.get("model", "auto")
        input_text = _extract_input_text(body)
        t0 = time.time()

        if client_wants_stream:
            # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
            try:
                it, first = await _open_upstream(account, body)
            except UpstreamError as e:
                log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
                raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
            except httpx.HTTPError as e:
                log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text)
                raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

            async def gen():
                _usage: dict | None = None
                _out_parts: list[str] = []
                _reason_parts: list[str] = []
                try:
                    _first_clean = _sanitize_chat_sse(first)
                    if _first_clean:
                        yield _first_clean + "\n\n"
                    _c, _r = _delta_parts(first)
                    if _c: _out_parts.append(_c)
                    if _r: _reason_parts.append(_r)
                    async for line in it:
                        _clean = _sanitize_chat_sse(line)
                        if not _clean:
                            continue  # 整行只剩空 delta，丢弃
                        yield _clean + "\n\n"
                        # 轻量解析：仅提取 usage 块用于本地记账（流经清洗后透传）
                        if line.startswith("data:") and line[5:].strip() not in ("[DONE]", ""):
                            try:
                                _chunk = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            if _chunk.get("usage"):
                                _usage = _chunk["usage"]
                            _c, _r = _delta_parts(line)
                            if _c: _out_parts.append(_c)
                            if _r: _reason_parts.append(_r)
                    log_usage("chat", model_name, account, t0, "ok", usage=_usage,
                               input_content=input_text,
                               output_content="".join(_out_parts),
                               reasoning_content="".join(_reason_parts))
                except UpstreamError as e:
                    log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
                    yield f'data: {_json_error(e.status_code, str(e.raw.decode("utf-8", "replace")))}'.encode()
                    yield b"\n\n"
                except Exception as e:  # noqa: BLE001
                    log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text)
                    yield f'data: {_json_error(502, str(e))}'.encode()
                    yield b"\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        try:
            if config.ratelimit:
                await _limiter(account.uid).wait_if_needed()
            collected = await collect_upstream(headers, body)
            _msg = (collected.get("choices") or [{}])[0].get("message", {})
            # 工具调用摘要并入输出记录（非流式 agent 回复常只有 tool_calls）
            _tc_summary = "".join(
                f"<tool_call:{(tc.get('function') or {}).get('name', '?')} {(tc.get('function') or {}).get('arguments', '')}>"
                for tc in _msg.get("tool_calls") or [] if isinstance(tc, dict))
            log_usage("chat", model_name, account, t0, "ok", usage=collected.get("usage"),
                       input_content=input_text,
                       output_content=(_msg.get("content") or "") + _tc_summary,
                       reasoning_content=_msg.get("reasoning_content") or "")
            return JSONResponse(content=collected)
        except UpstreamError as e:
            log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text)
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    @app.post("/v1/messages")
    async def messages(request: Request,
                       authorization: str | None = Header(default=None),
                       x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
                       anthropic_version: str | None = Header(default=None, alias="anthropic-version")):
        app_name = _check_api_key(authorization, x_api_key)
        log_usage = partial(_log_usage, app_name=app_name)
        account = _pick_account()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        chat_body = _enhance_body(anthropic_request_to_chat(payload))
        model_name = chat_body.get("model", "auto")
        input_text = _extract_input_text(chat_body)
        t0 = time.time()

        # Anthropic 默认流式；客户端可用 stream=false 请求非流式
        converter = AnthropicStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码（流式与非流式一致）
        try:
            it, first = await _open_upstream(account, chat_body)
        except UpstreamError as e:
            log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text)
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        async def gen():
            _reason_parts: list[str] = []
            try:
                evt = converter.feed_line(first)
                _c, _r = _delta_parts(first)
                if _r: _reason_parts.append(_r)
                if evt:
                    yield evt.encode()
                async for line in it:
                    evt = converter.feed_line(line)
                    _c, _r = _delta_parts(line)
                    if _r: _reason_parts.append(_r)
                    if evt:
                        yield evt.encode()
                yield converter.finish().encode()
                log_usage("anthropic", model_name, account, t0, "ok", usage=_conv_usage(converter._usage),
                           input_content=input_text,
                           output_content=(getattr(converter, "_text_content", "") or "") + converter.tools_summary(),
                           reasoning_content="".join(_reason_parts))
            except UpstreamError as e:
                log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
                yield _err_anthropic(e.status_code, str(e.raw.decode("utf-8", "replace"))).encode()
            except Exception as e:  # noqa: BLE001
                log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text)
                yield _err_anthropic(502, str(e)).encode()

        if client_wants_stream:
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 非流式：消费流并聚合（此时 gen 内的 _log_usage 会记录，这里不再重复）
        try:
            async for _ in gen():
                pass
            return JSONResponse(content=converter.get_nonstream_response())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"error": {"message": str(e), "type": "upstream_error"}})

    @app.post("/v1/responses")
    async def responses(request: Request,
                        authorization: str | None = Header(default=None),
                        x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
        app_name = _check_api_key(authorization, x_api_key)
        log_usage = partial(_log_usage, app_name=app_name)
        account = _pick_account()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        chat_body = _enhance_body(responses_request_to_chat(payload))
        model_name = chat_body.get("model", "auto")
        input_text = _extract_input_text(chat_body)
        t0 = time.time()

        converter = ResponsesStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
        try:
            it, first = await _open_upstream(account, chat_body)
        except UpstreamError as e:
            log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text)
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        async def gen():
            _reason_parts: list[str] = []
            try:
                evt = converter.feed_line(first)
                _c, _r = _delta_parts(first)
                if _r: _reason_parts.append(_r)
                if evt:
                    yield evt.encode()
                async for line in it:
                    evt = converter.feed_line(line)
                    _c, _r = _delta_parts(line)
                    if _r: _reason_parts.append(_r)
                    if evt:
                        yield evt.encode()
                yield converter.finish().encode()
                log_usage("responses", model_name, account, t0, "ok", usage=_conv_usage(converter._usage),
                           input_content=input_text,
                           output_content=(getattr(converter, "_content", "") or "") + converter.tools_summary(),
                           reasoning_content="".join(_reason_parts))
            except UpstreamError as e:
                log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text)
                yield _json_error(e.status_code, str(e.raw.decode("utf-8", "replace"))).encode()
                yield b"\n\n"
            except Exception as e:  # noqa: BLE001
                log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text)
                yield _json_error(502, str(e)).encode()
                yield b"\n\n"

        if client_wants_stream:
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        try:
            async for _ in gen():
                pass
            return JSONResponse(content=converter.get_nonstream_response())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"error": {"message": str(e), "type": "upstream_error"}})

    # ---------------- 管理 API（Phase 3/4） ----------------
    # 单用户一体化的本地管理接口：前后端直接绑定，WebUI 无需 Admin Token 认证即可访问

    def _accounts_with_checkin() -> list[dict]:
        """账号列表 + 本地签到状态（今日是否已签到）。"""
        checked = scheduler.checked_in_today()
        accounts = pool.all_accounts()
        for a in accounts:
            a["checkin_today"] = a["uid"] in checked
        return accounts

    @app.get("/admin/accounts")
    def admin_accounts():
        return {"accounts": _accounts_with_checkin()}

    @app.post("/admin/accounts/{uid}/enable")
    def admin_enable(uid: str):
        pool.set_enabled(uid, True)
        return {"ok": True}

    @app.post("/admin/accounts/{uid}/disable")
    def admin_disable(uid: str):
        pool.set_enabled(uid, False)
        return {"ok": True}

    @app.post("/admin/accounts/{uid}/priority")
    def admin_set_priority(uid: str, body: dict):
        """设置账号选号优先级（0-100，越大权重越高）。"""
        try:
            priority = max(0, min(100, int(body.get("priority", 0))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"error": {"message": "priority 需为数字"}})
        pool.set_priority(uid, priority)
        db.set_account_state(uid, priority=priority)
        return {"ok": True, "priority": priority}

    @app.delete("/admin/accounts/{uid}")
    def admin_delete_account(uid: str):
        return _remove_account(uid)

    @app.get("/admin/apps")
    def admin_apps():
        return {"apps": db.list_apps()}

    @app.post("/admin/apps")
    async def admin_create_app(body: dict):
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail={"error": {"message": "应用名称不能为空"}})
        if len(name) > 40:
            raise HTTPException(status_code=400, detail={"error": {"message": "应用名称过长（≤40 字符）"}})
        existing = db.list_apps()
        if any(a["name"] == name for a in existing):
            raise HTTPException(status_code=400, detail={"error": {"message": "应用名称已存在"}})
        key = _gen_api_key()
        note = str(body.get("note") or "").strip()
        app_id = db.create_app(name=name, key_hash=_hash_key(key), key_prefix=key[:9] + "…",
                               note=note, key_enc=encrypt_key(key))
        return {"ok": True, "app_id": app_id, "name": name, "key": key}  # 明文 key 仅此一次返回

    @app.get("/admin/apps/{app_id}/key")
    def admin_app_key(app_id: int):
        """返回应用的完整明文 Key（加密存储，解密后返回）。历史应用可能为空。"""
        enc = db.get_app_key_enc(app_id)
        if not enc:
            return {"ok": True, "name": "", "key": None, "unavailable": True}
        plain = decrypt_key(enc)
        return {"ok": True, "name": "", "key": plain if plain else None, "unavailable": not plain}

    @app.post("/admin/apps/{app_id}/toggle")
    def admin_toggle_app(app_id: int):
        enabled = db.toggle_app(app_id)
        return {"ok": True, "enabled": enabled}

    @app.delete("/admin/apps/{app_id}")
    def admin_delete_app(app_id: int):
        return {"ok": db.delete_app(app_id)}

    @app.post("/admin/credits/refresh")
    async def admin_refresh_credits():
        await scheduler.refresh_credits()
        return {"ok": True, "accounts": _accounts_with_checkin()}

    @app.post("/admin/checkin")
    async def admin_checkin():
        # 已签到的账号跳过，避免重复请求上游；仅对未签到账号执行
        checked = scheduler.checked_in_today()
        results = await scheduler.do_checkin()
        return {
            "ok": True,
            "results": [{"uid": u, **r} for u, r in results],
            "skipped": sorted(checked),
        }

    @app.get("/admin/usage/summary")
    def admin_usage_summary():
        return db.usage_summary()

    @app.get("/admin/usage/timeseries")
    def admin_usage_timeseries(granularity: str = "hour", points: int = 24, model: str | None = None):
        """调用趋势（用于折线图）。granularity=hour|day，points=桶数，model=可选过滤。"""
        if granularity not in ("hour", "day"):
            granularity = "hour"
        points = max(6, min(points, 90))
        return {"granularity": granularity, "points": points, "data": db.usage_timeseries(granularity, points, model)}

    @app.get("/admin/usage/recent")
    def admin_usage_recent(limit: int = 50, protocol: str | None = None,
                           model: str | None = None, app_name: str | None = None,
                           status: str | None = None):
        """最近使用记录，支持按 protocol/model/app_name/status 筛选。"""
        return {"records": db.usage_recent(min(limit, 500), protocol=protocol,
                                           model=model, app_name=app_name, status=status)}

    @app.get("/admin/usage/filters")
    def admin_usage_filters():
        """使用记录筛选项的可选值。"""
        return db.usage_filters()

    @app.get("/admin/usage/storage")
    def admin_usage_storage():
        """使用记录存储体积状态（供展示是否需瘦身）。"""
        return db.usage_content_stats()

    @app.post("/admin/usage/trim")
    def admin_usage_trim():
        """无损裁剪存量记录的超长 content（保留记录条目与元数据）。"""
        changed = db.trim_usage_content()
        return {"ok": True, "trimmed": changed, "stats": db.usage_content_stats()}

    @app.get("/admin/models")
    def admin_models():
        # 返回完整模型元数据（含 context_length / max_output_tokens / name / reasoning / 模态 / 能力），供 WebUI 模型页展示
        entries = models.list()
        # 合并 AA 评测数据（若有 key 且有匹配）
        for e in entries:
            if benchmarks.configured():
                try:
                    bb = benchmarks.map(e["id"])
                    if bb:
                        e["benchmark"] = bb
                except Exception:  # noqa: BLE001
                    pass
        return {"models": entries, "source": models.source()}

    @app.get("/admin/models/benchmarks")
    def admin_benchmarks():
        """返回各模型的 AA 评测数据（供 WebUI 评测卡片展示）。"""
        if not benchmarks.configured():
            return {"configured": False, "models": {}}
        out = {}
        for e in models.list():
            try:
                bb = benchmarks.map(e["id"])
                if bb:
                    out[e["id"]] = bb
            except Exception:  # noqa: BLE001
                pass
        return {"configured": True, "models": out}

    @app.post("/admin/models/benchmarks/refresh")
    async def admin_benchmarks_refresh():
        """强制刷新 AA 评测缓存（WebUI 评测页刷新按钮）。"""
        if not benchmarks.configured():
            raise HTTPException(status_code=400, detail={"error": {"message": "未配置 AA API key"}})
        await asyncio.to_thread(benchmarks.refresh)
        return {"ok": True, "configured": True}

    @app.post("/admin/models/refresh")
    async def admin_models_refresh():
        """强制刷新模型目录（WebUI 模型页的刷新按钮）。"""
        try:
            await asyncio.to_thread(models.refresh)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"error": {"message": f"模型刷新失败: {e}"}})
        return {"ok": True, "models": models.list()}

    @app.get("/admin/overview")
    def admin_overview():
        accounts = _accounts_with_checkin()
        stats = db.usage_credit_stats()
        # 剩余积分（所有账号之和；未拉到积分的账号按 0 计）
        remaining = sum(float(a.get("credits_remaining") or 0) for a in accounts)

        # 按模型分别计算兑换比例，再按使用分布（token 占比）加权得出综合预测。
        # 不同模型消耗速度差异大，不能用全局平均，否则会严重失真。
        total_tok = stats["total_tokens"] or 0.0
        pm: list[dict] = []
        for m in stats["models"]:
            if m["credits"] >= 0.05:  # 该模型历史积分足够，比例可信
                tpc = m["tokens"] / m["credits"]
                weight = (m["tokens"] / total_tok) if total_tok else 0.0
                pm.append({
                    "model": m["model"],
                    "credits": round(m["credits"], 2),
                    "tokens": int(m["tokens"]),
                    "tokens_per_credit": round(tpc, 1),
                    "weight": round(weight, 4),
                    "predicted_tokens": round(remaining * tpc) if remaining else 0,
                })

        # 综合预测 = 剩余积分 × Σ(模型token占比 × 该模型每积分token)
        blended = sum(p["weight"] * p["tokens_per_credit"] for p in pm) if pm else None
        predicted = (remaining * blended) if blended is not None and remaining else None

        return {
            "accounts": accounts,
            "models": models.ids(),
            "usage": db.usage_summary(),
            "recent": db.usage_recent(10),
            "prediction": {
                "remaining_credits": round(remaining, 2),
                "tokens_per_credit": round(blended, 1) if blended is not None else None,
                "predicted_tokens": round(predicted) if predicted is not None else None,
                "credits_used": round(stats["total_credits"], 2),
                "tokens_used": int(stats["total_tokens"]),
                "models": pm,
            },
        }

    # ---------------- 自动签到 / 额度刷新 设置 ----------------

    @app.get("/admin/settings")
    def admin_get_settings():
        s = db.get_settings()
        aa_key = s.get("aa_api_key", "")
        return {
            "checkin_hours": s.get("checkin_hours", "9,21"),
            "credit_refresh_min": s.get("credit_refresh_min", "30"),
            "model_refresh_hour": s.get("model_refresh_hour", "6"),
            "aa_refresh_hour": s.get("aa_refresh_hour", "7"),
            "keepalive_hour": s.get("keepalive_hour", "22"),
            "aa_api_key": aa_key,
            # 掩码用于前端展示（不泄露完整 key）
            "aa_api_key_masked": _mask_secret(aa_key),
            "aa_enabled": bool(aa_key),
        }

    def _validate_hour_field(value, field_name: str) -> str:
        try:
            v = int(value)
            if 0 <= v <= 23:
                return str(v)
        except (TypeError, ValueError):
            pass
        raise HTTPException(status_code=400, detail={"error": {"message": f"{field_name} 需为 0-23 的小时"}})

    @app.post("/admin/settings")
    async def admin_save_settings(request: Request):
        body = await request.json()
        checkin_hours = body.get("checkin_hours")
        credit_refresh_min = body.get("credit_refresh_min")
        model_refresh_hour = body.get("model_refresh_hour")
        keepalive_hour = body.get("keepalive_hour")
        if checkin_hours is not None:
            # 校验格式：逗号分隔的 0-23 整数
            try:
                hours = [int(h) for h in str(checkin_hours).split(",") if str(h).strip()]
                if not hours or any(h < 0 or h > 23 for h in hours):
                    raise ValueError
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=400, detail={"error": {"message": "签到时间需为 0-23 的小时，逗号分隔"}})
            db.save_settings(checkin_hours=",".join(str(h) for h in sorted(set(hours))))
        if credit_refresh_min is not None:
            try:
                v = int(credit_refresh_min)
                if v < 1 or v > 1440:
                    raise ValueError
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=400, detail={"error": {"message": "额度刷新间隔需为 1-1440 分钟"}})
            db.save_settings(credit_refresh_min=str(v))
        if model_refresh_hour is not None:
            db.save_settings(model_refresh_hour=_validate_hour_field(model_refresh_hour, "模型刷新时间"))
        if "aa_refresh_hour" in body:
            db.save_settings(aa_refresh_hour=_validate_hour_field(body.get("aa_refresh_hour"), "AA 评测刷新时间"))
        if keepalive_hour is not None:
            db.save_settings(keepalive_hour=_validate_hour_field(keepalive_hour, "token 保活时间"))
        if "aa_api_key" in body:
            aa_key = str(body.get("aa_api_key") or "").strip()
            # 留空且已有配置 → 视为不修改（避免误清空）；显式清除用特殊标记
            if aa_key == "" and db.get_settings().get("aa_api_key"):
                # 若传了空字符串，表示清除
                if body.get("clear_aa_api_key"):
                    db.save_settings(aa_api_key="")
                # 否则忽略（不覆盖）
            else:
                db.save_settings(aa_api_key=aa_key)
        # 设置已存入 DB，调度器下个周期自动生效
        return {"ok": True, **db.get_settings()}

    # ---------------- 账号管理：上传 auth 文件 / 扫码登录 ----------------

    @app.post("/admin/accounts/upload")
    async def admin_upload_auth(file: UploadFile = File(...)):
        """上传一份 CodeBuddy 原始 .info auth 文件并注册进账号池。"""
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail={"error": {"message": "空文件"}})
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": {"message": "不是合法的 JSON auth 文件"}})
        auth = data.get("auth") or {}
        account = data.get("account") or {}
        if not auth.get("accessToken"):
            raise HTTPException(status_code=400, detail={"error": {"message": "auth 中缺少 accessToken"}})
        uid = account.get("uid") or ""
        if not uid:
            raise HTTPException(status_code=400, detail={"error": {"message": "account 中缺少 uid"}})
        # 落盘到 auths/ 目录（校验并格式化）
        d = _auths_dir()
        path = d / f"workbuddy-{uid}.info"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        info = _register_account(path)
        # 刷新新账号额度
        await scheduler.refresh_credits()
        return {"ok": True, "account": info}

    @app.post("/admin/oauth/start")
    def admin_oauth_start():
        """发起扫码登录，返回 {state, authUrl} 用于前端展示二维码。"""
        try:
            return {"ok": True, **oauth_begin()}
        except OAuthError as e:
            raise HTTPException(status_code=502, detail={"error": {"message": str(e)}})

    @app.get("/admin/oauth/status")
    async def admin_oauth_status(state: str):
        """轮询扫码登录状态；ready 时自动落盘并注册账号。"""
        result = oauth_poll(state)
        if result.get("status") != "ready":
            return {"status": "pending"}
        auth = result["auth"]
        account = result["account"]
        uid = account.get("uid") or "unknown"
        d = _auths_dir()
        path = d / f"workbuddy-{uid}.info"
        session = {"auth": auth, "account": account}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        info = _register_account(path)
        # 登录成功自动签到一次（仅新账号；参考 Sliverkiss login.sh 登录即签到）
        try:
            skip = {a.uid for a in pool.accounts if a.uid != uid}
            checkin = await scheduler.do_checkin(skip=skip)
            info["checkin"] = next((r for u, r in checkin if u == uid), None)
        except Exception as e:  # noqa: BLE001
            info["checkin"] = {"ok": False, "message": str(e)}
        await scheduler.refresh_credits()
        return {"status": "ready", "account": info}

    @app.get("/admin/oauth/qr")
    def admin_oauth_qr(text: str):
        """把登录链接渲染成二维码（SVG）。"""
        img = qrcode.make(text, image_factory=SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    # ---------------- 安全中间件：限制 CORS + 管理接口鉴权 ----------------
    # 允许的跨域来源（仅本地开发用 Vite dev server 端口；生产同源无需 CORS）
    _DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}

    def _is_loopback(host: str) -> bool:
        return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")

    @app.middleware("http")
    async def _security(request: Request, call_next):
        path = request.url.path

        # Host 头校验：仅允许本机回环 host（防 DNS rebinding / 恶意网站通过域名打到 localhost 管理面）
        host_header = (request.headers.get("Host") or "").strip().lower()
        host_name = host_header.split(":", 1)[0] if host_header else ""
        if host_name not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse(status_code=403, content={"error": {"message": "Host 不被允许"}})

        # 管理接口鉴权（WebUI /admin/*）
        if path.startswith("/admin/"):
            if config.admin_token:
                auth = request.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if token != config.admin_token and request.headers.get("X-Admin-Token") != config.admin_token:
                    return JSONResponse(status_code=403, content={"error": {"message": "需要有效的 Admin Token"}})
            else:
                # 未设置 ADMIN_TOKEN：仅允许本机回环访问管理接口
                client_host = (request.client.host if request.client else "") or ""
                if not _is_loopback(client_host):
                    return JSONResponse(status_code=403, content={
                        "error": {"message": "管理接口仅允许本机访问；局域网访问请设置 ADMIN_TOKEN"}})

        # OPTIONS 预检：仅放行允许的跨域来源
        if request.method == "OPTIONS":
            origin = request.headers.get("Origin", "")
            if origin and origin in _DEV_ORIGINS:
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Api-Key, X-Admin-Token, anthropic-version",
                        "Access-Control-Max-Age": "600",
                    },
                )
            return Response(status_code=204)

        # 管理接口审计：记录写操作（POST/DELETE）供追溯
        if path.startswith("/admin/") and request.method in ("POST", "DELETE"):
            client_ip = (request.client.host if request.client else "") or ""
            logger.info("[audit] %s %s from %s (host=%s)", request.method, path, client_ip, host_header)

        response = await call_next(request)
        # 同源不做 CORS 限制；仅对白名单开发来源回显 Allow-Origin
        origin = request.headers.get("Origin", "")
        if origin and origin in _DEV_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Api-Key, X-Admin-Token, anthropic-version"
        return response

    # ---------------- WebUI 静态托管（Vite 构建产物 frontend/dist） ----------------
    _DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

    @app.get("/")
    def index():
        f = _DIST_DIR / "index.html"
        if f.exists():
            return FileResponse(f)
        return JSONResponse({"msg": "WorkBuddy One API 服务运行中，WebUI 未构建（请先 cd frontend && pnpm build）"})

    @app.get("/{asset_path:path}")
    def web_assets(asset_path: str):
        # 放行 API 路由（非前端资源）；/docs、/openapi.json 等元数据路径返回 404
        if asset_path.startswith(("v1/", "admin/", "health", "models")):
            raise HTTPException(status_code=404)
        if asset_path in ("docs", "redoc", "openapi.json"):
            raise HTTPException(status_code=404)
        f = (_DIST_DIR / asset_path).resolve()
        if f.is_file() and str(f).startswith(str(_DIST_DIR)):
            return FileResponse(f)
        # SPA fallback：非 API 的未知路径返回 index.html
        if (_DIST_DIR / "index.html").exists():
            return FileResponse(_DIST_DIR / "index.html")
        raise HTTPException(status_code=404)

    return app


def _mask_secret(s: str) -> str:
    """掩码密钥：保留前 4 位 + 末 4 位，中间用 *。"""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * (len(s) - 8)}{s[-4:]}"


def _conv_usage(usage: dict | None) -> dict:
    """把各协议转换器内部的 usage 归一化为 input/output tokens。"""
    if not usage:
        return {}
    return {
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
    }


def _err_anthropic(status: int, message: str) -> str:
    return json.dumps({"type": "error", "error": {"type": "api_error", "message": message}})


def _json_error(status: int, message: str) -> str:
    return json.dumps({"error": {"message": message, "type": "upstream_error"}})


def _safe_err(raw: bytes, status: int) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("error"):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"error": {"message": raw.decode("utf-8", "replace"), "type": "upstream_error"}}
