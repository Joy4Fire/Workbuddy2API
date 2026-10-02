"""多账号池：管理多个 CredentialManager，round-robin 轮换 + 冷却 + 额度感知。

单用户少量账号，简化设计（借鉴 Sliverkiss/cli2api 的调度思想，但不做复杂的
多因子加权/熔断/会话粘性，够用即可）。
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from . import region
from .config import config
from .credentials import CredentialManager
# 负缓存 TTL 上限与错误处置策略同源（gateway.errors 不反向依赖本模块，无循环导入）
from .gateway.errors import MODEL_BLOCK_MAX_TTL

# 项目 auths/ 目录（账号来源分类用）
_PROJECT_AUTHS = (Path(__file__).resolve().parent.parent / "auths")

# ---- 成本台账（P2-3）----
# 观测有效期：超过这个时长的单价不再参与分层（上游计费/活动会变，旧观测会误导）。
# 6 小时是折中——太短则每个新会话都要重新学，太长则活动结束后还按免费号挑。
COST_TTL = 6 * 3600.0
# EMA 平滑系数：新样本占 alpha。取 0.3 是为了"既跟得上变化，又不被单次异常带偏"
# （单次请求的 credit 可能因为缓存命中/长输出而大幅偏离常态）。
COST_EMA_ALPHA = 0.3
# 判定"实测免费"的阈值（每千 token 积分）。上游免费额度包返回 credit=0，
# 用一个小阈值而不是严格 ==0，容忍浮点与四舍五入。
COST_FREE_EPS = 1e-6


def _mgr_domain(mgr) -> str:
    """安全取凭据所属域名。

    部分测试用 Account(uid=..., mgr=None) 构造纯逻辑账号（只验证权重/冷却），
    所以这里必须容忍 mgr 缺失，缺失即按默认区域（国内版）处理。
    """
    return getattr(mgr, "domain", "") if mgr is not None else ""


def _mgr_encrypted_fields(mgr) -> list[str]:
    """安全取凭据的加密字段列表（mgr 缺失或读不出都按「未加密」处理）。"""
    if mgr is None:
        return []
    try:
        return list(mgr.encrypted_fields())
    except Exception:  # noqa: BLE001  仅用于展示，不因展示失败影响账号列表
        return []


def _account_source(acc) -> str:
    """判断账号来源：项目 auths/ 上传/扫码，或本机 CodeBuddy 目录。"""
    try:
        p = Path(acc.mgr.path).resolve()
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        if str(p).startswith(str(_PROJECT_AUTHS.resolve())):
            return "project"
    except Exception:  # noqa: BLE001
        pass
    return "local"


class Account:
    """池中的一个账号。"""

    def __init__(self, uid: str, mgr: CredentialManager):
        self.uid = uid
        self.mgr = mgr
        self.enabled = True
        self.disabled_reason = ""         # 禁用原因（"手动停用"/"保活连续失败"…），启用时为空
        # 系统自动禁用原因（与"运维手动停用"**独立**的第二个状态位，见 set_enabled 注释）。
        # 非空 = 该账号被系统摘出选号池（session 失效、账号被上游封禁等）。
        self.auto_disabled_reason = ""
        self.cooldown_until = 0.0          # 冷却截止时间
        self.failure_count = 0
        # 连败降权（P1-2）：**无权威分类**的失败（ErrClient/传输层）连续 N 次后临时出池。
        # 与 cooldown_until 分开的原因：冷却语义是"已知原因的等待"，连败语义是
        # "反复出同一个错、但说不清原因"——两者取更长者生效，但计数各归各的。
        self.fail_streak = 0
        self.degrade_until = 0.0           # 连败降权出池截止
        # (账号, 模型) 维度的冷却：模型级限流（6004）与"该后端无此模型"（11102）
        # 都落在这里。键 = 模型名，值 = 冷却截止时间戳。
        # 为什么必须按模型分：6004 是"这个模型此刻被限"，账号本身是健康的——
        # 冷却落在账号上会把整个号打停，而用户换个模型本来就能用。
        self.model_cooldowns: dict[str, float] = {}
        # 11102 负缓存的连续次数：同一个 (账号,模型) 反复被判不可用 → TTL 指数放大
        # （6h → 12h → 24h），避免每 6 小时就去上游碰一次钉子。
        self.model_block_streak: dict[str, int] = {}
        # (账号, 模型) 的积分单价台账（P2-3）：{模型: {"cost": 每千 token 积分, "ts": 观测时刻}}
        # 只记成功请求的观测，EMA 平滑；COST_TTL 之后视为无效（回落"无观测"）。
        self.model_costs: dict[str, dict] = {}
        # 单账号在途计数（P1-1）：只覆盖"建立连接 → 首字节到达"这段窗口，见 acquire_slot。
        # capacity_limit 由 AccountPool 按区域注入（0 = 不限制）；直接 new 出来的
        # Account（单测常用）保持 0，行为与加这个特性之前一致。
        self.in_flight = 0
        self.capacity_limit = 0
        self.credits_remaining: int | None = None
        self.credits_total: int | None = None
        self.credits_expire_at: float | None = None   # 积分最早到期时间戳（秒），无则 None
        self.credit_packages: list[dict] = []         # 积分构成明细（按商品聚合，运行时数据不落库）
        self.priority: int = 0             # 用户指定优先级（越大权重越高）
        self.last_used = 0.0
        # 上游签到状态（None = 还没同步过，界面回落本地记账）。语义见 db.py 同名注释。
        # 放运行时实例上是为了让 /admin/accounts 不必每次请求都打上游；
        # 由 scheduler.refresh_credits() 每轮顺带同步，并持久化到 DB 供重启恢复。
        self.checkin_today: bool | None = None
        self.checkin_active: bool | None = None
        self.checkin_synced_at: float | None = None
        # 区域（"cn"/"global"）：账号一建立就按 auth 文件里的 domain 定下来。
        # 缓存到实例上而不是每次 pick 现算——pick 持池锁，不该在里面反复 stat 文件。
        # domain 对同一账号不会变（换区域等于换账号），缓存是安全的。
        self.region_id = region.detect_region(_mgr_domain(mgr)).id

    def healthy(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.auto_disabled_reason:
            return False
        if self.cooldown_until > now:
            return False
        if self.degrade_until > now:
            return False
        # 额度耗尽则跳过（credits_remaining 已查到且为 0）
        if self.credits_remaining is not None and self.credits_remaining <= 0:
            return False
        return True

    def healthy_for_model(self, now: float, model: str = "") -> bool:
        """账号健康 **且** 该模型没被这个账号冷却（6004 模型级限流 / 11102 负缓存）。"""
        if not self.healthy(now):
            return False
        if model and self.model_cooldowns.get(model, 0.0) > now:
            return False
        return True

    def model_cooling(self, now: float, model: str) -> bool:
        return bool(model) and self.model_cooldowns.get(model, 0.0) > now

    def cost_of(self, now: float, model: str) -> float | None:
        """该 (账号, 模型) 的每千 token 积分单价；无观测或观测过期返回 None。"""
        if not model:
            return None
        obs = self.model_costs.get(model)
        if not obs or now - obs["ts"] > COST_TTL:
            return None
        return float(obs["cost"])

    def cost_tier(self, now: float, model: str) -> int:
        """成本分层：0 = 实测免费，1 = 无观测，2 = 实测收费。

        分层而不是排序的理由：账号之间**没有可比性**的地方太多（额度余量、到期
        紧迫度、区域），把单价塞进加权公式会和其他因子互相抵消，调不动也说不清。
        分层只回答一个问题："有没有明确的便宜号可用"。
        """
        c = self.cost_of(now, model)
        if c is None:
            return 1
        return 0 if c <= COST_FREE_EPS else 2

    def mark_failure(self, cooldown_seconds: float):
        self.failure_count += 1
        self.cooldown_until = time.time() + cooldown_seconds

    def mark_success(self):
        self.failure_count = 0
        self.fail_streak = 0
        self.degrade_until = 0.0
        self.last_used = time.time()


class AccountPool:
    """账号池：round-robin 选择健康账号。"""

    def __init__(self, managers: dict[str, CredentialManager]):
        self._lock = threading.Lock()
        self.accounts: list[Account] = [self._new_account(uid, mgr)
                                        for uid, mgr in managers.items()]

    @staticmethod
    def _new_account(uid: str, mgr: CredentialManager) -> Account:
        """建账号并按区域注入在途并发上限（P1-1）。

        上限按区域分档：国际版（global）的风控档位更严，给更小的窗口。
        放在建账号处而不是 pick 里现算，是为了让 pick 持锁时不读全局配置。
        """
        acc = Account(uid, mgr)
        acc.capacity_limit = (config.max_in_flight_global_effective
                              if acc.region_id == "global" else config.max_in_flight_effective)
        return acc

    def apply_capacity_limits(self):
        """按当前配置刷新所有账号的在途并发上限。

        WebUI 改完立即调用（见 admin_save_settings）：上限存在实例上而不是 pick
        时现读，所以改了必须显式刷一次，否则要等重启才生效。
        """
        with self._lock:
            for a in self.accounts:
                a.capacity_limit = (config.max_in_flight_global_effective
                                    if a.region_id == "global"
                                    else config.max_in_flight_effective)

    @property
    def count(self) -> int:
        return len(self.accounts)

    def healthy_count(self) -> int:
        now = time.time()
        return sum(1 for a in self.accounts if a.healthy(now))

    def all_accounts(self) -> list[dict]:
        now = time.time()
        return [{
            "uid": a.uid,
            "enabled": a.enabled,
            "disabled_reason": self.display_disabled_reason(a),
            "manual_disabled": not a.enabled,
            "auto_disabled_reason": a.auto_disabled_reason,
            "healthy": a.healthy(now),
            "cooldown_until": a.cooldown_until,
            "cooldown_remaining": max(0.0, a.cooldown_until - now),
            "failure_count": a.failure_count,
            "fail_streak": a.fail_streak,
            "degrade_until": a.degrade_until,
            "in_flight": a.in_flight,
            "model_cooldowns": {m: u for m, u in a.model_cooldowns.items() if u > now},
            "model_costs": {m: round(o["cost"], 6) for m, o in a.model_costs.items()
                            if now - o["ts"] <= COST_TTL},
            "credits_remaining": a.credits_remaining,
            "credits_total": a.credits_total,
            "credits_expire_at": a.credits_expire_at,
            "credit_packages": a.credit_packages,
            "priority": a.priority,
            "weight": round(self._weight(a, now), 3),
            "source": _account_source(a),
            # 区域：混池时前端要能看出每个账号是国内版还是国际版（模型集不同）
            "domain": _mgr_domain(a.mgr),
            "region": a.region_id,
            "region_label": region.REGIONS[a.region_id].label,
            # $wbEncrypted 加密登录态（WorkBuddy 5.6.0+）：非空表示该账号当前不可用，
            # 前端据此给出「登录态已加密」而不是误导性的「token 过期」
            "auth_encrypted_fields": _mgr_encrypted_fields(a.mgr),
            # 上游签到状态（None = 尚未同步，调用方自行回落本地记账）
            "checkin_today": a.checkin_today,
            "checkin_active": a.checkin_active,
            "checkin_synced_at": a.checkin_synced_at,
        } for a in self.accounts]

    @staticmethod
    def display_disabled_reason(a: Account) -> str:
        """给 WebUI 看的"为什么不可用"：手动停用优先，其次是系统自动禁用原因。

        两个状态位在存储上独立（见 set_enabled），但展示时必须合成一个字符串——
        前端只想知道"这个号为什么选不到"，不需要知道是哪个位干的。
        """
        if not a.enabled:
            return a.disabled_reason or "手动停用"
        return a.auto_disabled_reason

    def set_enabled(self, uid: str, enabled: bool, reason: str = ""):
        """启停账号（**运维手动**语义）。禁用时必须带原因（供 WebUI 展示）。

        与 `disable_auto` 的分工（P2-2）：
          - 本方法只动 `enabled`（手动位）。手动**启用**不清除自动禁用原因——
            否则用户点一下"启用"就会把一个 session 已失效的账号放回池子，
            下一个请求立刻再吃一次 401，看起来像"点了没用"。
          - 自动禁用原因只能由 `enable_auto` 清除，而它只在"有明确证据说明故障
            已恢复"时被调用（目前是用户重新扫码登录）。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.enabled = enabled
                    a.disabled_reason = "" if enabled else (reason or "已禁用")
                    return

    def disable_auto(self, uid: str, reason: str):
        """系统自动摘出账号（session 失效 / 被上游封禁）。与手动停用独立。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.auto_disabled_reason = reason or "系统自动禁用"
                    return

    def enable_auto(self, uid: str):
        """清除系统自动禁用（只在故障确有恢复路径时调用，如重新扫码登录）。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.auto_disabled_reason = ""
                    return

    def set_credits(self, uid: str, remain, total, expire_at=None, packages: list[dict] | None = None):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.credits_remaining = remain
                    a.credits_total = total
                    a.credits_expire_at = expire_at
                    if packages is not None:
                        a.credit_packages = packages
                    return

    def set_checkin_status(self, uid: str, today: bool, active: bool, synced_at: float):
        """写入从上游查到的签到状态（None 语义见 Account.checkin_today 注释）。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.checkin_today = bool(today)
                    a.checkin_active = bool(active)
                    a.checkin_synced_at = synced_at
                    return

    def set_priority(self, uid: str, priority: int):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.priority = int(priority or 0)
                    return

    def clear_cooldown(self, uid: str):
        """清除冷却与失败计数。用于签到/余额恢复后自动解冻冷却中的账号。

        注意：
        - 不复活被显式停用（enabled=False）的账号——手动停用/保活自动禁用
          的账号只能由用户在 WebUI 重新启用；否则每轮额度刷新都会把停用账号
          重新打开，停用状态形同虚设。
        - **也不清除自动禁用位**（`auto_disabled_reason`）——它是"上游说这个账号
          不能用了"（session 失效/被封禁），额度恢复不代表登录态恢复。
        - **不清模型级冷却**：6004/11102 是模型维度的事实，与账号余额无关。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.cooldown_until = 0.0
                    a.failure_count = 0
                    a.fail_streak = 0
                    a.degrade_until = 0.0
                    return

    # ---------------- (账号, 模型) 维度冷却（P0-2）----------------

    def cooldown_model(self, uid: str, model: str, ttl: float, neg_cache: bool = False):
        """把某个 (账号, 模型) 组合冷却 ttl 秒；neg_cache=True 走指数放大。

        两类来源共用这张表（与 Sliverkiss 的 modelCooldowns 同设计）：
          - 6004 模型级限流：`neg_cache=False`，TTL 由上游重置墙钟决定，
            每次覆盖（不放大）——上游已经明说了何时恢复，不该再叠惩罚。
          - 11102「该后端无此模型」：`neg_cache=True`，TTL 按连续命中次数指数放大
            （6h → 12h → 24h 封顶）。理由：模型可用性不会在几小时内变来变去，
            每次 6 小时就去上游碰一次钉子是纯浪费。
        """
        if not model:
            return
        with self._lock:
            for a in self.accounts:
                if a.uid != uid:
                    continue
                if neg_cache:
                    streak = a.model_block_streak.get(model, 0) + 1
                    a.model_block_streak[model] = streak
                    ttl = min(ttl * (2 ** (streak - 1)), MODEL_BLOCK_MAX_TTL)
                until = time.time() + max(1.0, ttl)
                # 取更晚者：并发请求可能同时命中，不该被后来的短 TTL 缩短
                a.model_cooldowns[model] = max(a.model_cooldowns.get(model, 0.0), until)
                return

    def clear_model_cooldown(self, uid: str, model: str = "") -> bool:
        """清除模型级冷却（model 为空则清全部）。返回是否真的清掉了东西。

        返回值的用途：调用方据此决定要不要顺手删 DB 里的持久化记录，
        避免每次成功请求都白跑一次 DELETE。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid != uid:
                    continue
                if model:
                    removed = a.model_cooldowns.pop(model, None) is not None
                    a.model_block_streak.pop(model, None)
                    return removed
                removed = bool(a.model_cooldowns) or bool(a.model_block_streak)
                a.model_cooldowns.clear()
                a.model_block_streak.clear()
                return removed
        return False

    def model_cooldowns(self, now: float | None = None) -> list[dict]:
        """当前所有仍在冷却的 (账号, 模型) 组合，供 WebUI/诊断展示。"""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for a in self.accounts:
                for model, until in a.model_cooldowns.items():
                    if until > now:
                        out.append({"uid": a.uid, "model": model, "until": until,
                                    "remaining": until - now})
        return sorted(out, key=lambda x: -x["remaining"])

    def load_model_blocks(self, rows: list[dict]):
        """启动时从 DB 恢复 (账号,模型) 冷却（含负缓存 streak）。

        已过期的记录直接跳过（不复活）；池里不存在的 uid 也跳过（账号被删过）。
        """
        now = time.time()
        with self._lock:
            by_uid = {a.uid: a for a in self.accounts}
            for r in rows:
                acc = by_uid.get(r.get("uid"))
                model = str(r.get("model") or "")
                until = float(r.get("until") or 0)
                if acc is None or not model or until <= now:
                    continue
                acc.model_cooldowns[model] = until
                streak = int(r.get("streak") or 0)
                if streak:
                    acc.model_block_streak[model] = streak

    def model_block_state(self, uid: str, model: str) -> dict | None:
        """取一条可落库的 (账号,模型) 冷却快照；已过期或不存在返回 None。"""
        now = time.time()
        with self._lock:
            for a in self.accounts:
                if a.uid != uid:
                    continue
                until = a.model_cooldowns.get(model, 0.0)
                if until <= now:
                    return None
                return {"uid": uid, "model": model, "until": until,
                        "streak": a.model_block_streak.get(model, 0)}
        return None

    # ---------------- 成本台账（P2-3）----------------

    def record_cost(self, uid: str, model: str, credits: float, tokens: int) -> dict | None:
        """用一次成功请求的用量更新 (账号, 模型) 单价台账。返回落库快照（无观测则 None）。

        只认"有 token 数"的观测：tokens=0 时单价无意义（除零），直接跳过。
        `credits=0` 是**有效观测**（免费额度包），会把它记成 tier0，不是"没有数据"。
        """
        if not model or tokens <= 0:
            return None
        try:
            sample = max(0.0, float(credits or 0.0)) * 1000.0 / float(tokens)
        except (TypeError, ValueError):
            return None
        with self._lock:
            for a in self.accounts:
                if a.uid != uid:
                    continue
                now = time.time()
                prev = a.model_costs.get(model)
                # 观测过期则丢弃旧值，从这一次重新起步（不把 6 小时前的价格混进来）
                if prev and now - prev["ts"] <= COST_TTL:
                    cost = prev["cost"] * (1 - COST_EMA_ALPHA) + sample * COST_EMA_ALPHA
                    samples = int(prev.get("samples", 1)) + 1
                else:
                    cost = sample
                    samples = 1
                a.model_costs[model] = {"cost": cost, "ts": now, "samples": samples}
                return {"uid": uid, "model": model, "cost_per_1k": cost,
                        "samples": samples, "updated_at": now}
        return None

    def load_costs(self, rows: list[dict]):
        """启动时从 DB 恢复单价台账（过期的直接跳过）。"""
        now = time.time()
        with self._lock:
            by_uid = {a.uid: a for a in self.accounts}
            for r in rows:
                acc = by_uid.get(r.get("uid"))
                model = str(r.get("model") or "")
                ts = float(r.get("updated_at") or 0)
                if acc is None or not model or now - ts > COST_TTL:
                    continue
                acc.model_costs[model] = {
                    "cost": float(r.get("cost_per_1k") or 0.0),
                    "ts": ts,
                    "samples": int(r.get("samples") or 1),
                }

    def cost_table(self, now: float | None = None) -> list[dict]:
        """当前有效的单价台账（供 WebUI/诊断展示），按单价降序。

        台账是 **(账号, 模型)** 维度的：账号天然归属某个区域，所以它同时也是
        "国内版账号 / 国际版账号"各自的**实测**消耗，与模型目录里那个上游标称的
        倍率（`credits`）不是一回事——这里是真的按 `积分 / token` 算出来的。

        只含 `COST_TTL` 内的观测：过期的价格比"没有价格"更误导（上游调价后
        旧值会一直骗人），所以宁可让这一行消失。
        """
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for a in self.accounts:
                for model, obs in a.model_costs.items():
                    if now - obs["ts"] > COST_TTL:
                        continue
                    out.append({"uid": a.uid, "model": model,
                                "cost_per_1k": round(obs["cost"], 6),
                                "samples": obs.get("samples", 1),
                                "updated_at": obs["ts"],
                                # 区域随行带出：混池时前端要能一眼看出这条是国内还是国际
                                "region": a.region_id,
                                "region_label": region.REGIONS[a.region_id].label,
                                "tier": 0 if obs["cost"] <= COST_FREE_EPS else 2})
        return sorted(out, key=lambda x: -x["cost_per_1k"])

    # ---------------- 连败降权（P1-2）----------------

    def note_failures(self, uid: str, threshold: int = 5, degrade_seconds: float = 600.0):
        """喂一次"无权威分类的失败"（ErrClient / 传输层）。达阈值则临时出池。

        为什么需要它：ErrClient 这类错误我们**故意不罚号**（未知 4xx 罚号会误伤
        好号），但"一个号连续 10 次都返回同一个说不清的错"显然是它有问题。
        连败计数就是给这种场景兜底的——单次偶发不罚，连续成串才降权。

        与冷却的关系：两者**取更长者生效**，不叠加。冷却到期但降权未到期 → 仍然
        选不到；反之亦然。带权威分类的错误（429/402/11115…）不喂连败计数——
        它们各有精确的恢复时刻，再叠一个"连续失败"只会让正常重试越堆越厚。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid != uid:
                    continue
                a.fail_streak += 1
                if a.fail_streak >= threshold:
                    a.degrade_until = max(a.degrade_until, time.time() + degrade_seconds)
                return

    # ---------------- 单账号在途并发（P1-1）----------------

    def acquire_slot(self, uid: str) -> bool:
        """占用一个在途名额；该账号已达上限返回 False（调用方自行决定要不要照发）。

        只覆盖"建立连接 → 首字节到达"这段窗口（见 inference.open_upstream_once）：
        上游的风控看的是**同时打到它的连接数**，而流一旦建立就只剩一条长连接，
        再计入并发数没有意义，反而会引入"流没读完就泄漏名额"的故障模式。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    if a.capacity_limit > 0 and a.in_flight >= a.capacity_limit:
                        return False
                    # 无上限时也计数，热修改上限不能丢失已有连接的所有权。
                    a.in_flight += 1
                    return True
        return True

    def release_slot(self, uid: str):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.in_flight = max(0, a.in_flight - 1)
                    return

    def in_flight_total(self) -> int:
        with self._lock:
            return sum(a.in_flight for a in self.accounts)

    def add_account(self, uid: str, mgr: CredentialManager) -> bool:
        """动态注册一个账号；已存在则返回 False。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    return False
            self.accounts.append(self._new_account(uid, mgr))
            return True

    def remove_account(self, uid: str) -> bool:
        """移除一个账号；不存在则返回 False。"""
        with self._lock:
            for i, a in enumerate(self.accounts):
                if a.uid == uid:
                    del self.accounts[i]
                    return True
            return False

    # 到期紧迫阈值（天）：距到期 ≤ 此天数视为「快到期」，进入硬性优先池
    EXPIRY_PRIORITY_DAYS = 7

    def _eligible(self, now, regions, model):
        enabled = [a for a in self.accounts if a.enabled and not a.auto_disabled_reason]
        if regions:
            scoped = [a for a in enabled if a.region_id in regions]
            enabled = scoped or enabled
        if model:
            enabled = [a for a in enabled if not a.model_cooling(now, model)]
        return enabled

    @staticmethod
    def _prefer(candidates, now, model, prices):
        # 先排除占满的账号，使低价通道繁忙时能由可用的高价通道接手。
        free = [a for a in candidates if a.capacity_limit <= 0 or a.in_flight < a.capacity_limit]
        candidates = free or candidates
        if model and candidates:
            known = [a for a in candidates if a.region_id in prices]
            if known:
                lowest = min(prices[a.region_id] for a in known)
                candidates = [a for a in known if prices[a.region_id] == lowest]
            # 同报价内保留免费/未知账号的探索；收费账号按实际单价继续比较。
            cheap = [a for a in candidates if a.cost_tier(now, model) != 2]
            if cheap:
                candidates = cheap
            else:
                lowest = min(a.cost_of(now, model) for a in candidates)
                candidates = [a for a in candidates if a.cost_of(now, model) == lowest]
        return candidates

    def can_reuse(self, uid, *, regions=None, model="", prices=None) -> bool:
        """粘性仅在当前优选候选里复用，不能绕过更低价的健康账号。"""
        with self._lock:
            now = time.time()
            candidates = [a for a in self._eligible(now, regions, model) if a.healthy(now)]
            return any(a.uid == uid for a in self._prefer(candidates, now, model, prices or {}))

    def pick(self, regions: set[str] | None = None, model: str = "", prices: dict | None = None,
             exclude: set[str] | None = None):
        """加权随机选择下一个健康账号。

        先限定健康且有空闲名额的候选，再按区域报价选最低档，同价内比较实测成本。
        粘性复用使用同一候选规则。最后仅在同价池内按积分到期和权重选择：
          阶段 1：存在「快到期」健康账号（到期 ≤ EXPIRY_PRIORITY_DAYS 天）时，
                 只在快到期账号里按 (优先级×额度×成功率) 加权选——先消耗快过期的积分。
          阶段 2：否则在所有健康账号里按全因子（含闲置补偿）加权选。
        无健康账号时退回「最早冷却到期账号」顶班。

        过滤器的**软硬**之分是刻意的：
          - `regions` 软过滤：指定区域内没有健康账号时退回不过滤。宁可试一次
            （可能是目录没拉到/别名），也不要直接把请求打成 503。
          - `model` 硬过滤：该模型在这个账号上被冷却（6004 模型级限流 / 11102 负缓存）
            时**跳过**，没有候选就返回 None。这不是"信息不足"，而是上游已经明确
            告诉我们这个组合不可用——退回重试只会再吃一次同样的错。
          - 在途名额软过滤：优先选有空闲名额的账号；全都占满时退回不过滤
            （单账号场景下不这样就永远选不出号）。名额上限按区域注入，见
            `Account.capacity_limit`。
        """
        with self._lock:
            now = time.time()
            all_enabled = [a for a in self._eligible(now, regions, model)
                           if not exclude or a.uid not in exclude]
            candidates = [a for a in all_enabled if a.healthy(now)]
            candidates = self._prefer(candidates, now, model, prices or {})
            if not candidates:
                # 没有健康账号：找一个已过期冷却的最早冷却账号（尽量）
                best = None
                best_expiry = float("inf")
                for acc in all_enabled:
                    # 兜底只能放宽短期冷却，不能绕过额度耗尽或连败摘除。
                    if acc.degrade_until > now or (acc.credits_remaining is not None and acc.credits_remaining <= 0):
                        continue
                    if acc.enabled and acc.cooldown_until < best_expiry:
                        best = acc
                        best_expiry = acc.cooldown_until
                if best is not None:
                    best.last_used = now
                return best
            # 阶段 1：快到期硬性优先
            urgent = [a for a in candidates if self._days_to_expiry(a, now) <= self.EXPIRY_PRIORITY_DAYS]
            pool_to_pick = urgent if urgent else candidates
            # 阶段 2（或快到期池内）：按因子加权（到期池内不再叠加闲置补偿，避免抵消优先意图）
            total_w = 0.0
            weights = []
            for a in pool_to_pick:
                w = self._weight(a, now, apply_idle=(not urgent))
                weights.append(w)
                total_w += w
            pick = random.uniform(0.0, total_w)
            acc = pool_to_pick[-1]
            for a, w in zip(pool_to_pick, weights):
                pick -= w
                if pick <= 0:
                    acc = a
                    break
            acc.last_used = now
            return acc

    @staticmethod
    def _days_to_expiry(acc: Account, now: float) -> float:
        if not acc.credits_expire_at:
            return float("inf")
        return (acc.credits_expire_at - now) / 86400.0

    @staticmethod
    def _weight(acc: Account, now: float, apply_idle: bool = True) -> float:
        """单账号选号权重（多因子乘积）。

        W = (1+priority) × C(额度充足) × E(到期紧迫) × S(成功率) × [I(闲置补偿)]
        在快到期硬性优先池内，闲置补偿不叠加（apply_idle=False），避免抵消优先意图。
        """
        # 1) 优先级：priority=0 默认 1 倍，每 +1 权重 +1 倍
        p = 1 + max(0, acc.priority or 0)
        # 2) 额度充足度：满额=1，耗尽=0.5（仍保留入选机会，避免饿死）
        c = 1.0
        if acc.credits_total:
            ratio = max(0.0, (acc.credits_remaining or 0)) / acc.credits_total
            c = 0.5 + 0.5 * ratio
        # 3) 到期紧迫度：越快到期权重越高
        e = 1.0
        days = AccountPool._days_to_expiry(acc, now)
        if days <= 0:
            e = 8.0
        elif days <= 1:
            e = 8.0
        elif days <= 3:
            e = 6.0
        elif days <= 7:
            e = 4.0
        elif days <= 30:
            e = 2.0
        # 4) 成功率：失败越多权重越低
        s = 1.0 / (1.0 + acc.failure_count)
        # 5) 闲置补偿（可关闭）：闲置越久权重越高，封顶 3 倍
        i = 1.0
        if apply_idle:
            idle_h = (now - acc.last_used) / 3600.0 if acc.last_used else 0.0
            i = min(1.0 + idle_h * 0.3, 3.0)
        return p * c * e * s * i

    def on_success(self, uid: str):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.mark_success()
                    return

    def on_failure(self, uid: str, cooldown_seconds: float):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.mark_failure(cooldown_seconds)
                    return
