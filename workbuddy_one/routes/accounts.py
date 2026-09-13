"""账号管理路由：列表/启停/优先级/删除、上传 auth 文件、扫码 OAuth。"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import qrcode
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import Response
from qrcode.image.svg import SvgPathImage

from ..config import PACKAGE_ROOT
from ..credentials import CredentialManager
from ..oauth import oauth_begin, oauth_poll, OAuthError


def _auths_dir() -> Path:
    d = PACKAGE_ROOT / "auths"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_uid(uid: str) -> str:
    """把任意 uid 规整为安全的文件名片段，防止路径穿越/非法字符。

    uid 来自用户上传的 JSON 或扫码结果，属不可信输入；只保留字母数字、连字符、下划线。
    非法则退化为 'unknown'，保证落盘路径始终限定在 auths/ 目录内。
    """
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", str(uid or "")).strip("-")
    return safe or "unknown"


def accounts_with_checkin(ctx) -> list[dict]:
    """账号列表 + 本地签到状态（今日是否已签到）。"""
    checked = ctx.scheduler.checked_in_today()
    accounts = ctx.pool.all_accounts()
    for a in accounts:
        a["checkin_today"] = a["uid"] in checked
    return accounts


def register(app: FastAPI, ctx) -> None:
    db, pool, scheduler = ctx.db, ctx.pool, ctx.scheduler

    def _register_account(path: Path) -> dict:
        """把一份 auth 文件注册进账号池（含 DB 落账）。返回账号摘要。"""
        mgr = CredentialManager(path)
        summary = mgr.summary()
        if not summary.get("uid"):
            raise HTTPException(status_code=400, detail={"error": {"message": "auth 文件中缺少 uid"}})
        uid = summary["uid"]
        added = pool.add_account(uid, mgr)
        db.upsert_account({"path": str(path)}, summary)
        ctx.managers[uid] = mgr
        return {"uid": uid, "added": added, **summary}

    def _remove_account(uid: str) -> dict:
        """从账号池、DB 移除账号；若其 auth 文件在项目 auths/ 下则一并删除。"""
        removed = pool.remove_account(uid)
        ctx.managers.pop(uid, None)
        db.delete_account(uid)
        ctx.limiters.pop(uid, None)  # 释放账号级限速器，避免字典无限增长
        # 删除项目内 auths/ 下的对应文件（本机 CodeBuddy 目录的保留，不删）
        d = _auths_dir()
        for f in d.glob(f"*{uid}*.info"):
            try:
                f.unlink()
            except OSError:
                pass
        return {"ok": True, "removed": removed, "uid": uid}

    @app.get("/admin/accounts")
    def admin_accounts():
        return {"accounts": accounts_with_checkin(ctx)}

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

    @app.post("/admin/credits/refresh")
    async def admin_refresh_credits():
        await scheduler.refresh_credits()
        return {"ok": True, "accounts": accounts_with_checkin(ctx)}

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
        import io
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")
