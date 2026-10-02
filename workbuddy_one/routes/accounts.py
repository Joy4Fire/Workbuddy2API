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
    """账号列表 + 今日签到状态。

    `checkin_today` **优先用上游真值**（必须是今天同步过的），未同步时才回落本地
    记账，并用 `checkin_source` 标明来源。为什么不让前端自己判断：跨零点后旧的上游
    快照会失效，这个"是否新鲜"的判定只该有一处实现。

    `checkin_active` 为 False 表示本期签到活动未开启 —— 此时 `checkin_today=False`
    是"签不了"而不是"用户没签"，前端必须分开显示，否则会变成一个说不清的「未签到」。
    """
    snapshot = ctx.scheduler.checkin_snapshot()
    accounts = ctx.pool.all_accounts()
    for a in accounts:
        st = snapshot.get(a["uid"])
        if st:
            a["checkin_today"] = st["today"]
            a["checkin_active"] = st["active"]
            a["checkin_synced_at"] = st["synced_at"]
            a["checkin_source"] = st["source"]
        else:
            a["checkin_source"] = "local"
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
        # 重新登录 = 登录态确实恢复了：清掉系统自动禁用位（session 失效/被上游封禁
        # 这两类自动禁用都靠重新扫码解决）。这是唯一会自动解除自动禁用的路径。
        if not added:
            pool.enable_auto(uid)
            db.set_account_state(uid, auto_disabled_reason="")
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
        # 列表只读快照；签到同步由后台预热/刷新执行，不能让页面轮询等待上游。
        return {"accounts": accounts_with_checkin(ctx)}

    @app.post("/admin/accounts/{uid}/enable")
    def admin_enable(uid: str, body: dict | None = None):
        """启用账号（清除**手动停用**位）。

        默认**不**清除系统自动禁用位（session 失效/被上游封禁）——否则用户点一下
        "启用"就会把一个登录态已废的账号放回池子，下一个请求立刻再吃一次同样的错，
        看起来像"点了没用"。确要强制放回时传 `{"force": true}`（前端会先弹确认）。
        """
        force = bool((body or {}).get("force"))
        pool.set_enabled(uid, True)  # 启用时清空手动停用原因
        fields = {"enabled": 1, "disabled_reason": ""}
        if force:
            pool.enable_auto(uid)
            fields["auto_disabled_reason"] = ""
        db.set_account_state(uid, **fields)
        # 回传仍存在的自动禁用原因，前端据此提示"手动位已清，但系统位还在"
        still = next((a.auto_disabled_reason for a in pool.accounts if a.uid == uid), "")
        return {"ok": True, "auto_disabled_reason": "" if force else still}

    @app.post("/admin/accounts/{uid}/disable")
    def admin_disable(uid: str):
        pool.set_enabled(uid, False, reason="手动停用")
        db.set_account_state(uid, enabled=0, disabled_reason="手动停用")
        return {"ok": True}

    @app.post("/admin/accounts/{uid}/clear-auto-disable")
    def admin_clear_auto_disable(uid: str):
        """只清系统自动禁用位（不动手动停用位）。

        与 enable 分开的意义：一个账号可能同时被"运维手动停用"和"系统自动禁用"，
        用户可能只想解除其中一个。两个位独立清除，语义才不互相污染。
        """
        pool.enable_auto(uid)
        db.set_account_state(uid, auto_disabled_reason="")
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
        # 已签到 / 活动未开启的账号由 do_checkin 自行跳过（按上游真值优先判定），
        # 避免重复请求上游。skipped 直接由结果反推，保证与界面显示同一口径。
        results = await scheduler.do_checkin()
        return {
            "ok": True,
            "results": [{"uid": u, **r} for u, r in results],
            "skipped": sorted(u for u, r in results if r.get("already") or r.get("inactive")),
            "accounts": accounts_with_checkin(ctx),
        }

    @app.get("/admin/streak")
    async def admin_streak():
        """查各账号的**活跃连登**天数、补签卡余额与**连登档位领奖状态**（只读）。

        这是**唯一**能让用户在界面上看到"补签判据"的地方：演练模式的日志在容器
        里，不开这个入口就没法确认判据对不对。

        注意这里报的是**活跃连登**（growth 域，每天有对话才续上），与账号页的
        **签到连登**是两条独立的计数——补签卡属于 growth 域，补的就是这一条。
        详见 `billing.py` 末尾的实测说明。

        `redemption` 是连登档位奖励（7d/14d/28d 每档每月一次，各送 1 张补签卡）：
        **它是补签卡唯一的常规来源**，所以这个字段是「补签功能为什么用不了」的答案
        （卡为 0 时看档位是否还 locked 就知道是"还没到 7 天"而不是"功能坏了"）。

        `travel` 是猫猫旅行状态（**国内版专属**，国际版恒为 `None`）：这是用户在界面上
        看"猫派出去了没有 / 有没有到站可领"的唯一入口，也是演练模式的判据确认窗口。

        三个上游查询**必须并发**：各自 `timeout=20`，串行时一个账号最坏 60s；
        账号一多就顶穿前端 30s 超时，界面只能看到一个错误提示、连原因都拿不到。
        读是幂等的，并发无副作用。
        """
        from .. import billing, region

        async def one(acc):
            if acc.mgr is None:
                return {"uid": acc.uid, "error": "无凭据"}
            try:
                st, heat = await asyncio.gather(
                    asyncio.to_thread(billing.fetch_streak, acc.mgr),
                    asyncio.to_thread(billing.fetch_heatmap, acc.mgr),
                )
            except Exception as e:  # noqa: BLE001
                return {"uid": acc.uid, "error": str(e)}
            # 抽奖次数单独取、失败只记 None：它是锦上添花，不该把整个连登查询带崩
            # （国际版抽奖根本没开，更没必要让它成为硬依赖）。
            try:
                chances = await asyncio.to_thread(billing.fetch_lottery_chances, acc.mgr)
            except Exception:  # noqa: BLE001
                chances = None
            # 猫猫旅行状态：**只有国内版有这套体系**，国际版直接标 None 少打一个
            # 必然返回空 data 的请求。同样是锦上添花，失败只记 None。
            travel = None
            if region.region_of_account(acc).id == "cn":
                try:
                    travel = await asyncio.to_thread(billing.fetch_travel_status, acc.mgr)
                except Exception:  # noqa: BLE001
                    travel = None
            yesterday = billing.growth_yesterday()
            today = billing.growth_today()
            return {
                "uid": acc.uid,
                "streak_days": st["days"],
                "month_total_days": st["month_total_days"],
                "makeup_balance": st["makeup_balance"],
                "makeup_max": st["makeup_max"],
                "makeup_dates": st["makeup_dates"],
                "yesterday": yesterday,
                "yesterday_score": heat.get(yesterday),
                # 今天是否已点亮活跃地图。heatmap 本来就已经拉过了，顺手带出来——
                # 这是用户唯一能"看到今天到底亮没亮"的地方（`do_active_map_check`
                # 只推 webhook，界面看不到）。**`None` = 地图里没有今天这一格**，
                # 与 `0`（有格子但没活跃）严格分开：上游换了窗口口径时前者会出现。
                "heat_today": heat.get(today),
                # 跨月判定必须走 billing.makeup_allowed()：这里原本写的是
                # `yesterday[:7] == billing.growth_yesterday()[:7]`——同一个函数调两次
                # 比较月份，**恒为 True**，界面上的「跨月」分支从来没显示过。
                "makeup_allowed": billing.makeup_allowed(),
                # 连登档位奖励（补签卡的唯一常规来源）与抽奖次数，供界面显示与领取。
                "redemption": st["redemption"],
                "lottery_chances": chances,
                # 猫猫旅行状态（国内版专属；国际版恒为 None）。
                "travel": travel,
            }

        rows = await asyncio.gather(*(one(a) for a in pool.accounts))
        return {"ok": True, "accounts": list(rows)}

    @app.post("/admin/redeem")
    async def admin_redeem(body: dict | None = None):
        """手动领取连登档位奖励（7d/14d/28d），可选顺带抽奖。

        `dry_run` 默认 True，与 `/admin/makeup` 一致：先看清"会领哪几档、各发多少"，
        再显式传 false 真领。

        与补签的区别：**领奖不消耗任何东西**（补签要花卡），且同月重复领同一档位
        上游返回 409 duplicate，是幂等无副作用的。所以这里默认演练纯粹是为了让用户
        先确认判据，不是出于风险考虑。
        """
        dry = True if body is None else bool(body.get("dry_run", True))
        draw = False if body is None else bool(body.get("draw", False))
        results = await scheduler.do_redeem(dry_run=dry, draw=draw)
        return {"ok": True, "dry_run": dry, "draw": draw, "results": results}

    @app.post("/admin/makeup")
    async def admin_makeup(body: dict | None = None):
        """手动触发一次补签检查。

        `dry_run` 默认 True —— 手动点一下也走演练，真要补必须显式传 false。
        默认演练的理由：补签花的是**用户自己的卡**，不能因为"点错了按钮"就花掉。
        （早先还担心过"判据链未验证"，那个疑问已于 2026-09-22 查清：heatmap 的 score
        正是活跃连登的直接度量，判据本身没问题——详见 `billing.py` 末尾的说明。）
        """
        dry = True if body is None else bool(body.get("dry_run", True))
        results = await scheduler.do_makeup(dry_run=dry)
        return {"ok": True, "dry_run": dry, "results": results}

    @app.post("/admin/travel")
    async def admin_travel(body: dict | None = None):
        """手动触发一次猫猫旅行巡检（无猫→领养 / idle→派出 / arrived→领取）。

        **只有国内版账号会被处理**：国际版没有这套体系（`travel/config` 返回空 `data`），
        调度层按区域过滤，结果里根本不会出现国际版账号——前端别把"没出现"当失败。

        `dry_run` 默认 True，与 `/admin/makeup`、`/admin/redeem` 一致：先看清"会做什么"
        再显式传 false。与补签不同，旅行**不消耗任何东西**（纯收益，每天 5~10 积分），
        默认演练纯粹是为了先确认状态机判断正确。
        """
        dry = True if body is None else bool(body.get("dry_run", True))
        results = await scheduler.do_travel(dry_run=dry)
        return {"ok": True, "dry_run": dry, "results": results}

    @app.post("/admin/active-map/check")
    async def admin_active_map_check(body: dict | None = None):
        """手动检查每个账号**今天**有没有点亮活跃地图。

        **只读**：只查 `growth/heatmap` 与 `growth/streak`，不写上游、不花积分，
        也不代发任何对话（2026-09-22 实测证明 chat API 点不亮地图，详见
        `scheduler.do_active_map_check` 的说明）。所以这里**没有 `dry_run`**——
        与补签/旅行不同，它本身就没有"会改状态"的分支。

        `notify` 默认 **False**：手动点一次就推一条 webhook 是骚扰；只有定时任务
        才该通知。需要连通知一起验证（比如刚配好 webhook）再显式传 true。
        """
        notify = False if body is None else bool(body.get("notify", False))
        results = await scheduler.do_active_map_check(notify=notify)
        return {"ok": True, "notify": notify, "results": results}

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
    def admin_oauth_start(region: str | None = None):
        """发起扫码登录，返回 {state, authUrl, region} 用于前端展示二维码。

        region 可选 "cn" / "global"（缺省 cn）：国际版账号必须打到国际站控制面，
        否则拿到的 authUrl 域名不对、登录回来的凭据不可用。
        """
        try:
            return {"ok": True, **oauth_begin(region_id=region)}
        except OAuthError as e:
            raise HTTPException(status_code=502, detail={"error": {"message": str(e)}})

    @app.get("/admin/oauth/status")
    async def admin_oauth_status(state: str, region: str | None = None):
        """轮询扫码登录状态；ready 时自动落盘并注册账号。

        region 必须与 /admin/oauth/start 时一致（前端回传 start 返回的 region）。
        """
        # oauth_poll 内含同步网络请求，放线程池避免阻塞事件循环
        result = await asyncio.to_thread(oauth_poll, state, region)
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
