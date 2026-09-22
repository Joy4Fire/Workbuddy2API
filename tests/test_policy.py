"""错误分类表 / 处置策略 / 账号池新维度（2026-09-21 吸收 Sliverkiss 的 P0~P2 改动）。

覆盖四块：
  1. `gateway.errors.classify` 的 13 类判定，重点是**判定顺序**里最容易踩的两条
     （429 先于余额关键词、11115 先于 404）。
  2. `action_for` 的处置策略表（罚不罚号 / 换不换号 / 罚多久）。
  3. `Retry-After` 头族解析。
  4. 账号池的 (账号,模型) 冷却、连败降权、在途并发、停用双状态位。
  5. DB v6 / v7 迁移、`model_blocks` 与 `model_costs` 的持久化与**删号级联**。
  6. 启动预热的时间上限（`scheduler._WARMUP_BUDGET_SECONDS`）—— 上游慢不该让 WebUI 打不开。

临时文件一律放系统临时目录（tempfile.mkdtemp()）：仓库可能在网络盘上，
而网络盘没有回收站，删除会退化成失败的 SHFileOperationW，单次 rmtree 实测 11~36 秒。
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wb_policy_"))


def tearDownModule():
    if _TMP.is_dir():
        shutil.rmtree(_TMP, True)


def _open_db(case: unittest.TestCase, name: str):
    """开一个测试库，并把关闭登记到用例的 cleanup 里。

    sqlite 连接不显式关闭时，GC 会抛 `ResourceWarning: unclosed database`；
    更麻烦的是在 Windows 上句柄没释放会让随后的 `unlink` 偶发失败。用 `addCleanup`
    保证用例无论从哪条路径退出都会关，比在每个用例里手写 try/finally 稳。
    """
    from workbuddy_one.db import Database
    db = Database(str(_TMP / name))
    case.addCleanup(db._conn.close)
    return db


def _code_only(obj) -> str:
    """取源码并**丢掉注释与字符串字面量**，只留真正的代码 token。

    守卫类用例要断言"代码里没有用到 X"，而 docstring/注释里**恰恰在解释为什么不能用 X**
    —— 直接对源码做子串匹配会把自己的说明文字也算成证据（本文件的两个守卫用例第一次
    就是这么误报的：`do_makeup` 的 docstring 写着"别把判据改成 `checkin_dates`"，
    于是 `assertNotIn("checkin_dates", src)` 挂了）。
    用 tokenize 过滤掉 COMMENT / STRING，守卫才是在检查代码而不是检查措辞。
    """
    import inspect
    import io
    import tokenize
    src = inspect.getsource(obj)
    kept = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING)]
    return " ".join(kept)


class TestErrorClassification(unittest.TestCase):
    """上游错误分类（协议知识）。"""

    @staticmethod
    def _kind(status, body, headers=None):
        from workbuddy_one.gateway.errors import classify
        return classify(status, body, headers).value

    def test_model_blocked_first(self):
        # 11102「该后端无此模型」：只认 400/404，且必须先于一切
        self.assertEqual(self._kind(400, b'{"code":11102,"msg":"service info not found"}'),
                         "model_blocked")
        self.assertEqual(self._kind(404, b'{"msg":"service info not found"}'), "model_blocked")
        # 429 带 11102 属限流语义，不归 model_blocked
        self.assertEqual(self._kind(429, b'{"code":11102}'), "soft_rate")

    def test_11102_does_not_match_on_id(self):
        """code 判定必须看独立字段，不能整段文本子串匹配。

        错误体里的 requestId 等字段可能恰好含 "11102"，整段匹配会误避让一个
        本来能用的模型。
        """
        body = b'{"code":400,"msg":"bad request","requestId":"req-11102-abcdef"}'
        self.assertEqual(self._kind(400, body), "client")

    def test_hard_credit_paths(self):
        self.assertEqual(self._kind(402, b'{"msg":"whatever"}'), "hard_credit")
        # 429 + 结构化业务码 14018 = 账号积分耗尽（必须先于通用 429 兜底）
        self.assertEqual(self._kind(429, b'{"code":14018,"msg":"credits exhausted"}'),
                         "hard_credit")
        # 非 429 的计费关键词
        self.assertEqual(self._kind(403, b'{"code":1,"msg":"\xe9\xa2\x9d\xe5\xba\xa6\xe4\xb8\x8d\xe8\xb6\xb3"}'),
                         "hard_credit")

    def test_429_beats_credit_keywords(self):
        """**最容易踩的顺序坑 #1**：429 必须先判在余额关键词之前。

        429 的 body 高频带 "quota exceeded" / "额度不足" 这类跨计费与限流两界的
        措辞。关键词先判会把它误归硬冷却到次日 04:00 —— 白扔一个号约 12 小时。
        """
        self.assertEqual(self._kind(429, b'{"msg":"quota exceeded"}'), "soft_rate")
        self.assertEqual(self._kind(429, '{"msg":"额度不足"}'.encode("utf-8")), "soft_rate")
        # 同样文案在非 429 上仍然按计费处理（历史语义不变）
        self.assertEqual(self._kind(200, b'{"msg":"quota exceeded"}'), "hard_credit")

    def test_session_dead_and_account_fault_before_429(self):
        """账号级终态等不来自愈，限流状态码不得掩盖它们。"""
        # 401 + 12153，body 里还混着 "rate limit" 也必须是 session_dead
        self.assertEqual(
            self._kind(401, b'{"code":12153,"msg":"Offline user session not found, rate limit"}'),
            "session_dead")
        # 429 + 14017（试用未激活）必须 account_fault，不能落 soft_rate
        self.assertEqual(self._kind(429, b'{"code":14017,"msg":"trial not activated"}'),
                         "account_fault")
        # 11140 只按 msg 关键词判（该 code 也承载模型级限流文案）
        self.assertEqual(self._kind(403, b'{"code":11140,"msg":"request illegal"}'), "account_fault")
        self.assertEqual(
            self._kind(400, b'{"code":11140,"msg":"The model provider is rate-limiting requests."}'),
            "soft_rate")

    def test_prompt_too_long_beats_404(self):
        """**最容易踩的顺序坑 #2**：11115 必须判在 404 之前。

        404 上打的 11115 若落 not_found，会去冷却一个无辜的账号 ——
        上下文超限与账号无关。
        """
        self.assertEqual(self._kind(404, b'{"code":11115,"msg":"prompt is too long"}'),
                         "prompt_too_long")
        self.assertEqual(self._kind(400, b'{"code": 11115}'), "prompt_too_long")
        self.assertEqual(self._kind(413, b"prompt is too long"), "prompt_too_long")
        # 429/5xx 上不判 11115（限流/服务端语义优先）
        self.assertEqual(self._kind(429, b"prompt is too long"), "soft_rate")
        self.assertEqual(self._kind(500, b'{"code":11115}'), "server")

    def test_waf_403(self):
        """403 且无业务信封（HTML/空体/纯文本）= WAF 拦截形态。"""
        self.assertEqual(self._kind(403, b"<html>403 Forbidden</html>"), "waf_block")
        self.assertEqual(self._kind(403, b""), "waf_block")
        self.assertEqual(self._kind(403, b"forbidden"), "waf_block")
        # 带业务信封的 403 走既有分类链
        self.assertEqual(self._kind(403, b'{"code":1234,"msg":"some business error"}'), "client")

    def test_image_invalid(self):
        self.assertEqual(self._kind(400, b'{"code": 11135}'), "image_invalid")
        self.assertEqual(self._kind(400, b'{"msg":"Parse message failed: invalid image_url content"}'),
                         "image_invalid")
        # 只在 400 上判
        self.assertEqual(self._kind(500, b'{"code":11135}'), "server")

    def test_misc_fallbacks(self):
        self.assertEqual(self._kind(404, b"not found"), "not_found")
        self.assertEqual(self._kind(500, b"boom"), "server")
        self.assertEqual(self._kind(503, b""), "server")
        self.assertEqual(self._kind(400, b'{"msg":"blocked by security policy"}'), "content_blocked")
        self.assertEqual(self._kind(400, b'{"msg":"Unmarshal chat params failed"}'), "bad_params")
        self.assertEqual(self._kind(400, b'{"msg":"unknown"}'), "client")
        self.assertEqual(self._kind(200, b"ok"), "none")


class TestActionPolicy(unittest.TestCase):
    """分类 → 处置策略（运营决策）。"""

    @staticmethod
    def _act(status, body, headers=None):
        from workbuddy_one.gateway.errors import action_for, classify
        return action_for(classify(status, body, headers), body, headers)

    def test_request_level_errors_fail_fast(self):
        """请求自身的问题：不罚号、不轮转（换了账号也一样错）。"""
        for status, body in (
            (400, b'{"code":11115,"msg":"prompt is too long"}'),
            (400, b'{"code":11135}'),
            (400, b'{"msg":"blocked by security policy"}'),
        ):
            act = self._act(status, body)
            self.assertTrue(act.fail_fast, body)
            self.assertFalse(act.rotate, body)
            self.assertEqual(act.cooldown, 0.0, body)

    def test_account_fault_disable_paths(self):
        # 12153 session 失效 → 禁用（软冷却救不活）
        act = self._act(401, b'{"code":12153}')
        self.assertTrue(act.disable)
        # 11140 账号被封 → 禁用
        self.assertTrue(self._act(403, b'{"msg":"request illegal"}').disable)
        # 14017 试用未激活 → 只软冷却（补完 register 可能自愈，禁用会让用户补完也用不了）
        act = self._act(429, b'{"code":14017,"msg":"trial not activated"}')
        self.assertFalse(act.disable)
        self.assertGreater(act.cooldown, 0)

    def test_model_blocked_is_model_scoped(self):
        act = self._act(400, b'{"code":11102,"msg":"service info not found"}')
        self.assertTrue(act.model_scoped)
        self.assertTrue(act.neg_cache)
        self.assertGreaterEqual(act.cooldown, 6 * 3600)   # 负缓存 6h 起

    def test_hard_credit_until_next_4am(self):
        from workbuddy_one.gateway.errors import next_day_4am
        act = self._act(402, b'{"msg":"payment required"}')
        self.assertGreater(act.cooldown, 0)
        # 冷却时长应约等于"距下一个 04:00"（夹了 60s 下限，误差容忍 5s）
        expected = max(60.0, next_day_4am() - time.time())
        self.assertAlmostEqual(act.cooldown, expected, delta=5.0)

    def test_client_rotates_without_penalty(self):
        act = self._act(400, b'{"msg":"unknown"}')
        self.assertTrue(act.rotate)
        self.assertEqual(act.cooldown, 0.0)
        self.assertFalse(act.disable)


class TestRetryAfter(unittest.TestCase):
    """Retry-After 头族解析（P1-2）。"""

    def test_seconds_and_ms(self):
        from workbuddy_one.gateway.errors import parse_retry_after
        self.assertEqual(parse_retry_after({"retry-after": "120"}), 120.0)
        self.assertEqual(parse_retry_after({"Retry-After": "120"}), 120.0)  # 大小写不敏感
        self.assertEqual(parse_retry_after({"retry-after-ms": "1500"}), 1.5)

    def test_ratelimit_reset_epoch(self):
        from workbuddy_one.gateway.errors import parse_retry_after
        now = time.time()
        sec = parse_retry_after({"x-ratelimit-reset": str(int(now + 300))})
        self.assertTrue(290 <= sec <= 310, sec)
        ms = parse_retry_after({"x-ratelimit-reset": str(int((now + 300) * 1000))})
        self.assertTrue(290 <= ms <= 310, ms)

    def test_rejects_non_numeric_and_out_of_range(self):
        from workbuddy_one.gateway.errors import parse_retry_after
        # HTTP-Date 形态不解析（宁缺毋滥，回落本地计算）
        self.assertIsNone(parse_retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        # 超过 2h 上限视为上游异常值
        self.assertIsNone(parse_retry_after({"retry-after": "99999"}))
        # 非正数丢弃
        self.assertIsNone(parse_retry_after({"retry-after": "0"}))
        self.assertIsNone(parse_retry_after({}))
        self.assertIsNone(parse_retry_after(None))

    def test_priority_body_reset_beats_header(self):
        """body 重置墙钟 > Retry-After 头（上游文案是更权威的口径）。"""
        from workbuddy_one.gateway.errors import action_for, classify
        from datetime import datetime, timedelta, timezone
        wall = datetime.fromtimestamp(time.time() + 600,
                                      tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        body = f"将在 {wall} UTC+8 重置".encode("utf-8")
        act = action_for(classify(429, body, {"retry-after": "10"}), body, {"retry-after": "10"})
        self.assertTrue(540 <= act.cooldown <= 660, act.cooldown)

    def test_header_used_when_body_has_no_reset_text(self):
        from workbuddy_one.gateway.errors import action_for, classify
        body = b'{"msg":"rate limit"}'
        act = action_for(classify(429, body, {"retry-after": "120"}), body, {"retry-after": "120"})
        self.assertEqual(act.cooldown, 120.0)


class TestPoolModelCooldown(unittest.TestCase):
    """(账号, 模型) 维度冷却与负缓存（P0-2）。"""

    @staticmethod
    def _pool():
        from workbuddy_one.pool import Account, AccountPool
        accounts = [Account(uid="a", mgr=None), Account(uid="b", mgr=None)]
        for a in accounts:
            a.credits_remaining = 100
        return AccountPool({a.uid: a for a in accounts})

    def test_model_cooldown_only_blocks_that_model(self):
        pool = self._pool()
        pool.cooldown_model("a", "glm-5.3", 300)
        # a 的 glm-5.3 被冷却 → 选号只会选到 b
        picked = {pool.pick(model="glm-5.3").uid for _ in range(20)}
        self.assertEqual(picked, {"b"})
        # 换个模型 a 又能用了（这就是"模型级"的意义：账号本身是健康的）
        picked = {pool.pick(model="gpt-5.4").uid for _ in range(30)}
        self.assertEqual(picked, {"a", "b"})

    def test_model_cooldown_hard_filter_returns_none(self):
        """全部账号都把该模型冷却了 → 返回 None（上游已明确说不可用，退回重试无意义）。"""
        pool = self._pool()
        pool.cooldown_model("a", "glm-5.3", 300)
        pool.cooldown_model("b", "glm-5.3", 300)
        self.assertIsNone(pool.pick(model="glm-5.3"))

    def test_negative_cache_exponential_ttl(self):
        from workbuddy_one.gateway.errors import MODEL_BLOCK_MAX_TTL
        pool = self._pool()
        acc = next(a for a in pool.accounts if a.uid == "a")
        now = time.time()
        pool.cooldown_model("a", "m", 6 * 3600, neg_cache=True)
        t1 = acc.model_cooldowns["m"] - now
        pool.cooldown_model("a", "m", 6 * 3600, neg_cache=True)
        t2 = acc.model_cooldowns["m"] - now
        pool.cooldown_model("a", "m", 6 * 3600, neg_cache=True)
        t3 = acc.model_cooldowns["m"] - now
        self.assertTrue(5.9 * 3600 < t1 < 6.1 * 3600, t1)
        self.assertTrue(11.9 * 3600 < t2 < 12.1 * 3600, t2)
        self.assertTrue(23.9 * 3600 < t3 <= MODEL_BLOCK_MAX_TTL + 1, t3)
        # 封顶：继续命中不再放大
        pool.cooldown_model("a", "m", 6 * 3600, neg_cache=True)
        self.assertLessEqual(acc.model_cooldowns["m"] - now, MODEL_BLOCK_MAX_TTL + 1)

    def test_clear_model_cooldown_reports_change(self):
        pool = self._pool()
        self.assertFalse(pool.clear_model_cooldown("a", "m"))  # 本来就没有
        pool.cooldown_model("a", "m", 300)
        self.assertTrue(pool.clear_model_cooldown("a", "m"))
        self.assertFalse(pool.clear_model_cooldown("a", "m"))

    def test_success_clears_model_cooldown(self):
        """成功即证明可用，负缓存必须被清掉（否则最长被避让 24 小时）。"""
        from workbuddy_one.gateway.errors import ErrKind
        pool = self._pool()
        pool.cooldown_model("a", "m", 300)
        self.assertTrue(pool.clear_model_cooldown("a", "m"))
        self.assertIsNone(pool.model_block_state("a", "m"))


class TestPoolDegrade(unittest.TestCase):
    """连败降权出池（P1-2）。"""

    @staticmethod
    def _pool():
        from workbuddy_one.pool import Account, AccountPool
        accounts = [Account(uid="a", mgr=None), Account(uid="b", mgr=None)]
        for a in accounts:
            a.credits_remaining = 100
        return AccountPool({a.uid: a for a in accounts})

    def test_single_failure_does_not_degrade(self):
        pool = self._pool()
        pool.note_failures("a", threshold=3, degrade_seconds=600)
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertEqual(acc.degrade_until, 0.0)
        self.assertTrue(acc.healthy(time.time()))

    def test_streak_reaches_threshold_degrades(self):
        pool = self._pool()
        for _ in range(3):
            pool.note_failures("a", threshold=3, degrade_seconds=600)
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertGreater(acc.degrade_until, time.time())
        self.assertFalse(acc.healthy(time.time()))
        # 降权期间选不到 a
        self.assertEqual({pool.pick().uid for _ in range(20)}, {"b"})

    def test_success_resets_streak(self):
        pool = self._pool()
        pool.note_failures("a", threshold=3, degrade_seconds=600)
        pool.note_failures("a", threshold=3, degrade_seconds=600)
        pool.on_success("a")
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertEqual(acc.fail_streak, 0)
        pool.note_failures("a", threshold=3, degrade_seconds=600)
        self.assertEqual(acc.degrade_until, 0.0)  # 没到阈值


class TestPoolCapacity(unittest.TestCase):
    """单账号在途并发上限（P1-1）。"""

    def _pool(self):
        # 注意：AccountPool(dict) 会用 dict 的 value 当 CredentialManager **重建**
        # Account 对象，所以属性必须在建池之后设到 pool.accounts 上。
        from workbuddy_one.pool import AccountPool
        pool = AccountPool({})
        pool.add_account("a", None)
        pool.add_account("b", None)
        for a in pool.accounts:
            a.credits_remaining = 100
            a.capacity_limit = 1
        return pool

    def test_acquire_and_release(self):
        pool = self._pool()
        self.assertTrue(pool.acquire_slot("a"))
        self.assertFalse(pool.acquire_slot("a"))   # 已达上限
        pool.release_slot("a")
        self.assertTrue(pool.acquire_slot("a"))
        pool.release_slot("a")

    def test_release_never_goes_negative(self):
        pool = self._pool()
        pool.release_slot("a")
        pool.release_slot("a")
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertEqual(acc.in_flight, 0)

    def test_pick_prefers_free_account(self):
        pool = self._pool()
        pool.acquire_slot("a")
        # a 占满 → 优先选 b（软过滤）
        self.assertEqual({pool.pick().uid for _ in range(20)}, {"b"})

    def test_pick_falls_back_when_all_full(self):
        """全都占满时退回不过滤——否则单账号场景永远选不出号。"""
        pool = self._pool()
        pool.acquire_slot("a")
        pool.acquire_slot("b")
        self.assertIsNotNone(pool.pick())

    def test_zero_limit_means_unlimited(self):
        from workbuddy_one.pool import Account, AccountPool
        pool = AccountPool({})
        pool.add_account("x", None)
        acc = next(a for a in pool.accounts if a.uid == "x")
        acc.capacity_limit = 0
        for _ in range(50):
            self.assertTrue(pool.acquire_slot("x"))


class TestPoolDualDisable(unittest.TestCase):
    """手动停用位与系统自动禁用位相互独立（P2-2）。"""

    @staticmethod
    def _pool():
        from workbuddy_one.pool import Account, AccountPool
        accounts = [Account(uid="a", mgr=None)]
        for a in accounts:
            a.credits_remaining = 100
        return AccountPool({a.uid: a for a in accounts})

    def test_manual_enable_keeps_auto_disable(self):
        """**核心语义**：点"启用"不该把一个 session 已失效的账号放回池子。"""
        pool = self._pool()
        pool.disable_auto("a", "登录态失效（12153）")
        pool.set_enabled("a", True)
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertTrue(acc.enabled)                       # 手动位已清
        self.assertEqual(acc.auto_disabled_reason, "登录态失效（12153）")  # 自动位还在
        self.assertFalse(acc.healthy(time.time()))
        self.assertIsNone(pool.pick())

    def test_enable_auto_restores(self):
        pool = self._pool()
        pool.disable_auto("a", "x")
        pool.enable_auto("a")
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertTrue(acc.healthy(time.time()))
        self.assertIsNotNone(pool.pick())

    def test_manual_disable_kept_by_clear_cooldown(self):
        """签到解冻（clear_cooldown）不得复活手动停用的账号。"""
        pool = self._pool()
        pool.set_enabled("a", False, reason="手动停用")
        pool.clear_cooldown("a")
        acc = next(a for a in pool.accounts if a.uid == "a")
        self.assertFalse(acc.enabled)
        self.assertIsNone(pool.pick())

    def test_display_reason_prefers_manual(self):
        pool = self._pool()
        acc = next(a for a in pool.accounts if a.uid == "a")
        pool.set_enabled("a", False, reason="手动停用")
        self.assertEqual(pool.display_disabled_reason(acc), "手动停用")
        pool.set_enabled("a", True)
        pool.disable_auto("a", "被上游封禁")
        self.assertEqual(pool.display_disabled_reason(acc), "被上游封禁")

    def test_all_accounts_exposes_both_bits(self):
        pool = self._pool()
        pool.disable_auto("a", "x")
        row = pool.all_accounts()[0]
        self.assertFalse(row["manual_disabled"])
        self.assertEqual(row["auto_disabled_reason"], "x")
        self.assertFalse(row["healthy"])


class TestModelBlocksPersistence(unittest.TestCase):
    """DB v6：迁移 + (账号,模型) 冷却持久化。"""

    def test_schema_version_at_least_6(self):
        # 不写死具体版本号：v7 又加了 model_costs，写死 6 会被下一次迁移误报。
        # 这里只断言"v6 引入的结构存在"，版本号单调递增由迁移框架保证。
        from workbuddy_one.db import SCHEMA_VERSION
        self.assertGreaterEqual(SCHEMA_VERSION, 6)

    def test_fresh_db_has_v6_structures(self):
        from workbuddy_one.db import SCHEMA_VERSION
        db = _open_db(self, "fresh_v6.db")
        self.assertIn("auto_disabled_reason", db._table_columns("accounts"))
        self.assertEqual(db._user_version(), SCHEMA_VERSION)
        self.assertEqual(db.model_blocks(), [])

    def test_migrate_v5_db_adds_column_and_table(self):
        import sqlite3
        from workbuddy_one.db import SCHEMA_VERSION
        old = str(_TMP / "legacy_v5.db")
        for f in list(_TMP.glob("legacy_v5.db*")) + list((_TMP / "backups").glob("legacy_v5*")):
            f.unlink(missing_ok=True)
        c = sqlite3.connect(old)
        c.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE,
                nickname TEXT, enterprise_id TEXT, domain TEXT, auth_json TEXT,
                enabled INTEGER DEFAULT 1, disabled_reason TEXT DEFAULT '',
                priority INTEGER DEFAULT 0, credits_remaining REAL, credits_total REAL,
                credits_expire_at TEXT, last_used_at REAL, last_checkin_date TEXT,
                checkin_today INTEGER, checkin_active INTEGER, checkin_synced_at REAL,
                failure_count INTEGER DEFAULT 0, cooldown_until REAL DEFAULT 0,
                created_at REAL, updated_at REAL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO accounts (uid, nickname, enabled) VALUES ('u1', '旧账号', 0);
        """)
        c.execute("PRAGMA user_version = 5")
        c.commit(); c.close()
        db = _open_db(self, "legacy_v5.db")
        self.assertIn("auto_disabled_reason", db._table_columns("accounts"))
        self.assertEqual(db._user_version(), SCHEMA_VERSION)
        # 旧数据保留，且旧库的 enabled=0 语义不变（仍是"手动停用"）
        row = db.get_account("u1")
        self.assertEqual(row["enabled"], 0)
        self.assertFalse(row["auto_disabled_reason"])

    def test_model_block_roundtrip(self):
        db = _open_db(self, "blocks.db")
        until = time.time() + 3600
        db.save_model_block("u1", "glm-5.3", until, streak=2, reason="11102")
        rows = db.model_blocks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uid"], "u1")
        self.assertEqual(rows[0]["streak"], 2)
        # 同主键再写 = 更新（不是插一条新的）
        db.save_model_block("u1", "glm-5.3", until + 100, streak=3, reason="11102")
        rows = db.model_blocks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["streak"], 3)
        db.delete_model_block("u1", "glm-5.3")
        self.assertEqual(db.model_blocks(), [])

    def test_purge_expired(self):
        db = _open_db(self, "purge.db")
        db.save_model_block("u1", "old", time.time() - 10)
        db.save_model_block("u1", "new", time.time() + 1000)
        db.purge_expired_model_blocks()
        self.assertEqual([r["model"] for r in db.model_blocks()], ["new"])

    def test_delete_account_clears_blocks(self):
        """删账号要连带清 `model_blocks` **和** `model_costs`。

        两张表都以 uid 关联账号、且**没有外键约束**。漏清任何一张，删号后重新
        用同一 uid 登录就会继承上一个账号的负缓存 / 单价数据——症状是"刚加的账号
        有几个模型莫名其妙不可用"或"明明是新账号却被当成贵的那个"，极难排查。
        """
        from workbuddy_one.db import Database
        path = str(_TMP / "delacct.db")
        db = Database(path)
        try:
            db.upsert_account({"path": "x"}, {"uid": "u1", "nickname": "n"})
            db.save_model_block("u1", "m", time.time() + 1000)
            db.save_model_cost("u1", "m", cost_per_1k=0.5, samples=3, updated_at=time.time())
            # 先确认两张表里真的有数据，否则下面的"清空了"断言可能是空对空
            self.assertEqual(len(db.model_blocks()), 1)
            self.assertEqual(len(db.model_costs()), 1)

            db.delete_account("u1")
            self.assertEqual(db.model_blocks(), [], "model_blocks 未级联清除")
            self.assertEqual(db.model_costs(), [], "model_costs 未级联清除")
        finally:
            db._conn.close()

    def test_pool_load_and_snapshot(self):
        from workbuddy_one.pool import Account, AccountPool
        pool = AccountPool({})
        pool.add_account("u1", None)
        acc = next(a for a in pool.accounts if a.uid == "u1")
        acc.credits_remaining = 100
        now = time.time()
        pool.load_model_blocks([
            {"uid": "u1", "model": "m1", "until": now + 3600, "streak": 2},
            {"uid": "u1", "model": "expired", "until": now - 10, "streak": 1},
            {"uid": "ghost", "model": "m2", "until": now + 3600, "streak": 1},
        ])
        self.assertIn("m1", acc.model_cooldowns)
        self.assertNotIn("expired", acc.model_cooldowns)   # 过期的不复活
        self.assertEqual(acc.model_block_streak["m1"], 2)
        snap = pool.model_block_state("u1", "m1")
        self.assertEqual(snap["streak"], 2)
        self.assertIsNone(pool.model_block_state("u1", "expired"))


