"""新增功能单元测试：模型目录 / 调度 / 设置校验 / 账号删除 / DB 路径解析。

运行：python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestConfigDbPath(unittest.TestCase):
    def test_relative_db_path_anchored_to_package_root(self):
        # 无论 cwd 在哪，默认 db_path 都应落在包根目录（Workbuddy2API/data）
        from workbuddy_one import config as cfgmod

        # 直接调用解析函数
        p = cfgmod._resolve_db_path("data/x.db")
        self.assertTrue(Path(p).is_absolute())
        self.assertTrue(Path(p).parent.name == "data")
        # 相对路径应锚定到包根目录，而非 cwd
        self.assertTrue(str(p).replace("\\", "/").startswith(
            str(cfgmod.PACKAGE_ROOT).replace("\\", "/")))

    def test_absolute_db_path_preserved(self):
        from workbuddy_one import config as cfgmod
        p = cfgmod._resolve_db_path("C:/tmp/custom.db")
        self.assertEqual(Path(p), Path("C:/tmp/custom.db"))

    def test_project_auths_dir_anchored_to_package_root(self):
        # auths/ 应锚定到包根目录（与上传/扫码落盘位置一致），而非进程 cwd
        from workbuddy_one.credentials import PROJECT_AUTHS_DIR
        self.assertEqual(PROJECT_AUTHS_DIR.parent.name, "Workbuddy2API")
        self.assertTrue(PROJECT_AUTHS_DIR.name == "auths")
        # auth_dirs() 扫描列表应包含它
        from workbuddy_one.credentials import auth_dirs
        self.assertIn(PROJECT_AUTHS_DIR, auth_dirs())


class TestModelRegistry(unittest.TestCase):
    def _registry(self):
        from workbuddy_one.models import ModelRegistry
        return ModelRegistry(pool=None)

    def test_no_static_fallback_empty_without_cache(self):
        # 静态兜底已移除：无缓存时 fallback/entries 应为空列表（而非过时静态清单）
        r = self._registry()
        self.assertEqual(r._fallback(), [])
        self.assertEqual(r._static_entries(), [])
        self.assertEqual(r.list_cached(), [])

    def test_fallback_keeps_last_cache(self):
        # 有缓存时 refresh 失败应保留最后一份成功数据
        r = self._registry()
        cached = [{"id": "glm-5.3", "object": "model", "created": 1, "owned_by": "x",
                   "name": "GLM-5.3", "context_length": 1000, "max_output_tokens": 100}]
        r._models = cached
        self.assertEqual(len(r._fallback()), 1)
        self.assertEqual(r._fallback()[0]["id"], "glm-5.3")

    def test_list_cached_never_fetches(self):
        # list_cached 只读快照：pool=None（无法拉取）时也不抛错，返回缓存
        r = self._registry()
        r._models = [{"id": "a", "object": "model", "created": 1, "owned_by": "x",
                      "name": "a", "context_length": 0, "max_output_tokens": 0}]
        out = r.list_cached()
        self.assertEqual(len(out), 1)
        self.assertIn("vision", out[0])  # 标准字段已注入

    def test_ttl_reads_from_db_setting(self):
        # TTL 可从 DB settings（model_ttl_min）动态读取
        from workbuddy_one.models import ModelRegistry, DEFAULT_TTL
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "ttl.db"))
        try:
            r = ModelRegistry(pool=None, db=db)
            # 默认：未设置时用 DEFAULT_TTL
            self.assertEqual(r._ttl(), DEFAULT_TTL)
            # 设置 120 分钟 → TTL = 7200 秒
            db.save_settings(model_ttl_min="120")
            self.assertEqual(r._ttl(), 7200)
            # 非法值（0/超界/非数字）→ 回退默认
            db.save_settings(model_ttl_min="0")
            self.assertEqual(r._ttl(), DEFAULT_TTL)
            db.save_settings(model_ttl_min="9999")
            self.assertEqual(r._ttl(), DEFAULT_TTL)
            db.save_settings(model_ttl_min="abc")
            self.assertEqual(r._ttl(), DEFAULT_TTL)
        finally:
            db._conn.close()
            for f in tmp.glob("ttl.db*"):
                f.unlink(missing_ok=True)


class TestReasoningConfig(unittest.TestCase):
    def test_extract_reasoning_dynamic(self):
        from workbuddy_one.models import _extract_reasoning
        m = {
            "id": "glm-5.3", "supportsReasoning": True, "onlyReasoning": False,
            "reasoning": {"canDisableThinking": True, "defaultEffort": "high",
                          "supportedEfforts": ["low", "high", "xhigh"]},
        }
        cfg = _extract_reasoning(m)
        self.assertTrue(cfg["supportsReasoning"])
        self.assertTrue(cfg["canDisableThinking"])
        self.assertEqual(cfg["supportedEfforts"], ["low", "high", "xhigh"])
        self.assertEqual(cfg["defaultEffort"], "high")

    def test_extract_reasoning_minimal(self):
        from workbuddy_one.models import _extract_reasoning
        # hy3 上游只给了 supportsReasoning/onlyReasoning，无 reasoning 详情
        cfg = _extract_reasoning({"id": "hy3", "supportsReasoning": True, "onlyReasoning": True})
        self.assertTrue(cfg["supportsReasoning"])
        self.assertTrue(cfg["onlyReasoning"])
        self.assertNotIn("canDisableThinking", cfg)
        self.assertNotIn("supportedEfforts", cfg)

    def test_reasoning_efforts_includes_off_when_disablable(self):
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=None)
        r._reasoning = {
            "glm-5.3": {"canDisableThinking": True, "supportedEfforts": ["low", "high", "xhigh"]},
            "hy4-preview": {"canDisableThinking": False, "supportedEfforts": ["high"]},
            "hy3": {"supportsReasoning": True, "onlyReasoning": True},
        }
        self.assertIn("off", r.reasoning_efforts("glm-5.3"))
        self.assertNotIn("off", r.reasoning_efforts("hy4-preview"))
        self.assertEqual(r.reasoning_efforts("hy4-preview"), ["high"])
        # 无动态配置 -> None（回退静态表）
        self.assertIsNone(r.reasoning_efforts("hy3"))
        self.assertIsNone(r.reasoning_efforts("unknown"))

    def test_normalize_uses_dynamic_efforts(self):
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"hy4-preview": ["high"], "glm-5.3": ["low", "high", "xhigh", "off"]}
        # 只支持 high，请求 low 降级为 high
        b = normalize_reasoning_effort({"model": "hy4-preview", "reasoning_effort": "low"}, dyn)
        self.assertEqual(b["reasoning_effort"], "high")
        # 可关闭思考：请求 off 保持 off
        b = normalize_reasoning_effort({"model": "glm-5.3", "reasoning_effort": "off"}, dyn)
        self.assertEqual(b["reasoning_effort"], "off")
        # 不支持 off：请求 off 取最低支持档
        b = normalize_reasoning_effort({"model": "hy4-preview", "reasoning_effort": "off"}, dyn)
        self.assertEqual(b["reasoning_effort"], "high")


class TestSchedulerParsing(unittest.TestCase):
    def test_parse_hours(self):
        from workbuddy_one.scheduler import _parse_hours, _parse_hour
        self.assertEqual(_parse_hours("9,21"), (9, 21))
        self.assertEqual(_parse_hours("21,9"), (9, 21))  # 排序去重
        self.assertEqual(_parse_hours("bad"), (9, 21))   # 非法回退默认
        self.assertEqual(_parse_hour("6", 22), 6)
        self.assertEqual(_parse_hour("99", 22), 22)      # 越界回退
        self.assertEqual(_parse_hour("abc", 22), 22)

    def test_checkin_skip_logic(self):
        from workbuddy_one.scheduler import Scheduler

        class FakeDB:
            def __init__(self):
                self.dates = {}
            def checkin_dates(self):
                return self.dates
            def set_checkin_date(self, uid, date):
                self.dates[uid] = date
            def get_settings(self):
                return {}

        class FakeAcc:
            def __init__(self, uid):
                self.uid = uid

        pool = type("P", (), {"accounts": [FakeAcc("a"), FakeAcc("b")]})()
        sched = Scheduler(pool, db=FakeDB())
        sched.checked_in_today()
        # 手动标 b 已签到
        sched.db.dates = {"b": "2099-01-01"}
        # checked_in_today 只返回"今天"签到的；这里用一个非今日日期应返回空
        self.assertEqual(sched.checked_in_today(), set())


class TestSettingsValidation(unittest.TestCase):
    def test_settings_roundtrip_and_whitelist(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "sett.db"))
        try:
            # 默认值
            s = db.get_settings()
            self.assertEqual(s["model_refresh_hour"], "6")
            self.assertEqual(s["keepalive_hour"], "22")
            self.assertEqual(s["checkin_hours"], "9,21")
            # 保存并回读
            db.save_settings(model_refresh_hour="8", keepalive_hour="23", checkin_hours="7,19")
            s2 = db.get_settings()
            self.assertEqual(s2["model_refresh_hour"], "8")
            self.assertEqual(s2["keepalive_hour"], "23")
            self.assertEqual(s2["checkin_hours"], "7,19")
            # 白名单外 key 不生效
            db.save_settings(unknown_key="x")
            self.assertNotIn("unknown_key", db.get_settings())
        finally:
            db._conn.close()
            for f in tmp.glob("sett.db*"):
                f.unlink(missing_ok=True)


class TestAccountDelete(unittest.TestCase):
    def test_delete_account(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "del.db"))
        try:
            db.upsert_account({"path": "/x"}, {"uid": "u1", "nick": "acc"})
            # 存在时删除成功
            self.assertTrue(db.delete_account("u1"))
            # 再次删除返回 False（已不存在）
            self.assertFalse(db.delete_account("u1"))
        finally:
            db._conn.close()
            for f in tmp.glob("del.db*"):
                f.unlink(missing_ok=True)


class TestModelCaps(unittest.TestCase):
    def test_modality_multimodal(self):
        from workbuddy_one.models import _extract_caps
        # 支持图像且未禁用 → 多模态
        caps = _extract_caps({"supportsImages": True, "disabledMultimodal": False, "supportsToolCall": True})
        self.assertEqual(caps["modality"], "multimodal")
        self.assertTrue(caps["supportsToolCall"])

    def test_modality_text(self):
        from workbuddy_one.models import _extract_caps
        # 不支持图像 → 纯文本
        caps = _extract_caps({"supportsImages": False, "supportsToolCall": True})
        self.assertEqual(caps["modality"], "text")

    def test_credits_parsing(self):
        from workbuddy_one.models import _extract_caps
        self.assertEqual(_extract_caps({"credits": "x0.79 credits"})["credits"], 0.79)
        self.assertEqual(_extract_caps({"credits": "x0.00"})["credits"], 0.0)
        self.assertIsNone(_extract_caps({})["credits"])


class TestAABenchmarks(unittest.TestCase):
    def test_key_prefers_db(self):
        from workbuddy_one.benchmarks import AABenchmarks
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "aa.db"))
        try:
            bb = AABenchmarks(db=db)
            self.assertFalse(bb.configured())
            db.save_settings(aa_api_key="db_key_123")
            self.assertTrue(bb.configured())
            self.assertEqual(bb.key(), "db_key_123")
        finally:
            db._conn.close()
            for f in tmp.glob("aa.db*"):
                f.unlink(missing_ok=True)

    def test_lookup_by_keyword(self):
        from workbuddy_one.benchmarks import AABenchmarks
        bb = AABenchmarks(db=None)
        rows = [
            {"id": "uuid1", "slug": "glm-5.3", "name": "GLM-5.3", "evaluations": {"artificial_analysis_intelligence_index": 70.5}},
            {"id": "uuid2", "slug": "deepseek-v4", "name": "DeepSeek V4", "evaluations": {"artificial_analysis_intelligence_index": 68.0}},
        ]
        # 用桩替换内部数据，验证匹配逻辑
        with bb._lock:
            bb._rows = rows
            bb._fetched_at = __import__("time").time()
            bb._last_fail = 0.0
        self.assertEqual(bb.lookup("glm-5.3")["name"], "GLM-5.3")
        self.assertEqual(bb.lookup("deepseek-v4-pro")["name"], "DeepSeek V4")
        self.assertIsNone(bb.lookup("unknown-model-xyz"))

    def test_map_slim(self):
        from workbuddy_one.benchmarks import AABenchmarks
        import time
        bb = AABenchmarks(db=None)
        rows = [{
            "id": "uuid", "slug": "glm-5.3", "name": "GLM-5.3",
            "model_creator": {"name": "Zhipu"},
            "evaluations": {
                "artificial_analysis_intelligence_index": 70.5,
                "artificial_analysis_coding_index": 66.0,
                "artificial_analysis_math_index": 61.2,
                "mmlu_pro": 0.8,  # 非选定字段，不应返回
            },
            "pricing": {"price_1m_input_tokens": 0.5, "price_1m_output_tokens": 1.5},
            "median_output_tokens_per_second": 120.0,
        }]
        with bb._lock:
            bb._rows = rows
            bb._fetched_at = time.time()
            bb._last_fail = 0.0
        m = bb.map("glm-5.3")
        self.assertIsNotNone(m)
        self.assertEqual(m["intelligence_index"], 70.5)
        self.assertEqual(m["coding_index"], 66.0)
        self.assertEqual(m["math_index"], 61.2)
        self.assertEqual(m["source"], "aa")
        # 只保留选定的三个指标，不再返回非指标字段
        self.assertNotIn("mmlu_pro", m)
        self.assertNotIn("agentic_index", m)
        self.assertNotIn("price_in", m)
        self.assertNotIn("speed_tps", m)

    def test_has_cache(self):
        from workbuddy_one.benchmarks import AABenchmarks
        bb = AABenchmarks(db=None)
        self.assertFalse(bb.has_cache())
        with bb._lock:
            bb._rows = [{"id": "x"}]
        self.assertTrue(bb.has_cache())


class TestUsageContent(unittest.TestCase):
    def test_log_usage_content_roundtrip(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "usage.db"))
        try:
            db.log_usage(
                model="hy3", protocol="chat", account_uid="u1",
                input_tokens=10, output_tokens=20, status="ok",
                input_content="user: 你好",
                output_content="你好，世界",
                reasoning_content="思考过程：先打招呼",
            )
            rec = db.usage_recent(1)[0]
            self.assertEqual(rec["model"], "hy3")
            self.assertEqual(rec["input_tokens"], 10)
            self.assertEqual(rec["output_tokens"], 20)
            self.assertEqual(rec["total_tokens"], 30)
            self.assertEqual(rec["input_content"], "user: 你好")
            self.assertEqual(rec["output_content"], "你好，世界")
            self.assertEqual(rec["reasoning_content"], "思考过程：先打招呼")
        finally:
            db._conn.close()
            for f in tmp.glob("usage.db*"):
                f.unlink(missing_ok=True)

    def test_log_usage_empty_content_defaults(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "usage2.db"))
        try:
            db.log_usage(model="m", protocol="chat", account_uid="u2")
            rec = db.usage_recent(1)[0]
            self.assertEqual(rec["input_content"], "")
            self.assertEqual(rec["output_content"], "")
            self.assertEqual(rec["reasoning_content"], "")
        finally:
            db._conn.close()
            for f in tmp.glob("usage2.db*"):
                f.unlink(missing_ok=True)

    def test_usage_content_columns_exist_in_schema(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "usage3.db"))
        try:
            cols = [r[1] for r in db._conn.execute("PRAGMA table_info(usage_logs)").fetchall()]
            for col in ("input_content", "output_content", "reasoning_content", "credits"):
                self.assertIn(col, cols)
        finally:
            db._conn.close()
            for f in tmp.glob("usage3.db*"):
                f.unlink(missing_ok=True)

    def test_log_usage_credits(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "usage4.db"))
        try:
            db.log_usage(model="m", protocol="chat", account_uid="u3",
                         input_tokens=10, output_tokens=20, credits=1.25)
            rec = db.usage_recent(1)[0]
            self.assertAlmostEqual(rec["credits"], 1.25)
            # 默认 credits 为 0
            db.log_usage(model="m2", protocol="chat", account_uid="u3")
            rec2 = db.usage_recent(1)[0]
            self.assertEqual(rec2["credits"], 0.0)
        finally:
            db._conn.close()
            for f in tmp.glob("usage4.db*"):
                f.unlink(missing_ok=True)

    def test_usage_credit_stats_per_model(self):
        from workbuddy_one.db import Database
        tmp = Path(__file__).resolve().parent / "_tmp"
        tmp.mkdir(exist_ok=True)
        db = Database(str(tmp / "usage5.db"))
        try:
            # 两个模型各有积分消耗；另加一条 credits=0 的记录（应被过滤）
            db.log_usage(model="a", protocol="chat", account_uid="u", credits=0.5, input_tokens=600, output_tokens=400)
            db.log_usage(model="a", protocol="chat", account_uid="u", credits=0.5, input_tokens=300, output_tokens=200)
            db.log_usage(model="b", protocol="chat", account_uid="u", credits=0.1, input_tokens=30, output_tokens=20)
            db.log_usage(model="c", protocol="chat", account_uid="u", credits=0, input_tokens=9000, output_tokens=999)
            stats = db.usage_credit_stats()
            self.assertAlmostEqual(stats["total_credits"], 1.1)
            self.assertAlmostEqual(stats["total_tokens"], 1550)
            by = {m["model"]: m for m in stats["models"]}
            self.assertIn("a", by)
            self.assertIn("b", by)
            self.assertNotIn("c", by)  # credits=0 的记录不计入
            self.assertAlmostEqual(by["a"]["credits"], 1.0)
            self.assertAlmostEqual(by["a"]["tokens"], 1500)
            self.assertAlmostEqual(by["b"]["tokens"], 50)
        finally:
            db._conn.close()
            for f in tmp.glob("usage5.db*"):
                f.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
