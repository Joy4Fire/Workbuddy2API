"""新增功能单元测试：模型目录 / 调度 / 设置校验 / 账号删除 / DB 路径解析。

运行：python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试库统一落在**系统临时目录**（原因同 test_core：仓库可能位于网络盘，
# 网络盘没有回收站，删除会退化成失败的 SHFileOperationW，单次 10~36 秒）。
_TMP = Path(tempfile.mkdtemp(prefix="wb_sched_"))


def setUpModule():
    """清掉上一轮残留的测试库（原因同 test_core.setUpModule：固定文件名 + 共享
    _TMP，残留会让后续整轮运行读到上次的数据而失败）。"""
    if _TMP.is_dir():
        for f in list(_TMP.glob("*.db*")):
            f.unlink(missing_ok=True)


def tearDownModule():
    shutil.rmtree(_TMP, True)


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
        tmp = _TMP
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

    # ---- off 是开关、不是最低档（2026-09-21 修的真实 bug）----

    def test_off_is_not_a_fallback_level(self):
        """**回归测试**：模型支持 off 时，客户端要低档不能被降级成 off。

        原实现只有一趟"在 ≤请求档 里选最高档"的循环，而 off 的 rank 是 0，
        **永远**满足 `idx <= req_idx` → 只要客户端要的档位低于模型的最低思考档，
        选出来就是 off：思考被整个关掉。客户端明确要了"思考"，这比不降级更糟。

        实测暴露路径：合并修复让 glm-5.2 拿回真实档位 [high,xhigh]（+off），
        客户端要 low/medium 全变成 off。
        """
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"glm-5.2": ["high", "xhigh", "off"]}
        for req in ("minimal", "low", "medium"):
            b = normalize_reasoning_effort({"model": "glm-5.2", "reasoning_effort": req}, dyn)
            self.assertEqual(b["reasoning_effort"], "high",
                             f"请求 {req} 应抬到最低思考档 high，不能变成 off")

    def test_off_is_not_a_fallback_level_when_list_is_wide(self):
        """档位表更宽时同样成立：minimal 要落到 low，不是 off。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max", "off"]}
        b = normalize_reasoning_effort({"model": "gpt-5.6-sol", "reasoning_effort": "minimal"}, dyn)
        self.assertEqual(b["reasoning_effort"], "low")

    def test_explicit_off_is_honored(self):
        """客户端**明确**要不思考时，支持 off 就给 off（含 none 同义）。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"glm-5.2": ["high", "xhigh", "off"]}
        for req in ("off", "none"):
            b = normalize_reasoning_effort({"model": "glm-5.2", "reasoning_effort": req}, dyn)
            self.assertEqual(b["reasoning_effort"], "off")

    def test_off_lifted_when_model_cannot_disable_thinking(self):
        """模型没有 off 档（onlyReasoning）：客户端要 off 必须抬到最低思考档。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"deepseek-v4-pro": ["low", "medium", "high"]}
        b = normalize_reasoning_effort({"model": "deepseek-v4-pro", "reasoning_effort": "off"}, dyn)
        self.assertEqual(b["reasoning_effort"], "low")

    def test_model_supporting_only_off(self):
        """退化情形：模型只支持 off，任何请求都只能 off（不能凭空造思考档）。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"m": ["off"]}
        for req in ("minimal", "high", "max", "off"):
            b = normalize_reasoning_effort({"model": "m", "reasoning_effort": req}, dyn)
            self.assertEqual(b["reasoning_effort"], "off")

    def test_levels_are_never_invented(self):
        """降级只能落到 supported 里的值，不许造出表外的档位。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        dyn = {"m": ["high", "xhigh", "off"]}
        for req in ("minimal", "low", "medium", "high", "xhigh", "max", "off", "none"):
            b = normalize_reasoning_effort({"model": "m", "reasoning_effort": req}, dyn)
            self.assertIn(b["reasoning_effort"], dyn["m"], f"请求 {req} 落到了表外")

    # ---- reasoning.effort：上游给了、我们以前直接丢掉的字段 ----

    def test_extract_reasoning_captures_effort(self):
        """国内版用 `reasoning.effort` 代替 `defaultEffort`，必须记下来。

        2026-09-21 实测：两区域共 18 个模型带这个字段，而我们的解析层只读
        canDisableThinking / defaultEffort / supportedEfforts —— effort 被静默丢弃。
        """
        from workbuddy_one.models import _extract_reasoning
        cfg = _extract_reasoning({"id": "auto", "supportsReasoning": True,
                                  "onlyReasoning": True,
                                  "reasoning": {"effort": "high", "summary": "auto"}})
        self.assertEqual(cfg["effort"], "high")
        self.assertTrue(cfg["onlyReasoning"])

    def test_effort_is_not_treated_as_a_supported_list(self):
        """**关键**：effort 是"默认档"，不是"唯一支持的档"。

        实测：上游给 auto 报 effort=high，但给它发 reasoning_effort=low 照样 200；
        glm-5.2 自称只支持 [high,xhigh]，发 low 也 200。所以把它当 supportedEfforts
        用会把客户端的合法选择无谓改写掉。→ reasoning_efforts() 必须返回 None。
        """
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=None)
        r._reasoning = {"auto": {"supportsReasoning": True, "onlyReasoning": True,
                                 "effort": "high"}}
        self.assertIsNone(r.reasoning_efforts("auto"))

    def test_default_effort_wins_when_both_present(self):
        """两个字段同时出现时只认 defaultEffort，避免同一概念两个来源打架。"""
        from workbuddy_one.models import _extract_reasoning
        cfg = _extract_reasoning({"id": "m",
                                  "reasoning": {"effort": "low", "defaultEffort": "high"}})
        self.assertEqual(cfg["defaultEffort"], "high")
        self.assertNotIn("effort", cfg)

    def test_reasoning_rank(self):
        from workbuddy_one.models import _reasoning_rank
        self.assertEqual(_reasoning_rank({"reasoning": {"supportedEfforts": ["high"]}}), 3)
        self.assertEqual(_reasoning_rank({"reasoning": {"defaultEffort": "high"}}), 2)
        self.assertEqual(_reasoning_rank({"reasoning": {"effort": "high"}}), 2)
        self.assertEqual(_reasoning_rank({"reasoning": {"onlyReasoning": True}}), 1)
        self.assertEqual(_reasoning_rank({}), 1)


