"""概览聚合路由（/admin/overview）：高频轮询端点，只读缓存，绝不上游网络请求。"""
from __future__ import annotations

import time

from fastapi import FastAPI

from .accounts import accounts_with_checkin


def register(app: FastAPI, ctx) -> None:
    db, models = ctx.db, ctx.models

    @app.get("/admin/overview")
    def admin_overview():
        accounts = accounts_with_checkin(ctx)
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

        # 活跃地图未点亮（今天还没在官方客户端聊过 → **活跃连登今天会断**）。
        #
        # 数据来自 scheduler 的**只读快照**，本端点绝不因此打上游 —— 概览是高频轮询
        # 端点，在这里查 heatmap 等于每秒往上游打几十个请求（见
        # `scheduler.active_map_snapshot` 的说明）。
        #
        # 这条横幅是**没有配 webhook 时唯一能看到提醒的地方**：`do_active_map_check`
        # 的 webhook 只在 `alert_webhook_url` 非空时才推，而容器日志用户不会去看。
        am = ctx.scheduler.active_map_snapshot()
        if am:
            unlit = am.get("unlit") or []
            unknown = am.get("unknown") or []
            if unlit:
                who = "、".join(f"{u['uid'][:8]}（{u['region_label']}）" for u in unlit[:3])
                if len(unlit) > 3:
                    who += f" 等 {len(unlit)} 个"
                alerts.append({
                    "level": "warning",
                    "message": (
                        f"今天（{am['date']}）有 {len(unlit)} 个账号没有点亮活跃地图，"
                        f"活跃连登今天会断档：{who}。"
                        f"到官方客户端聊一句即可点亮（当天 24:00 前有效）——"
                        f"注意本网关的对话请求点不亮它，只有官方客户端会。"
                    ),
                })
            if unknown:
                # 单独一条、用 `info` 而不是 `warning`：**这是"没查出来"，不是"没点亮"**。
                # 两者都用红色的话，用户一眼分不清哪条是确认的问题、哪条只是不确定。
                # 但必须显示 —— 一次 TLS 抖动不能让某个账号静默失去保护。
                who = "、".join(f"{u['uid'][:8]}（{u['region_label']}）" for u in unknown[:3])
                if len(unknown) > 3:
                    who += f" 等 {len(unknown)} 个"
                alerts.append({
                    "level": "info",
                    "message": (
                        f"另有 {len(unknown)} 个账号的活跃地图查询失败，"
                        f"无法判断今天是否点亮：{who}。"
                        f"保险起见也去官方客户端聊一句（当天 24:00 前有效）。"
                    ),
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
