"""Workbuddy2API 核心模块单元测试（标准库 unittest，无需额外依赖）。

运行：python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestReasoning(unittest.TestCase):
    def test_tool_choice_function(self):
        from workbuddy_one.reasoning import normalize_tool_choice
        b = {"model": "x", "tool_choice": {"type": "function", "function": {"name": "get_weather"}}}
        normalize_tool_choice(b)
        self.assertEqual(b["tool_choice"], "get_weather")

    def test_tool_choice_none(self):
        from workbuddy_one.reasoning import normalize_tool_choice
        b = {"model": "x", "tool_choice": "none", "tools": [1]}
        normalize_tool_choice(b)
        self.assertNotIn("tools", b)
        self.assertNotIn("tool_choice", b)

    def test_reasoning_downgrade(self):
        from workbuddy_one.reasoning import normalize_reasoning_effort
        b = {"model": "deepseek-v4-flash", "reasoning_effort": "max"}
        normalize_reasoning_effort(b)
        self.assertEqual(b["reasoning_effort"], "high")

    def test_reasoning_pass_through_supported(self):
        from workbuddy_one.reasoning import normalize_reasoning_effort
        b = {"model": "glm-5.2", "reasoning_effort": "low"}
        normalize_reasoning_effort(b)
        self.assertEqual(b["reasoning_effort"], "low")

    def test_reasoning_unknown_model(self):
        from workbuddy_one.reasoning import normalize_reasoning_effort
        b = {"model": "unknown", "reasoning_effort": "max"}
        normalize_reasoning_effort(b)
        self.assertEqual(b["reasoning_effort"], "max")


class TestDesensitize(unittest.TestCase):
    def test_zero_width_inserted(self):
        from workbuddy_one.desensitize import desensitize_body
        b = {"messages": [{"role": "system", "content": "Refuse DoS attacks and credential testing"}]}
        out = desensitize_body(b, roles=("system",))
        self.assertIn("\u200b", out["messages"][0]["content"])

    def test_normal_message_unchanged_structure(self):
        from workbuddy_one.desensitize import desensitize_body
        b = {"messages": [{"role": "user", "content": "hello world"}]}
        out = desensitize_body(b, roles=("system",))
        self.assertEqual(out["messages"][0]["content"], "hello world")


class TestPool(unittest.TestCase):
    def _pool(self):
        from workbuddy_one.pool import Account, AccountPool
        accounts = [
            Account(uid="a", mgr=None),
            Account(uid="b", mgr=None),
            Account(uid="c", mgr=None),
        ]
        for a in accounts:
            a.credits_remaining = 100
        return AccountPool({a.uid: a for a in accounts})

    def test_weighted_distribution(self):
        pool = self._pool()
        # 同权重：多次 pick 应覆盖到全部账号（加权随机虽不保证严格轮换，但不该只选一个）
        picked = [pool.pick().uid for _ in range(30)]
        self.assertEqual(len(set(picked)), 3)
        # 每个账号占比应落在 (5%, 75%) 区间（同权重下不该有账号被完全饿死或垄断）
        from collections import Counter
        cnt = Counter(picked)
        for uid in ("a", "b", "c"):
            self.assertTrue(0.05 < cnt[uid] / len(picked) < 0.75, f"{uid} 占比异常: {cnt}")

    def test_expiry_prioritized(self):
        # 到期紧迫度：快到期账号进入硬性优先池，应显著优先被选
        from workbuddy_one.pool import AccountPool
        import time
        now = time.time()
        pool = AccountPool({})
        pool.add_account("far", None)
        pool.add_account("soon", None)
        far = next(a for a in pool.accounts if a.uid == "far")
        soon = next(a for a in pool.accounts if a.uid == "soon")
        far.credits_remaining = 1000; far.credits_total = 1000; far.credits_expire_at = now + 90 * 86400
        soon.credits_remaining = 1000; soon.credits_total = 1000; soon.credits_expire_at = now + 1 * 86400
        # 快到期账号应为硬性优先（阶段1只在快到期池选）
        from collections import Counter
        cnt = Counter(pool.pick().uid for _ in range(100))
        self.assertEqual(cnt.get("soon", 0), 100, f"到期硬性优先未生效: {cnt}")
        # 权重比：快到期 E=8 vs 长期 E=1（其他因子相同）
        w_soon = pool._weight(soon, now)
        w_far = pool._weight(far, now)
        self.assertAlmostEqual(w_soon / w_far, 8.0, delta=0.01)

    def test_priority_weight(self):
        from workbuddy_one.pool import AccountPool
        import time
        now = time.time()
        pool = AccountPool({})
        pool.add_account("low", None)
        pool.add_account("high", None)
        low = next(a for a in pool.accounts if a.uid == "low")
        high = next(a for a in pool.accounts if a.uid == "high")
        low.credits_remaining = 100; low.credits_total = 100; low.priority = 0
        high.credits_remaining = 100; high.credits_total = 100; high.priority = 3
        w_high = pool._weight(high, now)
        w_low = pool._weight(low, now)
        # priority=3 → (1+3)=4 倍
        self.assertAlmostEqual(w_high / w_low, 4.0, delta=0.01)

    def test_failure_cooldown(self):
        pool = self._pool()
        first = pool.pick()
        pool.on_failure(first.uid, 60.0)
        # 冷却中的账号不应被选中
        for _ in range(5):
            self.assertNotEqual(pool.pick().uid, first.uid)

    def test_disable(self):
        pool = self._pool()
        pool.set_enabled("a", False)
        for _ in range(5):
            self.assertNotEqual(pool.pick().uid, "a")


class TestDB(unittest.TestCase):
    def test_log_and_summary(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "test.db"))
        try:
            db.upsert_account({"path": "/x"}, {"uid": "u1", "nick": "acc"})
            db.log_usage(model="m1", protocol="chat", account_uid="u1",
                         input_tokens=10, output_tokens=5, latency_ms=100, status="ok")
            db.log_usage(model="m1", protocol="chat", account_uid="u1",
                         input_tokens=0, output_tokens=0, latency_ms=50, status="error", error="boom")
            s = db.usage_summary()
            self.assertEqual(s["total_requests"], 2)
            self.assertGreaterEqual(s["total_tokens"], 15)
            recent = db.usage_recent(10)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[0]["status"], "error")
        finally:
            db._conn.close()
            for f in tmp.glob("test.db*"):
                f.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