class _RegionPool:
    """只实现 `_fetch_from_upstream` 用到的两个入口。"""

    class _Acc:
        def __init__(self, rid):
            self.uid = rid
            self.region_id = rid

    def __init__(self, rids):
        self.accounts = [self._Acc(r) for r in rids]

    def pick(self, regions=None):
        for a in self.accounts:
            if regions is None or a.region_id in regions:
                return a
        return None


class TestRegionMergePrefersRicherReasoning(unittest.TestCase):
    """跨区域合并：信息更全的 reasoning 元数据不能被信息更少的覆盖。

    原来的 `merged.setdefault` 配合 `sorted(by_region)`（"cn" < "global"）
    等于**国内版永远赢**。实测这会丢数据：glm-5.2 国际版给
    supportedEfforts=[high,xhigh]，国内版只有 effort=medium → 丢掉档位后
    reasoning_efforts() 返回 None，一路回落到 reasoning.KNOWN_EFFORTS 的静态猜测表。
    """

    @staticmethod
    def _entry(mid, cfg):
        return {"id": mid, "reasoning": cfg}

    def _run(self, cn_cfg, gl_cfg, mid="glm-5.2"):
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=_RegionPool(["cn", "global"]))

        def fake_fetch(acc):
            cfg = cn_cfg if acc.region_id == "cn" else gl_cfg
            e = self._entry(mid, cfg)
            return [(e, (mid, cfg))]

        r._fetch_one = fake_fetch
        out, regions = r._fetch_from_upstream()
        entry = next(e for e, _ in out if e["id"] == mid)
        return entry["reasoning"], regions

    def test_richer_global_entry_survives_poorer_cn_entry(self):
        cn = {"supportsReasoning": True, "onlyReasoning": True, "effort": "medium"}
        gl = {"supportsReasoning": True, "onlyReasoning": False, "canDisableThinking": True,
              "defaultEffort": "high", "supportedEfforts": ["high", "xhigh"]}
        got, regions = self._run(cn, gl)
        self.assertEqual(got["supportedEfforts"], ["high", "xhigh"])
        # 区域归属不受影响：两个区域都要记上，选号时才能按模型过滤
        self.assertEqual(regions["glm-5.2"], {"cn", "global"})

    def test_equal_richness_keeps_first(self):
        """同分时保持"先到先得"，不要顺手改掉既有行为。"""
        cn = {"supportsReasoning": True, "onlyReasoning": True, "supportedEfforts": ["low"]}
        gl = {"supportsReasoning": True, "onlyReasoning": True, "supportedEfforts": ["high"]}
        got, _ = self._run(cn, gl)
        self.assertEqual(got["supportedEfforts"], ["low"])

    def test_poorer_entry_does_not_overwrite_richer(self):
        """反向：国内版更全时不能被国际版覆盖（规则必须对称）。"""
        cn = {"supportsReasoning": True, "onlyReasoning": True,
              "supportedEfforts": ["low", "high"]}
        gl = {"supportsReasoning": True, "onlyReasoning": True, "effort": "high"}
        got, _ = self._run(cn, gl)
        self.assertEqual(got["supportedEfforts"], ["low", "high"])