class TestPromptMode(unittest.TestCase):
    """系统提示词三模式（P2-1）。"""

    @staticmethod
    def _run(mode, text, messages):
        from workbuddy_one.reasoning import sanitize_body
        body = sanitize_body({"model": "m", "messages": list(messages)},
                             prompt_mode=mode, prompt_text=text)
        return body["messages"]

    def test_passthrough_is_zero_change(self):
        msgs = [{"role": "system", "content": "客户端规范"}, {"role": "user", "content": "hi"}]
        self.assertEqual(self._run("passthrough", "网关提示词", msgs), msgs)
        # 空 text 即使 mode=append 也不生效（避免"配了模式没填词"把请求改坏）
        self.assertEqual(self._run("append", "", msgs), msgs)
        self.assertEqual(self._run("", "网关提示词", msgs), msgs)

    def test_custom_replaces_all_system(self):
        msgs = [{"role": "system", "content": "客户端规范"},
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "又一条"}]
        out = self._run("custom", "网关提示词", msgs)
        self.assertEqual(out[0], {"role": "system", "content": "网关提示词"})
        self.assertEqual([m["role"] for m in out], ["system", "user"])
        self.assertEqual(out[1]["content"], "hi")

    def test_append_keeps_client_prompt(self):
        """**核心语义**：客户端规范与网关提示词共存。"""
        msgs = [{"role": "system", "content": "客户端规范"}, {"role": "user", "content": "hi"}]
        out = self._run("append", "网关提示词", msgs)
        self.assertEqual(out[0]["content"], "客户端规范")
        self.assertEqual(out[1], {"role": "system", "content": "网关提示词"})
        self.assertEqual(out[2]["content"], "hi")

    def test_append_after_consecutive_system_block(self):
        msgs = [{"role": "system", "content": "a"},
                {"role": "system", "content": "b"},
                {"role": "user", "content": "hi"}]
        out = self._run("append", "gw", msgs)
        self.assertEqual([m["content"] for m in out], ["a", "b", "gw", "hi"])

    def test_append_without_system_goes_first(self):
        """客户端没带 system 时插到最前——顺带满足上游"首条必须是 system"的校验。"""
        msgs = [{"role": "user", "content": "hi"}]
        out = self._run("append", "gw", msgs)
        self.assertEqual(out[0], {"role": "system", "content": "gw"})
        self.assertEqual(out[1]["content"], "hi")

    def test_developer_normalized_before_append(self):
        """normalize_roles 先跑：developer 会被算进"开头 system 块"里。"""
        msgs = [{"role": "developer", "content": "dev"}, {"role": "user", "content": "hi"}]
        out = self._run("append", "gw", msgs)
        self.assertEqual([m["content"] for m in out], ["dev", "gw", "hi"])


