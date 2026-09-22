"""设置路由（/admin/settings）：读取/保存。响应不回明文 key。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from ..config import config
from ..region import REGIONS


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
    db, pool = ctx.db, ctx.pool

    def _region_counts() -> list[dict]:
        """账号池的区域分布：设置页要能让用户一眼看出"我现在有哪些区域的账号"。

        区域由账号 auth 文件里的 domain 决定，用户不能直接改——这里只做统计展示。
        """
        counts: dict[str, int] = {}
        for a in pool.all_accounts():
            rid = str(a.get("region") or "cn")
            counts[rid] = counts.get(rid, 0) + 1
        return [
            {"id": rid, "label": REGIONS[rid].label if rid in REGIONS else rid, "count": counts[rid]}
            for rid in sorted(counts)
        ]

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
            # 补签保连登（P3 试点）：总开关默认关，演练默认开（见 scheduler 同名方法）
            "makeup_enabled": s.get("makeup_enabled", "0"),
            "makeup_dry_run": s.get("makeup_dry_run", "1"),
            # 猫猫旅行（国内版专属）：总开关默认关，演练默认开（见 scheduler 同名方法）
            "travel_enabled": s.get("travel_enabled", "0"),
            "travel_dry_run": s.get("travel_dry_run", "1"),
            # 活跃地图每日提醒：默认**开**（只读 + 只提醒，不写上游、不花积分）
            "active_map_enabled": s.get("active_map_enabled", "1"),
            "active_map_hour": s.get("active_map_hour", "23"),
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
            # 系统提示词三模式（P2-1）：passthrough（默认，零改动）/ custom / append
            "prompt_mode": s.get("prompt_mode", "passthrough"),
            "prompt_text": s.get("prompt_text", ""),
            # 区域与网络：DB 值（用户设过的）与环境变量值分开返回——
            # 前端才能显示"你没设过，当前生效的是环境变量里的 XXX"，而不是误导成空。
            "regions": _region_counts(),
            "backend": s.get("backend", ""),
            "proxy": s.get("proxy", ""),
            "workbuddy_exe": s.get("workbuddy_exe", ""),
            # 在途并发上限（P1-1）：0 = 不限制。DB 值与环境变量值分开返回，
            # 前端才能显示"你没设过，当前生效的是环境变量里的 X"。
            "max_in_flight": s.get("max_in_flight", ""),
            "max_in_flight_global": s.get("max_in_flight_global", ""),
            "env_max_in_flight": str(config.max_in_flight),
            "env_max_in_flight_global": str(config.max_in_flight_global),
            "env_backend": config.backend,
            "env_proxy": config.proxy,
            "env_workbuddy_exe": config.workbuddy_exe,
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
        if "makeup_enabled" in body:
            val = str(body.get("makeup_enabled") or "").strip().lower()
            db.save_settings(makeup_enabled="1" if val in ("1", "true", "on") else "0")
        if "makeup_dry_run" in body:
            val = str(body.get("makeup_dry_run") or "").strip().lower()
            db.save_settings(makeup_dry_run="1" if val in ("1", "true", "on") else "0")
        if "travel_enabled" in body:
            val = str(body.get("travel_enabled") or "").strip().lower()
            db.save_settings(travel_enabled="1" if val in ("1", "true", "on") else "0")
        if "travel_dry_run" in body:
            val = str(body.get("travel_dry_run") or "").strip().lower()
            db.save_settings(travel_dry_run="1" if val in ("1", "true", "on") else "0")
        if "active_map_enabled" in body:
            val = str(body.get("active_map_enabled") or "").strip().lower()
            db.save_settings(active_map_enabled="1" if val in ("1", "true", "on") else "0")
        if "active_map_hour" in body:
            # 0~23；非法值直接丢弃（落进"空值=不改"分支），别写一个跑不起来的点
            try:
                h = int(body.get("active_map_hour"))
                if 0 <= h <= 23:
                    db.save_settings(active_map_hour=str(h))
            except (TypeError, ValueError):
                pass
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
        if "prompt_mode" in body:
            mode = str(body.get("prompt_mode") or "").strip().lower() or "passthrough"
            if mode not in ("passthrough", "custom", "append"):
                raise HTTPException(status_code=400, detail={"error": {"message":
                    "系统提示词模式需为 passthrough / custom / append"}})
            db.save_settings(prompt_mode=mode)
        if "prompt_text" in body:
            db.save_settings(prompt_text=str(body.get("prompt_text") or ""))
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
        if "backend" in body:
            v = str(body.get("backend") or "").strip().rstrip("/")
            if v and not v.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail={"error": {"message":
                    "BACKEND 需为 http:// 或 https:// 开头的地址；留空表示按账号区域自动选择（推荐）"}})
            db.save_settings(backend=v)
        if "proxy" in body:
            v = str(body.get("proxy") or "").strip()
            if v and not v.startswith(("http://", "https://", "socks5://", "socks5h://")):
                raise HTTPException(status_code=400, detail={"error": {"message":
                    "PROXY 需为 http:// / https:// / socks5:// 开头的地址；留空表示直连"}})
            db.save_settings(proxy=v)
        if "workbuddy_exe" in body:
            # 允许直接粘贴带引号的 Windows 路径（用户从资源管理器复制出来的样子）
            db.save_settings(workbuddy_exe=str(body.get("workbuddy_exe") or "").strip().strip('"'))
        for key, label in (("max_in_flight", "单账号在途并发上限"),
                           ("max_in_flight_global", "国际版在途并发上限")):
            if key not in body:
                continue
            v = str(body.get(key) or "").strip()
            if v == "":
                db.save_settings(**{key: ""})   # 留空 = 回落环境变量
                continue
            try:
                n = int(v)
                if n < 0 or n > 64:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={"error": {
                    "message": f"{label}需为 0-64 的整数（0 = 不限制；留空 = 用环境变量）"}})
            db.save_settings(**{key: str(n)})
        # 这几项是"每次调用现读"的（区域判定 / 出站客户端 / 解密探测），
        # 所以保存后立刻重载覆盖即可生效，不需要重启进程。
        config.load_overrides(db.get_settings())
        # 在途并发上限缓存在账号实例上（pick 持锁时不读配置），改完必须显式刷一次
        pool.apply_capacity_limits()
        # 设置已存入 DB，调度器下个周期自动生效；响应结构与 GET 一致（不回明文 key）
        resp = dict(admin_get_settings())
        resp["ok"] = True
        return resp