class TestOnlyReasoningMergesConservatively(unittest.TestCase):
    """`onlyReasoning` 不能跟着"档位信息更全的赢"一起走（2026-09-22 实测修复）。

    实测 glm-5.2：国内版 `onlyReasoning=true`、国际版 `false`（且带
    `canDisableThinking` 与 `supportedEfforts=[high,xhigh]`）。国际版档位信息更全
    → 整条 entry 归国际版 → `off` 被放进 `reasoning_efforts()`。
    但真实请求实测：**两个区域发 `reasoning_effort="off"` 都 200 且照样出思维链**
    （reasoning_tokens 103 / 203；不带该参数时反而是 0）→ 国际版那句
    `onlyReasoning: false` 不被服务端兑现，`off` 是个**假的"不思考"开关**。

    结论：`supportedEfforts` 取"更全"，`onlyReasoning` 取"保守 OR"，两者分开。
    """

    @staticmethod
    def _run_cfgs(cn_cfg, gl_cfg, mid="glm-5.2"):
        """跑一次合并，返回 (entry 里的 reasoning, meta 里的 reasoning)。

        刻意让两者是**不同的 dict 对象**——真实代码里 `_extract_reasoning` 被调了
        两次，`entry["reasoning"]` 供 /v1/models 输出、`meta[1]` 供
        `ModelRegistry._reasoning` 查表。合成同一个对象会让"只修了一处"测不出来。
        """
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=_RegionPool(["cn", "global"]))

        def fake_fetch(acc):
            src = cn_cfg if acc.region_id == "cn" else gl_cfg
            e = {"id": mid, "reasoning": dict(src)}
            return [(e, (mid, dict(src)))]

        r._fetch_one = fake_fetch
        out, _ = r._fetch_from_upstream()
        for e, meta in out:
            if e["id"] == mid:
                return e["reasoning"], meta[1]
        raise AssertionError(f"合并结果里没有 {mid}")

    def test_only_reasoning_true_survives_richer_metadata(self):
        """国际版档位更全 → 档位归它；但它说的 onlyReasoning=false 要被国内版翻成 true。"""
        cn = {"supportsReasoning": True, "onlyReasoning": True, "effort": "medium"}
        gl = {"supportsReasoning": True, "onlyReasoning": False, "canDisableThinking": True,
              "defaultEffort": "high", "supportedEfforts": ["high", "xhigh"]}
        entry_cfg, meta_cfg = self._run_cfgs(cn, gl)
        for cfg in (entry_cfg, meta_cfg):
            self.assertTrue(cfg["onlyReasoning"], f"onlyReasoning 被国际版覆盖了：{cfg}")
            self.assertEqual(cfg["supportedEfforts"], ["high", "xhigh"])

    def test_only_reasoning_merges_when_loser_comes_second(self):
        """反向分支：国内版更全（先到且不被替换）时，落选者（国际版）的
        onlyReasoning=true 同样不能丢——规则必须对称。"""
        cn = {"supportsReasoning": True, "onlyReasoning": False,
              "supportedEfforts": ["low", "high"]}
        gl = {"supportsReasoning": True, "onlyReasoning": True, "effort": "high"}
        entry_cfg, meta_cfg = self._run_cfgs(cn, gl)
        for cfg in (entry_cfg, meta_cfg):
            self.assertTrue(cfg["onlyReasoning"], f"落选者的 onlyReasoning 被丢了：{cfg}")
            self.assertEqual(cfg["supportedEfforts"], ["low", "high"])

    def test_merged_result_no_longer_offers_off(self):
        """合并结果直接喂给 reasoning_efforts()：不能再放出 off。"""
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=_RegionPool(["cn", "global"]))
        cn = {"supportsReasoning": True, "onlyReasoning": True, "effort": "medium"}
        gl = {"supportsReasoning": True, "onlyReasoning": False, "canDisableThinking": True,
              "defaultEffort": "high", "supportedEfforts": ["high", "xhigh"]}

        def fake_fetch(acc):
            src = cn if acc.region_id == "cn" else gl
            e = {"id": "glm-5.2", "reasoning": dict(src)}
            return [(e, ("glm-5.2", dict(src)))]

        r._fetch_one = fake_fetch
        out, _ = r._fetch_from_upstream()
        r._reasoning = dict(m[1] for m in out if m[1])
        self.assertEqual(r.reasoning_efforts("glm-5.2"), ["high", "xhigh"])

    def test_two_regions_agreeing_on_true_stays_true(self):
        """两区域一致（都是 true）时不受影响——这是 5 个共有模型里的 4 个。"""
        cfg = {"supportsReasoning": True, "onlyReasoning": True,
               "defaultEffort": "high", "supportedEfforts": ["low", "high", "max"]}
        entry_cfg, meta_cfg = self._run_cfgs(cfg, dict(cfg), mid="glm-5.3")
        for got in (entry_cfg, meta_cfg):
            self.assertTrue(got["onlyReasoning"])
            self.assertEqual(got["supportedEfforts"], ["low", "high", "max"])


