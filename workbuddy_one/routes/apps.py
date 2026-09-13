"""应用 API Key 管理路由（/admin/apps*）。"""
from __future__ import annotations

import secrets

from fastapi import FastAPI, HTTPException

from .._crypto import encrypt as encrypt_key, decrypt as decrypt_key
from ..gateway.inference import hash_key


def _gen_api_key() -> str:
    return "sk-" + secrets.token_hex(16)


def register(app: FastAPI, ctx) -> None:
    db = ctx.db

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
        app_id = db.create_app(name=name, key_hash=hash_key(key), key_prefix=key[:9] + "…",
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
