"""后台调度：每日自动签到 + 定期额度刷新 + 模型目录刷新 + token 保活。

单用户简化：使用 asyncio 后台任务，每日在配置时间签到，定期刷新各账号额度，
定时刷新模型目录与 token（保活）。所有时间配置从数据库 settings 动态读取，改动即时生效。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import billing
from .pool import AccountPool

logger = logging.getLogger("workbuddy_one.scheduler")

# 附件归档目录（与 app.py 的 ATTACH_DIR 同一位置）：归档只增不减，由每日清理任务
# 按使用记录保留期一并清理。此处独立定义路径，避免 scheduler→app 循环导入。
_ATTACH_DIR = Path(__file__).resolve().parent.parent / "data" / "attachments"


def cleanup_attachments(retention_days: int) -> int:
    """删除 mtime 早于保留期的归档附件，返回删除数量。

    归档附件的生命周期与使用记录一致：记录删了，附件留着也无人引用。
    """
    if not _ATTACH_DIR.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in _ATTACH_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


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
    def __init__(self, pool: AccountPool, *, db=None, models=None, benchmarks=None, credit_interval_min: int = 30,
                 usage_retention_days: int = 90):
        self.pool = pool
        self.db = db
        self.models = models
        self.benchmarks = benchmarks
        self.credit_interval_min = credit_interval_min
        self.usage_retention_days = max(1, int(usage_retention_days))
        self._last_checkin_date: str | None = None
        self._last_keepalive_date: str | None = None
        self._last_model_refresh_date: str | None = None
        self._last_aa_refresh_date: str | None = None
        self._last_cleanup_date: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        # 连续保活失败计数（uid → 次数）：避免偶发网络抖动一次就永久禁用账号
        self._keepalive_fails: dict[str, int] = {}
        # 保活连续失败阈值：达到才视为 session 失效并禁用账号
        self._keepalive_fail_threshold = 3
        # 签到失败重试：失败后间隔 2 小时重试，最多 3 次，避免当天积分白丢
        self._checkin_fail_count = 0
        self._checkin_retry_at = 0.0
        # 每周全量备份的节奏（上次备份时间；重启后从磁盘最近一份周备份恢复节奏）
        self._last_backup_dt: datetime | None = None
        # 积分预警去重：同一警报 6 小时内只推送一次
        self._last_alert_at = 0.0

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        # 启动时：刷新额度 + 模型目录 + AA 评测（全部预热，页面首开即有数据）
        await self.refresh_credits()
        if self.models:
            try:
                await asyncio.to_thread(self.models.refresh)
            except Exception:  # noqa: BLE001
                pass
        if self.benchmarks and self.benchmarks.configured() and not self.benchmarks.has_cache():
            try:
                await asyncio.to_thread(self.benchmarks.refresh)
            except Exception:  # noqa: BLE001
                pass
        # 启动时做一次存量瘦身：早期版本曾全量重复入库，可能残留超大 content（单条 30 万+ 字符）
        if self.db:
            try:
                await asyncio.to_thread(self.db.trim_usage_content)
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

    def _aa_refresh_hour(self) -> int:
        return _parse_hour(self._setting("aa_refresh_hour", "7"), 7)

    def _keepalive_hour(self) -> int:
        return _parse_hour(self._setting("keepalive_hour", "22"), 22)

    def _keepalive_enabled(self) -> bool:
        return self._setting("keepalive_enabled", "1") == "1"

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
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
            try:
                res = await billing.fetch_credits(acc.mgr)
                self.pool.set_credits(acc.uid, res["remain"], res["total"], res.get("expire_at"),
                                      packages=res.get("packages"))
                # 持久化最近额度到 DB，进程重启后可恢复（避免额度盲区）
                if self.db:
                    self.db.set_account_state(acc.uid, credits_remaining=res["remain"],
                                              credits_total=res["total"],
                                              credits_expire_at=res.get("expire_at"))
                # 余额 > 0 的冷却账号自动解冻（参考 Sliverkiss ReenableIfCredits）
                if res["remain"] > 0:
                    self.pool.clear_cooldown(acc.uid)
                logger.info("额度 %s: remain=%s total=%s expire=%s", acc.uid, res["remain"],
                            res["total"], res.get("expire_at"))
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
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
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
        # 注意：不再在这里置位 _last_checkin_date——由 _run 根据结果决定
        # （全部成功/已签到才置位，存在失败则按重试策略补签），手动签到不受影响
        # 签到后刷新额度（含自动解冻）
        await self.refresh_credits()
        return results

    async def do_keepalive(self):
        """强制刷新所有账号 token；连续多次失败（session 可能失效）才自动禁用。

        偶发单次失败仅计数，不立即禁用，避免网络抖动导致账号"莫名被禁用"；
        成功一次即重置计数，并在账号是被保活禁用的情况下自动恢复启用。
        """
        if not self._keepalive_enabled():
            logger.info("保活已由用户关闭（keepalive_enabled=0），跳过")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
            if not acc.enabled:
                continue
            ok = await asyncio.to_thread(acc.mgr.keepalive)
            if ok:
                self._keepalive_fails.pop(acc.uid, None)  # 重置失败计数
                logger.info("token 保活 %s: ok", acc.uid)
            else:
                fails = self._keepalive_fails.get(acc.uid, 0) + 1
                self._keepalive_fails[acc.uid] = fails
                if fails >= self._keepalive_fail_threshold:
                    logger.error("token 保活 %s: 连续 %d 次失败（session 可能失效），自动禁用", acc.uid, fails)
                    self.pool.set_enabled(acc.uid, False, reason="保活连续失败（session 可能失效），请重新扫码登录")
                    # 同步落库：否则重启后账号"复活"，坏 session 继续打上游
                    if self.db:
                        self.db.set_account_state(acc.uid, enabled=0,
                                                  disabled_reason="保活连续失败（session 可能失效），请重新扫码登录")
                    self._keepalive_fails[acc.uid] = 0  # 禁用后重置，等待用户重新登录
                else:
                    logger.warning("token 保活 %s: 失败 %d/%d 次（暂不禁用）", acc.uid, fails,
                                   self._keepalive_fail_threshold)
        self._last_keepalive_date = today

    async def refresh_models(self):
        if not self.models:
            return
        try:
            await asyncio.to_thread(self.models.refresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("模型刷新异常: %s", e)

    async def refresh_benchmarks(self):
        """每日刷新 AA 评测数据（未配置 key 时静默跳过）。"""
        if not self.benchmarks:
            return
        if not self.benchmarks.configured():
            logger.info("未配置 AA key，跳过评测刷新")
            return
        try:
            await asyncio.to_thread(self.benchmarks.refresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("AA 评测刷新异常: %s", e)

    async def cleanup_usage(self):
        """每日清理过期使用记录，防止 DB 无限膨胀。

        历史 content（含 base64 图片等大文本）体积增长很快，超期删除能控制单库体积；
        顺带 VACUUM 回收删除/增删产生的空闲页，把文件体积压回真实数据量。
        同时清理 data/attachments 附件归档（只增不减，与使用记录同一保留期）。
        """
        if not self.db:
            return
        try:
            changed = await asyncio.to_thread(
                self.db.cleanup_usage, self.usage_retention_days, True)
            logger.info("使用记录清理完成（保留 %d 天，含 VACUUM）", self.usage_retention_days)
        except Exception as e:  # noqa: BLE001
            logger.warning("使用记录清理异常: %s", e)
            return None
        try:
            removed = await asyncio.to_thread(cleanup_attachments, self.usage_retention_days)
            if removed:
                logger.info("附件清理完成：删除 %d 个超期归档附件", removed)
        except Exception as e:  # noqa: BLE001
            logger.warning("附件清理异常: %s", e)
        return changed

    def _backup_dir(self) -> Path:
        return self.db.path.parent / "backups"

    def _weekly_backup(self):
        """执行一次每周全量备份（backup API 快照），保留最近 4 份。"""
        bdir = self._backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        name = f"workbuddy.db.weekly.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        self.db.backup_to(str(bdir / name))
        backups = sorted(bdir.glob("workbuddy.db.weekly.*.bak"))
        for old in backups[:-4]:
            try:
                old.unlink()
            except OSError:
                pass
        self._last_backup_dt = datetime.now()
        logger.info("每周备份完成: %s", name)

    def _backup_due(self, now: datetime) -> bool:
        """距上次全量备份 ≥7 天则到期。重启后从磁盘最近一份周备份恢复节奏。"""
        if not self.db:
            return False
        if self._last_backup_dt is None:
            bdir = self._backup_dir()
            weekly = sorted(bdir.glob("workbuddy.db.weekly.*.bak")) if bdir.is_dir() else []
            if weekly:
                self._last_backup_dt = datetime.fromtimestamp(weekly[-1].stat().st_mtime)
        if self._last_backup_dt is None:
            return True
        return (now - self._last_backup_dt).total_seconds() >= 7 * 86400

    async def _check_credit_alert(self):
        """积分预警：余额占比低于阈值或积分即将到期时推送 webhook。

        6 小时内不重复推送（警报持续时每 6 小时提醒一次，解除后重置）。
        """
        if self._setting("alert_enabled", "0") != "1":
            return
        url = self._setting("alert_webhook_url", "").strip()
        if not url:
            return
        try:
            threshold = float(self._setting("alert_threshold_percent", "10"))
        except (TypeError, ValueError):
            threshold = 10.0
        try:
            expiry_days = float(self._setting("alert_expiry_days", "3"))
        except (TypeError, ValueError):
            expiry_days = 3.0
        accounts = self.pool.all_accounts()
        remain = sum(float(a.get("credits_remaining") or 0) for a in accounts)
        cap = sum(float(a.get("credits_total") or 0) for a in accounts)
        messages: list[str] = []
        now_ts = time.time()
        if cap > 0 and remain / cap * 100 < threshold:
            messages.append(f"积分余额 {remain:.0f}/{cap:.0f}（占 {remain / cap * 100:.0f}%），低于预警阈值 {threshold:.0f}%")
        for a in accounts:
            exp = a.get("credits_expire_at")
            if exp and (a.get("credits_remaining") or 0) > 0:
                days = (float(exp) - now_ts) / 86400
                if 0 <= days <= expiry_days:
                    messages.append(f"账号 {a['uid'][:8]} 的积分将在 {days:.1f} 天后到期，请及时消耗")
        if not messages:
            self._last_alert_at = 0.0  # 警报解除，重置去重窗口
            return
        if now_ts - self._last_alert_at < 6 * 3600:
            return
        ok = await self._send_webhook(url, "Workbuddy2API 积分预警", "\n".join(messages))
        if ok:
            self._last_alert_at = now_ts
            logger.info("积分预警已推送: %s", "；".join(messages))

    async def _send_webhook(self, url: str, title: str, body: str) -> bool:
        """按 URL 自动识别 webhook 平台（企微/飞书/Bark/通用 JSON）。失败只记日志。"""
        try:
            if "qyapi.weixin.qq.com" in url:
                payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
            elif "open.feishu.cn" in url:
                payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
            else:  # Bark 与通用 JSON webhook 均接受 {"title","body"}
                payload = {"title": title, "body": body}
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("webhook 推送失败 HTTP %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("webhook 推送异常: %s", e)
            return False

    async def _run(self):
        last_credit = 0.0
        while self._running:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hours = self._checkin_hours()
            # 每日签到：追赶语义——只要已过当天最早的签到点且今天未签就执行，
            # 服务在签到窗口之后才启动（本机开发常态）也能补上当天签到；
            # do_checkin 内部按 DB 记录跳过已签账号。
            # 失败重试：存在失败时每 2 小时补签一次，最多 3 次，避免当天积分白丢
            if now.hour >= min(hours) and self._last_checkin_date != today and now.timestamp() >= self._checkin_retry_at:
                failed: list[str] = []
                try:
                    results = await self.do_checkin()
                    failed = [uid for uid, r in results if not r.get("ok") and not r.get("already")]
                except Exception as e:  # noqa: BLE001
                    logger.warning("签到任务异常: %s", e)
                    failed = ["exception"]
                if failed:
                    self._checkin_fail_count += 1
                    if self._checkin_fail_count >= 3:
                        logger.warning("签到连续失败 %d 次（%s），今天不再重试",
                                       self._checkin_fail_count, "、".join(failed))
                        self._last_checkin_date = today  # 放弃今天，明天重来
                    else:
                        self._checkin_retry_at = now.timestamp() + 7200
                        logger.warning("签到有失败（%s），%.1f 小时后重试（第 %d/3 次）",
                                       "、".join(failed), 2.0, self._checkin_fail_count)
                else:
                    self._checkin_fail_count = 0
                    self._last_checkin_date = today
            # 每周全量备份（每天 3 点检查，距上次 ≥7 天才执行；错过窗口会自动补上）
            if now.hour == 3 and self._backup_due(now):
                try:
                    await asyncio.to_thread(self._weekly_backup)
                except Exception as e:  # noqa: BLE001
                    logger.warning("每周备份失败: %s", e)
            # 积分预警检查（内部自带开关/去重，成本可忽略）
            try:
                await self._check_credit_alert()
            except Exception as e:  # noqa: BLE001
                logger.warning("积分预警检查异常: %s", e)
            # 每日 token 保活
            if now.hour == self._keepalive_hour() and self._last_keepalive_date != today:
                try:
                    await self.do_keepalive()
                except Exception as e:  # noqa: BLE001
                    logger.warning("保活任务异常: %s", e)
            # 每日使用记录清理（每天凌晨 4 点执行一次）
            if now.hour == 4 and self._last_cleanup_date != today:
                try:
                    await self.cleanup_usage()
                except Exception as e:  # noqa: BLE001
                    logger.warning("使用记录清理异常: %s", e)
                self._last_cleanup_date = today
            # 每日模型刷新
            if now.hour == self._model_refresh_hour() and self._last_model_refresh_date != today:
                try:
                    await self.refresh_models()
                except Exception as e:  # noqa: BLE001
                    logger.warning("模型刷新任务异常: %s", e)
                self._last_model_refresh_date = today
            # 每日 AA 评测刷新
            if now.hour == self._aa_refresh_hour() and self._last_aa_refresh_date != today:
                try:
                    await self.refresh_benchmarks()
                except Exception as e:  # noqa: BLE001
                    logger.warning("AA 评测刷新任务异常: %s", e)
                self._last_aa_refresh_date = today
            # 定期刷新额度
            interval = self._credit_interval()
            if now.timestamp() - last_credit >= interval * 60:
                try:
                    await self.refresh_credits()
                except Exception as e:  # noqa: BLE001
                    logger.warning("额度刷新异常: %s", e)
                last_credit = now.timestamp()
            await asyncio.sleep(60)