class TestPartialRegionFailureKeepsModels(unittest.TestCase):
    """某个区域拉取失败时**不能把该区域的模型整片丢掉**。

    实测场景（2026-09-22）：容器冷启动时国际版账号拉目录撞上一次瞬时 TLS 失败
    （`[SSL: UNEXPECTED_EOF_WHILE_READING]`），于是 13 个国际版专属模型
    （`gpt-5.6-*` / `gpt-5.5` / `gemini-3.5-flash` / `kimi-k3` …）从 `/v1/models`
    里静默消失；更糟的是这份"半份目录"被当成**成功结果**缓存下来
    （`_fetched_at` 被刷新、负缓存被清），最长要等 24 小时后的定时刷新才可能恢复。

    模型清单是用户可见的东西，不能因为一次网络抖动就少一半 —— 所以：
      ① 某区域失败 → 沿用该区域上次的条目；
      ② 每区域最多重试一次（冷启动时没有"上一份"可兜底，重试是唯一补救）；
      ③ 一个区域都没成功 → 仍然返回 None，交给 `refresh()` 记负缓存。
    """

    @staticmethod
    def _entry(mid):
        return {"id": mid, "name": mid, "reasoning": {"supportsReasoning": True}}

    def _registry(self):
        from workbuddy_one.models import ModelRegistry
        return ModelRegistry(pool=_RegionPool(["cn", "global"]))

    @staticmethod
    def _ok(acc):
        """该区域返回一个**只有它才有的**模型，便于判断"谁被丢掉了"。"""
        mid = f"{acc.region_id}-only"
        return [(TestPartialRegionFailureKeepsModels._entry(mid),
                 (mid, {"supportsReasoning": True}))]

    @staticmethod
    def _no_sleep():
        """重试里的 `time.sleep(0.3)` 在测试里要摘掉，否则整套测试被拖慢。"""
        from unittest.mock import patch
        from workbuddy_one import models as models_mod
        return patch.object(models_mod.time, "sleep", lambda _s: None)

    def test_failed_region_falls_back_to_previous_models(self):
        r = self._registry()
        r._fetch_one = lambda acc: self._ok(acc)
        r.refresh()                      # 先建立一份两区域的完整目录
        # 注意 "auto" 是 `_fetch_from_upstream` 无条件补进来的（两个区域目录里都没有它），
        # 所以任何一次刷新后的集合里都会有它。
        self.assertEqual({m["id"] for m in r.list_cached()}, {"auto", "cn-only", "global-only"})

        # 之后国际版这次拉不到（瞬时失败 / 账号在冷却）
        r._fetch_one = lambda acc: None if acc.region_id == "global" else self._ok(acc)
        with self._no_sleep():
            r.refresh()
        ids = {m["id"] for m in r.list_cached()}
        self.assertIn("global-only", ids, "一个区域失败不该让它的模型整片消失")
        self.assertIn("cn-only", ids)
        # 区域归属也必须保住：否则选号时无法按模型收敛区域
        self.assertEqual(r.regions_for("global-only"), {"global"})

    def test_carried_over_entry_and_meta_are_separate_dicts(self):
        """兜底数据也要保持 `entry["reasoning"]` 与 `meta[1]` 是**两个 dict**。

        真实数据里它们就是两份（`_extract_reasoning` 被调了两次）。兜底时合成同一个
        对象会让"只改了一处"的 bug 照样全绿 —— `onlyReasoning` 合并踩过这个坑。
        """
        r = self._registry()
        r._fetch_one = lambda acc: self._ok(acc)
        r.refresh()
        by_region = r._entries_by_region()
        self.assertEqual(set(by_region), {"cn", "global"})
        for rid, items in by_region.items():
            for entry, meta in items:
                self.assertIsNot(entry["reasoning"], meta[1], f"{rid} 的 reasoning 被共用")

    def test_transient_failure_is_retried_once(self):
        """一次瞬时失败要重试——冷启动时没有"上一份目录"可兜底，重试是唯一补救。"""
        r = self._registry()
        calls = {"global": 0}

        def flaky(acc):
            if acc.region_id == "global":
                calls["global"] += 1
                if calls["global"] == 1:
                    return None
            return self._ok(acc)

        r._fetch_one = flaky
        with self._no_sleep():
            out, regions = r._fetch_from_upstream()
        self.assertEqual(calls["global"], 2, "第一次失败后必须重试一次")
        self.assertEqual({e["id"] for e, _ in out}, {"auto", "cn-only", "global-only"})
        self.assertEqual(regions["global-only"], {"global"})

    def test_retry_is_bounded(self):
        """重试上限为 1 次：区域级失败多伴随后续刷新，再多重试只会拖慢本进程。"""
        r = self._registry()
        calls = {"global": 0}

        def always_fail_global(acc):
            if acc.region_id == "global":
                calls["global"] += 1
                return None
            return self._ok(acc)

        r._fetch_one = always_fail_global
        with self._no_sleep():
            r._fetch_from_upstream()
        self.assertEqual(calls["global"], 2)

    def test_all_regions_failed_does_not_fake_a_success(self):
        """一个区域都没成功时**不能**用兜底数据伪装成一次成功刷新。

        否则 `_last_fail` 被清、`_fetched_at` 被刷新：上游长期挂掉时我们既不记负缓存、
        也不再重试，还会一直对外发一份越来越旧的目录。
        """
        r = self._registry()
        r._fetch_one = lambda acc: self._ok(acc)
        r.refresh()
        r._fetch_one = lambda acc: None
        with self._no_sleep():
            out = r.refresh()
        # 旧缓存仍然对外可用（`_fallback()` 的既有语义）
        self.assertEqual({m["id"] for m in out}, {"auto", "cn-only", "global-only"})
        self.assertNotEqual(r._last_fail, 0.0, "全失败必须记负缓存，否则会一直重打上游")