class TestPromptModePlumbing(unittest.TestCase):
    """设置 → `enhance_body` → `sanitize_body` 的**接线**。

    为什么要在语义用例之外单独测这一环：`TestPromptMode` 直接调 `sanitize_body`，
    验证的是"模式怎么改消息"；而"用户在 WebUI 里把模式设成 append 之后，真实出站
    请求到底变没变"还多经过一跳 —— `enhance_body` 里那次 `settings.get("prompt_mode")`。
    键名拼错（`prompt_mode_` / `promptMode`）时上面所有用例**依然全绿**，
    线上却静默不生效，因为 `settings.get()` 取不到就是 None，走的是"空值=passthrough"分支。

    这正是本项目**已经复发过两次**的 bug 类（`DEFAULT_SETTINGS` 漏登记 → 键被静默丢弃）。
    所以这里既测端到端行为，也留一条源码级漂移守卫。
    """

    class _Ctx:
        """只实现 `enhance_body` 真正用到的几个入口，避免起真 DB / 真模型目录。"""

        def __init__(self, settings):
            self._settings = settings
            self.db = self

        def get_settings(self):
            return dict(self._settings)

        @property
        def models(self):
            return self

        def max_output_tokens(self, model):
            return None

        def reasoning_efforts(self, model):
            return None

    def _run(self, settings, messages):
        from workbuddy_one.gateway.inference import enhance_body
        body = {"model": "m", "messages": messages}
        return enhance_body(self._Ctx(settings), body)["messages"]

    # 期望值都**独立构造**：`TestPromptMode` 里拿 `msgs` 自己和结果比，即使
    # sanitize_body 原地改坏了入参也看不出来；这里把两边分开写。
    _CLIENT = [{"role": "system", "content": "客户端规范"},
               {"role": "user", "content": "hi"}]

    def test_append_setting_reaches_outgoing_body(self):
        out = self._run({"prompt_mode": "append", "prompt_text": "网关提示词"},
                        [dict(m) for m in self._CLIENT])
        self.assertEqual([m["content"] for m in out],
                         ["客户端规范", "网关提示词", "hi"])

    def test_custom_setting_reaches_outgoing_body(self):
        out = self._run({"prompt_mode": "custom", "prompt_text": "网关提示词"},
                        [dict(m) for m in self._CLIENT])
        self.assertEqual(out, [{"role": "system", "content": "网关提示词"},
                               {"role": "user", "content": "hi"}])

    def test_default_settings_are_zero_change(self):
        """用 `DEFAULT_SETTINGS` 的**实际默认值**跑一遍，必须逐字节等于入参。

        比"断言默认值是 passthrough"更硬：万一以后有人把默认改成 append 却忘了
        把 prompt_text 留空，这条会直接失败。
        """
        from workbuddy_one.db import Database
        msgs = [dict(m) for m in self._CLIENT]
        self.assertEqual(self._run(dict(Database.DEFAULT_SETTINGS), msgs), self._CLIENT)

    def test_unknown_mode_falls_back_to_passthrough(self):
        """库里存了脏值（手改 DB / 老版本残留）时不能把请求改坏，只能当作不改动。"""
        msgs = [dict(m) for m in self._CLIENT]
        self.assertEqual(self._run({"prompt_mode": "bogus", "prompt_text": "网关提示词"}, msgs),
                         self._CLIENT)

    def test_enhance_body_only_reads_whitelisted_keys(self):
        """源码级漂移守卫：`enhance_body` 读的 settings 键必须都在白名单里。

        白名单（`DEFAULT_SETTINGS`）外的键会被 `save_settings` **静默丢弃**，
        而 GET 端点的 `s.get(key, default)` 又把"键不存在"掩盖成"有默认值" ——
        于是 UI 显示正常、保存也返回 200，功能就是不生效，且没有任何日志。
        对函数源码做一次文本扫描，成本极低，但能在键名拼错的那一刻就报错。
        """
        import inspect
        import re
        from workbuddy_one.db import Database
        from workbuddy_one.gateway import inference

        src = inspect.getsource(inference.enhance_body)
        used = set(re.findall(r"""settings\.get\(\s*["']([^"']+)["']""", src))
        # 正则与代码脱节时（比如以后改用 settings["x"]）这条会先炸，避免守卫静默失效
        self.assertTrue(used, "没从 enhance_body 里扫到任何 settings 键，正则需要同步")
        missing = sorted(used - set(Database.DEFAULT_SETTINGS))
        self.assertEqual(missing, [], f"enhance_body 读了不在白名单里的设置键：{missing}")


class TestCostLedger(unittest.TestCase):
    """成本台账 + 成本分层选号（P2-3）。"""

    def _pool(self):
        from workbuddy_one.pool import AccountPool
        pool = AccountPool({})
        for uid in ("free", "paid", "fresh"):
            pool.add_account(uid, None)
        for a in pool.accounts:
            a.credits_remaining = 100
        return pool

    def test_record_cost_ema(self):
        pool = self._pool()
        # 第一次观测：直接采纳样本（1000 token 花 2 积分 → 2 积分/千 token）
        snap = pool.record_cost("paid", "m", 2.0, 1000)
        self.assertAlmostEqual(snap["cost_per_1k"], 2.0, places=6)
        self.assertEqual(snap["samples"], 1)
        # 第二次：EMA 平滑（alpha=0.3）
        snap = pool.record_cost("paid", "m", 4.0, 1000)
        self.assertAlmostEqual(snap["cost_per_1k"], 2.0 * 0.7 + 4.0 * 0.3, places=6)
        self.assertEqual(snap["samples"], 2)

    def test_zero_credits_is_a_valid_observation(self):
        """免费额度包返回 credit=0 —— 这是"实测免费"的有效观测，不是"没数据"。"""
        pool = self._pool()
        snap = pool.record_cost("free", "m", 0, 1000)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["cost_per_1k"], 0.0)
        acc = next(a for a in pool.accounts if a.uid == "free")
        self.assertEqual(acc.cost_tier(time.time(), "m"), 0)

    def test_no_tokens_is_not_an_observation(self):
        pool = self._pool()
        self.assertIsNone(pool.record_cost("free", "m", 1.0, 0))
        self.assertIsNone(pool.record_cost("free", "", 1.0, 100))

    def test_tier_classification(self):
        pool = self._pool()
        now = time.time()
        pool.record_cost("free", "m", 0, 1000)
        pool.record_cost("paid", "m", 5.0, 1000)
        acc = {a.uid: a for a in pool.accounts}
        self.assertEqual(acc["free"].cost_tier(now, "m"), 0)
        self.assertEqual(acc["fresh"].cost_tier(now, "m"), 1)   # 无观测
        self.assertEqual(acc["paid"].cost_tier(now, "m"), 2)
        # 无观测模型一律 tier1
        self.assertEqual(acc["paid"].cost_tier(now, "other"), 1)

    def test_expired_observation_falls_back_to_tier1(self):
        from workbuddy_one.pool import COST_TTL
        pool = self._pool()
        pool.record_cost("paid", "m", 5.0, 1000)
        acc = next(a for a in pool.accounts if a.uid == "paid")
        acc.model_costs["m"]["ts"] = time.time() - COST_TTL - 1
        self.assertEqual(acc.cost_tier(time.time(), "m"), 1)

    def test_pick_prefers_free_and_unmeasured(self):
        """免费号与未测号一起优先，实测收费的作兜底。"""
        pool = self._pool()
        pool.record_cost("free", "m", 0, 1000)
        pool.record_cost("paid", "m", 5.0, 1000)
        picked = {pool.pick(model="m").uid for _ in range(60)}
        self.assertNotIn("paid", picked)          # 有便宜的就不选收费号
        self.assertEqual(picked, {"free", "fresh"})  # 未测号不被跳过（防饿死）

    def test_pick_falls_back_to_paid_when_only_option(self):
        pool = self._pool()
        pool.record_cost("paid", "m", 5.0, 1000)
        for uid in ("free", "fresh"):
            pool.set_enabled(uid, False, reason="test")
        self.assertEqual({pool.pick(model="m").uid for _ in range(10)}, {"paid"})

    def test_cost_table_only_lists_fresh(self):
        from workbuddy_one.pool import COST_TTL
        pool = self._pool()
        pool.record_cost("paid", "m", 5.0, 1000)
        pool.record_cost("free", "old", 1.0, 1000)
        acc = next(a for a in pool.accounts if a.uid == "free")
        acc.model_costs["old"]["ts"] = time.time() - COST_TTL - 1
        rows = pool.cost_table()
        self.assertEqual([(r["uid"], r["model"]) for r in rows], [("paid", "m")])

    def test_cost_table_carries_region(self):
        """台账每行必须带出**区域**，否则混池时分不清这条是国内版还是国际版的实测值。

        台账本身是 (账号, 模型) 维度——账号天然归属区域，所以它同时也是"国内版 /
        国际版各自的真实消耗"。不带区域的话前端只能显示一串 uid，看不出对比关系。
        """
        pool = self._pool()
        pool.record_cost("paid", "m", 5.0, 1000)
        pool.record_cost("free", "m", 0.0, 1000)
        acc = next(a for a in pool.accounts if a.uid == "paid")
        acc.region_id = "global"          # 直接改区域：_pool() 用 mgr=None 构造，默认按国内版
        rows = {r["uid"]: r for r in pool.cost_table()}
        self.assertEqual(rows["paid"]["region"], "global")
        self.assertEqual(rows["paid"]["region_label"], "国际版")
        self.assertEqual(rows["free"]["region"], "cn")
        self.assertEqual(rows["free"]["region_label"], "国内版")

    def test_load_costs_skips_stale(self):
        from workbuddy_one.pool import COST_TTL
        pool = self._pool()
        now = time.time()
        pool.load_costs([
            {"uid": "paid", "model": "m", "cost_per_1k": 3.0, "samples": 4, "updated_at": now},
            {"uid": "paid", "model": "stale", "cost_per_1k": 3.0, "samples": 1,
             "updated_at": now - COST_TTL - 1},
        ])
        acc = next(a for a in pool.accounts if a.uid == "paid")
        self.assertIn("m", acc.model_costs)
        self.assertNotIn("stale", acc.model_costs)


class TestCostLedgerDB(unittest.TestCase):
    """DB v7：成本台账持久化。"""

    def test_schema_version_at_least_7(self):
        from workbuddy_one.db import SCHEMA_VERSION
        self.assertGreaterEqual(SCHEMA_VERSION, 7)

    def test_roundtrip_and_purge(self):
        db = _open_db(self, "costs.db")
        now = time.time()
        db.save_model_cost("u1", "m", 2.5, 3, now)
        db.save_model_cost("u1", "m", 3.5, 4, now + 1)   # 同主键 = 更新
        rows = db.model_costs()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cost_per_1k"], 3.5, places=6)
        self.assertEqual(rows[0]["samples"], 4)
        db.save_model_cost("u1", "old", 1.0, 1, now - 99999)
        db.purge_stale_model_costs(now - 100)
        self.assertEqual([r["model"] for r in db.model_costs()], ["m"])
        db.delete_model_cost("u1", "m")
        self.assertEqual(db.model_costs(), [])


