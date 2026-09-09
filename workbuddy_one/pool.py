"""多账号池：管理多个 CredentialManager，round-robin 轮换 + 冷却 + 额度感知。

单用户少量账号，简化设计（借鉴 Sliverkiss/cli2api 的调度思想，但不做复杂的
多因子加权/熔断/会话粘性，够用即可）。
"""
from __future__ import annotations

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
        self.cooldown_until = 0.0          # 冷却截止时间
        self.failure_count = 0
        self.credits_remaining: int | None = None
        self.credits_total: int | None = None
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
        self._rr = 0

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
            "healthy": a.healthy(now),
            "cooldown_until": a.cooldown_until,
            "failure_count": a.failure_count,
            "credits_remaining": a.credits_remaining,
            "credits_total": a.credits_total,
            "source": _account_source(a),
        } for a in self.accounts]

    def set_enabled(self, uid: str, enabled: bool):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.enabled = enabled
                    return

    def set_credits(self, uid: str, remain, total):
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.credits_remaining = remain
                    a.credits_total = total
                    return

    def clear_cooldown(self, uid: str, enabled: bool = True):
        """清除冷却（可选同时启用）。用于签到/余额恢复后自动解冻。"""
        with self._lock:
            for a in self.accounts:
                if a.uid == uid:
                    a.cooldown_until = 0.0
                    a.failure_count = 0
                    if enabled:
                        a.enabled = True
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
                    self._rr = max(0, self._rr - 1)
                    return True
            return False

    def pick(self) -> Optional[Account]:
        """round-robin 选择下一个健康账号。无健康账号返回 None。"""
        with self._lock:
            now = time.time()
            n = len(self.accounts)
            if n == 0:
                return None
            for _ in range(n):
                self._rr = (self._rr + 1) % n
                acc = self.accounts[self._rr]
                if acc.healthy(now):
                    acc.last_used = now
                    return acc
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
