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


def _cooldown_for(status_code: int) -> float:
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



def create_app() -> FastAPI:
    # 关闭自动生成的 /docs、/redoc、/openapi.json：本地网关无需暴露 API 文档（减少攻击面）
    app = FastAPI(title="Workbuddy2API", version="0.4.1",
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
            # 恢复停用状态：手动停用/保活自动禁用的账号不能因重启"复活"，
            # 原因一并恢复（WebUI 展示"为什么不可用"）
            if _row.get("enabled") == 0:
                pool.set_enabled(_acc.uid, False, reason=_row.get("disabled_reason") or "手动停用")
    models = ModelRegistry(pool, db=db)
    benchmarks = AABenchmarks(db=db)
    scheduler = Scheduler(pool, db=db, models=models, benchmarks=benchmarks,
                          credit_interval_min=config.credit_refresh_min,
                          usage_retention_days=config.usage_retention_days)

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

    def _safe_uid(uid: str) -> str:
        """把任意 uid 规整为安全的文件名片段，防止路径穿越/非法字符。

        uid 来自用户上传的 JSON 或扫码结果，属不可信输入；只保留字母数字、连字符、下划线。
        非法则退化为 'unknown'，保证落盘路径始终限定在 auths/ 目录内。
        """
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_\-]", "", str(uid or "")).strip("-")
        return safe or "unknown"

    def _remove_account(uid: str) -> dict:
        """从账号池、DB 移除账号；若其 auth 文件在项目 auths/ 下则一并删除。"""
        removed = pool.remove_account(uid)
        managers.pop(uid, None)
        db.delete_account(uid)
        _limiters.pop(uid, None)  # 释放账号级限速器，避免字典无限增长
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
        # fallback 顶班账号（无健康账号时选出的最早冷却到期者）若还要冷却 30 秒以上，
        # 不硬打——把请求送上去只会再吃一个 429，形成 429 风暴；直接明确拒绝
        if acc.cooldown_until - time.time() > 30:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "上游限流中，所有账号均在冷却，请稍后重试", "type": "rate_limit_error"}})
        return acc

    # 账号级限速器
    _limiters: dict[str, AsyncAccountRateLimiter] = {}

    def _limiter(uid: str) -> AsyncAccountRateLimiter:
        if uid not in _limiters:
            _limiters[uid] = AsyncAccountRateLimiter(min_interval=config.ratelimit_interval)
        return _limiters[uid]

    def _resolve_model(model: str) -> str:
        """按设置里的 model_aliases 把别名解析为真实模型名（每行一条：别名=真实模型）。

        让硬编码熟名字（gpt-4o 等）的客户端开箱即用。每次读 settings（几行的
        SQLite 查询，单用户场景开销可忽略）。
        """
        if not model:
            return model
        aliases = _parse_model_aliases(db.get_settings().get("model_aliases") or "")
        return aliases.get(model, model)

    def _get_headers(account):
        """取账号请求头（token 过期时自动刷新）；刷新失败转 503 并冷却该账号。

        RuntimeError 直接漏到 FastAPI 会变成裸 500，客户端无从得知该做什么。
        """
        try:
            return account.mgr.get_headers()
        except RuntimeError as e:
            pool.on_failure(account.uid, COOLDOWN_HARD)
            raise HTTPException(status_code=503, detail={
                "error": {"message": f"账号 token 刷新失败，请到 WebUI 重新扫码登录（{e}）",
                          "type": "auth_error"}})

    async def _open_upstream_once(account, body: dict):
        """对单个账号建立上游连接并预取首个 SSE 行，成功返回 (iterator, 首行)。"""
        if config.ratelimit:
            await _limiter(account.uid).wait_if_needed()
        headers = _get_headers(account)
        it = stream_upstream(headers, body).__aiter__()
        first = await it.__anext__()   # 首次 anext 会真正发起上游请求；失败抛 UpstreamError
        return it, first

    async def _open_upstream(account, body: dict):
        """在返回 StreamingResponse 前，先建立上游连接并预取首个 SSE 行。

        这样上游在流真正开始前失败时，能返回正确的 HTTP 状态码（而非 200+SSE 错误），
        客户端可以正确识别错误而不是卡住等待。
        返回 (iterator, 首行, 实际使用的账号)——429/502/503 时会自动换健康账号重试一次
        （仅一次，不递归），换号后调用方必须用返回的账号记账，而不是最初的账号。
        """
        try:
            it, first = await _open_upstream_once(account, body)
            return it, first, account
        except UpstreamError as e:
            if e.status_code not in (429, 502, 503):
                raise
            # 该账号已确认打不通：先上冷却，避免下面 pick() 又选中它原地重试
            pool.on_failure(account.uid, _cooldown_for(e.status_code))
            if pool.healthy_count() < 1:
                # 没有其它健康账号：放弃重试，按原错误交给调用方记录/返回
                #（后续请求会被 _pick_account 的冷却兜底挡下并得到 503）
                raise
            alt = _pick_account()
            try:
                it, first = await _open_upstream_once(alt, body)
            except (UpstreamError, httpx.HTTPError):
                pool.on_failure(alt.uid, COOLDOWN_SOFT)
                raise
            return it, first, alt

    def _enhance_body(body: dict) -> dict:
        """统一规整 + 可选脱敏，构造最终上游请求体。"""
        # 模型别名解析：客户端用熟名字（gpt-4o 等）也能路由到真实模型
        if body.get("model"):
            body["model"] = _resolve_model(str(body["model"]))
        # max_tokens 按模型实际上限裁剪：Claude Code 常发 32000+，
        # 超过部分模型上限会被上游拒绝（目录未知时不裁剪）
        max_out = models.max_output_tokens(str(body.get("model") or ""))
        if max_out and body.get("max_tokens") and body["max_tokens"] > max_out:
            logger.info("max_tokens %s 超过模型 %s 上限，裁剪为 %s",
                        body["max_tokens"], body.get("model"), max_out)
            body["max_tokens"] = max_out
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
                   input_content="", output_content="", reasoning_content="", app_name="", update_pool=True):
        usage = usage or {}
        # update_pool=False 用于"客户端断开补记"等非账号过错的场景：
        # 断开不是账号的失败，不应计入失败数/冷却（否则 Claude Code 常见的主动中断
        # 会把健康账号打成"冷却中"）
        if update_pool:
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

    async def _extract_input_text(body: dict) -> str:
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
        # 归档内部有最大 20MB 的同步文件读 + sha256，放线程池避免阻塞事件循环
        if ".dsh" in result.lower() and "attachments" in result.lower():
            result = await asyncio.to_thread(_archive_attachments, result)
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
        data = models.list()
        # 别名条目：复用真实模型的元数据（模态/上下文等），id 换成别名——
        # 客户端模型下拉里直接出现熟名字（gpt-4o 等），选中即路由到真实模型
        aliases = _parse_model_aliases(db.get_settings().get("model_aliases") or "")
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

        返回 {input_tokens: int}。这里做轻量估算（字符数/4 + 每条消息固定开销），
        供客户端做上下文管理；实际 token 数以模型返回的 usage 为准。
        """
        _check_api_key(authorization, x_api_key)
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
        return {"input_tokens": _estimate_tokens(" ".join(t for t in texts if t), n_msg)}

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
        headers = _get_headers(account)
        # 记账用解析后的真实模型名（别名请求按真实模型归因，避免按模型统计被打碎）
        model_name = body.get("model", "auto")
        input_text = await _extract_input_text(body)
        t0 = time.time()

        if client_wants_stream:
            # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
            # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
            try:
                it, first, account = await _open_upstream(account, body)
            except UpstreamError as e:
                log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                          cooldown=_cooldown_for(e.status_code))
                raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
            except httpx.HTTPError as e:
                log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text,
                          cooldown=COOLDOWN_SOFT)
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
                except (asyncio.CancelledError, GeneratorExit):
                    # 客户端中途断开：上游已消耗的积分要补记一条，否则使用记录缺失、
                    # 积分预测失真。CancelledError 继承自 BaseException，不会被下面的
                    # except Exception 捕获。断开非账号过错，不计入失败/冷却。
                    log_usage("chat", model_name, account, t0, "aborted", "client disconnected",
                              usage=_usage, input_content=input_text,
                              output_content="".join(_out_parts),
                              reasoning_content="".join(_reason_parts), update_pool=False)
                    raise
                except UpstreamError as e:
                    log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                              cooldown=_cooldown_for(e.status_code))
                    yield f'data: {_json_error(e.status_code, str(e.raw.decode("utf-8", "replace")))}\n\n'.encode()
                    yield b"data: [DONE]\n\n"
                except Exception as e:  # noqa: BLE001
                    log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text,
                              cooldown=COOLDOWN_SOFT)
                    yield f'data: {_json_error(502, str(e))}\n\n'.encode()
                    yield b"data: [DONE]\n\n"
            return StreamingResponse(_with_keepalive(gen(), 15.0), media_type="text/event-stream",
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
            log_usage("chat", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                      cooldown=_cooldown_for(e.status_code))
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("chat", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=COOLDOWN_SOFT)
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
        input_text = await _extract_input_text(chat_body)
        t0 = time.time()

        # Anthropic 默认流式；客户端可用 stream=false 请求非流式
        converter = AnthropicStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码（流式与非流式一致）
        # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
        try:
            it, first, account = await _open_upstream(account, chat_body)
        except UpstreamError as e:
            log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                      cooldown=_cooldown_for(e.status_code))
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=COOLDOWN_SOFT)
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        # 闭包错误标志：gen 捕获 UpstreamError 后只 yield 错误事件不抛出（流式路径需要），
        # 非流式路径靠它区分"正常结束"与"中途出错"，避免把半截内容当 200 成功返回
        errored = {"flag": False, "status": 0, "msg": ""}

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
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端中途断开：补记已产生的输出（CancelledError 不走 except Exception）；
                # 断开非账号过错，不计入失败/冷却
                log_usage("anthropic", model_name, account, t0, "aborted", "client disconnected",
                          usage=_conv_usage(converter._usage),
                          input_content=input_text,
                          output_content=(getattr(converter, "_text_content", "") or "") + converter.tools_summary(),
                          reasoning_content="".join(_reason_parts), update_pool=False)
                raise
            except UpstreamError as e:
                errored["flag"] = True
                errored["status"] = e.status_code
                errored["msg"] = str(e.raw.decode("utf-8", "replace"))
                log_usage("anthropic", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                          cooldown=_cooldown_for(e.status_code))
                yield _err_anthropic(e.status_code, errored["msg"]).encode()
            except Exception as e:  # noqa: BLE001
                errored["flag"] = True
                errored["status"] = 502
                errored["msg"] = str(e)
                log_usage("anthropic", model_name, account, t0, "error", str(e), input_content=input_text,
                          cooldown=COOLDOWN_SOFT)
                yield _err_anthropic(502, str(e)).encode()

        if client_wants_stream:
            return StreamingResponse(_with_keepalive(gen(), 15.0), media_type="text/event-stream",
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
        app_name = _check_api_key(authorization, x_api_key)
        log_usage = partial(_log_usage, app_name=app_name)
        account = _pick_account()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})

        chat_body = _enhance_body(responses_request_to_chat(payload))
        model_name = chat_body.get("model", "auto")
        input_text = await _extract_input_text(chat_body)
        t0 = time.time()

        converter = ResponsesStreamConverter(model=model_name)
        client_wants_stream = payload.get("stream", True)

        # 预取上游首个事件：失败则直接返回正确 HTTP 状态码
        # （换号重试后 account 会被重新绑定，gen() 里记账用的就是实际服务的账号）
        try:
            it, first, account = await _open_upstream(account, chat_body)
        except UpstreamError as e:
            log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                      cooldown=_cooldown_for(e.status_code))
            raise HTTPException(status_code=e.status_code, detail=_safe_err(e.raw, e.status_code))
        except httpx.HTTPError as e:
            log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text,
                      cooldown=COOLDOWN_SOFT)
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

        # 闭包错误标志：用途同 /v1/messages——非流式路径区分正常结束与中途出错
        errored = {"flag": False, "status": 0, "msg": ""}

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
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端中途断开：补记已产生的输出（CancelledError 不走 except Exception）；
                # 断开非账号过错，不计入失败/冷却
                log_usage("responses", model_name, account, t0, "aborted", "client disconnected",
                          usage=_conv_usage(converter._usage),
                          input_content=input_text,
                          output_content=(getattr(converter, "_content", "") or "") + converter.tools_summary(),
                          reasoning_content="".join(_reason_parts), update_pool=False)
                raise
            except UpstreamError as e:
                errored["flag"] = True
                errored["status"] = e.status_code
                errored["msg"] = str(e.raw.decode("utf-8", "replace"))
                log_usage("responses", model_name, account, t0, "error", f"HTTP {e.status_code}", input_content=input_text,
                          cooldown=_cooldown_for(e.status_code))
                yield f'data: {_json_error(e.status_code, errored["msg"])}\n\n'.encode()
                yield b"data: [DONE]\n\n"
            except Exception as e:  # noqa: BLE001
                errored["flag"] = True
                errored["status"] = 502
                errored["msg"] = str(e)
                log_usage("responses", model_name, account, t0, "error", str(e), input_content=input_text,
                          cooldown=COOLDOWN_SOFT)
                yield f'data: {_json_error(502, str(e))}\n\n'.encode()
                yield b"data: [DONE]\n\n"

        if client_wants_stream:
            return StreamingResponse(_with_keepalive(gen(), 15.0), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 非流式：中途出错时返回上游真实状态码而非 200+半截内容
        async for _ in gen():
            pass
        if errored["flag"]:
            raise HTTPException(status_code=errored["status"] or 502,
                                detail={"error": {"message": errored["msg"], "type": "upstream_error"}})
        return JSONResponse(content=converter.get_nonstream_response())

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
        pool.set_enabled(uid, True)  # 启用时清空禁用原因
        # 同步落库：否则重启/重建容器后停用状态丢失，账号"复活"
        db.set_account_state(uid, enabled=1, disabled_reason="")
        return {"ok": True}

    @app.post("/admin/accounts/{uid}/disable")
    def admin_disable(uid: str):
        pool.set_enabled(uid, False, reason="手动停用")
        db.set_account_state(uid, enabled=0, disabled_reason="手动停用")
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
    def admin_usage_recent(page: int = 1, page_size: int = 20, protocol: str | None = None,
                           model: str | None = None, app_name: str | None = None,
                           status: str | None = None, search: str | None = None):
        """最近使用记录（服务端分页），支持按 protocol/model/app_name/status 筛选。

        search 为内容关键字搜索（输入/输出/思考链 LIKE 匹配）。
        返回 {records, total, page, page_size}：records 是 light 投影（不含大文本），
        total 是符合筛选条件的总数——分页必须由后端完成，前端本地分页只能翻到
        一次性拉回来的那批记录（老版本"到第 5 页就没了"的根因）；
        完整内容由 /admin/usage/{record_id:int} 详情端点按需获取。
        """
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        return {
            "records": db.usage_recent(page_size, offset=(page - 1) * page_size,
                                       protocol=protocol, model=model,
                                       app_name=app_name, status=status, light=True,
                                       search=search),
            "total": db.usage_count(protocol=protocol, model=model,
                                    app_name=app_name, status=status, search=search),
            "page": page,
            "page_size": page_size,
        }

    @app.get("/admin/usage/{record_id:int}")
    def admin_usage_detail(record_id: int):
        """单条使用记录详情（含完整输入/输出/思考链 content），供记录页详情弹窗。

        路径参数必须用 `:int` 转换器：FastAPI 的 `{record_id}` 会匹配任意单段路径
        （int 校验在路由匹配之后才做），注册顺序又在 filters/storage 等字面路由之前，
        会把 GET /admin/usage/filters 吞掉变成 422。
        """
        r = db.get_usage(record_id)
        if not r:
            raise HTTPException(status_code=404, detail={"error": {"message": "记录不存在"}})
        return {"record": r}

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
        # 注：请求路径只读当前缓存（后台调度器每 30 分钟自动刷新），
        # 缓存为空时现场拉取一次（首次访问或刚清空场景），之后仍走缓存。
        entries = models.list_cached()
        source = models.source()
        if not entries:
            try:
                entries = models.refresh()
                source = models.source()
            except Exception as e:  # noqa: BLE001
                logger.warning("模型现场刷新失败: %s", e)
        # 合并 AA 评测数据（若有 key 且有匹配；只读缓存，绝不阻塞在网络请求上）
        for e in entries:
            if benchmarks.configured():
                try:
                    bb = benchmarks.map(e["id"])
                    if bb:
                        e["benchmark"] = bb
                except Exception:  # noqa: BLE001
                    pass
        return {"models": entries, "source": source,
                "aa_configured": benchmarks.configured()}

    @app.get("/admin/models/benchmarks")
    def admin_benchmarks():
        """返回各模型的 AA 评测数据（供 WebUI 评测卡片展示）。

        只读缓存：首次调用若缓存为空才现场拉取一次，之后由调度器每日刷新。
        （sync 路由运行在线程池，可直接调用阻塞的 refresh()）
        """
        if not benchmarks.configured():
            return {"configured": False, "models": {}}
        if not benchmarks.has_cache():
            try:
                benchmarks.refresh()
            except Exception as e:  # noqa: BLE001
                logger.warning("AA 评测现场刷新失败: %s", e)
        out = {}
        # 只读缓存快照：`or models.ids()` 兜底会在缓存为空时把同步上游调用带回请求路径
        # （最长 20s 超时），正是概览页曾经专门规避的问题。缓存为空就返回空数据，
        # 由调度器启动/每日任务预热。
        for e in models.ids_cached():
            try:
                bb = benchmarks.map(e)
                if bb:
                    out[e] = bb
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

        # 积分预警（WebUI 顶部横幅）：余额占比低于阈值或积分即将到期
        try:
            threshold = float(db.get_settings().get("alert_threshold_percent", "10"))
        except (TypeError, ValueError):
            threshold = 10.0
        try:
            expiry_days = float(db.get_settings().get("alert_expiry_days", "3"))
        except (TypeError, ValueError):
            expiry_days = 3.0
        alerts = []
        total_cap = sum(float(a.get("credits_total") or 0) for a in accounts)
        if total_cap > 0 and remaining / total_cap * 100 < threshold:
            alerts.append({
                "level": "warning",
                "message": f"积分余额 {remaining:.0f}/{total_cap:.0f}（占 {remaining / total_cap * 100:.0f}%），低于预警阈值 {threshold:.0f}%",
            })
        now_ts = time.time()
        for a in accounts:
            exp = a.get("credits_expire_at")
            if exp and (a.get("credits_remaining") or 0) > 0:
                days = (float(exp) - now_ts) / 86400
                if 0 <= days <= expiry_days:
                    alerts.append({
                        "level": "info",
                        "message": f"账号 {a['uid'][:8]} 的积分将在 {days:.1f} 天后到期（剩余 {a.get('credits_remaining'):.0f}），请及时消耗",
                    })

        return {
            "accounts": accounts,
            # 概览是高频轮询端点：模型只读缓存快照，绝不触发上游网络请求
            #（缓存由调度器定期刷新，为空时显示 0，由模型页引导刷新）
            "models": models.ids_cached(),
            "usage": db.usage_summary(),
            # 概览高频轮询：只取元数据，不携带大体积 content，避免 payload 膨胀
            "recent": db.usage_recent(10, light=True),
            "prediction": {
                "remaining_credits": round(remaining, 2),
                "tokens_per_credit": round(blended, 1) if blended is not None else None,
                "predicted_tokens": round(predicted) if predicted is not None else None,
                "credits_used": round(stats["total_credits"], 2),
                "tokens_used": int(stats["total_tokens"]),
                "models": pm,
            },
            "alerts": alerts,
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
            "model_ttl_min": s.get("model_ttl_min", "60"),
            "aa_refresh_hour": s.get("aa_refresh_hour", "7"),
            "keepalive_hour": s.get("keepalive_hour", "22"),
            "keepalive_enabled": s.get("keepalive_enabled", "1"),
            # 不回明文 key（掩码用于前端展示），完整 key 只在保存时传入、存 DB 即可
            "aa_api_key_masked": _mask_secret(aa_key),
            "aa_enabled": bool(aa_key),
            # 积分预警
            "alert_enabled": s.get("alert_enabled", "0"),
            "alert_webhook_url": s.get("alert_webhook_url", ""),
            "alert_threshold_percent": s.get("alert_threshold_percent", "10"),
            "alert_expiry_days": s.get("alert_expiry_days", "3"),
            # 模型别名映射（每行一条：别名=真实模型）
            "model_aliases": s.get("model_aliases", ""),
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
        if "model_ttl_min" in body:
            try:
                ttl = int(body.get("model_ttl_min"))
                if ttl < 1 or ttl > 1440:
                    raise ValueError
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=400, detail={"error": {"message": "模型缓存 TTL 需为 1-1440 分钟"}})
            db.save_settings(model_ttl_min=str(ttl))
        if "aa_refresh_hour" in body:
            db.save_settings(aa_refresh_hour=_validate_hour_field(body.get("aa_refresh_hour"), "AA 评测刷新时间"))
        if keepalive_hour is not None:
            db.save_settings(keepalive_hour=_validate_hour_field(keepalive_hour, "token 保活时间"))
        if "keepalive_enabled" in body:
            val = str(body.get("keepalive_enabled") or "").strip()
            db.save_settings(keepalive_enabled="1" if val in ("1", "true", "on") else "0")
        if "alert_enabled" in body:
            val = str(body.get("alert_enabled") or "").strip().lower()
            db.save_settings(alert_enabled="1" if val in ("1", "true", "on") else "0")
        if "alert_webhook_url" in body:
            db.save_settings(alert_webhook_url=str(body.get("alert_webhook_url") or "").strip())
        if "alert_threshold_percent" in body:
            try:
                v = float(body.get("alert_threshold_percent"))
                if not 1 <= v <= 90:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={"error": {"message": "余额预警阈值需为 1-90 的数字"}})
            db.save_settings(alert_threshold_percent=str(v))
        if "alert_expiry_days" in body:
            try:
                v = float(body.get("alert_expiry_days"))
                if not 1 <= v <= 90:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={"error": {"message": "到期预警天数需为 1-90 的数字"}})
            db.save_settings(alert_expiry_days=str(v))
        if "model_aliases" in body:
            raw = str(body.get("model_aliases") or "")
            for ln in raw.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                alias, _, real = ln.partition("=")
                if not alias.strip() or not real.strip():
                    raise HTTPException(status_code=400,
                                        detail={"error": {"message": f"模型别名格式错误：{ln}（应为 别名=真实模型）"}})
            db.save_settings(model_aliases=raw)
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
        # 设置已存入 DB，调度器下个周期自动生效；响应结构与 GET 一致（不回明文 key）
        resp = dict(admin_get_settings())
        resp["ok"] = True
        return resp

    # ---------------- 账号管理：上传 auth 文件 / 扫码登录 ----------------

    @app.post("/admin/accounts/upload")
    async def admin_upload_auth(file: UploadFile = File(...)):
        """上传一份 CodeBuddy 原始 .info auth 文件并注册进账号池。"""
        MAX_AUTH_SIZE = 10 * 1024 * 1024  # 10MB 上限，防止内存耗尽攻击
        raw = await file.read(MAX_AUTH_SIZE + 1)
        if len(raw) > MAX_AUTH_SIZE:
            raise HTTPException(status_code=413, detail={"error": {"message": "auth 文件过大（>10MB）"}})
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
        # 落盘到 auths/ 目录（校验并格式化）；uid 属不可信输入，先做安全规整
        d = _auths_dir()
        path = d / f"workbuddy-{_safe_uid(uid)}.info"
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
        # oauth_poll 内含同步网络请求，放线程池避免阻塞事件循环
        result = await asyncio.to_thread(oauth_poll, state)
        if result.get("status") == "expired":
            # state 失效等不可恢复错误：明确告知前端，避免二维码永远转圈
            return {"status": "expired"}
        if result.get("status") != "ready":
            return {"status": "pending"}
        auth = result["auth"]
        account = result["account"]
        uid = account.get("uid") or "unknown"
        d = _auths_dir()
        path = d / f"workbuddy-{_safe_uid(uid)}.info"
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
                # 未设置 ADMIN_TOKEN：仅允许本机回环访问管理接口。
                # 兼容 Docker 端口映射：容器化后客户端 IP 是 Docker 网桥网关（如 172.x.x.1），
                # 不再等于 127.0.0.1。此时改以“Host 头为回环”为准（上方已校验 Host 只允许
                # localhost/127.0.0.1/::1，DNS rebinding 防护仍在）；真正的 LAN 直连 Host 会被
                # 上方拦截，需设 ADMIN_TOKEN 才能从局域网访问。
                client_host = (request.client.host if request.client else "") or ""
                if not (_is_loopback(client_host) or _is_loopback(host_name)):
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
        # 严格判定位于 dist 目录内（is_relative_to 避免 "dist-evil" 之类的兄弟目录前缀绕过）
        if f.is_file() and f.is_relative_to(_DIST_DIR):
            return FileResponse(f)
        # SPA fallback：仅无扩展名的路径回退 index.html（前端路由）；
        # 带扩展名的未知文件（如 /foo.js、/main.css）是真不存在，返回 404
        if "." not in Path(asset_path).name and (_DIST_DIR / "index.html").exists():
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
    # Anthropic SSE 规范要求错误事件带 `event: error` 头，只发裸 data 行时
    # Claude Code 等客户端可能不识别而一直挂起等待
    return ("event: error\ndata: " +
            json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}) + "\n\n")


def _json_error(status: int, message: str) -> str:
    return json.dumps({"error": {"message": message, "type": "upstream_error"}})





async def _with_keepalive(gen, interval: float = 15.0):
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


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def _estimate_tokens(text: str, n_msg: int) -> int:
    """本地粗估输入 token：CJK 约 1.5 字符/token，其它 4 字符/token，另加每条消息结构开销。

    刻意不调用上游（Claude Code 发正式请求前的预检，打上游又慢又耗配额）；
    英文经验公式 /4 对中文严重低估（实际约 1.5 字符/token），分段加权。
    """
    if not text:
        return 4
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk / 1.5) + int(other / 4) + n_msg * 4 + 4)


def _parse_model_aliases(raw: str) -> dict[str, str]:
    """解析设置里的模型别名映射（每行一条：别名=真实模型），非法行忽略。"""
    out: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        alias, _, real = line.partition("=")
        alias, real = alias.strip(), real.strip()
        if alias and real:
            out[alias] = real
    return out


def _safe_err(raw: bytes, status: int) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("error"):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"error": {"message": raw.decode("utf-8", "replace"), "type": "upstream_error"}}