class TestGrowthEndpoints(unittest.TestCase):
    """连登/活跃地图/补签卡的 CST 日期口径与响应归一化（P3）。"""

    def test_growth_yesterday_is_cst(self):
        from datetime import datetime, timezone, timedelta
        from workbuddy_one.billing import growth_yesterday
        cst = timezone(timedelta(hours=8))
        self.assertEqual(growth_yesterday(datetime(2026, 9, 21, 12, 0, tzinfo=cst)), "2026-09-20")
        # 月初回退到上月末
        self.assertEqual(growth_yesterday(datetime(2026, 3, 1, 0, 30, tzinfo=cst)), "2026-02-28")
        # **时区无关**：同一瞬时点在 UTC 表示下必须得到同一个 CST 日期
        # （CST 09-21 04:00 == UTC 09-20 20:00，昨日都是 09-20）
        utc = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(growth_yesterday(utc), "2026-09-20")
        # naive datetime 按 CST 解释，不按容器本地时区
        self.assertEqual(growth_yesterday(datetime(2026, 9, 21, 12, 0)), "2026-09-20")

    def test_growth_today_is_cst(self):
        from datetime import datetime, timezone, timedelta
        from workbuddy_one.billing import growth_today
        cst = timezone(timedelta(hours=8))
        self.assertEqual(growth_today(datetime(2026, 9, 21, 12, 0, tzinfo=cst)), "2026-09-21")
        # 跨日：CST 09-21 00:30 == UTC 09-20 16:30，CST 侧已是 21 日
        self.assertEqual(growth_today(datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)), "2026-09-21")

    def test_makeup_allowed_only_within_same_month(self):
        """补签只允许当月 —— **月初第一天必须为 False**。

        这是一条回归用例：`/admin/streak` 里原本写的是
        `yesterday[:7] == billing.growth_yesterday()[:7]`（同一个函数调两次比较月份），
        恒为 True，界面上的「跨月」分支从来没显示过。**任何"比较月份"的实现都必须
        让这条用例通过**——包括把两个不同来源的日期拿来比。
        """
        from datetime import datetime, timezone, timedelta
        from workbuddy_one.billing import makeup_allowed
        cst = timezone(timedelta(hours=8))
        # 月中：昨日与今日同月 → 允许
        self.assertTrue(makeup_allowed(datetime(2026, 9, 21, 12, 0, tzinfo=cst)))
        # 月初第一天：昨日是上个月最后一天 → **不允许**（上游会 400）
        self.assertFalse(makeup_allowed(datetime(2026, 9, 1, 12, 0, tzinfo=cst)))
        self.assertFalse(makeup_allowed(datetime(2026, 3, 1, 12, 0, tzinfo=cst)))
        # 年初：昨日是去年 12-31 → 不允许
        self.assertFalse(makeup_allowed(datetime(2026, 1, 1, 12, 0, tzinfo=cst)))
        # 跨年但同月（不可能，仅确认月份比较不含年份以外的东西）：2 月 1 日不允许
        self.assertFalse(makeup_allowed(datetime(2026, 2, 1, 0, 0, tzinfo=cst)))

    def test_admin_streak_uses_the_shared_month_rule(self):
        """接线守卫：路由层必须调 `billing.makeup_allowed()`，不能自己再写一遍月份比较。

        用 `_code_only` 而不是裸源码：本文件里那句"曾经写错的写法"写在注释里，
        裸匹配会把它当成证据（守卫用例必须只看代码，不看措辞）。
        """
        from workbuddy_one.routes import accounts as accounts_mod
        code = _code_only(accounts_mod)
        self.assertIn("makeup_allowed", code,
                      "/admin/streak 的跨月判定必须走 billing.makeup_allowed()")
        # 回归特征：`growth_yesterday()` **被调两次**就说明又在手算月份比较了
        # （原 bug 是 `yesterday[:7] == billing.growth_yesterday()[:7]` → 恒真）。
        # 调一次是合理的：要拿昨日日期去查 heatmap 的 score。
        self.assertEqual(code.count("growth_yesterday"), 1,
                         "growth_yesterday 只该调一次（取昨日日期）；"
                         "出现两次说明月份比较又各写各的了")

    def test_fetch_streak_normalizes(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        payload = {"code": 0, "data": {"streak": {"days": 2, "month_total_days": 7,
                                                  "makeup_dates": ["2026-09-01"]},
                                       "makeup_cards": {"balance": 1, "max": 4}}}
        with patch.object(billing, "_growth_get", return_value=payload):
            st = billing.fetch_streak(object())
        self.assertEqual(st["days"], 2)
        self.assertEqual(st["month_total_days"], 7)
        self.assertEqual(st["makeup_balance"], 1)
        self.assertEqual(st["makeup_max"], 4)
        self.assertEqual(st["makeup_dates"], ["2026-09-01"])

    def test_fetch_streak_raises_on_business_error(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        with patch.object(billing, "_growth_get", return_value={"code": 400, "msg": "boom"}):
            with self.assertRaises(RuntimeError):
                billing.fetch_streak(object())

    def test_fetch_heatmap_normalizes_and_truncates_date(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        payload = {"code": 0, "data": {"cells": [
            {"date": "2026-09-20T00:00:00+08:00", "score": 12},
            {"date": "2026-09-21", "score": 0},
            {"score": 5},                      # 无 date：跳过
        ]}}
        with patch.object(billing, "_growth_get", return_value=payload):
            heat = billing.fetch_heatmap(object())
        self.assertEqual(heat, {"2026-09-20": 12, "2026-09-21": 0})

    def test_use_makeup_card_reports_business_error(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        with patch.object(billing, "_growth_post",
                          return_value={"code": 400, "msg": "only current month makeup allowed"}):
            r = billing.use_makeup_card(object(), "1999-01-01")
        self.assertFalse(r["ok"])
        self.assertIn("only current month", r["message"])
        with patch.object(billing, "_growth_post", return_value={"code": 0}):
            self.assertTrue(billing.use_makeup_card(object(), "2026-09-20")["ok"])


class _FakeSettingsDB:
    def __init__(self, settings=None):
        self._s = dict(settings or {})

    def get_settings(self):
        return dict(self._s)


class TestMakeupPilot(unittest.IsolatedAsyncioTestCase):
    """补签保连登的判据链（P3）。"""

    def _sched(self, settings=None):
        from workbuddy_one.pool import AccountPool
        from workbuddy_one.scheduler import Scheduler
        pool = AccountPool({})
        pool.add_account("u1", None)
        acc = pool.accounts[0]
        acc.credits_remaining = 100
        acc.mgr = object()          # 非 None 即可：billing 调用全部被 mock
        return Scheduler(pool, db=_FakeSettingsDB(settings)), acc

    def _patch_billing(self, streak=None, heat=None, use_result=None):
        from unittest.mock import patch
        from workbuddy_one import billing
        st = streak or {"days": 3, "month_total_days": 5, "makeup_balance": 1,
                        "makeup_max": 4, "makeup_dates": []}
        ht = heat if heat is not None else {billing.growth_yesterday(): 0}
        use = use_result or {"ok": True, "message": "补签成功"}
        return [
            patch.object(billing, "fetch_streak", return_value=st),
            patch.object(billing, "fetch_heatmap", return_value=ht),
            patch.object(billing, "use_makeup_card", return_value=use),
        ]

    async def _run(self, sched, patches, **kw):
        for p in patches:
            p.start()
        try:
            return await sched.do_makeup(**kw)
        finally:
            for p in patches:
                p.stop()

    async def test_dry_run_does_not_call_upstream(self):
        """默认演练：判据齐全也不真补（补签要花用户自己的卡）。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched, _ = self._sched()
        patches = self._patch_billing()
        with patch.object(billing, "use_makeup_card") as used:
            results = await self._run(sched, patches, dry_run=True)
        used.assert_not_called()
        self.assertEqual(results[0]["action"], "would-makeup")
        self.assertEqual(results[0]["streak_days"], 3)

    async def test_real_run_calls_upstream(self):
        sched, _ = self._sched()
        results = await self._run(sched, self._patch_billing(), dry_run=False)
        self.assertEqual(results[0]["action"], "makeup")

    async def test_skips_when_yesterday_had_activity(self):
        sched, _ = self._sched()
        patches = self._patch_billing(heat={__import__("workbuddy_one.billing", fromlist=["x"]).growth_yesterday(): 42})
        results = await self._run(sched, patches, dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        # 判据是**对话活跃**（活跃连登），不是签到状态 —— 措辞要能看出这一点
        self.assertIn("有对话活动", results[0]["reason"])
        self.assertIn("活跃连登未断", results[0]["reason"])

    async def test_cross_month_skips_before_touching_upstream(self):
        """跨月（昨日属上月）整轮跳过，且**一个上游请求都不发**。

        上游只允许补当月，月初第一天必然如此。这里连探测都不做，是因为补不了
        就是补不了——没必要为每个账号白打两次请求。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched, _ = self._sched()
        with patch.object(billing, "makeup_allowed", return_value=False), \
             patch.object(billing, "fetch_streak") as fs, \
             patch.object(billing, "fetch_heatmap") as fh:
            results = await sched.do_makeup(dry_run=False)
        self.assertEqual(results, [])
        fs.assert_not_called()
        fh.assert_not_called()

    def test_judges_by_activity_not_by_checkin(self):
        """判据必须是**对话活跃**（heatmap），不是签到（checkin_dates）。

        上游有两条独立的连登：签到连登（billing 域）与活跃连登（growth 域）。
        补签卡挂在 growth 域 → 保护的是活跃连登。2026-09-21 实测两个口径结论**相反**
        （国内版账号已签到但当天无对话），拿 `checkin_dates` 判会把该补的那天漏掉。
        这条用例把"用哪个口径"钉死，防止后来者凭直觉"修正"成签到口径。
        """
        import inspect
        from workbuddy_one.scheduler import Scheduler
        code = _code_only(Scheduler.do_makeup)
        self.assertIn("fetch_heatmap", code, "活跃连登的判据来自 heatmap")
        self.assertNotIn("checkin_dates", code, "别用签到日期判补签——那是另一条连登")
        self.assertNotIn("fetch_checkin_status", code, "同上：补签判据不查签到状态")

    async def test_skips_without_cards(self):
        sched, _ = self._sched()
        st = {"days": 3, "month_total_days": 5, "makeup_balance": 0,
              "makeup_max": 4, "makeup_dates": []}
        results = await self._run(sched, self._patch_billing(streak=st), dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("无可用补签卡", results[0]["reason"])

    async def test_skips_when_already_made_up(self):
        sched, _ = self._sched()
        from workbuddy_one import billing
        st = {"days": 3, "month_total_days": 5, "makeup_balance": 1,
              "makeup_max": 4, "makeup_dates": [billing.growth_yesterday()]}
        results = await self._run(sched, self._patch_billing(streak=st), dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("已补过", results[0]["reason"])

    async def test_skips_when_heatmap_has_no_evidence(self):
        """活跃地图没覆盖昨日 → 无判据，不动（宁可漏补也不乱补）。"""
        sched, _ = self._sched()
        results = await self._run(sched, self._patch_billing(heat={}), dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("无判据", results[0]["reason"])

    async def test_query_failure_is_isolated(self):
        """单账号查询失败不影响其它账号，也不抛出去。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched, _ = self._sched()
        with patch.object(billing, "fetch_streak", side_effect=RuntimeError("网络挂了")):
            results = await sched.do_makeup(dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertIn("查询失败", results[0]["reason"])

    async def test_skips_disabled_accounts(self):
        sched, acc = self._sched()
        sched.pool.set_enabled("u1", False, reason="手动停用")
        self.assertEqual(await sched.do_makeup(dry_run=False), [])
        sched.pool.set_enabled("u1", True)
        sched.pool.disable_auto("u1", "session 失效")
        self.assertEqual(await sched.do_makeup(dry_run=False), [])

    async def test_default_switches_are_off_and_dry(self):
        """默认：总开关关、演练开 —— 出厂状态绝不消耗用户的补签卡。"""
        sched, _ = self._sched()
        self.assertFalse(sched._makeup_enabled())
        self.assertTrue(sched._makeup_dry_run())
        sched2, _ = self._sched({"makeup_enabled": "1", "makeup_dry_run": "0"})
        self.assertTrue(sched2._makeup_enabled())
        self.assertFalse(sched2._makeup_dry_run())

    # ---- 两阶段执行的并发性质（不要把读改回串行 / 把写改成并发）----

    def _sched_multi(self, n=2, settings=None):
        from workbuddy_one.pool import AccountPool
        from workbuddy_one.scheduler import Scheduler
        pool = AccountPool({})
        for i in range(n):
            pool.add_account(f"u{i}", None)
        for a in pool.accounts:
            a.credits_remaining = 100
            a.mgr = object()
        return Scheduler(pool, db=_FakeSettingsDB(settings))

    async def test_per_account_queries_run_concurrently(self):
        """单个账号的 streak 与 heatmap 必须**并发**发起。

        判据用 `threading.Barrier(2)` 而不是掐表：两者都到达屏障才能通过，
        串行执行时第一个会卡到超时抛 BrokenBarrierError → 用例失败。
        这样不依赖机器快慢（仓库在网络盘上，掐表测试很容易假阴性）。

        为什么值得守：两个请求各自 timeout=20，串行时一个账号最坏 40s、
        N 个账号就是 40N 秒，界面上点「立即检查」要等好几分钟。
        """
        import threading
        from unittest.mock import patch
        from workbuddy_one import billing

        barrier = threading.Barrier(2, timeout=5)

        def slow_streak(_mgr):
            barrier.wait()
            return {"days": 1, "month_total_days": 1, "makeup_balance": 0,
                    "makeup_max": 4, "makeup_dates": []}

        def slow_heat(_mgr):
            barrier.wait()
            return {billing.growth_yesterday(): 0}

        sched = self._sched_multi(1)
        with patch.object(billing, "fetch_streak", side_effect=slow_streak), \
             patch.object(billing, "fetch_heatmap", side_effect=slow_heat):
            results = await sched.do_makeup(dry_run=True)
        # 走到判据链而不是"查询失败" → 说明两个请求都真的完成了（屏障被双双放行）
        self.assertEqual(len(results), 1)
        self.assertNotIn("查询失败", results[0]["reason"])

    async def test_actual_makeup_writes_are_serial(self):
        """实际补签必须**串行**：花的是用户自己的卡，不能并发打上游。

        用"同时在执行的最大个数"来判断，而不是耗时——并发时计数会到 2。
        """
        import threading
        import time
        from unittest.mock import patch
        from workbuddy_one import billing

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow_use(_mgr, _date):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return {"ok": True, "message": "补签成功"}

        sched = self._sched_multi(3)
        with patch.object(billing, "fetch_streak",
                          return_value={"days": 1, "month_total_days": 1, "makeup_balance": 2,
                                        "makeup_max": 4, "makeup_dates": []}), \
             patch.object(billing, "fetch_heatmap",
                          return_value={billing.growth_yesterday(): 0}), \
             patch.object(billing, "use_makeup_card", side_effect=slow_use):
            results = await sched.do_makeup(dry_run=False)

        self.assertEqual([r["action"] for r in results], ["makeup"] * 3)
        self.assertEqual(state["peak"], 1, "补签请求出现并发，会瞬时冲击上游风控")


def _redemption(claimable=(), days=3, next_tier="7d", next_remaining=4):
    """构造 `fetch_streak` 的返回值，档位状态由 `claimable` 反推。

    期望值在这里**独立构造**（照抄上游实测的真实形状），不拿被测函数的输出
    跟自己比——那样被测函数原地改坏了返回值也看不出来。
    """
    status = {t: ("available" if t in claimable else "locked") for t in ("7d", "14d", "28d")}
    return {
        "days": days, "month_total_days": days, "makeup_balance": 0, "makeup_max": 4,
        "makeup_dates": [],
        "redemption": {
            "status": status,
            "claimable": [t for t in ("7d", "14d", "28d") if status[t] == "available"],
            "tiers": [
                {"tier": "7d", "days": 7, "credit": 0, "energy": 2, "cards": 1, "chances": 1},
                {"tier": "14d", "days": 14, "credit": 50, "energy": 3, "cards": 1, "chances": 1},
                {"tier": "28d", "days": 28, "credit": 150, "energy": 5, "cards": 1, "chances": 1},
            ],
            "next_tier": next_tier,
            "next_tier_remaining": next_remaining,
        },
    }


class TestRedemptionParsing(unittest.TestCase):
    """连登档位奖励（活跃地图领奖）的解析与错误语义。

    这一项是 2026-09-22 评估 `CODE_REVIEW_TODO.md` #11 剩下四个任务群后**唯一**
    决定落地的：它是**补签卡唯一的常规来源**，而两个账号的补签卡余额都是 0
    （因为活跃连登还没到 7 天），所以补签功能目前根本没有卡可用。
    """

    def _payload(self, claimable=(), extra_red=None, tiers=None):
        status = {f"tier_{t}_status": ("available" if t in claimable else "locked")
                  for t in ("7d", "14d", "28d")}
        red = {"tiers": tiers if tiers is not None else [
            {"tier": "7d", "days": 7, "credit": 0, "energy": 2, "cards": 1, "chances": 1},
        ], **status}
        red.update(extra_red or {})
        return {"code": 0, "data": {
            "streak": {"days": 9, "month_total_days": 9, "next_tier": "14d",
                       "next_tier_remaining": 5, "makeup_dates": []},
            "makeup_cards": {"balance": 0, "max": 4},
            "redemption_status": red,
        }}

    def test_parses_status_claimable_and_tiers(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        with patch.object(billing, "_growth_get",
                          return_value=self._payload(claimable=("7d",))):
            st = billing.fetch_streak(object())
        red = st["redemption"]
        self.assertEqual(red["status"], {"7d": "available", "14d": "locked", "28d": "locked"})
        self.assertEqual(red["claimable"], ["7d"])
        self.assertEqual(red["tiers"][0],
                         {"tier": "7d", "days": 7, "credit": 0, "energy": 2,
                          "cards": 1, "chances": 1})
        self.assertEqual(red["next_tier"], "14d")
        self.assertEqual(red["next_tier_remaining"], 5)

    def test_missing_status_field_defaults_to_locked(self):
        """缺 `tier_*_status` 字段时按 **locked**，不能按 available。

        宁可显示"领不了"，也不能因为字段缺失把不可领的档位显示成可领而误点
        （上游改版删字段时，这是唯一安全的默认方向）。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        payload = self._payload()
        del payload["data"]["redemption_status"]["tier_7d_status"]
        with patch.object(billing, "_growth_get", return_value=payload):
            st = billing.fetch_streak(object())
        self.assertEqual(st["redemption"]["status"]["7d"], "locked")
        self.assertEqual(st["redemption"]["claimable"], [])

    def test_missing_redemption_block_is_tolerated(self):
        """整个 `redemption_status` 缺失（老账号/未开活动）也要能返回，不能抛。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        payload = self._payload()
        del payload["data"]["redemption_status"]
        with patch.object(billing, "_growth_get", return_value=payload):
            st = billing.fetch_streak(object())
        self.assertEqual(st["redemption"]["claimable"], [])
        self.assertEqual(st["redemption"]["tiers"], [])

    def test_redeem_success_returns_granted(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        resp = (200, {"code": 0, "data": {"credit_granted": 50, "energy_granted": 3,
                                          "cards_granted": 1, "chances_granted": 1}})
        with patch.object(billing, "_growth_post_full", return_value=resp):
            r = billing.redeem_tier(object(), "14d")
        self.assertTrue(r["ok"])
        self.assertFalse(r["normal"])
        self.assertEqual(r["granted"]["cards_granted"], 1)
        self.assertEqual(r["granted"]["credit_granted"], 50)

    def test_redeem_already_claimed_is_normal(self):
        """同月重复领 → 409 duplicate，属正常态（幂等无副作用），不该当故障。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        with patch.object(billing, "_growth_post_full",
                          return_value=(409, {"code": 409, "msg": "duplicate"})):
            r = billing.redeem_tier(object(), "7d")
        self.assertFalse(r["ok"])
        self.assertTrue(r["normal"], "已领过是正常态，不该触发重试或告警")

    def test_redeem_not_enough_days_is_normal(self):
        """天数不足 → 403，实测文案就是「连续登录天数不足，请继续打卡或使用补签卡」。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        msg = "连续登录天数不足，请继续打卡或使用补签卡"
        with patch.object(billing, "_growth_post_full",
                          return_value=(403, {"code": 403, "msg": msg})):
            r = billing.redeem_tier(object(), "7d")
        self.assertFalse(r["ok"])
        self.assertTrue(r["normal"])
        self.assertIn("补签卡", r["message"])

    def test_redeem_server_error_is_not_normal(self):
        """真故障（5xx）不能被归成正常态，否则会静默丢掉真实错误。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        with patch.object(billing, "_growth_post_full",
                          return_value=(500, {"code": 500, "msg": "internal error"})):
            r = billing.redeem_tier(object(), "7d")
        self.assertFalse(r["ok"])
        self.assertFalse(r["normal"])
        self.assertIn("500", r["message"])

    def test_draw_disabled_and_no_chance_are_both_normal(self):
        """抽奖的两个 400 都是正常态，且**文案按区域不同**，判据要两个都认。

        实测：国内版 `insufficient lottery chance balance`（没次数）、
        国际版 `lottery disabled`（抽奖活动在国际版根本没开）。只认一个的话，
        另一个区域每次都会被刷成 WARN。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        for msg in ("insufficient lottery chance balance", "lottery disabled"):
            with patch.object(billing, "_growth_post_full",
                              return_value=(400, {"code": 400, "msg": msg})):
                r = billing.draw_lottery(object())
            self.assertFalse(r["ok"], msg)
            self.assertTrue(r["normal"], f"{msg} 应归为正常态")

    def test_draw_success_returns_prize(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        resp = (200, {"code": 0, "data": {"prize_code": "credit_10", "prize_name": "10 积分",
                                          "prize_type": "credit", "credit_amount": 10}})
        with patch.object(billing, "_growth_post_full", return_value=resp):
            r = billing.draw_lottery(object())
        self.assertTrue(r["ok"])
        self.assertEqual(r["prize"]["credit"], 10)

    def test_client_token_is_fresh_every_call(self):
        """幂等键必须每次新生成。

        上游按 `client_token` 去重，复用旧值可能让本次领取/抽奖被**静默吞掉**
        （返回成功但什么都没发生）——参考实现里官方 SPA 每次也是新 uuid。
        """
        from workbuddy_one import billing
        tokens = {billing._growth_client_token("redeem-7d") for _ in range(50)}
        self.assertEqual(len(tokens), 50, "幂等键出现重复，可能被上游去重吞掉本次领取")
        self.assertTrue(all(t.startswith("redeem-7d-") for t in tokens))


class TestRedeemPilot(unittest.IsolatedAsyncioTestCase):
    """`Scheduler.do_redeem` 的两阶段行为。"""

    def _sched(self, n=1):
        from workbuddy_one.pool import AccountPool
        from workbuddy_one.scheduler import Scheduler
        pool = AccountPool({})
        for i in range(n):
            pool.add_account(f"u{i}", None)
        for a in pool.accounts:
            a.credits_remaining = 100
            a.mgr = object()
        return Scheduler(pool, db=_FakeSettingsDB())

    async def test_dry_run_does_not_write(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d",))), \
             patch.object(billing, "redeem_tier") as redeemed:
            results = await sched.do_redeem(dry_run=True)
        redeemed.assert_not_called()
        self.assertEqual(results[0]["action"], "would-redeem")
        self.assertIn("7d", results[0]["reason"])
        self.assertEqual(results[0]["claimable"], ["7d"])

    async def test_no_claimable_reports_distance_to_next_tier(self):
        """没到档位时要报进度——这是用户唯一能看到的进度信息。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=(), days=3, next_remaining=4)):
            results = await sched.do_redeem(dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("还差", results[0]["reason"])
        self.assertIn("4", results[0]["reason"])

    async def test_broken_streak_message_does_not_imply_distance_from_streak(self):
        """连登断档时 `next_tier_remaining` 与 `days` 会脱节，措辞不能暗示二者相减。

        实测（2026-09-22）国内版：`streak.days=0` 而 `next_tier_remaining=2`
        —— 因为该字段的口径是「档位天数 − **当月最长连续段**」（本月最长 5 天），
        **不是**「档位天数 − 当前连登」。若文案写成"当前活跃连登 0 天，还差 2 天"，
        读起来像"再连 2 天就够"——而连登其实已经断了。所以两个数必须分开陈述，
        且 `remaining` 要标明是**上游口径**，不能当成我们自己的算术结果。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=(), days=0, next_remaining=2)):
            results = await sched.do_redeem(dry_run=False)
        reason = results[0]["reason"]
        self.assertIn("活跃连登 0 天", reason)
        self.assertIn("上游报还差 2 天", reason)
        # 不能出现把两个数拼成一句因果的旧写法
        self.assertNotIn("当前活跃连登", reason)

    async def test_real_run_redeems_every_claimable_tier_in_order(self):
        """一次点下去要把所有可领档位都领掉，且按 7d → 14d 顺序。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        seen = []

        def fake_redeem(_mgr, tier):
            seen.append(tier)
            return {"ok": True, "normal": False, "message": "领取成功",
                    "granted": {"cards_granted": 1}}

        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d", "14d"))), \
             patch.object(billing, "redeem_tier", side_effect=fake_redeem):
            results = await sched.do_redeem(dry_run=False)
        self.assertEqual(seen, ["7d", "14d"])
        self.assertEqual(results[0]["action"], "redeem")
        self.assertEqual([c["tier"] for c in results[0]["claimed"]], ["7d", "14d"])

    async def test_normal_rejection_does_not_abort_remaining_tiers(self):
        """某个档位被正常拒绝（如已领过），不能中断后面档位的领取。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        calls = []

        def fake_redeem(_mgr, tier):
            calls.append(tier)
            if tier == "7d":
                return {"ok": False, "normal": True, "message": "本月已领取过该档位"}
            return {"ok": True, "normal": False, "message": "领取成功", "granted": {}}

        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d", "14d"))), \
             patch.object(billing, "redeem_tier", side_effect=fake_redeem):
            results = await sched.do_redeem(dry_run=False)
        self.assertEqual(calls, ["7d", "14d"])
        self.assertFalse(results[0]["claimed"][0]["ok"])
        self.assertTrue(results[0]["claimed"][1]["ok"])

    async def test_draw_only_when_asked_and_chances_present(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d",))), \
             patch.object(billing, "fetch_lottery_chances", return_value=0), \
             patch.object(billing, "redeem_tier",
                          return_value={"ok": True, "normal": False, "message": "ok",
                                        "granted": {}}), \
             patch.object(billing, "draw_lottery") as drew:
            await sched.do_redeem(dry_run=False, draw=True)
        drew.assert_not_called()          # 次数为 0 就不抽
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d",))), \
             patch.object(billing, "fetch_lottery_chances", return_value=2), \
             patch.object(billing, "redeem_tier",
                          return_value={"ok": True, "normal": False, "message": "ok",
                                        "granted": {}}), \
             patch.object(billing, "draw_lottery",
                          return_value={"ok": True, "normal": False, "message": "抽奖成功",
                                        "prize": {"credit": 10}}):
            results = await sched.do_redeem(dry_run=False, draw=True)
        self.assertTrue(results[0]["draw"]["ok"])

    async def test_query_failure_is_isolated(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched(n=2)
        with patch.object(billing, "fetch_streak",
                          side_effect=[RuntimeError("网络挂了"), _redemption(claimable=("7d",))]), \
             patch.object(billing, "redeem_tier",
                          return_value={"ok": True, "normal": False, "message": "ok",
                                        "granted": {}}):
            results = await sched.do_redeem(dry_run=False)
        self.assertIn("网络挂了", results[0]["reason"])
        self.assertEqual(results[1]["action"], "redeem")

    async def test_skips_disabled_accounts(self):
        sched = self._sched()
        sched.pool.set_enabled(sched.pool.accounts[0].uid, False, reason="手动停用")
        self.assertEqual(await sched.do_redeem(dry_run=False), [])

    async def test_actual_redeem_writes_are_serial(self):
        """真领必须**串行**：会改上游账号状态，不能并发冲击上游。

        判据用"同时在执行的最大个数"，不掐表（仓库在网络盘上，掐表易假阴性）。
        """
        import threading
        import time
        from unittest.mock import patch
        from workbuddy_one import billing

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow_redeem(_mgr, _tier):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return {"ok": True, "normal": False, "message": "ok", "granted": {}}

        sched = self._sched(n=3)
        with patch.object(billing, "fetch_streak",
                          return_value=_redemption(claimable=("7d",))), \
             patch.object(billing, "redeem_tier", side_effect=slow_redeem):
            results = await sched.do_redeem(dry_run=False)

        self.assertEqual([r["action"] for r in results], ["redeem"] * 3)
        self.assertEqual(state["peak"], 1, "领奖请求出现并发，会瞬时冲击上游风控")


class _FakeDomainMgr:
    """只带 `domain` 属性的假凭据管理器。

    `region.region_of_account()` **只读 `mgr.domain`**，所以给一个带 domain 的哑对象
    就能精确控制账号归属区域。不用 `object()`：那个取不到 domain 会回落国内版，
    没法构造"国际版账号"这一侧。
    """

    def __init__(self, domain):
        self.domain = domain


def _travel(state="idle", daily_limit_reached=False, record_id=0, reward_credit=0):
    """构造 `fetch_travel_status` 的返回值（照抄上游实测形状）。"""
    return {
        "state": state,
        "daily_limit_reached": daily_limit_reached,
        "record_id": record_id,
        "reward_credit": reward_credit,
        "location": "咖啡馆" if state not in ("none", "idle") else "",
        "arrive_at": 0,
    }


class TestTravelPilot(unittest.IsolatedAsyncioTestCase):
    """`Scheduler.do_travel` 的状态机与两阶段行为（猫猫旅行，**国内版专属**）。

    状态机：`none`(无猫→领养) → `idle`(派出) → `traveling`(等) → `arrived`(领取)。
    `daily_limit_reached` 决定**每天只有 1 趟**。
    """

    def _sched(self, regions=("cn",), settings=None):
        from workbuddy_one.pool import AccountPool
        from workbuddy_one.scheduler import Scheduler
        pool = AccountPool({})
        for i, _ in enumerate(regions):
            pool.add_account(f"u{i}", None)
        for a, rid in zip(pool.accounts, regions):
            a.credits_remaining = 100
            a.mgr = _FakeDomainMgr(
                "www.codebuddy.cn" if rid == "cn" else "www.workbuddy.ai")
        return Scheduler(pool, db=_FakeSettingsDB(settings))

    # ---- 区域过滤 ----

    async def test_only_cn_accounts_are_processed(self):
        """国际版没有这套体系（`travel/config` 返回空 data），调度层必须按区域跳过。

        不跳过的话每轮白打两个必然失败的请求——账号一多就是持续的无效出站。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched(regions=("cn", "global"))
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("idle")), \
             patch.object(billing, "travel_depart",
                          return_value={"ok": True, "normal": False, "message": "已派出"}) as departed:
            results = await sched.do_travel(dry_run=False)
        self.assertEqual([r["uid"] for r in results], ["u0"])
        self.assertEqual(departed.call_count, 1)

    def test_region_filter_is_in_code_not_just_comment(self):
        """区域过滤必须真在代码里——注释里写了不算。

        注意 `_code_only()` 用 `tokenize` 把**字符串字面量也一并滤掉**，所以这里
        断言不了 `"cn"` 这个取值本身；取值正确性由上面那条行为用例
        （`test_only_cn_accounts_are_processed`）兜底，这条只守"有没有真的调区域判定"。
        """
        from workbuddy_one.scheduler import Scheduler
        code = _code_only(Scheduler.do_travel)
        self.assertIn("region_of_account", code)

    # ---- 演练 / 实跑 ----

    async def test_default_switches_are_off_and_dry(self):
        """默认：总开关关、演练开 —— 出厂状态不会替用户动账号。"""
        sched = self._sched()
        self.assertFalse(sched._travel_enabled())
        self.assertTrue(sched._travel_dry_run())
        sched2 = self._sched(settings={"travel_enabled": "1", "travel_dry_run": "0"})
        self.assertTrue(sched2._travel_enabled())
        self.assertFalse(sched2._travel_dry_run())

    async def test_dry_run_does_not_write(self):
        """演练模式一个写请求都不发（只报"会做什么"）。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("idle")), \
             patch.object(billing, "travel_depart") as departed, \
             patch.object(billing, "travel_claim") as claimed:
            results = await sched.do_travel(dry_run=True)
        departed.assert_not_called()
        claimed.assert_not_called()
        self.assertEqual(results[0]["action"], "would-depart")
        self.assertEqual(results[0]["state"], "idle")

    async def test_dry_run_reports_would_claim_for_arrived(self):
        """到站时演练要报出**会领多少分**——这是用户确认判据的唯一途径。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status",
                          return_value=_travel("arrived", record_id=7589803, reward_credit=10)), \
             patch.object(billing, "travel_claim") as claimed, \
             patch.object(billing, "travel_depart") as departed:
            results = await sched.do_travel(dry_run=True)
        claimed.assert_not_called()
        departed.assert_not_called()
        self.assertEqual(results[0]["action"], "would-claim")
        self.assertIn("10", results[0]["reason"])

    async def test_idle_departs(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("idle")), \
             patch.object(billing, "travel_depart",
                          return_value={"ok": True, "normal": False, "message": "已派出"}) as departed:
            results = await sched.do_travel(dry_run=False)
        departed.assert_called_once()
        self.assertEqual(results[0]["action"], "depart")

    async def test_arrived_claims_then_departs_in_same_round(self):
        """到站后**同一轮顺手把当天那趟派出去**（一轮走两步）。

        这是刻意的：只做"一个动作"的话，节奏会被 `checkin_hours` 绑死——
        配成单点时（如只有 21 点）会退化成"两天才领一次"。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status",
                          return_value=_travel("arrived", record_id=7589803, reward_credit=10)), \
             patch.object(billing, "travel_claim",
                          return_value={"ok": True, "normal": False,
                                        "message": "领取成功", "reward": 10}) as claimed, \
             patch.object(billing, "travel_depart",
                          return_value={"ok": True, "normal": False, "message": "已派出"}) as departed:
            results = await sched.do_travel(dry_run=False)
        self.assertEqual(results[0]["action"], "claim+depart")
        self.assertEqual(results[0]["reward"], 10)
        # record_id 必须是探测到的那一个（写死/传错就领不到）
        self.assertEqual(claimed.call_args.args[1], 7589803)
        departed.assert_called_once()

    async def test_traveling_is_skipped_without_writes(self):
        """行程进行中只等下一轮：不轮询、不等待、不发写请求。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("traveling")), \
             patch.object(billing, "travel_depart") as departed, \
             patch.object(billing, "travel_claim") as claimed:
            results = await sched.do_travel(dry_run=False)
        departed.assert_not_called()
        claimed.assert_not_called()
        self.assertEqual(results[0]["action"], "skip")

    async def test_daily_limit_reached_skips_depart(self):
        """今日已派出过就不重复派——上游每天只允许 1 趟。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status",
                          return_value=_travel("idle", daily_limit_reached=True)), \
             patch.object(billing, "travel_depart") as departed:
            results = await sched.do_travel(dry_run=False)
        departed.assert_not_called()
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("每天 1 趟", results[0]["reason"])

    async def test_arrived_without_record_id_does_not_claim(self):
        """上游没给 record_id 就不领（拿不到幂等键，硬发只会拿到 400）。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status",
                          return_value=_travel("arrived", record_id=0, reward_credit=10)), \
             patch.object(billing, "travel_claim") as claimed:
            results = await sched.do_travel(dry_run=False)
        claimed.assert_not_called()
        self.assertIn("record_id", results[0]["reason"])

    # ---- 领养 ----

    async def test_no_buddy_adopts_once_per_day(self):
        """无猫时领养，且**当天只试一次**。

        门槛未达标（`first_buddy task not completed yet`）是预期行为——新账号要先
        攒够对话量，当天重试也不会成功，重复打只会轰炸上游。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        reject = {"ok": False, "normal": True, "message": "对话量未达领养门槛（预期行为，明日再试）"}
        with patch.object(billing, "fetch_buddy", return_value=None), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("none")), \
             patch.object(billing, "buddy_adopt", return_value=reject) as adopt:
            first = await sched.do_travel(dry_run=False)
            second = await sched.do_travel(dry_run=False)
        self.assertEqual(first[0]["action"], "adopt-skip")
        self.assertEqual(adopt.call_count, 1, "当天重复领养会轰炸上游")
        self.assertIn("今日已试过领养", second[0]["reason"])

    async def test_adopt_success_reports_adopt(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value=None), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("none")), \
             patch.object(billing, "buddy_adopt",
                          return_value={"ok": True, "normal": False, "message": "领养成功"}):
            results = await sched.do_travel(dry_run=False)
        self.assertEqual(results[0]["action"], "adopt")

    async def test_dry_run_does_not_adopt(self):
        """演练不领养（领养也是写请求）。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value=None), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("none")), \
             patch.object(billing, "buddy_adopt") as adopt:
            results = await sched.do_travel(dry_run=True)
        adopt.assert_not_called()
        self.assertEqual(results[0]["action"], "would-adopt")

    # ---- 两阶段执行的并发性质 ----

    async def test_actual_writes_are_serial(self):
        """真跑必须**串行**：写请求会改上游账号状态，不能并发冲击上游风控。

        判据用"同时在执行的最大个数"，不掐表（仓库在网络盘上，掐表易假阴性）。
        """
        import threading
        import time
        from unittest.mock import patch
        from workbuddy_one import billing

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow_depart(_mgr, _loc=1):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return {"ok": True, "normal": False, "message": "已派出"}

        sched = self._sched(regions=("cn", "cn", "cn"))
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("idle")), \
             patch.object(billing, "travel_depart", side_effect=slow_depart):
            results = await sched.do_travel(dry_run=False)

        self.assertEqual([r["action"] for r in results], ["depart"] * 3)
        self.assertEqual(state["peak"], 1, "旅行写请求出现并发，会瞬时冲击上游风控")

    async def test_query_failure_is_isolated(self):
        """一个账号查询失败不能带崩其他账号。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched(regions=("cn", "cn"))
        bad = sched.pool.accounts[0].mgr

        def fake_status(mgr):
            if mgr is bad:
                raise RuntimeError("网络挂了")
            return _travel("idle")

        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", side_effect=fake_status), \
             patch.object(billing, "travel_depart",
                          return_value={"ok": True, "normal": False, "message": "已派出"}):
            results = await sched.do_travel(dry_run=False)
        self.assertIn("查询失败", results[0]["reason"])
        self.assertIn("网络挂了", results[0]["reason"])
        self.assertEqual(results[1]["action"], "depart")

    async def test_skips_disabled_accounts(self):
        sched = self._sched()
        sched.pool.set_enabled(sched.pool.accounts[0].uid, False, reason="手动停用")
        self.assertEqual(await sched.do_travel(dry_run=False), [])

    async def test_unknown_state_is_reported_not_guessed(self):
        """上游以后加了新状态时要能立刻看见，不要猜成 skip 一了百了。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_buddy", return_value={"id": 1}), \
             patch.object(billing, "fetch_travel_status", return_value=_travel("brand_new_state")):
            results = await sched.do_travel(dry_run=False)
        self.assertEqual(results[0]["action"], "skip")
        self.assertIn("未知状态", results[0]["reason"])


