"""Workbuddy2API 核心模块单元测试（标准库 unittest，无需额外依赖）。

运行：python -m unittest discover -s tests
"""
import asyncio
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

    def test_v41_flash_effort_known(self):
        # deepseek-v4.1-flash 目录支持 reasoning 至 high（cli2api #146 目录元数据）
        from workbuddy_one.reasoning import normalize_reasoning_effort
        b = {"model": "deepseek-v4.1-flash", "reasoning_effort": "max"}
        normalize_reasoning_effort(b)
        self.assertEqual(b["reasoning_effort"], "high")

    # --- developer 角色归一（上游 role 白名单，防 11128） ---
    def test_normalize_roles_developer(self):
        from workbuddy_one.reasoning import normalize_roles
        b = {"messages": [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]}
        normalize_roles(b)
        self.assertEqual([m["role"] for m in b["messages"]], ["system", "user", "assistant"])
        # 内容不动
        self.assertEqual(b["messages"][0]["content"], "sys")

    def test_normalize_roles_no_messages(self):
        from workbuddy_one.reasoning import normalize_roles
        b = {"model": "x"}
        self.assertEqual(normalize_roles(b), b)

    def test_sanitize_body_maps_developer(self):
        # sanitize_body 全链路：developer 应被归一
        from workbuddy_one.reasoning import sanitize_body
        b = {"model": "glm-5.2", "messages": [{"role": "developer", "content": "s"}]}
        sanitize_body(b)
        self.assertEqual(b["messages"][0]["role"], "system")

    # --- DeepSeek 思维链开关注入 ---
    def test_inject_thinking_deepseek_default(self):
        from workbuddy_one.reasoning import inject_thinking
        b = {"model": "deepseek-v4-pro", "messages": []}
        inject_thinking(b)
        self.assertEqual(b["thinking"], {"type": "enabled"})

    def test_inject_thinking_explicit_untouched(self):
        from workbuddy_one.reasoning import inject_thinking
        b = {"model": "deepseek-v4-pro", "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        inject_thinking(b)
        self.assertEqual(b["thinking"], {"type": "enabled"})
        self.assertEqual(b["reasoning_effort"], "high")  # 明确 enabled 时 effort 保留

    def test_inject_thinking_disabled_removes_effort(self):
        from workbuddy_one.reasoning import inject_thinking
        b = {"model": "deepseek-v4-flash", "thinking": {"type": "disabled"}, "reasoning_effort": "high"}
        inject_thinking(b)
        self.assertNotIn("reasoning_effort", b)

    def test_inject_thinking_missing_type(self):
        from workbuddy_one.reasoning import inject_thinking
        b = {"model": "deepseek-v4-pro", "thinking": {}}
        inject_thinking(b)
        self.assertEqual(b["thinking"]["type"], "enabled")

    def test_inject_thinking_non_deepseek_untouched(self):
        from workbuddy_one.reasoning import inject_thinking
        b = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}
        inject_thinking(b)
        self.assertNotIn("thinking", b)

    # --- DeepSeek 多轮 reasoning_content 回填 ---
    def test_backfill_copies_and_fills(self):
        from workbuddy_one.reasoning import backfill_reasoning_content
        b = {"model": "deepseek-v4-pro", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a", "reasoning_content": "思考A"},
            {"role": "assistant", "content": "b", "reasoning": "思考B"},
            {"role": "assistant", "content": "c"},
        ]}
        backfill_reasoning_content(b)
        msgs = b["messages"]
        self.assertEqual(msgs[1]["reasoning_content"], "思考A")  # 已有保留
        self.assertEqual(msgs[2]["reasoning_content"], "思考B")  # reasoning 复制
        self.assertEqual(msgs[3]["reasoning_content"], "")       # 两者皆无补空串

    def test_backfill_no_trace_untouched(self):
        from workbuddy_one.reasoning import backfill_reasoning_content
        b = {"model": "deepseek-v4-pro", "messages": [{"role": "assistant", "content": "a"}]}
        backfill_reasoning_content(b)
        self.assertNotIn("reasoning_content", b["messages"][0])

    def test_backfill_non_deepseek_untouched(self):
        from workbuddy_one.reasoning import backfill_reasoning_content
        b = {"model": "glm-5.3", "messages": [{"role": "assistant", "content": "a", "reasoning": "x"}]}
        backfill_reasoning_content(b)
        self.assertNotIn("reasoning_content", b["messages"][0])


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

    def test_migration_auto_backup(self):
        """旧版本库升级时自动备份到 <db目录>/backups；升级后再次打开不重复备份。"""
        import sqlite3
        from workbuddy_one.db import Database, SCHEMA_VERSION
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        old = tmp / "old_backup_test.db"
        backups_dir = tmp / "backups"
        for f in list(tmp.glob("old_backup_test.db*")) + list(backups_dir.glob("old_backup_test.pre-migrate-*")):
            f.unlink(missing_ok=True)
        # 建一个旧版本库（user_version=1，旧表结构）
        c = sqlite3.connect(str(old))
        c.executescript("""
            CREATE TABLE accounts (uid TEXT UNIQUE, nickname TEXT);
            CREATE TABLE usage_logs (ts REAL, model TEXT);
            INSERT INTO accounts (uid, nickname) VALUES ('u-old', '旧账号');
        """)
        c.execute("PRAGMA user_version = 1")
        c.commit()
        c.close()
        try:
            db = Database(str(old))
            try:
                # 数据保留 + 版本到位
                self.assertIsNotNone(db.get_account("u-old"))
                self.assertEqual(db._user_version(), SCHEMA_VERSION)
                # 备份文件存在且含旧数据快照
                baks = list(backups_dir.glob("old_backup_test.pre-migrate-v1-*.bak"))
                self.assertTrue(baks, "迁移前应生成备份文件")
                src = sqlite3.connect(str(baks[0]))
                try:
                    self.assertEqual(src.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
                finally:
                    src.close()
                # 已是最新版本的库再次打开：不再产生新备份
                n_before = len(list(backups_dir.glob("old_backup_test.pre-migrate-*")))
                db2 = Database(str(old))
                db2._conn.close()
                self.assertEqual(n_before, len(list(backups_dir.glob("old_backup_test.pre-migrate-*"))))
            finally:
                db._conn.close()
        finally:
            for f in list(tmp.glob("old_backup_test.db*")) + list(backups_dir.glob("old_backup_test.pre-migrate-*")):
                f.unlink(missing_ok=True)

    def test_fresh_db_no_backup(self):
        """全新空库首次初始化不算"升级"，不产生迁移备份。"""
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        fresh = tmp / "fresh_backup_test.db"
        backups_dir = tmp / "backups"
        for f in list(tmp.glob("fresh_backup_test.db*")) + list(backups_dir.glob("fresh_backup_test.pre-migrate-*")):
            f.unlink(missing_ok=True)
        try:
            db = Database(str(fresh))
            db._conn.close()
            leftovers = list(backups_dir.glob("fresh_backup_test.pre-migrate-*")) if backups_dir.exists() else []
            self.assertEqual(leftovers, [])
        finally:
            for f in list(tmp.glob("fresh_backup_test.db*")) + list(backups_dir.glob("fresh_backup_test.pre-migrate-*")):
                f.unlink(missing_ok=True)

    def test_migrate_old_schema(self):
        """旧 schema 库（缺列/缺 apps 表）打开后应升级到当前版本且保留数据。"""
        import sqlite3
        from workbuddy_one.db import Database, SCHEMA_VERSION
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        old = str(tmp / "old_schema.db")
        # 建一个「旧版」库：accounts 无 last_checkin_date、usage_logs 无内容列、无 apps 表
        c = sqlite3.connect(old)
        c.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE,
                nickname TEXT, enterprise_id TEXT, domain TEXT, auth_json TEXT,
                enabled INTEGER DEFAULT 1, credits_remaining REAL, credits_total REAL,
                created_at REAL, updated_at REAL);
            CREATE TABLE usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                model TEXT, protocol TEXT, account_uid TEXT, input_tokens INTEGER,
                output_tokens INTEGER, total_tokens INTEGER, latency_ms REAL,
                status TEXT, error TEXT);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO accounts (uid, nickname) VALUES ('legacy-uid', '旧账号');
            INSERT INTO settings (key, value) VALUES ('checkin_hours', '8,20');
        """)
        c.commit(); c.close()
        try:
            db = Database(old)
            try:
                # accounts 列已补齐
                cols = db._table_columns("accounts")
                self.assertIn("last_checkin_date", cols)
                self.assertIn("priority", cols)
                self.assertIn("credits_expire_at", cols)
                # usage_logs 内容列已补齐
                ucols = db._table_columns("usage_logs")
                for col in ("input_content", "output_content", "reasoning_content", "credits", "app_name"):
                    self.assertIn(col, ucols)
                # apps 表已创建 + key_enc
                self.assertIn("key_enc", db._table_columns("apps"))
                # 数据保留
                self.assertIsNotNone(db.get_account("legacy-uid"))
                self.assertEqual(db.get_settings().get("checkin_hours"), "8,20")
                # 版本号已写入
                self.assertEqual(db._user_version(), SCHEMA_VERSION)
            finally:
                db._conn.close()
        finally:
            # 迁移前自动备份会产生 *.pre-migrate-*.bak，一并清理
            for f in list(tmp.glob("old_schema.db*")) + list((tmp / "backups").glob("old_schema.db.pre-migrate-*")):
                f.unlink(missing_ok=True)

    def test_merge_legacy_data(self):
        """当前空库 + 发现旧库时，自动合并 accounts/settings/usage_logs 数据。"""
        import sqlite3
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        legacy = str(tmp / "legacy.db")
        target = str(tmp / "target.db")
        # 清理上次运行残留，保证幂等
        for f in list(tmp.glob("legacy.db*")) + list(tmp.glob("target.db*")):
            f.unlink(missing_ok=True)
        # 建旧库并写入数据
        c = sqlite3.connect(legacy)
        c.executescript("""
            CREATE TABLE accounts (uid TEXT UNIQUE, nickname TEXT, auth_json TEXT);
            CREATE TABLE usage_logs (ts REAL, model TEXT, protocol TEXT, status TEXT);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO accounts (uid, nickname) VALUES ('leg-1', '旧账号1');
            INSERT INTO usage_logs (ts, model, protocol, status) VALUES (1000, 'hy3', 'chat', 'ok');
            INSERT INTO settings (key, value) VALUES ('credit_refresh_min', '15');
        """)
        c.commit(); c.close()
        try:
            db = Database(target)
            try:
                db._merge_legacy_db(legacy)  # 手动触发合并（target 路径下无候选旧库，直接调方法）
                self.assertIsNotNone(db.get_account("leg-1"))
                self.assertEqual(db.get_settings().get("credit_refresh_min"), "15")
                recent = db.usage_recent(10)
                self.assertTrue(any(r["model"] == "hy3" for r in recent))
            finally:
                db._conn.close()
        finally:
            for f in list(tmp.glob("legacy.db*")) + list(tmp.glob("target.db*")):
                f.unlink(missing_ok=True)




class TestAppHelpers(unittest.TestCase):
    """app.py 模块级帮助函数（token 估算 / 别名解析 / 流式心跳）。"""

    def test_estimate_tokens_cjk_vs_latin(self):
        from workbuddy_one.reasoning import estimate_tokens as _estimate_tokens
        # 同样字符数：中文估算应显著高于英文（CJK ~1.5 字符/token vs /4）
        cjk = _estimate_tokens("这是一段中文测试文本用于验证估算" * 1, 1)
        latin = _estimate_tokens("abcdefghij" * 3, 1)  # 30 字符
        self.assertGreater(cjk, latin)
        # 空文本有最小开销
        self.assertEqual(_estimate_tokens("", 0), 4)

    def test_parse_model_aliases(self):
        from workbuddy_one.reasoning import parse_model_aliases as _parse_model_aliases
        raw = "gpt-4o=deepseek-v4-pro\n\n  kimi = kimi-k3-1  \nbad-line\n=x\nx="
        self.assertEqual(
            _parse_model_aliases(raw),
            {"gpt-4o": "deepseek-v4-pro", "kimi": "kimi-k3-1"},
        )
        self.assertEqual(_parse_model_aliases(""), {})


class TestUsageSearch(unittest.TestCase):
    def test_search_filters_and_escapes(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "search_test.db"))
        try:
            db.log_usage(model="m", protocol="chat", account_uid="u", status="ok",
                         input_content="帮我写一个 quicksort 算法", output_content="def quicksort...")
            db.log_usage(model="m", protocol="chat", account_uid="u", status="ok",
                         input_content="今天天气怎么样", output_content="晴天")
            db.log_usage(model="m", protocol="chat", account_uid="u", status="ok",
                         input_content="带下划线的 a_b 搜索应转义", output_content="")
            self.assertEqual(db.usage_count(search="quicksort"), 1)
            self.assertEqual(db.usage_count(search="晴天"), 1)  # 输出侧命中
            self.assertEqual(db.usage_count(search="不存在的内容xyz"), 0)
            # LIKE 转义：下划线是通配符，必须按字面匹配
            self.assertEqual(db.usage_count(search="a_b"), 1)
            self.assertEqual(db.usage_count(search="axb"), 0)
            rows = db.usage_recent(10, search="quicksort")
            self.assertEqual(len(rows), 1)
        finally:
            db._conn.close()
            for f in tmp.glob("search_test.db*"):
                f.unlink(missing_ok=True)


class TestDisabledReason(unittest.TestCase):
    def test_persist_and_clear(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "reason_test.db"))
        try:
            db.upsert_account({"path": "/x"}, {"uid": "u-reason", "nickname": "n"})
            db.set_account_state("u-reason", enabled=0, disabled_reason="保活连续失败")
            row = db.get_account("u-reason")
            self.assertEqual(row["enabled"], 0)
            self.assertEqual(row["disabled_reason"], "保活连续失败")
            db.set_account_state("u-reason", enabled=1, disabled_reason="")
            row = db.get_account("u-reason")
            self.assertEqual(row["enabled"], 1)
            self.assertEqual(row["disabled_reason"], "")
        finally:
            db._conn.close()
            for f in tmp.glob("reason_test.db*"):
                f.unlink(missing_ok=True)


class TestKeepalive(unittest.IsolatedAsyncioTestCase):
    async def test_keepalive_injected_during_silence(self):
        from workbuddy_one.gateway.sse import with_keepalive as _with_keepalive

        async def slow():
            yield "a"
            await asyncio.sleep(0.15)  # 静默期 > interval，应插入心跳
            yield "b"

        out = [c if isinstance(c, str) else c.decode() async for c in _with_keepalive(slow(), 0.05)]
        self.assertEqual(out[0], "a")
        self.assertEqual(out[-1], "b")
        self.assertTrue(any("keepalive" in t for t in out[1:-1]), out)

    async def test_no_keepalive_when_streaming_fast(self):
        from workbuddy_one.gateway.sse import with_keepalive as _with_keepalive

        async def fast():
            for i in range(5):
                yield f"c{i}"
                await asyncio.sleep(0.001)

        out = [c if isinstance(c, str) else c.decode() async for c in _with_keepalive(fast(), 1.0)]
        self.assertEqual(out, ["c0", "c1", "c2", "c3", "c4"])


if __name__ == "__main__":
    unittest.main()
