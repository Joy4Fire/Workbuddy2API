"""多账号池：管理多个 CredentialManager，round-robin 轮换 + 冷却 + 额度感知。

单用户少量账号，简化设计（借鉴 Sliverkiss/cli2api 的调度思想，但不做复杂的
多因子加权/熔断/会话粘性，够用即可）。
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Optional

from .credentials import CredentialManager

# 项目 auths/ 目录（账号来源分类用）
_PROJECT_AUTHS = (Path(__file__).resolve().parent.parent / "auths")


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
        self.cooldown_until = 0.0          # 冷却截止时间
        self.failure_count = 0
        self.credits_remaining: int | None = None
        self.credits_total: int | None = None
        self.credits_expire_at: float | None = None   # 积分最早到期时间戳（秒），无则 None
        self.credit_packages: list[dict] = []         # 积分构成明细（按商品聚合，运行时数据不落库）
        self.priority: int = 0             # 用户指定优先级（越大权重越高）
        self.last_used = 0.0

    def healthy(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.cooldown_until > now:
            return False
        # 额度耗尽则跳过（credits_remaining 已查到且为 0）
        if self.credits_remaining is not None and self.credits_remaining <= 0:
            return False
        return True

    def mark_failure(self, cooldown_seconds: float):
        self.failure_count += 1
        self.cooldown_until = time.time() + cooldown_seconds

    def mark_success(self):
        self.failure_count = 0
        self.last_used = time.time()


class AccountPool:
    """账号池：round-robin 选择健康账号。"""

    def __init__(self, managers: dict[str, CredentialManager]):
        self._lock = threading.Lock()
        self.accounts: list[Account] = [Account(uid, mgr) for uid, mgr in managers.items()]

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
            "disabled_reason": a.disabled_reason,
            "healthy": a.healthy(now),
            "cooldown_until": a.cooldown_until,
            "failure_count": a.failure_count,
            "credits_remaining": a.credits_remaining,
            "credits_total": a.credits_total,
            "credits_expire_at": a.credits_expire_at,
            "credit_packages": a.credit_packages,
            "priority": a.priority,
            "weight": round(self._weight(a, now), 3),
            "source": _account_source(a),
        } for a in self.accounts]

    def set_enabled(self, uid: str, enabled: bool, reason: str = ""):
        """启停账号。禁用时必须带原因（供 WebUI 展示"为什么不可用"），启用时清空原因。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.enabled = enabled
                    a.disabled_reason = "" if enabled else (reason or "已禁用")
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

    def set_priority(self, uid: str, priority: int):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.priority = int(priority or 0)
                    return

    def clear_cooldown(self, uid: str):
        """清除冷却与失败计数。用于签到/余额恢复后自动解冻冷却中的账号。

        注意：不复活被显式停用（enabled=False）的账号——手动停用/保活自动禁用
        的账号只能由用户在 WebUI 重新启用；否则每轮额度刷新都会把停用账号
        重新打开，停用状态形同虚设。
        """
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.cooldown_until = 0.0
                    a.failure_count = 0
                    return

    def add_account(self, uid: str, mgr: CredentialManager) -> bool:
        """动态注册一个账号；已存在则返回 False。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    return False
            self.accounts.append(Account(uid, mgr))
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

    def pick(self):
        """加权随机选择下一个健康账号。

        两阶段策略（积分优先消耗）：
          阶段 1：存在「快到期」健康账号（到期 ≤ EXPIRY_PRIORITY_DAYS 天）时，
                 只在快到期账号里按 (优先级×额度×成功率) 加权选——先消耗快过期的积分。
          阶段 2：否则在所有健康账号里按全因子（含闲置补偿）加权选。
        无健康账号时退回「最早冷却到期账号」顶班。
        """
        with self._lock:
            now = time.time()
            candidates = [a for a in self.accounts if a.healthy(now)]
            if not candidates:
                # 没有健康账号：找一个已过期冷却的最早冷却账号（尽量）
                best = None
                best_expiry = float("inf")
                for acc in self.accounts:
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