class TestActiveMapPilot(unittest.IsolatedAsyncioTestCase):
    """`Scheduler.do_active_map_check`：**只读 + 只提醒**的每日活跃地图检查。

    这个任务的前提是一条**实测结论**，不是设计偏好（别推翻它）：
    2026-09-22 受控实验证明 **chat API 点不亮活跃地图** —— 给当天 `score == 0` 的
    国内版账号发了两次真实对话请求（都 HTTP 200），复查今天格子**仍然是 0**；
    历史数据同样（09-12 该账号 31 次请求 / 419 积分，当天 heat 仍是 0）。
    参考实现点亮它靠的是「对话事件连发上报」= 伪造客户端事件上报，本项目不做。

    所以这一节最核心的守卫是：**它绝不发任何写请求、绝不代发对话**。
    """

    def _sched(self, regions=("cn",), settings=None):
        from workbuddy_one.pool import AccountPool
        from workbuddy_one.scheduler import Scheduler
        pool = AccountPool({})
        for i, _ in enumerate(regions):
            pool.add_account(f"u{i}", None)
        for a, rid in zip(pool.accounts, regions):
            a.credits_remaining = 100
            a.mgr = _FakeDomainMgr(
                "www.codebuddy.cn" if rid == "cn" else "www.workbuddy.ai")
        return Scheduler(pool, db=_FakeSettingsDB(settings))

    def _patch(self, heat, streak=None, today=None):
        """patch 掉两个只读上游查询。`heat` 为 dict 或 Exception。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        st = streak or {"days": 3, "month_total_days": 5, "makeup_balance": 1,
                        "makeup_max": 4, "makeup_dates": []}
        t = today or billing.growth_today()
        heat_patch = (patch.object(billing, "fetch_heatmap", side_effect=heat)
                      if isinstance(heat, Exception)
                      else patch.object(billing, "fetch_heatmap", return_value=heat))
        return t, [
            patch.object(billing, "fetch_streak", return_value=st),
            heat_patch,
        ]

    # ---- 判据 ----

    async def test_unlit_when_today_cell_is_zero(self):
        """今天有格子且 score=0 → 未点亮，需要提醒。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            results = await sched.do_active_map_check(notify=True)
        self.assertEqual(results[0]["action"], "unlit")
        self.assertEqual(results[0]["heat_today"], 0)
        self.assertEqual(results[0]["streak_days"], 3)
        hook.assert_awaited_once()

    async def test_lit_when_today_cell_is_positive(self):
        """今天 score>0 → 已点亮，跳过、不提醒。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 4})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            results = await sched.do_active_map_check(notify=True)
        self.assertEqual(results[0]["action"], "lit")
        hook.assert_not_called()

    async def test_no_cell_today_is_not_treated_as_unlit(self):
        """地图里**没有今天这一格** → `no-cell`，**绝不能当成"未点亮"**。

        这是本节最重要的一条：`heat.get(today)` 取不到值时会返回 `None`，
        若判据写成 `if not heat[today]` 或 `heat[today] == 0` 的宽松形式，
        上游一换窗口口径就会**全量误报**——用户被叫去点一堆其实不需要点的账号，
        而且连登根本没断。`None`（无判据）与 `0`（真没活跃）必须严格分开。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        # 只给"昨天"的格子，今天缺席
        today, patches = self._patch({billing.growth_yesterday(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            results = await sched.do_active_map_check(notify=True)
        self.assertEqual(results[0]["action"], "no-cell")
        self.assertNotIn("heat_today", results[0])   # 没有判据就别编一个 0 出来
        hook.assert_not_called()

    async def test_query_failure_does_not_break_other_accounts(self):
        """一个账号查询失败不影响其余账号，且失败原因要带出来。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched(regions=("cn", "cn"))
        bad = sched.pool.accounts[0].mgr
        today = billing.growth_today()

        def fake_heat(mgr):
            if mgr is bad:
                raise RuntimeError("网络挂了")
            return {today: 0}

        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_streak",
                          return_value={"days": 1, "month_total_days": 1}), \
             patch.object(billing, "fetch_heatmap", side_effect=fake_heat):
            results = await sched.do_active_map_check(notify=False)
        self.assertEqual(results[0]["action"], "query-failed")
        self.assertIn("网络挂了", results[0]["reason"])
        self.assertEqual(results[1]["action"], "unlit")

    async def test_skips_disabled_accounts(self):
        sched = self._sched()
        sched.pool.set_enabled(sched.pool.accounts[0].uid, False, reason="手动停用")
        self.assertEqual(await sched.do_active_map_check(notify=False), [])

    # ---- 只读守卫（本功能的存在前提）----

    async def test_is_read_only_no_write_request_at_all(self):
        """**绝不发写请求**：不代发对话、不补签、不领奖、不旅行。

        这条守的是本功能的**根本前提**：实测已证明 chat API 点不亮活跃地图，
        所以任何"顺手替你发一条对话"的实现都是**无效且白花积分**的。
        真要做那种事，必须先推翻 2026-09-22 的受控实验（见类 docstring）。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        # 所有可能的写通道全部挂上哨兵：被调用即失败
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(billing, "use_makeup_card") as w1, \
             patch.object(billing, "redeem_tier") as w2, \
             patch.object(billing, "travel_depart") as w3, \
             patch.object(billing, "travel_claim") as w4, \
             patch.object(billing, "buddy_adopt") as w5, \
             patch.object(sched, "_send_webhook"):
            await sched.do_active_map_check(notify=False)
        for name, m in (("use_makeup_card", w1), ("redeem_tier", w2),
                        ("travel_depart", w3), ("travel_claim", w4),
                        ("buddy_adopt", w5)):
            m.assert_not_called()

    async def test_has_no_dry_run_parameter(self):
        """**没有 `dry_run`**：它本来就没有"会改状态"的分支。

        与补签/旅行刻意不同——那两个默认演练是为了"先确认判据/先确认状态机"，
        这里连可写的东西都没有，多一个 dry_run 只会让人误以为"实跑会做别的事"。
        """
        import inspect
        from workbuddy_one.scheduler import Scheduler
        sig = inspect.signature(Scheduler.do_active_map_check)
        self.assertNotIn("dry_run", sig.parameters)
        self.assertIn("notify", sig.parameters)

    # ---- 开关与定时 ----

    async def test_default_is_on_unlike_makeup_and_travel(self):
        """**唯一默认开**的定时任务：只读 + 只提醒，最坏结果就是一条用户本来就要的提醒。

        这条是刻意与补签/旅行相反的（那两个默认关）。若有人"为了统一风格"把它
        也改成默认关，用户就会静默失去连登保护——所以钉住。
        """
        sched = self._sched()
        self.assertTrue(sched._active_map_enabled())
        self.assertEqual(sched._active_map_hour(), 23)
        off = self._sched(settings={"active_map_enabled": "0"})
        self.assertFalse(off._active_map_enabled())
        h = self._sched(settings={"active_map_hour": "21"})
        self.assertEqual(h._active_map_hour(), 21)

    def test_hour_setting_falls_back_when_garbage(self):
        """脏值必须回落默认 23，而不是抛异常把整个调度循环带崩。"""
        sched = self._sched(settings={"active_map_hour": "abc"})
        self.assertEqual(sched._active_map_hour(), 23)
        sched2 = self._sched(settings={"active_map_hour": "99"})
        self.assertEqual(sched2._active_map_hour(), 23)

    async def test_notify_false_never_pushes_webhook(self):
        """手动检查（notify=False）绝不推通知：点一次骚扰一条没人受得了。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            await sched.do_active_map_check(notify=False)
        hook.assert_not_called()

    async def test_webhook_body_warns_that_gateway_requests_do_not_light_it(self):
        """通知正文必须明确写"本网关的请求点不亮，只有官方客户端会"。

        这不是文案洁癖：不知道这一点的人会去翻本网关的日志、以为是网关没生效，
        或者干脆再发几次请求白花积分。文案本身是**功能的一部分**。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            await sched.do_active_map_check(notify=True)
        body = hook.await_args.args[2]
        self.assertIn("官方客户端", body)
        self.assertIn("不会", body)
        self.assertIn(today, body)
        # 纯文本渠道（Bark / 企微 / 飞书都是纯文本渲染）会**原样显示** Markdown 标记，
        # `**不会**` 在手机上就是带星号的 `**不会**`。真实投递实测踩到过，钉住。
        self.assertNotIn("**", body, "webhook 正文是纯文本，别用 Markdown 强调")

    async def test_no_webhook_configured_still_returns_results(self):
        """没配 webhook 也不能报错：日志里那条 warning 就是唯一可见信号。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook") as hook:
            results = await sched.do_active_map_check(notify=True)
        hook.assert_not_called()
        self.assertEqual(results[0]["action"], "unlit")

    def test_scheduler_registers_active_map_task(self):
        """定时任务必须真注册进 `_run`，否则"加了功能但永远不会自己跑"。"""
        import inspect
        from workbuddy_one.scheduler import Scheduler
        src = inspect.getsource(Scheduler._run)
        self.assertIn("do_active_map_check", src)
        self.assertIn("_active_map_enabled", src)
        self.assertIn("_last_active_map_date", src)
        # 必须是 `>=` 而不是 `==`：服务在检查点之后才启动（本机开发常态）也要补跑，
        # 否则 23:05 重启就白等一天、连登直接断。当天只跑一次由日期槽位保证。
        self.assertIn("now.hour >= self._active_map_hour()", src,
                      "用 == 判定会让「晚于检查点启动」直接漏掉一整天")

    # ---- 快照（供概览页横幅只读）----

    async def test_snapshot_is_none_before_any_check(self):
        """从没查过 → `None`，调用方不该据此报任何东西。"""
        sched = self._sched()
        self.assertIsNone(sched.active_map_snapshot())

    async def test_snapshot_records_unlit_accounts(self):
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1], \
             patch.object(sched, "_send_webhook"):
            await sched.do_active_map_check(notify=False)
        snap = sched.active_map_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["date"], today)
        self.assertEqual(snap["total"], 1)
        self.assertEqual([u["uid"] for u in snap["unlit"]], ["u0"])
        # 区域标签要带上：混池时只报 uid 前缀，用户分不清是哪个区域的账号
        self.assertIn("region_label", snap["unlit"][0])
        self.assertEqual(snap["unlit"][0]["streak_days"], 3)

    async def test_snapshot_recorded_even_when_all_lit(self):
        """全部点亮也要落快照。

        概览要能区分「查过了、都亮着」与「从来没查过」—— 两者都不该显示横幅，
        但原因完全不同，排查时（"定时任务到底跑没跑"）这个区别是决定性的。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 5})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1]:
            await sched.do_active_map_check(notify=False)
        snap = sched.active_map_snapshot()
        self.assertIsNotNone(snap, "全亮时没落快照 → 概览分不清「都亮着」和「没查过」")
        self.assertEqual(snap["unlit"], [])

    async def test_manual_check_also_updates_snapshot(self):
        """`notify=False` 的手动检查也要落快照 —— 用户点一次，概览横幅应立刻同步。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1]:
            await sched.do_active_map_check(notify=False)
        self.assertIsNotNone(sched.active_map_snapshot())

    async def test_snapshot_is_ignored_after_midnight(self):
        """跨零点后旧快照必须失效。

        拿昨天的结果去提醒，会让用户对着一个**已经过去的日子**白跑一趟官方客户端，
        而今天的连登照样断 —— 假警报比不提醒更糟。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1]:
            await sched.do_active_map_check(notify=False)
        self.assertIsNotNone(sched.active_map_snapshot())
        # 把快照的日期改成昨天，模拟"跨零点后"
        sched._active_map_snapshot["date"] = "2000-01-01"
        self.assertIsNone(sched.active_map_snapshot(), "过期快照没被拦下 → 会报昨天的假警报")

    async def test_snapshot_accessor_does_not_hit_upstream(self):
        """读快照**不能**打上游：它是给高频轮询的概览端点用的。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        with patch.object(billing, "fetch_heatmap") as h, \
             patch.object(billing, "fetch_streak") as s:
            sched.active_map_snapshot()
        h.assert_not_called()
        s.assert_not_called()


    # ---- 容错：判据只依赖 heatmap，装饰性数据不许有否决权 ----

    async def test_streak_failure_does_not_lose_heat_verdict(self):
        """**streak 查失败不能吞掉 heatmap 的结论**（这条是实测漏报的回归守卫）。

        `streak` 只提供 `streak_days` 供展示，判据完全来自 `heatmap.score`。
        以前两者用裸 `asyncio.gather` 绑在一起，任一个失败就把整条判成 `query-failed`
        → **明明 heatmap 已经返回 `score=0`，也照样不提醒**。
        把装饰性数据和判据绑在一起，等于让装饰性数据拥有否决权 —— 而后果是
        **漏报**，也就是本功能唯一要防的那件事。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap", return_value={today: 0}), \
             patch.object(billing, "fetch_streak",
                          side_effect=RuntimeError("streak 抖了")), \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            results = await sched.do_active_map_check(notify=True)
        self.assertEqual(results[0]["action"], "unlit",
                         "streak 失败把 heatmap 的「未点亮」结论吞掉了 → 漏报")
        self.assertIsNone(results[0]["streak_days"], "streak 拿不到就该是 None，不是编一个数")
        hook.assert_awaited_once()   # 该提醒的照常提醒

    async def test_heatmap_failure_is_retried_once(self):
        """重试**在 `billing._growth_get` 里**做，调度器只打一次。

        这里断言的是"调度器**不**重复重试"——两处都做会变成 3 次尝试、耗时翻倍，
        而调用方是按"一个账号最坏 20s"安排并发与前端超时的。
        （重试本身的用例见 `TestGrowthGetRetry`。）
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap", return_value={today: 0}) as hm, \
             patch.object(billing, "fetch_streak",
                          return_value={"days": 3, "month_total_days": 5}):
            await sched.do_active_map_check(notify=False)
        self.assertEqual(hm.call_count, 1,
                         "调度器自己又重试了一遍 → 和 billing 里的重试叠加成 3 次尝试")

    async def test_heatmap_failure_after_retry_is_unknown_not_unlit(self):
        """重试仍失败 → `query-failed`，且**必须进"未知"桶**，不能并进 lit/unlit。

        并进 `unlit` 是编造结论（我们并不知道它没点亮）；并进 `lit` 是假装没事。
        正确做法是照常报出来，让用户自己决定要不要保险起见去聊一句。
        """
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap",
                          side_effect=ConnectionError("UNEXPECTED_EOF_WHILE_READING")), \
             patch.object(billing, "fetch_streak",
                          return_value={"days": 3, "month_total_days": 5}):
            results = await sched.do_active_map_check(notify=False)
        self.assertEqual(results[0]["action"], "query-failed")
        snap = sched.active_map_snapshot()
        self.assertEqual(snap["unlit"], [], "查询失败被并进了 unlit —— 那是编造结论")
        self.assertEqual([u["uid"] for u in snap["unknown"]], ["u0"])

    async def test_unknown_is_surfaced_in_webhook_without_claiming_unlit(self):
        """查不到也必须推通知（否则一次 TLS 抖动就让账号静默失去保护），
        但**措辞不能说成"未点亮"** —— 那是我们不知道的事。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap",
                          side_effect=ConnectionError("TLS 抖动")), \
             patch.object(billing, "fetch_streak",
                          return_value={"days": 3, "month_total_days": 5}), \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            await sched.do_active_map_check(notify=True)
        hook.assert_awaited_once()   # 不推 = 静默失去保护
        body = hook.await_args.args[2]
        self.assertIn("查询失败", body)
        self.assertIn("无法判断", body)
        self.assertNotIn("没有点亮", body, "把「查不到」说成「没点亮」是编造结论")
        self.assertNotIn("**", body, "webhook 正文是纯文本，别用 Markdown 强调")

    async def test_lit_account_with_failed_streak_is_still_lit(self):
        """反向也要守住：heatmap 说已点亮时，streak 失败不能把它判成 query-failed。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap", return_value={today: 7}), \
             patch.object(billing, "fetch_streak", side_effect=RuntimeError("抖了")), \
             patch.object(sched, "_send_webhook") as hook:
            sched.db._s["alert_webhook_url"] = "https://example.com/hook"
            results = await sched.do_active_map_check(notify=True)
        self.assertEqual(results[0]["action"], "lit")
        hook.assert_not_called()

    async def test_no_cell_with_failed_streak_stays_no_cell(self):
        """`no-cell`（上游换了窗口口径）也不能被 streak 失败改判成 query-failed。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today = billing.growth_today()
        with patch.object(billing, "growth_today", return_value=today), \
             patch.object(billing, "fetch_heatmap",
                          return_value={billing.growth_yesterday(): 0}), \
             patch.object(billing, "fetch_streak", side_effect=RuntimeError("抖了")):
            results = await sched.do_active_map_check(notify=False)
        self.assertEqual(results[0]["action"], "no-cell")

    async def test_snapshot_always_has_unknown_key(self):
        """`unknown` 键必须**恒存在**（哪怕是空列表）：概览那边按 `or []` 读，
        但如果键本身缺失，说明有人重构时忘了它 —— 钉住形状，别让静默消失重演。"""
        from unittest.mock import patch
        from workbuddy_one import billing
        sched = self._sched()
        today, patches = self._patch({billing.growth_today(): 0})
        with patch.object(billing, "growth_today", return_value=today), \
             patches[0], patches[1]:
            await sched.do_active_map_check(notify=False)
        snap = sched.active_map_snapshot()
        self.assertIn("unlit", snap)
        self.assertIn("unknown", snap)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _ScriptedClient:
    """按脚本反应的假 httpx 客户端：每次 `get` 依次弹出一个行为。

    行为是 Exception → 抛；是 dict → 当作响应体返回。
    脚本用尽后**重复最后一个行为**（方便表达"一直失败"）。
    """

    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls = 0

    def get(self, url, headers=None):
        self.calls += 1
        idx = min(self.calls, len(self._behaviors)) - 1
        b = self._behaviors[idx]
        if isinstance(b, Exception):
            raise b
        return _FakeResp(b)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeGrowthMgr:
    """`_growth_get` 真正用到的两样东西：`domain` 与 `get_headers()`。"""

    def __init__(self, domain="www.codebuddy.cn"):
        self.domain = domain

    def get_headers(self):
        return {"X-Domain": self.domain}


