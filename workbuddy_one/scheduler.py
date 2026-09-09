"""后台调度：每日自动签到 + 定期额度刷新 + 模型目录刷新 + token 保活。

单用户简化：使用 asyncio 后台任务，每日在配置时间签到，定期刷新各账号额度，
定时刷新模型目录与 token（保活）。所有时间配置从数据库 settings 动态读取，改动即时生效。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from . import billing
from .pool import AccountPool

logger = logging.getLogger("workbuddy_one.scheduler")


def _parse_hours(s: str) -> tuple[int, ...]:
    parts = [p.strip() for p in str(s).split(",") if p.strip()]
    try:
        return tuple(sorted({int(p) for p in parts})) if parts else (9, 21)
    except ValueError:
        return (9, 21)


def _parse_hour(s: str, default: int) -> int:
    try:
        v = int(str(s).strip())
        if 0 <= v <= 23:
            return v
    except (TypeError, ValueError):
        pass
    return default


class Scheduler:
    def __init__(self, pool: AccountPool, *, db=None, models=None, credit_interval_min: int = 30):
        self.pool = pool
        self.db = db
        self.models = models
        self.credit_interval_min = credit_interval_min
        self._last_checkin_date: str | None = None
        self._last_keepalive_date: str | None = None
        self._last_model_refresh_date: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        # 启动时：刷新额度 + 模型目录
        await self.refresh_credits()
        if self.models:
            try:
                await asyncio.to_thread(self.models.refresh)
            except Exception:  # noqa: BLE001
                pass

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def _setting(self, key: str, default: str) -> str:
        if self.db:
            try:
                return self.db.get_settings().get(key, default)
            except Exception:  # noqa: BLE001
                pass
        return default

    def _checkin_hours(self) -> tuple[int, ...]:
        return _parse_hours(self._setting("checkin_hours", "9,21"))

    def _credit_interval(self) -> int:
        try:
            return max(1, int(self._setting("credit_refresh_min", "30")))
        except (TypeError, ValueError):
            return self.credit_interval_min

    def _model_refresh_hour(self) -> int:
        return _parse_hour(self._setting("model_refresh_hour", "6"), 6)

    def _keepalive_hour(self) -> int:
        return _parse_hour(self._setting("keepalive_hour", "22"), 22)

    def checked_in_today(self) -> set[str]:
        """返回今日已签到的账号 uid 集合（本地记录判定）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.db:
            return set()
        try:
            dates = self.db.checkin_dates()
        except Exception:  # noqa: BLE001
            return set()
        return {uid for uid, d in dates.items() if d == today}

    async def refresh_credits(self):
        for acc in self.pool.accounts:
            try:
                res = await billing.fetch_credits(acc.mgr)
                self.pool.set_credits(acc.uid, res["remain"], res["total"])
                # 持久化最近额度到 DB，进程重启后可恢复（避免额度盲区）
                if self.db:
                    self.db.set_account_state(acc.uid, credits_remaining=res["remain"], credits_total=res["total"])
                # 余额 > 0 的冷却账号自动解冻（参考 Sliverkiss ReenableIfCredits）
                if res["remain"] > 0:
                    self.pool.clear_cooldown(acc.uid, enabled=True)
                logger.info("额度 %s: remain=%s total=%s", acc.uid, res["remain"], res["total"])
            except Exception as e:  # noqa: BLE001
                logger.warning("额度查询失败 %s: %s", acc.uid, e)

    async def do_checkin(self, skip: set[str] | None = None):
        """执行签到。skip 为已签到账号集合，默认取本地记录中今日已签到的账号。

        已签到的账号不会重复请求上游（避免重复扣积分/重复奖励）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if skip is None:
            skip = self.checked_in_today()
        results = []
        for acc in self.pool.accounts:
            if acc.uid in skip:
                logger.info("签到跳过 %s（今日已签到）", acc.uid)
                results.append((acc.uid, {"ok": False, "message": "今日已签到", "already": True}))
                continue
            try:
                res = await billing.daily_checkin(acc.mgr)
                results.append((acc.uid, res))
                logger.info("签到 %s: %s", acc.uid, res)
                # 无论成功或已签到，都记为当日已签到（避免重复请求上游）
                if self.db:
                    self.db.set_checkin_date(acc.uid, today)
            except Exception as e:  # noqa: BLE001
                logger.warning("签到失败 %s: %s", acc.uid, e)
                results.append((acc.uid, {"ok": False, "message": str(e)}))
        self._last_checkin_date = today
        # 签到后刷新额度（含自动解冻）
        await self.refresh_credits()
        return results

    async def do_keepalive(self):
        """强制刷新所有账号 token；session 失效的账号自动禁用。"""
        today = datetime.now().strftime("%Y-%m-%d")
        for acc in self.pool.accounts:
            if not acc.enabled:
                continue
            ok = await asyncio.to_thread(acc.mgr.keepalive)
            if ok:
                logger.info("token 保活 %s: ok", acc.uid)
            else:
                logger.warning("token 保活 %s: 失败（session 可能失效），自动禁用", acc.uid)
                self.pool.set_enabled(acc.uid, False)
        self._last_keepalive_date = today

    async def refresh_models(self):
        if not self.models:
            return
        try:
            await asyncio.to_thread(self.models.refresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("模型刷新异常: %s", e)

    async def _run(self):
        last_credit = 0.0
        while self._running:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hours = self._checkin_hours()
            # 每日签到：到点且今天未签
            if now.hour in hours and self._last_checkin_date != today:
                try:
                    await self.do_checkin()
                except Exception as e:  # noqa: BLE001
                    logger.warning("签到任务异常: %s", e)
            # 每日 token 保活
            if now.hour == self._keepalive_hour() and self._last_keepalive_date != today:
                try:
                    await self.do_keepalive()
                except Exception as e:  # noqa: BLE001
                    logger.warning("保活任务异常: %s", e)
            # 每日模型刷新
            if now.hour == self._model_refresh_hour() and self._last_model_refresh_date != today:
                try:
                    await self.refresh_models()
                except Exception as e:  # noqa: BLE001
                    logger.warning("模型刷新任务异常: %s", e)
            # 定期刷新额度
            interval = self._credit_interval()
            if now.timestamp() - last_credit >= interval * 60:
                try:
                    await self.refresh_credits()
                except Exception as e:  # noqa: BLE001
                    logger.warning("额度刷新异常: %s", e)
                last_credit = now.timestamp()
            await asyncio.sleep(60)