class TestCreditsByRegion(unittest.TestCase):
    """上游对同一模型的**成本系数按区域可能不同**，合并时两个值都要留下。

    实测（2026-09-22）：`hy4-preview` 国内版 `x0.29`、国际版 `x0.00`（免费）。
    而合并只按 reasoning 信息量挑赢家、`credits` 根本不参与比较 → 不额外记一份
    分区域明细，落败区域的价格就静默消失了，界面只能显示"恰好赢了的那个"。

    注意 `None` 与 `0.0` 是两件事：`None` = 上游没给（不记入明细），
    `0.0` = 上游明确给了 0（免费），必须记。
    """

    @staticmethod
    def _no_sleep():
        from unittest.mock import patch
        from workbuddy_one import models as models_mod
        return patch.object(models_mod.time, "sleep", lambda _s: None)

    def _registry(self, by_region):
        """by_region: {区域 id: [(模型 id, credits)] 或 [(模型 id, credits, reasoning)]}"""
        from workbuddy_one.models import ModelRegistry
        r = ModelRegistry(pool=_RegionPool(sorted(by_region)))

        def fetch(acc):
            out = []
            for item in by_region[acc.region_id]:
                mid, credits = item[0], item[1]
                cfg = item[2] if len(item) > 2 else {"supportsReasoning": True}
                out.append(({"id": mid, "name": mid, "credits": credits,
                             "reasoning": dict(cfg)}, (mid, dict(cfg))))
            return out

        r._fetch_one = fetch
        return r

    def _entries(self, r):
        with self._no_sleep():
            out, _ = r._fetch_from_upstream()
        self.assertIsNotNone(out, "两区域都给了数据，不该返回 None")
        return {e["id"]: e for e, _ in out}

    def test_same_model_different_price_keeps_both(self):
        """两区域不同价 → 明细里两个值都在（这是 hy4-preview 的真实形状）。"""
        r = self._registry({"cn": [("hy4-preview", 0.29)], "global": [("hy4-preview", 0.0)]})
        e = self._entries(r)["hy4-preview"]
        self.assertEqual(e["credits_by_region"], {"cn": 0.29, "global": 0.0})

    def test_identical_price_recorded_per_region(self):
        """两区域同价也照记：前端据此判断"没必要并列显示"，不靠猜。"""
        r = self._registry({"cn": [("glm-5.2", 0.79)], "global": [("glm-5.2", 0.79)]})
        e = self._entries(r)["glm-5.2"]
        self.assertEqual(e["credits_by_region"], {"cn": 0.79, "global": 0.79})
        self.assertEqual(e["credits"], 0.79)

    def test_region_without_price_is_not_recorded(self):
        """`None` 表示上游没给，**不是 0** —— 记成 0 会显示成"免费"。"""
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", None)]})
        e = self._entries(r)["m"]
        self.assertEqual(e["credits_by_region"], {"cn": 0.29})

    def test_merged_credits_is_the_winning_region_value(self):
        """`credits` 保持原语义：取"赢了合并"的那个区域的值（这里国际版信息更全）。"""
        rich = {"supportsReasoning": True, "supportedEfforts": ["high", "xhigh"]}
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", 0.0, rich)]})
        e = self._entries(r)["m"]
        self.assertEqual(e["credits"], 0.0)
        self.assertEqual(e["credits_by_region"], {"cn": 0.29, "global": 0.0})

    def test_winner_without_price_falls_back_to_the_other_region(self):
        """赢家没给价格时用另一个区域的真实值补上——显示一个真价格比显示 `-` 有用。"""
        rich = {"supportsReasoning": True, "supportedEfforts": ["high", "xhigh"]}
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", None, rich)]})
        e = self._entries(r)["m"]
        self.assertEqual(e["credits"], 0.29)
        self.assertEqual(e["credits_by_region"], {"cn": 0.29})

    def test_fallback_restores_each_region_own_price(self):
        """兜底路径也要还原成**各区域自己的**价格。

        `_entries_by_region()` 拿到的 `credits` 是合并后的（赢家的值），直接沿用会把
        失败区域的价格算成赢家的价格——兜底出来的明细就成了假的。
        """
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", 0.0)]})
        ok = r._fetch_one
        r.refresh()                                   # 建立一份完整缓存
        r._fetch_one = lambda acc: None if acc.region_id == "global" else ok(acc)
        e = self._entries(r)["m"]
        self.assertEqual(e["credits_by_region"], {"cn": 0.29, "global": 0.0})
        self.assertEqual(e["credits"], 0.29)

    def test_synthetic_auto_has_no_region_prices(self):
        """合成出来的 `auto` 没有上游来源，不该凭空带上成本明细。"""
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", 0.0)]})
        self.assertNotIn("credits_by_region", self._entries(r)["auto"])

    def test_cached_list_keeps_region_prices(self):
        """`/admin/models` 走的是 `list_cached()`，明细必须能穿过这条路径到界面。"""
        r = self._registry({"cn": [("m", 0.29)], "global": [("m", 0.0)]})
        r.refresh()
        e = {x["id"]: x for x in r.list_cached()}["m"]
        self.assertEqual(e["credits_by_region"], {"cn": 0.29, "global": 0.0})


