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


from . import billing, net, region
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


# 启动预热的**总时间预算**（秒）。
#
# 为什么必须有上限：uvicorn 在 lifespan 的 startup 阶段**不监听端口**，所以"预热慢"
# 会直接变成"整个 WebUI 打不开"——不是降级，是彻底不可用，容器还会被判 unhealthy。
# 2026-09-22 重建容器时实测：AA 评测那次 `SSLEOFError` 把启动拖了 **105 秒**。
# 预热是"页面首开即有数据"的**优化**，不该成为**可用性前提**。
#
# 取 30 秒的依据：正常网络下 2 账号的额度+签到状态刷新约 18 秒、模型目录有缓存时几乎
# 瞬时，30 秒足够完整跑完 —— 即**正常情况行为与加这个上限之前完全一致**，只有上游
# 异常时才会提前放行。`tests/test_policy.py::TestStartupWarmupBudget` 钉住它。
_WARMUP_BUDGET_SECONDS = 30.0


def _log_warmup_failure(task: "asyncio.Task") -> None:
    """预热任务若在**放行之后**才失败，把异常取出来记日志。

    不加这个回调的话，超时放行后 `_warmup_task` 就没人 await 了，一旦它抛异常，
    asyncio 只会在垃圾回收时打一句 "Task exception was never retrieved" —— 既看不出
    是哪一步失败，也看不出"服务其实没事"。这里显式记下来，并说清不影响服务。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("启动预热在放行之后失败（不影响对外服务）：%s", exc)


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
        self._last_makeup_date: str | None = None
        # 猫猫旅行的"本轮已跑"标记：值是 `YYYY-MM-DDTHH`。**不能用日期**——
        # 旅行每个签到小时点都要跑一次（一天两趟：早上出发、晚上领取），
        # 用日期做标记会让当天第二次直接被跳过。
        self._last_travel_slot: str | None = None
        # 领养当日已试标记（uid → CST 日期）：对话量未达门槛时上游回 400，
        # 当天重试也不会成功，记下来避免对上游重试轰炸。
        self._adopt_tried: dict[str, str] = {}
        # 活跃地图每日提醒的"今天已跑"标记。用**日期**（不是日期+小时）：
        # 每天只需提醒一次，重复提醒只会变成骚扰。
        self._last_active_map_date: str | None = None
        # 最近一次活跃地图检查的**结果快照**（供 `/admin/overview` 只读）。
        # 为什么要有它：概览是**高频轮询端点、明确禁止上游网络请求**，而活跃地图判据
        # 必须打上游（heatmap + streak）——所以只能在调度器里查完缓存下来，概览读缓存。
        # 结构：{"date": CST 日期, "checked_at": ts, "unlit": [{uid, region_label,
        # streak_days}], "total": 账号数}。`date != 今天` 时视为无效（跨零点即失效）。
        self._active_map_snapshot: dict | None = None
        self._last_model_refresh_date: str | None = None
        self._last_aa_refresh_date: str | None = None
        self._last_cleanup_date: str | None = None
        self._task: asyncio.Task | None = None
        # 启动预热任务（见 start()）。单独留引用有两个原因：别被 GC 掉，
        # 以及 stop() 时要能取消它。
        self._warmup_task: asyncio.Task | None = None
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
        # auths 目录热加载：上次的目录指纹（文件名+mtime+size），变化时自动注册新账号
        self._auth_dir_fingerprint: str | None = None
        self._reload_auths: object = None  # 由 app.py 装配时注入回调（避免循环导入）

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        # 预热（额度 / 模型目录 / AA 评测 / 存量瘦身）带**总预算**：正常网络下会完整跑完
        # （行为与加预算之前一致），上游异常时最多等 _WARMUP_BUDGET_SECONDS 就放行，
        # 让 WebUI 先可用。
        #
        # `shield` 是这里的关键：不加它，`wait_for` 一超时就会**取消** `_warmup`，
        # 后面的步骤就真的不跑了 —— 那样日志里那句"后台继续"就是假的。
        # shield 让超时只放弃"等待"，_warmup 仍在后台跑完，缓存照样会填上。
        self._warmup_task = asyncio.create_task(self._warmup())
        self._warmup_task.add_done_callback(_log_warmup_failure)
        try:
            await asyncio.wait_for(asyncio.shield(self._warmup_task),
                                   timeout=_WARMUP_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "启动预热超过 %.0f 秒仍未完成：先放行对外服务，预热在后台继续"
                "（页面上的额度/模型数据可能稍后才出现）", _WARMUP_BUDGET_SECONDS)

    async def _warmup(self):
        """启动预热：额度 + 模型目录 + AA 评测 + 存量瘦身。

        顺序、异常处理与原先内联在 `start()` 里的版本完全一致 —— 抽出来只是为了
        给它套一个时间上限（见 `_WARMUP_BUDGET_SECONDS`）。
        """
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
        # 预热还在后台跑的话也取消掉（`to_thread` 里的同步工作无法真正中断，
        # 但至少不再等它、也不会留下悬挂的 task）
        if self._warmup_task:
            self._warmup_task.cancel()

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

    def _makeup_enabled(self) -> bool:
        """补签保连登开关。**默认关闭**——它会消耗补签卡，属于"用户资产"，
        不能默认替他花掉。"""
        return self._setting("makeup_enabled", "0") == "1"

    def _makeup_dry_run(self) -> bool:
        """补签演练模式。**默认开启**：即使补签总开关打开了，也只记录判断结果、
        不发补签请求。要真补必须显式把这一项关掉。

        为什么默认演练：补签花的是**用户自己的卡**（属于用户资产），
        不能因为"配置里手滑"就替他花掉。
        （早先还写着"判据链里有一环未验证"——那个疑问已于 2026-09-22 查清：
        heatmap 的 score 正是活跃连登的直接度量，判据本身没问题。）
        """
        return self._setting("makeup_dry_run", "1") == "1"

    def _travel_enabled(self) -> bool:
        """猫猫旅行开关。**默认关闭**——它每天会向上游写两次状态
        （派出 / 领取），属于"替用户动账号"的动作，默认不替他开。"""
        return self._setting("travel_enabled", "0") == "1"

    def _travel_dry_run(self) -> bool:
        """猫猫旅行演练模式。**默认开启**：只看状态、报"会派出/会领取多少"，
        不发写请求。

        为什么默认演练：与补签不同，旅行**不消耗任何东西**（纯收益），所以这里
        默认演练纯粹是"先让用户确认状态机判断对不对"，不是出于风险考虑。
        """
        return self._setting("travel_dry_run", "1") == "1"

    def _active_map_enabled(self) -> bool:
        """活跃地图每日提醒开关。**默认开**——它是唯一一个"只读 + 只提醒"的任务：
        不写上游、不花积分、不动账号状态，最坏结果就是一条用户本来就要的提醒。
        所以这里不套用补签/旅行那套"默认关"的保守档。

        （为什么不"自动发一条对话把连登续上"：2026-09-22 受控实验证明 chat API
        **根本不点亮活跃地图** —— 给当天 score=0 的账号发两次真实请求后格子仍是 0；
        历史数据里 31 次请求 / 419 积分的那天同样是 0。参考实现靠的是「对话事件
        连发上报」，那属于伪造客户端事件上报，不做。详见 `billing.py` 注释。）
        """
        return self._setting("active_map_enabled", "1") == "1"

    def _active_map_hour(self) -> int:
        return _parse_hour(self._setting("active_map_hour", "23"), 23)

    def checked_in_today(self) -> set[str]:
        """返回今日已签到的账号 uid 集合（**本地记账**判定）。

        注意口径：这是"**本网关**今天替他签过没有"，不是"账号今天签到没有"。
        界面展示请用 `checkin_snapshot()`（优先上游真值）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.db:
            return set()
        try:
            dates = self.db.checkin_dates()
        except Exception:  # noqa: BLE001
            return set()
        return {uid for uid, d in dates.items() if d == today}

    @staticmethod
    def _checkin_status_fresh(acc, today: str) -> bool:
        """账号缓存的上游签到状态是否是**今天**查的。

        上游返回的 `today_checked_in` 是"某一天"的快照，跨零点即失效。
        不校验日期的话，重启后会把昨天的"已签到"当成今天 —— 比不显示更糟。
        """
        ts = getattr(acc, "checkin_synced_at", None)
        if not ts:
            return False
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") == today
        except (OSError, OverflowError, ValueError):
            return False

    def checkin_snapshot(self) -> dict[str, dict]:
        """每个账号的「今日签到」状态（界面口径）。

        优先级：**今天同步过的上游真值** > 本地记账。
        返回 {uid: {today, active, synced_at, source}}，source ∈ {"upstream","local"}。
        `active` 为 None 表示"本期活动是否开启"未知（未同步/已过期）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        local = self.checked_in_today()
        out: dict[str, dict] = {}
        for acc in self.pool.accounts:
            fresh = self._checkin_status_fresh(acc, today)
            if fresh:
                out[acc.uid] = {
                    "today": bool(acc.checkin_today),
                    "active": acc.checkin_active,
                    "synced_at": acc.checkin_synced_at,
                    "source": "upstream",
                }
            else:
                out[acc.uid] = {
                    "today": acc.uid in local,
                    "active": None,
                    "synced_at": acc.checkin_synced_at,
                    "source": "local",
                }
        return out

    def active_map_snapshot(self) -> dict | None:
        """最近一次活跃地图检查的结果（**只读缓存**，供 `/admin/overview`）。

        `None` = **今天还没查过**（进程刚起、或检查点还没到）→ 调用方不该据此报任何东西。
        `date != 今天` 的快照直接视为无效：跨零点后它描述的是昨天，拿它提醒会变成假警报。

        为什么不做成"概览自己去查"：概览是**高频轮询端点**（前端每隔几秒拉一次），
        而活跃地图判据必须打上游两个接口 —— 放那儿就是每秒往上游打几十个请求。
        所以查询只在调度器里发生（每天一次，或用户手动点一次），概览只读这里。
        """
        snap = self._active_map_snapshot
        if not snap:
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        if snap.get("date") != today:
            return None
        return snap

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

        # 顺带同步**上游**签到状态：界面「今日签到」列以它为准。本地记账只知道
        # "我们替他签过没有"，用户在官方客户端自己签到时会漏记。
        # 这里用 only_stale=False：每轮都重查，才能及时反映"用户刚在客户端签了"。
        await self.sync_checkin_status(only_stale=False)

    async def sync_checkin_status(self, only_stale: bool = True):
        """同步各账号的**上游**签到状态，写入账号池与 DB。

        only_stale=True 只处理"没有今天数据"的账号：供 /admin/accounts 在返回前
        补齐（否则刚添加的账号、或跨零点后，要等下一轮定时刷新界面才准）。
        only_stale=False 全量重查：供定期刷新，以便及时看到用户在官方客户端签到。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
            if only_stale and self._checkin_status_fresh(acc, today):
                continue
            try:
                st = await billing.fetch_checkin_status(acc.mgr)
                ts = time.time()
                self.pool.set_checkin_status(acc.uid, st["today_checked_in"], st["active"], ts)
                if self.db:
                    self.db.set_checkin_status(acc.uid, st["today_checked_in"], st["active"], ts)
            except Exception as e:  # noqa: BLE001
                logger.warning("签到状态查询失败 %s: %s", acc.uid, e)

    async def do_checkin(self, skip: set[str] | None = None):
        """执行签到。skip 显式给出时按它跳过；否则自行判定。

        跳过判定**优先用上游真值**（`acc.checkin_today`，且必须是今天同步过的），
        上游未知时才回落本地记账。为什么必须改：本地记账只知道"我们替他签过没有"，
        于是用户在官方客户端自己签到后我们会白打一次上游；反过来我们从没签过时
        又会把"未签到"报给用户（即使上游其实是已签到）。

        结果里可能带三个互斥标记：
          already  —— 今日已签到（跳过，非失败）
          inactive —— 签到活动本期未开启（跳过，非失败，**不该重试**）
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if skip is None:
            # 先补齐上游签到状态再判断。start() 里 _run 任务与首轮 refresh_credits
            # 是**并发**的，do_checkin 可能抢在同步之前跑 —— 那样会拿陈旧的本地记账
            # 做判断，对"活动未开启"的账号白打一次上游、还被误报成签到失败。
            # only_stale=True：状态新鲜时零上游请求。
            await self.sync_checkin_status(only_stale=True)
        local_skip = self.checked_in_today()
        results = []
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
            if skip is None:
                fresh = self._checkin_status_fresh(acc, today)
                if fresh and acc.checkin_today:
                    logger.info("签到跳过 %s（上游显示今日已签到）", acc.uid)
                    results.append((acc.uid, {"ok": False, "message": "今日已签到", "already": True}))
                    continue
                if fresh and acc.checkin_active is False:
                    # 活动没开，签不了。归为"跳过"而不是"失败"，
                    # 否则 _run 会当成失败每 2 小时重试 3 次，纯属白打上游。
                    logger.info("签到跳过 %s（签到活动未开启）", acc.uid)
                    results.append((acc.uid, {"ok": False, "message": "签到活动未开启", "inactive": True}))
                    continue
                if not fresh and acc.uid in local_skip:
                    logger.info("签到跳过 %s（本地记账显示今日已签到）", acc.uid)
                    results.append((acc.uid, {"ok": False, "message": "今日已签到", "already": True}))
                    continue
            elif acc.uid in skip:
                logger.info("签到跳过 %s（调用方指定跳过）", acc.uid)
                results.append((acc.uid, {"ok": False, "message": "今日已签到", "already": True}))
                continue
            try:
                res = await billing.daily_checkin(acc.mgr)
                results.append((acc.uid, res))
                logger.info("签到 %s: %s", acc.uid, res)
                # 只有上游确认"成功"或"已签到"才落账。
                # 之前是无条件写 —— 上游报错（例如"活动未开启"）也会被记成
                # "今日已签到"，界面出现假阳性，且第二天才自愈。
                if res.get("ok") or res.get("already"):
                    if self.db:
                        self.db.set_checkin_date(acc.uid, today)
                    # 顺手把上游状态刷新成"今天已签"，界面立刻正确，无需等下一轮同步
                    active = acc.checkin_active if acc.checkin_active is not None else True
                    self.pool.set_checkin_status(acc.uid, True, active, time.time())
                    if self.db:
                        self.db.set_checkin_status(acc.uid, True, active, time.time())
            except Exception as e:  # noqa: BLE001
                logger.warning("签到失败 %s: %s", acc.uid, e)
                results.append((acc.uid, {"ok": False, "message": str(e)}))
        # 注意：不再在这里置位 _last_checkin_date——由 _run 根据结果决定
        # （全部成功/已签到才置位，存在失败则按重试策略补签），手动签到不受影响
        # 签到后刷新额度（含自动解冻）；refresh_credits 同时会重新同步上游签到状态
        await self.refresh_credits()
        return results

    async def do_makeup(self, dry_run: bool | None = None) -> list[dict]:
        """补签**活跃连登**（P3 最小试点）：昨日无对话活动且有补签卡时补昨日。

        **补的是哪条连登**（2026-09-22 实测，见 `billing.py` 末尾的说明）：
        上游有两条独立的连登——签到连登（billing 域 `checkin-activity-status.streak_days`）
        与**活跃连登**（growth 域 `streak.days`，由对话活跃驱动）。补签卡
        （`makeup_cards` / `makeup_dates`）挂在 growth 域，所以**它保护的是活跃连登**，
        而 heatmap 的 `score` 正是活跃连登的直接度量。别把判据改成 `checkin_dates`：
        实测 2026-09-21 那天两个口径结论相反（国内版已签到但无对话 → 活跃连登断了）。

        判据链（任一环不满足就跳过，绝不猜）：
          1. 昨日**在当月内** —— 实测上游只允许补当月
             （`only current month makeup allowed`），跨月断档补不了；
          2. heatmap 昨日 score == 0（那天没有任何对话活动 → 活跃连登会断）；
          3. 补签卡余额 > 0（`streak.makeup_cards.balance`）；
          4. 昨日不在 `makeup_dates` 里（没补过）。

        为什么盯"昨日"：活跃连登的断档只可能发生在「上一个自然日」（今日尚未结算）。

        dry_run: None = 用设置里的 makeup_dry_run；True/False 强制。演练模式只
        记录判断结果、不发请求，返回值里 `action` 标成 "would-makeup"。

        单个账号的查询/补签失败只记日志跳过，不影响其它账号，也不影响主流程。

        两阶段执行（**不要改回串行 for 循环**）：
          - 阶段一「只读探测」**全部并发**：每个账号的 streak + heatmap 也并发。
            这两个请求各自 `timeout=20`，串行时一个账号最坏 40s、N 个账号就是
            40N 秒——界面上点「立即检查」要等好几分钟，前端 30s 超时直接报错。
            读是幂等的，并发没有副作用。
          - 阶段二「写」**严格串行**：补签花的是用户自己的卡，且会改变上游状态，
            不能并发打上游（也避免瞬时并发触发风控）。
        """
        from . import billing
        if dry_run is None:
            dry_run = self._makeup_dry_run()
        yesterday = billing.growth_yesterday()
        # 跨月判定：昨日与今日的月份不同 → 补不了，直接跳过整轮。
        # 规则实现收敛在 billing.makeup_allowed()（此前路由层自己写了一遍且写错了）。
        if not billing.makeup_allowed():
            logger.info("补签跳过：昨日 %s 不在当月（上游只允许补当月）", yesterday)
            return []

        candidates = [a for a in list(self.pool.accounts)
                      if a.enabled and not a.auto_disabled_reason and a.mgr is not None]
        if not candidates:
            return []

        async def probe(acc):
            """只读探测一个账号：返回 (acc, streak, heat, error_msg)。"""
            try:
                # 两个查询彼此独立，并发发起——这是把最坏耗时从 2×20s 压到 20s 的关键
                streak, heat = await asyncio.gather(
                    asyncio.to_thread(billing.fetch_streak, acc.mgr),
                    asyncio.to_thread(billing.fetch_heatmap, acc.mgr),
                )
                return acc, streak, heat, ""
            except Exception as e:  # noqa: BLE001
                return acc, None, None, f"查询失败：{e}"

        probed = await asyncio.gather(*(probe(a) for a in candidates))

        results: list[dict] = []
        for acc, streak, heat, err in probed:
            item = {"uid": acc.uid, "yesterday": yesterday, "action": "skip", "reason": ""}
            if err or streak is None or heat is None:
                item["reason"] = err or "查询失败"
                results.append(item)
                continue
            item["streak_days"] = streak["days"]
            item["makeup_balance"] = streak["makeup_balance"]
            if yesterday in streak["makeup_dates"]:
                item["reason"] = "昨日已补过"
                results.append(item)
                continue
            if yesterday not in heat:
                item["reason"] = "活跃地图未覆盖昨日（无判据）"
                results.append(item)
                continue
            if heat[yesterday] != 0:
                item["reason"] = f"昨日有对话活动（score={heat[yesterday]}），活跃连登未断"
                results.append(item)
                continue
            if streak["makeup_balance"] <= 0:
                item["reason"] = "无可用补签卡"
                results.append(item)
                continue
            if dry_run:
                item["action"] = "would-makeup"
                item["reason"] = "演练模式：判据齐全，未实际补签"
                logger.info("[演练] 账号 %s 昨日 %s 无对话活动且持有 %d 张补签卡，将补签（活跃连登 %d 天）",
                            acc.uid[:8], yesterday, streak["makeup_balance"], streak["days"])
                results.append(item)
                continue
            try:
                r = await asyncio.to_thread(billing.use_makeup_card, acc.mgr, yesterday)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "message": str(e)}
            item["action"] = "makeup" if r.get("ok") else "makeup-failed"
            item["reason"] = r.get("message", "")
            if r.get("ok"):
                logger.info("账号 %s 补签 %s 成功（活跃连登 %d 天）",
                            acc.uid[:8], yesterday, streak["days"])
            else:
                logger.warning("账号 %s 补签 %s 失败：%s", acc.uid[:8], yesterday, r.get("message"))
            results.append(item)
        return results

    async def do_redeem(self, dry_run: bool = True, draw: bool = False) -> list[dict]:
        """领取连登档位奖励（7d/14d/28d），可选顺带把抽奖次数用掉。

        为什么值得做（2026-09-22 实测结论，见 `billing.py` 末尾）：
        **补签卡唯一的常规来源就是这里的档位奖励**（7d/14d/28d 各送 1 张）。
        实测两个账号 `makeup_cards.balance` 都是 0、三个档位全是 `locked`，
        所以「补签保连登」目前**没有任何卡可用** —— 先领到档位奖励，补签才有意义。

        为什么默认**手动**、不做成定时任务：
        档位是**每月每档一次**的事件（一个月最多领 3 次），不像签到那样天天要跑；
        而领奖会写上游账号状态。给它一个带演练的按钮，比加一条无人值守的写路径更合适。
        真要自动化只需再加一个开关，判据已经收敛在 `billing.redeem_tier()` 里。

        `dry_run=True`（默认）：只读探测 + 报"会领哪几档、各发什么"，不发写请求。
        `draw=True`：把 `lottery/chances` 里的次数抽掉（国际版抽奖未开启，
        上游会回 400 `lottery disabled`，归入正常态、只记录不告警）。

        两阶段执行（与 `do_makeup` 同构，理由相同）：
          阶段一「只读探测」全部并发（每账号的 streak 与 chances 也并发）；
          阶段二「写」严格串行（会改上游状态，不并发打上游）。
        """
        from . import billing

        candidates = [a for a in list(self.pool.accounts)
                      if a.enabled and not a.auto_disabled_reason and a.mgr is not None]
        if not candidates:
            return []

        async def probe(acc):
            """只读探测：返回 (acc, streak, chances, err)。chances 失败记 None。"""
            try:
                streak = await asyncio.to_thread(billing.fetch_streak, acc.mgr)
            except Exception as e:  # noqa: BLE001
                return acc, None, None, f"查询失败：{e}"
            chances = None
            if draw:
                try:
                    chances = await asyncio.to_thread(billing.fetch_lottery_chances, acc.mgr)
                except Exception:  # noqa: BLE001
                    chances = None  # 抽奖次数取不到不影响领奖
            return acc, streak, chances, ""

        probed = await asyncio.gather(*(probe(a) for a in candidates))

        results: list[dict] = []
        for acc, streak, chances, err in probed:
            item: dict = {"uid": acc.uid, "action": "skip", "reason": "",
                          "streak_days": None, "claimed": [], "draw": None}
            if err or streak is None:
                item["reason"] = err or "查询失败"
                results.append(item)
                continue
            red = streak["redemption"]
            item["streak_days"] = streak["days"]
            item["makeup_balance"] = streak["makeup_balance"]
            item["tier_status"] = red["status"]
            item["claimable"] = red["claimable"]
            if not red["claimable"]:
                # 报出进度，但**两个数必须分开说**：`next_tier_remaining` 是上游口径，
                # 实测等于「档位天数 − **当月最长连续段**」，**不是**「档位天数 − 当前连登」
                # （国内版 `days=0` 却报 2，因为本月最长连续段是 5，连登其实早已断档）。
                # 写成"当前连登 X 天，还差 Y 天"会让人以为再连 Y 天就够——那是错的。
                # 口径推导见 billing.py 的字段说明。
                nxt = red.get("next_tier")
                item["reason"] = (f"没有可领档位（活跃连登 {streak['days']} 天；"
                                  f"下一档 {nxt}，上游报还差 {red['next_tier_remaining']} 天）"
                                  if nxt else "没有可领档位")
                results.append(item)
                continue
            if dry_run:
                granted = [t for t in red["tiers"] if t["tier"] in red["claimable"]]
                item["action"] = "would-redeem"
                item["reason"] = "演练模式：可领 " + "、".join(
                    f"{t['tier']}（积分 {t['credit']}/能量 {t['energy']}/"
                    f"补签卡 {t['cards']}/抽奖 {t['chances']}）" for t in granted)
                logger.info("[演练] 账号 %s 可领档位 %s（活跃连登 %d 天）",
                            acc.uid[:8], red["claimable"], streak["days"])
                results.append(item)
                continue

            # 阶段二：写。按档位从低到高依次领；单个档位失败不影响后续档位。
            for tier in red["claimable"]:
                try:
                    r = await asyncio.to_thread(billing.redeem_tier, acc.mgr, tier)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "normal": False, "message": str(e)}
                item["claimed"].append({"tier": tier, **{k: r.get(k) for k in
                                                         ("ok", "normal", "message", "granted")}})
                if r.get("ok"):
                    logger.info("账号 %s 领取 %s 档位成功：%s",
                                acc.uid[:8], tier, r.get("granted"))
                elif r.get("normal"):
                    # 天数不足/本月已领属正常拒绝，只记 info 不刷 WARN。
                    logger.info("账号 %s 领取 %s 档位被拒（正常态）：%s",
                                acc.uid[:8], tier, r.get("message"))
                else:
                    logger.warning("账号 %s 领取 %s 档位失败：%s",
                                   acc.uid[:8], tier, r.get("message"))
            item["action"] = "redeem"

            if draw and chances:
                try:
                    d = await asyncio.to_thread(billing.draw_lottery, acc.mgr)
                except Exception as e:  # noqa: BLE001
                    d = {"ok": False, "normal": False, "message": str(e)}
                item["draw"] = {k: d.get(k) for k in ("ok", "normal", "message", "prize")}
                if d.get("ok"):
                    logger.info("账号 %s 抽奖中得：%s", acc.uid[:8], d.get("prize"))
                elif d.get("normal"):
                    logger.info("账号 %s 抽奖被拒（正常态）：%s", acc.uid[:8], d.get("message"))
                else:
                    logger.warning("账号 %s 抽奖失败：%s", acc.uid[:8], d.get("message"))
            results.append(item)
        return results

    async def do_travel(self, dry_run: bool | None = None) -> list[dict]:
        """猫猫旅行巡检：无猫 → 领养；`idle` → 派出；`arrived` → 领取。

        **收益口径（2026-09-22 实测，别被"每 4 小时 10 积分"误导）**：
        四个地点（咖啡馆 / 商场店铺 / 健身房 / 古镇客栈）参数**完全相同** ——
        时长 `1~4` 小时随机、奖励 `5~10` 积分随机；`daily_limit_reached` 决定
        **每天只有 1 趟**。所以真实收益是**每天 5~10 积分**，4 小时是单趟最长时长。

        **只跑国内版账号**：国际版没有这套体系（`travel/config` 返回空 `data`），
        不按区域跳过就会每轮白打两个必然失败的请求。

        **一轮可以走两步（领取后再派出）**，这是刻意的：只做"一个动作"的话，
        节奏会被 `checkin_hours` 的配置绑死（配成单点时会退化成两天才领一次）。
        领取后 `daily_limit_reached` 通常已在新的一天里重置，顺手把当天那趟派出去，
        才能稳定保持"每天 1 趟"。多出来的那次 depart 若被上游按规则拒绝，
        `billing.travel_depart` 会把它归成正常态，不刷 WARN。

        `dry_run`：None = 用设置里的 `travel_dry_run`；True/False 强制。
        演练模式只报"会做什么"，不发任何写请求。

        两阶段执行（与 `do_makeup` / `do_redeem` 同构，理由相同）：
          阶段一「只读探测」全部并发（每账号的猫档案与旅行状态也并发）；
          阶段二「写」严格串行（会改上游状态，不并发打上游、避免瞬时风控）。
        """
        from . import billing, region

        if dry_run is None:
            dry_run = self._travel_dry_run()

        candidates = [a for a in list(self.pool.accounts)
                      if a.enabled and not a.auto_disabled_reason and a.mgr is not None
                      and region.region_of_account(a).id == "cn"]
        if not candidates:
            return []

        async def probe(acc):
            """只读探测：返回 (acc, buddy, travel, err)。buddy=None 表示未领养。"""
            try:
                buddy, travel = await asyncio.gather(
                    asyncio.to_thread(billing.fetch_buddy, acc.mgr),
                    asyncio.to_thread(billing.fetch_travel_status, acc.mgr),
                )
                return acc, buddy, travel, ""
            except Exception as e:  # noqa: BLE001
                return acc, None, None, f"查询失败：{e}"

        probed = await asyncio.gather(*(probe(a) for a in candidates))

        results: list[dict] = []
        for acc, buddy, travel, err in probed:
            item: dict = {"uid": acc.uid, "action": "skip", "reason": "",
                          "state": None, "reward": None}
            if err or travel is None:
                item["reason"] = err or "查询失败"
                results.append(item)
                continue
            item["state"] = travel["state"]
            item["daily_limit_reached"] = travel["daily_limit_reached"]
            item["record_id"] = travel["record_id"]
            item["reward_credit"] = travel["reward_credit"]
            item["location"] = travel["location"]

            # ---- 未领养：先同意协议再领养 ----
            if buddy is None:
                if self._adopt_tried_today(acc.uid):
                    item["reason"] = "今日已试过领养（对话量未达门槛），明日再试"
                    results.append(item)
                    continue
                if dry_run:
                    item["action"] = "would-adopt"
                    item["reason"] = "演练模式：未领养，将同意协议并领养第一只猫"
                    results.append(item)
                    continue
                r = await asyncio.to_thread(billing.buddy_adopt, acc.mgr)
                # 无论成败都记下当日已试：门槛未达是预期行为，重复打只会轰炸上游。
                self._mark_adopt_tried(acc.uid)
                item["action"] = "adopt" if r.get("ok") else "adopt-skip"
                item["reason"] = r.get("message", "")
                if r.get("ok"):
                    logger.info("账号 %s 领养猫成功", acc.uid[:8])
                elif r.get("normal"):
                    logger.info("账号 %s 领养被拒（正常态）：%s", acc.uid[:8], r.get("message"))
                else:
                    logger.warning("账号 %s 领养失败：%s", acc.uid[:8], r.get("message"))
                results.append(item)
                continue

            # ---- 行程进行中：等下一轮（不轮询、不等待） ----
            if travel["state"] == "traveling":
                item["reason"] = "行程进行中，等下一轮巡检"
                results.append(item)
                continue

            # ---- 已到站：领取 ----
            if travel["state"] == "arrived":
                if not travel["record_id"]:
                    item["reason"] = "已到站但上游没给 record_id，无法领取"
                    results.append(item)
                    continue
                if dry_run:
                    item["action"] = "would-claim"
                    item["reason"] = (f"演练模式：将领取 {travel['reward_credit']} 积分"
                                      f"（{travel['location'] or '未知地点'}，"
                                      f"record {travel['record_id']}）")
                    results.append(item)
                    continue
                r = await asyncio.to_thread(billing.travel_claim, acc.mgr, travel["record_id"])
                item["action"] = "claim" if r.get("ok") else "claim-skip"
                item["reason"] = r.get("message", "")
                item["reward"] = r.get("reward")
                if r.get("ok"):
                    logger.info("账号 %s 领取旅行奖励成功：%s 积分（record %s）",
                                acc.uid[:8], r.get("reward"), travel["record_id"])
                elif r.get("normal"):
                    logger.info("账号 %s 领取旅行奖励被拒（正常态）：%s",
                                acc.uid[:8], r.get("message"))
                else:
                    logger.warning("账号 %s 领取旅行奖励失败：%s",
                                   acc.uid[:8], r.get("message"))

            # ---- 派出（idle，或刚领取完想把当天那趟补上） ----
            if travel["state"] in ("arrived", "idle"):
                if travel["daily_limit_reached"]:
                    if not item["reason"]:
                        item["reason"] = "今日已派出过（每天 1 趟）"
                    results.append(item)
                    continue
                if dry_run:
                    if item["action"] == "skip":
                        item["action"] = "would-depart"
                        item["reason"] = "演练模式：将派出一次旅行（时长 1~4h，奖励 5~10 积分）"
                    results.append(item)
                    continue
                d = await asyncio.to_thread(billing.travel_depart, acc.mgr)
                if d.get("ok"):
                    item["action"] = "claim+depart" if item["action"] == "claim" else "depart"
                    item["reason"] = (item["reason"] + "；已派出今日行程").lstrip("；") \
                        if item["reason"] else "已派出"
                    logger.info("账号 %s 已派出旅行", acc.uid[:8])
                elif d.get("normal"):
                    # 正常态（今日已派出 / 状态冲突）：只记 info，不刷 WARN。
                    if not item["reason"]:
                        item["action"] = "depart-skip"
                        item["reason"] = d.get("message", "")
                    logger.info("账号 %s 派出被拒（正常态）：%s", acc.uid[:8], d.get("message"))
                else:
                    item["action"] = "depart-failed"
                    item["reason"] = d.get("message", "")
                    logger.warning("账号 %s 派出失败：%s", acc.uid[:8], d.get("message"))
                results.append(item)
                continue

            # 未知状态：原样报出来，别猜（上游以后加了新状态时能立刻看见）
            item["reason"] = f"未知状态 {travel['state']!r}，跳过"
            results.append(item)
        return results

    async def do_active_map_check(self, notify: bool = True) -> list[dict]:
        """检查每个账号**今天**有没有点亮活跃地图；没点亮就提醒（不代发任何请求）。

        **只读**：只查 `growth/heatmap` 与 `growth/streak`，绝不写上游、不花积分。

        为什么是"提醒"而不是"自动激活"（2026-09-22 受控实验，别推翻）：
        给当天 `score == 0` 的国内版账号发了两次真实对话请求（都 HTTP 200），
        复查今天格子**仍然是 0**；历史数据同样（09-12 该账号 31 次请求 / 419 积分，
        当天 heat 仍是 0）。也就是说**活跃地图不由 chat API 驱动**，与 UA / 使用端
        （CLI 还是 WorkBuddy）无关。参考实现点亮它靠的是「对话事件连发上报」，
        那属于伪造客户端事件上报 —— 本项目对 开学季 / 夜猫子 已明确拒绝过同类做法，
        不做。所以这里只能把事实告诉用户，由他**自己在官方客户端聊一句**点亮。

        判据（任一环不成立就不猜）：
          1. heatmap 里**有今天这一格** —— 没有就是"无判据"（上游换了窗口口径），
             报出来但不提醒，免得拿不存在的数据去吓人；
          2. `score == 0` → 今天还没有任何对话活跃，活跃连登今天会断；
          3. `score > 0` → 已点亮，跳过；
          4. heatmap **查不到**（重试后仍失败）→ `query-failed`，归入"未知"桶。

        ⚠️ **判据只依赖 heatmap**，`streak` 只提供 `streak_days` 供展示 ——
        所以两者必须**各自容错**。以前是 `asyncio.gather` 裸抛，任一个失败就把整条
        判成 `query-failed` → **实测因此漏报过**：streak 一抖动，明明 heatmap 已经
        返回 `score=0`，也照样不提醒。**别让装饰性数据对判据拥有否决权。**
        （实测 2026-09-22：本机对国际版上游这两个接口**各约 25% 的请求**吃瞬时网络错误、
        国内版 0%，且**两者独立失败** —— 所以修复前"任一失败"约 44% 概率丢掉判据。
        重试已下沉到 `billing._growth_get(retries=1)`，这里不再重复做。）

        **"未知"必须单独成一桶**（`unknown`），不能并进 `unlit` 或 `lit`：
        并进前者是编造结论，并进后者是假装没事。它要照常进日志、webhook 与概览横幅
        ——否则一次 TLS 抖动就让某个账号**静默失去保护**，而本功能存在的意义就是防这个。

        两阶段（与补签同构，**不要改回串行**）：探测全部并发——`heatmap` 与
        `streak` 各自 `timeout=20`，串行时一个账号最坏 40s，界面上点一次要等好几分钟。
        读是幂等的，并发没有副作用。本方法**没有任何写阶段**。

        notify=False 时只返回结果、不推 webhook（供 WebUI 手动检查用——手动点一次
        就推一条通知，那是骚扰）。
        """
        from . import billing
        today = billing.growth_today()
        candidates = [a for a in list(self.pool.accounts)
                      if a.enabled and not a.auto_disabled_reason and a.mgr is not None]
        if not candidates:
            return []

        async def fetch_heat(acc):
            """取活跃地图，返回 `(heat | None, 错误串)`。

            重试**不在这里做** —— 已经下沉到 `billing._growth_get(retries=1)`，
            两处都做会变成 3 次尝试、耗时翻倍（见那边的说明）。
            """
            try:
                return await asyncio.to_thread(billing.fetch_heatmap, acc.mgr), ""
            except Exception as e:  # noqa: BLE001
                return None, f"查询失败：{type(e).__name__}: {e}"

        async def fetch_streak_safe(acc):
            """取连登天数，**失败返回 None**（它是展示信息，不该影响判据）。"""
            try:
                return await asyncio.to_thread(billing.fetch_streak, acc.mgr)
            except Exception:  # noqa: BLE001
                return None

        async def probe(acc):
            """只读探测一个账号：返回 (acc, streak, heat, heat_err)。

            ⚠️ **判据只依赖 heatmap**（`score` 才是活跃地图的直接度量），
            `streak` 只提供 `streak_days` 供展示。所以两者**必须各自容错**：

            以前是两个 `asyncio.gather` 裸抛，任一个失败就把整条判成 `query-failed`
            → **实测因此漏报过**：streak 一抖动，明明 heatmap 已经返回 `score=0`，
            也照样不提醒。**把装饰性数据和判据绑在一起，等于让装饰性数据拥有否决权。**
            """
            (heat, heat_err), streak = await asyncio.gather(
                fetch_heat(acc), fetch_streak_safe(acc),
            )
            return acc, streak, heat, heat_err

        probed = await asyncio.gather(*(probe(a) for a in candidates))

        results: list[dict] = []
        unlit: list[tuple] = []      # (acc, streak_days)
        unknown: list[tuple] = []    # (acc, 原因) —— 查不到、判不了
        for acc, streak, heat, heat_err in probed:
            item = {"uid": acc.uid, "date": today, "action": "skip", "reason": ""}
            # **只按 heatmap 判定**：heat 拿到就能判，streak 缺失不影响（记 None）
            if heat is None:
                item["action"] = "query-failed"
                item["reason"] = heat_err or "查询失败"
                results.append(item)
                unknown.append((acc, item["reason"]))
                continue
            item["streak_days"] = streak["days"] if streak else None
            item["month_total_days"] = streak["month_total_days"] if streak else None
            if today not in heat:
                item["action"] = "no-cell"
                item["reason"] = "活跃地图未覆盖今天（无判据，不提醒）"
                results.append(item)
                continue
            item["heat_today"] = heat[today]
            if heat[today] > 0:
                item["action"] = "lit"
                item["reason"] = f"今天已有对话活跃（score={heat[today]}）"
                results.append(item)
                continue
            item["action"] = "unlit"
            item["reason"] = "今天还没有任何对话活跃，活跃连登会在今天断档"
            unlit.append((acc, streak["days"] if streak else None))
            results.append(item)

        # 落快照（**无论有没有未点亮账号都要落**）：概览要能区分"查过了、全都亮着"
        # 与"从来没查过"——前者不该显示任何横幅，后者也不该，但原因完全不同。
        # 手动调用也会落，这是刻意的：用户点一次「立即检查」，概览横幅应当立刻同步。
        self._active_map_snapshot = {
            "date": today,
            "checked_at": time.time(),
            "total": len(candidates),
            "unlit": [
                {
                    "uid": a.uid,
                    "region_label": region.REGIONS[a.region_id].label,
                    "streak_days": d,
                }
                for a, d in unlit
            ],
            # 查不到 → 判不了。**必须和"已点亮"分开**：混进 unlit 是编造结论，
            # 混进 lit 是假装没事。它该以"未知"的身份出现，由用户决定要不要去聊一句。
            "unknown": [
                {"uid": a.uid, "region_label": region.REGIONS[a.region_id].label, "reason": r}
                for a, r in unknown
            ],
        }

        if unlit:
            # 日志固定打一条：webhook 没配时这就是唯一的可见信号（容器日志）
            logger.warning("活跃地图：%d 个账号今天未点亮（%s）——需要到官方客户端聊一句",
                           len(unlit), "、".join(a.uid[:8] for a, _ in unlit))
        if unknown:
            # 单独打一条、用不同措辞：这是"没查出来"，不是"没点亮"。
            # 不打的话，一次 TLS 抖动就会让某个账号**静默失去保护**。
            logger.warning("活跃地图：%d 个账号查询失败、无法判断是否点亮（%s）——"
                           "建议一并去官方客户端聊一句（保险起见）",
                           len(unknown), "、".join(a.uid[:8] for a, _ in unknown))
        if notify and (unlit or unknown):
            url = self._setting("alert_webhook_url", "").strip()
            if url:
                parts = []
                if unlit:
                    lines = [f"· {a.uid[:8]}（{region.REGIONS[a.region_id].label}）"
                             f"当前连登 {d if d is not None else '未知'} 天" for a, d in unlit]
                    parts.append(f"今天（{today}）还有 {len(unlit)} 个账号没有点亮活跃地图，"
                                 f"活跃连登会在今天断档：\n" + "\n".join(lines))
                if unknown:
                    lines = [f"· {a.uid[:8]}（{region.REGIONS[a.region_id].label}）"
                             for a, _ in unknown]
                    parts.append(f"另有 {len(unknown)} 个账号查询失败、无法判断是否点亮：\n"
                                 + "\n".join(lines))
                body = ("\n\n".join(parts)
                        + "\n\n到官方客户端聊一句即可点亮（今天 24:00 前有效）。"
                        # 纯文本渠道（Bark / 企微 / 飞书都是纯文本渲染），
                        # 别在这里用 ** 之类的 Markdown 强调——会原样显示出星号。
                        + "\n注：本网关的对话请求不会点亮活跃地图，只有官方客户端会。")
                await self._send_webhook(url, "Workbuddy2API 活跃地图提醒", body)
        return results

    def _adopt_tried_today(self, uid: str) -> bool:
        """该账号今日是否已试过领养（且因门槛未达被拒）。"""
        from . import billing
        return self._adopt_tried.get(uid) == billing.growth_today()

    def _mark_adopt_tried(self, uid: str) -> None:
        """记下"该账号今日已试过领养"。"""
        from . import billing
        self._adopt_tried[uid] = billing.growth_today()

    async def do_keepalive(self):
        """强制刷新所有账号 token；连续多次失败（session 可能失效）才自动禁用。

        偶发单次失败仅计数，不立即禁用，避免网络抖动导致账号"莫名被禁用"；
        成功一次即重置计数。
        """
        if not self._keepalive_enabled():
            logger.info("保活已由用户关闭（keepalive_enabled=0），跳过")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for acc in list(self.pool.accounts):  # 快照遍历：管理端点可能并发增删账号
            # 自动禁用的账号也不再保活：它的 session 已经废了，继续刷只会白打上游
            if not acc.enabled or acc.auto_disabled_reason:
                continue
            ok = await asyncio.to_thread(acc.mgr.keepalive)
            if ok:
                self._keepalive_fails.pop(acc.uid, None)  # 重置失败计数
                logger.info("token 保活 %s: ok", acc.uid)
            else:
                fails = self._keepalive_fails.get(acc.uid, 0) + 1
                self._keepalive_fails[acc.uid] = fails
                if fails >= self._keepalive_fail_threshold:
                    reason = "保活连续失败（session 可能失效），请重新扫码登录"
                    logger.error("token 保活 %s: 连续 %d 次失败（session 可能失效），自动禁用", acc.uid, fails)
                    # 走**自动禁用位**而不是手动停用位：这样用户点"启用"不会把一个
                    # session 已失效的账号放回池子（下一个请求立刻再吃一次失败）
                    self.pool.disable_auto(acc.uid, reason)
                    # 同步落库：否则重启后账号"复活"，坏 session 继续打上游
                    if self.db:
                        self.db.set_account_state(acc.uid, auto_disabled_reason=reason)
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
            async with net.async_client(timeout=10) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("webhook 推送失败 HTTP %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("webhook 推送异常: %s", e)
            return False

    def _check_auth_dir_changes(self):
        """auths 目录指纹变化时触发重载回调（新增账号自动进池，免重启）。

        回调由 app.py 装配时注入（需要访问 CredentialManager/账号池/DB，
        放 scheduler 里会循环导入）。只做"新增"，不自动删除——移除账号
        仍走 WebUI 手动操作（含清理 DB/限速器等副作用，自动删风险大）。
        """
        if self._reload_auths is None:
            return
        try:
            from .credentials import find_auth_files
            parts = []
            for f in find_auth_files():
                try:
                    st = f.stat()
                    parts.append(f"{f.name}:{int(st.st_mtime)}:{st.st_size}")
                except OSError:
                    continue
            fp = "|".join(sorted(parts))
            if self._auth_dir_fingerprint is None:
                self._auth_dir_fingerprint = fp  # 首轮只记基线，不触发
                return
            if fp != self._auth_dir_fingerprint:
                self._auth_dir_fingerprint = fp
                logger.info("auths 目录变化，触发热加载")
                self._reload_auths()
        except Exception as e:  # noqa: BLE001
            logger.warning("auths 热加载检查异常: %s", e)

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
                    # inactive（活动未开启）不算失败：重试多少次都签不上，
                    # 计进 failed 只会白打上游 3 轮
                    failed = [uid for uid, r in results
                              if not r.get("ok") and not r.get("already") and not r.get("inactive")]
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
            # auths 目录热加载：新增凭证文件自动进池，免手动重启（Sliverkiss 同款思路）
            self._check_auth_dir_changes()
            # 每日 token 保活
            if now.hour == self._keepalive_hour() and self._last_keepalive_date != today:
                try:
                    await self.do_keepalive()
                except Exception as e:  # noqa: BLE001
                    logger.warning("保活任务异常: %s", e)
            # 补签保连登（P3 试点）：默认关闭；打开后默认还是演练模式。
            # 放在签到之后、同一小时窗口里跑一次——补签判据盯的是"昨日"，
            # 与今天的签到结果无关，早跑晚跑结论一样。
            if (self._makeup_enabled() and now.hour == min(hours)
                    and self._last_makeup_date != today):
                try:
                    await self.do_makeup()
                except Exception as e:  # noqa: BLE001
                    logger.warning("补签任务异常: %s", e)
                self._last_makeup_date = today
            # 猫猫旅行：默认关闭。打开后默认还是演练模式。
            # 在**每个签到小时点**都跑一次（默认 9 点与 21 点），理由是行程要 1~4 小时
            # 才到站：早上那轮负责派出、晚上那轮负责领取，合起来才是"每天 1 趟"。
            # 标记用「日期+小时」而不是日期——用日期会让当天第二轮直接被跳过。
            if self._travel_enabled():
                slot = f"{today}T{now.hour}"
                if now.hour in hours and self._last_travel_slot != slot:
                    try:
                        await self.do_travel()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("猫猫旅行任务异常: %s", e)
                    self._last_travel_slot = slot
            # 活跃地图每日提醒：默认开（只读 + 只提醒，不写上游、不花积分）。
            # 用 `>=` 而不是 `==`：服务在检查点之后才启动（本机开发常态）也能补上，
            # 否则 23:05 重启就白等一天、连登直接断。当天只跑一次由日期槽位保证。
            if (self._active_map_enabled() and now.hour >= self._active_map_hour()
                    and self._last_active_map_date != today):
                try:
                    await self.do_active_map_check()
                except Exception as e:  # noqa: BLE001
                    logger.warning("活跃地图提醒异常: %s", e)
                self._last_active_map_date = today
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
