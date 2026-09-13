"""设置路由（/admin/settings）：读取/保存。响应不回明文 key。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request


def _mask_secret(s: str) -> str:
    """掩码密钥：保留前 4 位 + 末 4 位，中间用 *。"""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * (len(s) - 8)}{s[-4:]}"


def _validate_hour_field(value, field_name: str) -> str:
    try:
        v = int(value)
        if 0 <= v <= 23:
            return str(v)
    except (TypeError, ValueError):
        pass
    raise HTTPException(status_code=400, detail={"error": {"message": f"{field_name} 需为 0-23 的小时"}})


def register(app: FastAPI, ctx) -> None:
    db = ctx.db

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