class TestKnownEffortsTable(unittest.TestCase):
    """静态档位表必须与上游目录的约定保持一致。

    `KNOWN_EFFORTS` **不是**冷启动兜底，而是"目录没给 supportedEfforts"时的
    生效策略（29 个模型里只有 9 个带 supportedEfforts）——表里写错就是线上写错。
    """

    # 2026-09-21 实测上游目录（两区域并集）里 onlyReasoning=true 的模型：
    # 只产思维链、关不掉思考。快照会过期，但过期时这条测试会失败，正好提醒重核。
    ONLY_REASONING = {
        "auto", "hy3", "hy3-x", "hy4-preview", "deepseek-v4-pro", "deepseek-v4.1-flash",
        "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash", "glm-5v-turbo",
        "kimi-k2.6", "kimi-k2.7", "kimi-k2.8-preview", "kimi-k3", "kimi-k3-1",
        "minimax-m3", "fast-model", "balanced-model", "primary-model",
        "gpt-5.3-codex", "gpt-5.4", "gpt-5.5", "gemini-3.5-flash",
    }

    # 同日实测的**在册**模型全集（国内版 16 + 国际版 18 的并集）。
    LIVE_MODELS = ONLY_REASONING | {
        "deep-model", "default-model", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    }

    def test_no_off_for_only_reasoning_models(self):
        """onlyReasoning 模型不能提供 off：客户端发 off 会被透传，而 inject_thinking
        又同时注入 thinking:{type:enabled}，等于给上游两个矛盾的信号。"""
        from workbuddy_one.reasoning import KNOWN_EFFORTS
        bad = {m: e for m, e in KNOWN_EFFORTS.items()
               if m in self.ONLY_REASONING and "off" in e}
        self.assertEqual(bad, {}, f"onlyReasoning 模型不该提供 off：{bad}")

    def test_all_keys_are_live_models(self):
        """表里只该留当前目录里还活着的模型。

        已下架的（kimi-k2.5 / deepseek-v4-flash / minimax-m3-pay / hy3-preview-agent）
        留着只会误导：这些名字已不在目录里，请求先被 11102 挡掉，根本走不到档位裁剪。
        """
        from workbuddy_one.reasoning import KNOWN_EFFORTS
        stale = sorted(set(KNOWN_EFFORTS) - self.LIVE_MODELS)
        self.assertEqual(stale, [], f"表里有已下架/未知的模型：{stale}")

    def test_entries_are_ranked_lists(self):
        """条目必须是非空的合法档位列表，且不含重复。"""
        from workbuddy_one.reasoning import KNOWN_EFFORTS, _EFFORT_RANK
        for model, efforts in KNOWN_EFFORTS.items():
            with self.subTest(model=model):
                self.assertTrue(efforts, f"{model} 的档位列表为空")
                self.assertEqual(len(efforts), len(set(efforts)), f"{model} 有重复档位")
                unknown = [e for e in efforts if e not in _EFFORT_RANK]
                self.assertEqual(unknown, [], f"{model} 含未知档位：{unknown}")


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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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
        tmp = _TMP
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


# ---------------------------------------------------------------------------
# 上游签到状态（2026-09-21）
#
# 背景：界面「今日签到」原来只读本地 `last_checkin_date`，语义是"本网关替你
# 签过没有"。用户在官方客户端自己签到时我们不知情，会错误显示「未签到」；
# 反之 `set_checkin_date` 无条件写，上游报错时又会假阳。下面这组用例把
# 新的口径钉住：**以上游 today_checked_in 为准，且只在确认成功/已签到时落账**。
# ---------------------------------------------------------------------------


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


class _FakeDB:
    """够用的 DB 替身：只实现签到相关方法。"""

    def __init__(self, dates=None):
        self.dates = dict(dates or {})
        self.status: dict[str, tuple] = {}

    def checkin_dates(self):
        return self.dates

    def set_checkin_date(self, uid, date):
        self.dates[uid] = date

    def set_checkin_status(self, uid, today, active, synced_at):
        self.status[uid] = (today, active, synced_at)

    def get_settings(self):
        return {}


class _FakeAcc:
    def __init__(self, uid, today=None, active=None, synced=None):
        self.uid = uid
        self.mgr = object()
        self.checkin_today = today
        self.checkin_active = active
        self.checkin_synced_at = synced


class _FakePool:
    def __init__(self, accounts):
        self.accounts = accounts

    def set_checkin_status(self, uid, today, active, synced_at):
        for a in self.accounts:
            if a.uid == uid:
                a.checkin_today = today
                a.checkin_active = active
                a.checkin_synced_at = synced_at
                return


class _FakeListPool:
    """只给 all_accounts() 用的替身（模拟 AccountPool 的输出）。"""

    def __init__(self, rows):
        self._rows = rows

    def all_accounts(self):
        return [dict(r) for r in self._rows]


def _sched(accounts, dates=None):
    from workbuddy_one.scheduler import Scheduler
    db = _FakeDB(dates)
    return Scheduler(_FakePool(accounts), db=db), db