class TestGrowthGetRetry(unittest.TestCase):
    """`billing._growth_get` 的瞬时失败重试（heatmap / streak 各约 25% 失败率的对策）。

    实测（2026-09-22，容器内直连打 8 次）：本机对国际版上游 `heatmap` 与 `streak`
    **各约 25%** 的请求吃 `ConnectError` / `ConnectTimeout`，国内版 0%，
    而且**两者独立失败** → 修复前"任一失败"约 44% 概率丢掉判据
    （该提醒的不提醒、该补签的不补签，都是**假阴性**）。
    """

    def _run(self, behaviors, retries=1, fast_seconds=60.0):
        """返回 (实际请求次数, 结果, 异常)。

        `fast_seconds` 用来**强制**"快速失败"判定，从而不依赖真实耗时：
        设成很大的数 = 任何失败都算"快"（会重试）；设成负数 = 都不算"快"（不重试）。
        """
        from unittest.mock import patch
        from workbuddy_one import billing, net
        fake = _ScriptedClient(behaviors)
        with patch.object(net, "client", return_value=fake), \
             patch.multiple(billing, _FAST_FAIL_SECONDS=fast_seconds):
            # ⚠️ 必须先调用、再读 `fake.calls`：写成 `return fake.calls, billing._growth_get(...)`
            # 的话，元组按从左到右求值 → `fake.calls` 在调用**之前**就被取走，永远是 0。
            try:
                out = billing._growth_get(_FakeGrowthMgr(), "/x", retries=retries)
                err = None
            except Exception as e:  # noqa: BLE001
                out, err = None, e
            return fake.calls, out, err

    def test_fast_failure_then_success(self):
        """第一次瞬时失败、第二次成功 → 拿到数据，共 2 次请求。"""
        # `_FAST_FAIL_SECONDS` 设大 = 强制判定为"快速失败"（不依赖真实耗时）
        calls, out, err = self._run([ConnectionError("blip"), {"code": 0}],
                                    fast_seconds=60.0)
        self.assertIsNone(err)
        self.assertEqual(out, {"code": 0})
        self.assertEqual(calls, 2)

    def test_fast_failure_twice_raises_after_two_calls(self):
        """两次都快速失败 → 抛出去，且**只打 2 次**（不是无限重试）。"""
        calls, out, err = self._run([ConnectionError("blip")], fast_seconds=60.0)
        self.assertIsInstance(err, ConnectionError)
        self.assertEqual(calls, 2, "重试次数不受控")

    def test_slow_failure_is_not_retried(self):
        """**跑满超时才失败 → 不重试。**

        否则耗时翻倍会顶穿前端 30s 超时（调用方是按"一个账号最坏 20s"
        安排并发与超时的）。慢失败说明网络确实不通，再打一次只是白等。
        """
        calls, out, err = self._run([ConnectionError("slow")], fast_seconds=-1.0)
        self.assertIsInstance(err, ConnectionError)
        self.assertEqual(calls, 1, "慢失败被重试了 → 耗时翻倍会顶穿前端超时")

    def test_retries_zero_keeps_old_behavior(self):
        """`retries=0`（默认）→ 一次都不重试，既有调用方行为不变。"""
        calls, out, err = self._run([ConnectionError("blip")], retries=0,
                                    fast_seconds=60.0)
        self.assertIsInstance(err, ConnectionError)
        self.assertEqual(calls, 1)

    def test_business_error_is_not_retried(self):
        """业务错误（`code != 0`）**不重试**：那是确定性结果，重试只是白打上游。

        `_growth_get` 只负责传输，业务码由调用方判 —— 所以这里应当**一次返回**，
        不抛异常。
        """
        calls, out, err = self._run([{"code": 400, "msg": "无卡"}], fast_seconds=60.0)
        self.assertIsNone(err)
        self.assertEqual(out["code"], 400)
        self.assertEqual(calls, 1, "业务错误被当网络错误重试了")

    def test_heatmap_and_streak_ask_for_one_retry(self):
        """两个判据读接口必须真的传 `retries=1`（改了名字/漏传就静默退回不重试）。"""
        import inspect
        from workbuddy_one import billing
        for fn in (billing.fetch_heatmap, billing.fetch_streak):
            src = inspect.getsource(fn)
            self.assertIn("retries=1", src,
                          f"{fn.__name__} 没要求重试 → 25% 的瞬时失败会直接丢判据")

    # ---- 下面三条用**真实** `_FAST_FAIL_SECONDS`，别再改成假阈值 ----
    #
    # 为什么单独一组：上面所有用例都靠 `fast_seconds=60.0` 把阈值换成假值，
    # 于是**阈值本身从来没有被验证过**。`_FAST_FAIL_SECONDS` 原本取 5.0，
    # 比实测的连接层失败耗时（5.03~5.05s）**只小 0.03s**，恰好落在错误的一侧
    # → 重试分支在生产上**一次都没执行过**，而测试全绿。
    # 所以这一组只伪造**时钟**，阈值用真值。

    def _run_with_elapsed(self, behaviors, elapsed, retries=1):
        """返回 (实际请求次数, 结果, 异常)；把每次尝试的耗时伪造成 `elapsed` 秒。"""
        from unittest.mock import patch
        from workbuddy_one import billing, net
        fake = _ScriptedClient(behaviors)
        clock = [0.0]

        def fake_monotonic():
            # 每次读表都前进 elapsed → `time.monotonic() - started` 恰好等于 elapsed
            v = clock[0]
            clock[0] += elapsed
            return v

        with patch.object(net, "client", return_value=fake), \
             patch.object(billing.time, "monotonic", side_effect=fake_monotonic), \
             patch.object(billing.time, "sleep", return_value=None):
            try:
                out = billing._growth_get(_FakeGrowthMgr(), "/x", retries=retries)
                err = None
            except Exception as e:  # noqa: BLE001
                out, err = None, e
            return fake.calls, out, err

    def test_real_threshold_sits_between_the_two_measured_failure_modes(self):
        """⚠️ 阈值常量本身必须被验证 —— 它卡错边会让重试**静默失效**。

        2026-09-22 容器内实测 20 次单次尝试：4 次失败全是 `ConnectError`，
        耗时 5.03 / 5.04 / 5.05 / 5.05s（极其集中，是个固定的 ~5s 机制）；
        另一种失败是 `ConnectTimeout` 跑满 20.09s。
        → 阈值必须**大于**前者（否则那些失败一个都不重试）、
          **小于**后者（否则跑满超时的失败也会重试，耗时翻倍顶穿前端超时）。
        """
        from workbuddy_one import billing
        self.assertGreater(billing._FAST_FAIL_SECONDS, 6.0,
                           "阈值低于实测的连接层失败耗时(5.05s) → 重试永不触发")
        self.assertLess(billing._FAST_FAIL_SECONDS, 20.0,
                        "阈值不低于整体超时(20s) → 跑满超时也重试，耗时翻倍顶穿前端超时")

    def test_measured_connect_failure_duration_is_retried(self):
        """回归：**实测那 5.05s 的连接层失败必须触发重试**。

        这正是这条增量的起因 —— 阈值 5.0 时它被判成「慢失败」，
        重试一次都没发出去（实测：调用 10 次、真实请求也正好 10 次）。
        """
        calls, out, err = self._run_with_elapsed(
            [ConnectionError("blip"), {"code": 0}], elapsed=5.05)
        self.assertIsNone(err)
        self.assertEqual(out, {"code": 0})
        self.assertEqual(calls, 2, "5.05s 的连接层失败没被重试 → 阈值卡错了边")

    def test_timeout_at_full_20s_is_not_retried(self):
        """跑满整体超时（实测 20.09s 的 `ConnectTimeout`）→ **不重试**。"""
        calls, out, err = self._run_with_elapsed([ConnectionError("timeout")],
                                                 elapsed=20.0)
        self.assertIsInstance(err, ConnectionError)
        self.assertEqual(calls, 1, "跑满超时还重试 → 耗时翻倍会顶穿前端超时")