class TestCheckinStatusParsing(unittest.IsolatedAsyncioTestCase):
    """上游 checkin-activity-status 的解析与错误处理。"""

    async def test_parses_normalized_fields(self):
        from unittest import mock
        from workbuddy_one import billing
        payload = {"code": 0, "msg": "OK", "data": {
            "active": True, "today_checked_in": True, "streak_days": 4,
            "checkin_dates": ["2026-09-21", "2026-09-20"], "total_credits": 400,
        }}
        with mock.patch.object(billing, "_post_json", mock.AsyncMock(return_value=payload)):
            st = await billing.fetch_checkin_status(object())
        self.assertEqual(st, {
            "active": True, "today_checked_in": True, "streak_days": 4,
            "checkin_dates": ["2026-09-21", "2026-09-20"], "total_credits": 400.0,
        })

    async def test_inactive_activity_shape(self):
        """国际版实测形态：活动未开启时 active=False 且 today_checked_in 恒 False。

        这正是"显示未签到"其实不是 bug 的那种情况 —— 解析层要原样保留 active，
        否则上层就没法把它和"用户没签"区分开。
        """
        from unittest import mock
        from workbuddy_one import billing
        payload = {"code": 0, "data": {"active": False, "today_checked_in": False,
                                       "streak_days": 0, "checkin_dates": [],
                                       "total_credits": 0}}
        with mock.patch.object(billing, "_post_json", mock.AsyncMock(return_value=payload)):
            st = await billing.fetch_checkin_status(object())
        self.assertFalse(st["active"])
        self.assertFalse(st["today_checked_in"])
        self.assertEqual(st["checkin_dates"], [])

    async def test_raises_on_error_code(self):
        from unittest import mock
        from workbuddy_one import billing
        with mock.patch.object(billing, "_post_json",
                               mock.AsyncMock(return_value={"code": 11102, "msg": "service info not found"})):
            with self.assertRaises(RuntimeError):
                await billing.fetch_checkin_status(object())

    async def test_raises_on_missing_data(self):
        from unittest import mock
        from workbuddy_one import billing
        with mock.patch.object(billing, "_post_json", mock.AsyncMock(return_value={"code": 0})):
            with self.assertRaises(RuntimeError):
                await billing.fetch_checkin_status(object())


class TestCheckinSnapshot(unittest.TestCase):
    """checkin_snapshot：上游真值优先，且必须校验"是不是今天同步的"。"""

    def test_prefers_fresh_upstream_over_local_bookkeeping(self):
        import time
        acc = _FakeAcc("a", today=False, active=True, synced=time.time())
        sched, _ = _sched([acc], dates={"a": _today()})  # 本地记着"签过"
        snap = sched.checkin_snapshot()["a"]
        self.assertFalse(snap["today"])                  # 以上游为准
        self.assertEqual(snap["source"], "upstream")

    def test_stale_upstream_falls_back_to_local(self):
        """跨零点后旧的"已签到"绝不能当今天用 —— 比不显示更糟。"""
        import time
        acc = _FakeAcc("a", today=True, active=True, synced=time.time() - 3 * 86400)
        sched, _ = _sched([acc], dates={"a": _today()})
        snap = sched.checkin_snapshot()["a"]
        self.assertTrue(snap["today"])
        self.assertEqual(snap["source"], "local")
        self.assertIsNone(snap["active"])

    def test_never_synced_uses_local(self):
        acc = _FakeAcc("a")
        sched, _ = _sched([acc], dates={})
        self.assertEqual(sched.checkin_snapshot()["a"],
                         {"today": False, "active": None, "synced_at": None, "source": "local"})