class TestVersionSingleSource(unittest.TestCase):
    """版本号**只有一个真源**，且"旧数据是否已升级"对用户可见。

    背景（2026-09-22）：改动前版本号有 5 处独立副本 ——
    `workbuddy_one/__init__.py`(0.4.1)、`app.py` 里写死的 FastAPI version(0.4.1)、
    `AppSidebar.vue` 里写死的 `v0.4.1`、`pyproject.toml`(0.4.1)、
    `frontend/package.json`(**0.4.0**)。**已经漂了**：package.json 是 0.4.0
    而后端是 0.4.1，而没有任何东西会报错。
    前端那处更糟 —— 它决定用户看到什么，忘了同步就永远显示上一个版本。

    现在：真源 = `workbuddy_one/__init__.py`；`pyproject.toml` 用 hatchling
    动态读它；`app.py` 引用变量；前端从 `/health` **读接口**。
    """

    def _root(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent

    def test_frontend_package_json_matches_backend_version(self):
        """前端 package.json 的版本必须与后端一致（它俩已经漂过一次）。"""
        import json
        from workbuddy_one import __version__
        pkg = json.loads((self._root() / "frontend" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["version"], __version__,
                         "package.json 与后端版本不一致 —— 说明又出现了第二份副本")

    def test_pyproject_reads_version_from_the_single_source(self):
        """pyproject 必须**动态**取版本，且指向 `workbuddy_one/__init__.py`。"""
        txt = (self._root() / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', txt,
                      "pyproject 又把版本写死了 → 会出现第二份副本")
        self.assertIn('path = "workbuddy_one/__init__.py"', txt,
                      "动态版本的来源没指向唯一真源")
        self.assertNotIn('version = "0.', txt, "pyproject 里不该再有写死的版本号")

    def test_init_version_line_matches_hatchling_pattern(self):
        """`__version__ = "x"` 这一行必须能被 **hatchling 的正则**匹配到。

        为什么需要这条：本地 venv 里**没有 hatchling**（项目不本地打包），
        所以"动态版本能不能解析出来"平时完全没被验证过 —— 一旦有人把这行改成
        `__version__: str = "0.5.0"` 或挪进函数里，打包时版本号会直接取不到，
        而这里不会有任何报错。用它的正则在这里兜住。
        """
        import re
        from workbuddy_one import __version__
        src = (self._root() / "workbuddy_one" / "__init__.py").read_text(encoding="utf-8")
        # 与 hatchling `version.source.code.CodeSource.DEFAULT_PATTERN` 同构
        m = re.search(r'(?im)^(__version__|VERSION) *= *([\'"])v?(?P<version>.+?)\2', src)
        self.assertIsNotNone(
            m, "hatchling 的动态版本正则匹配不到这一行 → 打包时版本号会取不到")
        self.assertEqual(m.group("version"), __version__)

    def test_app_and_sidebar_do_not_hardcode_the_version(self):
        """`app.py` 与侧边栏都不能再写死版本号。

        前端那处是用户**直接看到**的：写死就会一直显示旧版本，且不会有任何报错。
        """
        import re
        root = self._root()
        app_src = (root / "workbuddy_one" / "app.py").read_text(encoding="utf-8")
        self.assertIn("version=__version__", app_src,
                      "app.py 没引用唯一真源 __version__")
        self.assertNotRegex(app_src, r'version\s*=\s*"0\.',
                            "app.py 里还写死了版本号")

        side_src = (root / "frontend" / "src" / "components" / "AppSidebar.vue").read_text(encoding="utf-8")
        self.assertNotRegex(side_src, r"v0\.\d",
                            "侧边栏还硬编码着版本号 → 用户会看到过期版本")
        self.assertIn("/health", side_src,
                      "侧边栏没从 /health 读版本")

    def test_health_exposes_version_schema_and_migration(self):
        """`/health` 必须带版本 / schema 版本 / 本次是否迁移过。

        为什么必须是**行为**用例（而不是源码扫描）：前端就靠这几个字段显示，
        字段名拼错 = 界面永远显示 `…`，而不会有任何报错。
        """
        import types
        from workbuddy_one import __version__
        from workbuddy_one.db import SCHEMA_VERSION
        from workbuddy_one.routes import webui

        routes: dict = {}

        class _App:
            def get(self, path):
                def deco(fn):
                    routes[path] = fn
                    return fn
                return deco

        ctx = types.SimpleNamespace(
            db=types.SimpleNamespace(migrated_from=6),
            auth_files_count=2,
            managers={"u1": object()},
        )
        webui.register(_App(), ctx)
        data = routes["/health"]()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["migrated_from"], 6,
                         "发生了迁移却没报出来 → 用户无法确认旧数据已升级")

        # 老 ctx / 非迁移启动：字段必须在但为 None（前端按 null 判定，不能缺字段）
        ctx2 = types.SimpleNamespace(db=types.SimpleNamespace(),
                                     auth_files_count=0, managers={})
        webui.register(_App(), ctx2)
        self.assertIsNone(routes["/health"]()["migrated_from"])

    def test_dockerfile_dependency_list_matches_pyproject(self):
        """Dockerfile 里显式列的依赖必须与 pyproject 的 `dependencies` 一致。

        为什么需要：容器里的 `pip install` 读不到 pyproject（装的是显式列出的包），
        所以依赖清单**存在两份**。只改一处 → 本地 venv 能跑而容器起不来（或反之），
        而且不会有任何东西报错 —— 又是"多处副本必然漂移"那一类。

        顺带钉住"构建必须可指定镜像源"：直连 pypi.org 在国内网络下会间歇性吃
        TLS 握手中断（`SSLEOFError ... UNEXPECTED_EOF_WHILE_READING`），
        表现为 `No matching distribution found`（看着像包不存在，其实是网络）。
        """
        import re
        import tomllib
        root = self._root()
        deps = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        df = (root / "Dockerfile").read_text(encoding="utf-8")

        m = re.search(r"RUN pip install(.*?)(?=\n[A-Z]|\Z)", df, re.S)
        self.assertIsNotNone(m, "Dockerfile 里找不到 pip install 步骤")
        block = m.group(1)
        # 排除 `"$PIP_INDEX_URL"` 这类变量引用，只留真正的包规格
        listed = [s for s in re.findall(r'"([^"]+)"', block) if not s.startswith("$")]
        self.assertEqual(sorted(d.strip() for d in listed),
                         sorted(d.strip() for d in deps),
                         "Dockerfile 的依赖清单与 pyproject 漂了 —— 两处必须一致")
        self.assertIn("PIP_INDEX_URL", df,
                      "构建没提供可覆盖的镜像源 → 国内网络下会间歇性装不上依赖")

    def test_database_records_which_version_it_migrated_from(self):
        """`Database.migrated_from`：旧库有值、新库为 None。

        它是"本次启动到底有没有真的迁移过"的唯一判据。写反了（比如恒为 None）
        会让界面永远显示"无需迁移"，用户以为数据没升级。
        """
        import sqlite3
        from workbuddy_one.db import SCHEMA_VERSION

        # 全新库 → 不算迁移（首次建表不是升级）
        fresh = _open_db(self, "version_fresh.db")
        self.assertIsNone(fresh.migrated_from,
                          "全新库被当成迁移 → 界面会误报「数据已升级」")

        # 旧库（v5）→ 必须记下来源版本
        old = str(_TMP / "version_old.db")
        for f in list(_TMP.glob("version_old.db*")) + list((_TMP / "backups").glob("version_old*")):
            f.unlink(missing_ok=True)
        c = sqlite3.connect(old)
        c.executescript("""
            CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE,
                nickname TEXT, enabled INTEGER DEFAULT 1, created_at REAL, updated_at REAL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        c.execute("PRAGMA user_version = 5")
        c.commit(); c.close()

        db = _open_db(self, "version_old.db")
        self.assertEqual(db._user_version(), SCHEMA_VERSION)
        self.assertEqual(db.migrated_from, 5,
                         "旧库升级后没记下来源版本 → 用户看不到「数据已升级」")


class TestStartupWarmupBudget(unittest.IsolatedAsyncioTestCase):
    """启动预热必须有时间上限（`CODE_REVIEW_TODO.md` #33）。

    背景：uvicorn 在 lifespan 的 startup 阶段**不监听端口**，所以"预热慢"会直接
    变成"整个 WebUI 打不开"——不是降级，是彻底不可用，容器还会被判 unhealthy。
    2026-09-22 重建容器时实测被 AA 评测的 SSL 失败拖了 **105 秒**。
    预热是"页面首开即有数据"的优化，不该是可用性前提。
    """

    async def _start_with(self, warmup, budget=None):
        """跑一次 start()，返回 (耗时秒, 告警文本列表, scheduler)。

        用**自定义 handler** 而不是 `assertLogs`：这几条用例里有的会告警、有的不该告警，
        `assertLogs` 在没有日志时会直接失败，套不上。
        """
        import logging
        from unittest import mock
        from workbuddy_one import scheduler as S
        from workbuddy_one.pool import AccountPool

        pool = AccountPool({})
        pool.add_account("u1", None)
        sched = S.Scheduler(pool, db=None)
        # `_run` 的后台循环会真的去打上游；本用例只关心 start() 有没有被预热拖住
        sched._run = mock.AsyncMock()
        sched._warmup = warmup

        warnings: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda r: warnings.append(r.getMessage())
        log = logging.getLogger("workbuddy_one.scheduler")
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)

        patches = []
        if budget is not None:
            patches.append(mock.patch.object(S, "_WARMUP_BUDGET_SECONDS", budget))
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        # 交给清理阶段停：超时那条用例要在**测试体内**等后台预热跑完，
        # 提前 stop() 会把预热取消掉，用例就假失败了。
        self.addAsyncCleanup(sched.stop)
        t0 = time.monotonic()
        await sched.start()
        return time.monotonic() - t0, warnings, sched

    async def test_hanging_warmup_does_not_block_startup(self):
        """上游卡住时 start() 必须按时返回，而不是一直等下去。"""
        import asyncio

        async def hang():
            await asyncio.Event().wait()   # 永不返回：模拟上游挂住

        elapsed, warnings, _ = await self._start_with(hang, budget=0.2)
        self.assertLess(elapsed, 2.0, "预热超时后 start() 仍被拖住")
        self.assertTrue(any("启动预热超过" in m for m in warnings),
                        f"超时没有告警，用户不会知道预热没跑完：{warnings}")

    async def test_timed_out_warmup_keeps_running_in_background(self):
        """超时只放弃「等待」——预热本身必须继续跑完。

        这条用例专门钉 `asyncio.shield`：`asyncio.wait_for` 默认会**取消**被等的协程，
        少了 shield，日志里那句"预热在后台继续"就是谎话（说了、没做）。
        """
        import asyncio

        done = asyncio.Event()

        async def slow():
            await asyncio.sleep(0.35)
            done.set()

        await self._start_with(slow, budget=0.1)
        # 不超时本身就是断言；超时会抛 TimeoutError 让用例红。
        await asyncio.wait_for(done.wait(), timeout=3.0)

    async def test_real_budget_lets_a_normal_warmup_finish(self):
        """用**真值**预算跑：正常的（快的）预热必须被 await 完，不能提前放行。

        防的是"把常量改坏"：预算若被写成 0 或负数，上面两条用例照样全绿，
        但"页面首开即有数据"这个既有语义会**静默失效**。
        """
        ran = []

        async def quick():
            ran.append(True)

        await self._start_with(quick)          # 不传 budget → 用模块里的真值
        self.assertEqual(ran, [True],
                         "快预热没被 await 完 → 首开页面会没有数据（既有语义被破坏）")

    def test_budget_is_a_sane_positive_bound(self):
        """预算本身要合理：必须 > 0，也不能大到"打不开"持续到分钟级。"""
        from workbuddy_one import scheduler as S
        self.assertGreater(S._WARMUP_BUDGET_SECONDS, 0)
        self.assertLessEqual(S._WARMUP_BUDGET_SECONDS, 60.0,
                             "预算过大 → 上游异常时 WebUI 仍会长时间打不开")


if __name__ == "__main__":
    unittest.main()