class TestDoCheckinUsesUpstream(unittest.IsolatedAsyncioTestCase):
    """do_checkin 的跳过与落账口径。"""

    async def _run(self, acc, daily_result, dates=None):
        from unittest import mock
        from workbuddy_one import billing
        sched, db = _sched([acc], dates=dates)
        with mock.patch.object(billing, "daily_checkin",
                               mock.AsyncMock(return_value=daily_result)), \
                mock.patch.object(sched, "sync_checkin_status", mock.AsyncMock()), \
                mock.patch.object(sched, "refresh_credits", mock.AsyncMock()):
            results = await sched.do_checkin()
        return results, db

    async def test_syncs_upstream_status_before_deciding(self):
        """回归：启动竞态。

        start() 里 _run 任务与首轮 refresh_credits 是并发的，do_checkin 可能抢在
        签到状态同步之前跑 —— 那样会拿陈旧的本地记账判断，对"活动未开启"的账号
        白打一次上游、还被误报成签到失败。所以 do_checkin 必须先自己补齐状态。
        """
        import time
        from unittest import mock
        from workbuddy_one import billing
        acc = _FakeAcc("a")  # 状态未知，本地记账也没有
        sched, _ = _sched([acc])

        async def fake_sync(only_stale=True):
            # 模拟同步把上游真值填进来：活动未开启
            acc.checkin_today = False
            acc.checkin_active = False
            acc.checkin_synced_at = time.time()

        daily = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(sched, "sync_checkin_status", fake_sync), \
                mock.patch.object(billing, "daily_checkin", daily), \
                mock.patch.object(sched, "refresh_credits", mock.AsyncMock()):
            results = await sched.do_checkin()
        daily.assert_not_awaited()                    # 不该白打上游
        self.assertTrue(results[0][1].get("inactive"))

    async def test_upstream_checked_in_is_skipped(self):
        import time
        acc = _FakeAcc("a", today=True, active=True, synced=time.time())
        results, db = await self._run(acc, {"ok": True})
        self.assertTrue(results[0][1].get("already"))
        self.assertNotIn("a", db.dates)  # 跳过了，不该写账

    async def test_inactive_activity_is_skipped_not_failed(self):
        """活动未开启要归为"跳过"，否则 _run 会每 2 小时白打上游重试。"""
        import time
        acc = _FakeAcc("a", today=False, active=False, synced=time.time())
        results, db = await self._run(acc, {"ok": True})
        r = results[0][1]
        self.assertTrue(r.get("inactive"))
        self.assertFalse(r.get("ok"))
        self.assertFalse(r.get("already"))
        self.assertNotIn("a", db.dates)

    async def test_upstream_error_does_not_record_checkin_date(self):
        """回归：上游报错（如"活动未开启"）绝不能被记成"今日已签到"（假阳性）。"""
        acc = _FakeAcc("a")
        results, db = await self._run(acc, {"ok": False, "message": "活动未开启"})
        self.assertFalse(results[0][1]["ok"])
        self.assertNotIn("a", db.dates)

    async def test_already_records_checkin_date(self):
        acc = _FakeAcc("a")
        results, db = await self._run(acc, {"ok": False, "message": "已签到", "already": True})
        self.assertEqual(db.dates.get("a"), _today())

    async def test_success_records_and_marks_upstream_true(self):
        acc = _FakeAcc("a")
        results, db = await self._run(acc, {"ok": True, "message": "签到成功"})
        self.assertEqual(db.dates.get("a"), _today())
        self.assertTrue(acc.checkin_today)          # 池内状态顺手刷新
        self.assertTrue(db.status["a"][0])          # 也落了库


class TestAccountsWithCheckin(unittest.TestCase):
    """路由层合并：上游真值 + 来源标注。"""

    def test_marks_upstream_source(self):
        import time
        from workbuddy_one.routes.accounts import accounts_with_checkin
        acc = _FakeAcc("a", today=True, active=True, synced=time.time())
        sched, _ = _sched([acc], dates={})
        pool = _FakeListPool([{"uid": "a", "checkin_today": None, "checkin_active": None,
                               "checkin_synced_at": None}])
        ctx = type("C", (), {"scheduler": sched, "pool": pool})()
        out = accounts_with_checkin(ctx)
        self.assertTrue(out[0]["checkin_today"])
        self.assertEqual(out[0]["checkin_source"], "upstream")

    def test_falls_back_to_local_and_marks_it(self):
        from workbuddy_one.routes.accounts import accounts_with_checkin
        acc = _FakeAcc("a")  # 从未同步
        sched, _ = _sched([acc], dates={"a": _today()})
        pool = _FakeListPool([{"uid": "a", "checkin_today": None, "checkin_active": None,
                               "checkin_synced_at": None}])
        ctx = type("C", (), {"scheduler": sched, "pool": pool})()
        out = accounts_with_checkin(ctx)
        self.assertTrue(out[0]["checkin_today"])
        self.assertEqual(out[0]["checkin_source"], "local")


class TestCheckinStatusDb(unittest.TestCase):
    """DB 层：v5 迁移新增的三列，以及"未同步 → 不返回"的语义。"""

    def _db(self, name):
        from workbuddy_one.db import Database
        return Database(str(_TMP / name))

    def test_schema_has_checkin_status_columns(self):
        db = self._db("checkin_cols.db")
        try:
            cols = db._table_columns("accounts")
            for c in ("checkin_today", "checkin_active", "checkin_synced_at"):
                self.assertIn(c, cols)
            self.assertEqual(db.checkin_status(), {})  # 新库从未同步过
        finally:
            db._conn.close()

    def test_set_and_read_roundtrip(self):
        db = self._db("checkin_rt.db")
        try:
            db.upsert_account({"path": "x"}, {"uid": "u1", "nickname": "n"})
            self.assertEqual(db.checkin_status(), {})
            db.set_checkin_status("u1", True, False, 123.0)
            self.assertEqual(db.checkin_status()["u1"],
                             {"today": True, "active": False, "synced_at": 123.0})
        finally:
            db._conn.close()

    def test_last_checkin_date_kept_separate(self):
        """本地记账（last_checkin_date）与上游真值（checkin_today）互不覆盖。"""
        db = self._db("checkin_sep.db")
        try:
            db.upsert_account({"path": "x"}, {"uid": "u1", "nickname": "n"})
            db.set_checkin_status("u1", False, True, 1.0)
            db.set_checkin_date("u1", "2026-09-20")
            self.assertEqual(db.checkin_dates(), {"u1": "2026-09-20"})
            self.assertFalse(db.checkin_status()["u1"]["today"])
        finally:
            db._conn.close()


if __name__ == "__main__":
    unittest.main()
