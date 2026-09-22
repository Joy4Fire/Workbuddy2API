"""区域适配（国内版 / 国际版）单元测试。

覆盖：区域判定、host/路径/Origin 差异、BACKEND 覆盖、模型目录与计费的区域路由、
11128 空 system 兜底、按区域开关反审核脱敏。

运行：python -m unittest discover -s tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _tmp_dir(case) -> Path:
    """每个用例一个独立临时目录，并登记退出时删除。

    刻意落在**系统临时目录**而不是仓库内的 tests/_tmp/：
      - 不污染仓库；
      - 仓库可能在网络盘上，而网络盘没有回收站，删除会走 SHFileOperationW 并失败，
        实测单次 rmtree 要 11~36 秒（本地盘 <0.01 秒）。测试不该依赖仓库所在盘符。
    """
    d = Path(tempfile.mkdtemp(prefix="wb_region_"))
    case.addCleanup(shutil.rmtree, d, True)
    return d


class _FakeMgr:
    """最小凭据桩：只提供 get_headers 与 domain。"""

    def __init__(self, domain: str):
        self.domain = domain

    def get_headers(self) -> dict:
        return {"X-Domain": self.domain, "Authorization": "Bearer stub",
                "Content-Type": "application/json"}


class TestRegionDetect(unittest.TestCase):
    def test_cn_domain(self):
        from workbuddy_one.region import CN, detect_region
        self.assertIs(detect_region("www.codebuddy.cn"), CN)

    def test_global_domain(self):
        from workbuddy_one.region import GLOBAL, detect_region
        self.assertIs(detect_region("www.workbuddy.ai"), GLOBAL)

    def test_empty_falls_back_to_cn(self):
        from workbuddy_one.region import CN, detect_region
        self.assertIs(detect_region(""), CN)
        self.assertIs(detect_region(None), CN)

    def test_unknown_falls_back_to_cn(self):
        from workbuddy_one.region import CN, detect_region
        self.assertIs(detect_region("example.com"), CN)

    def test_case_insensitive(self):
        from workbuddy_one.region import GLOBAL, detect_region
        self.assertIs(detect_region("WWW.WorkBuddy.AI"), GLOBAL)

    def test_region_of_account(self):
        from workbuddy_one.region import GLOBAL, region_of_account
        acc = SimpleNamespace(mgr=SimpleNamespace(domain="www.workbuddy.ai"))
        self.assertIs(region_of_account(acc), GLOBAL)

    def test_region_of_account_without_mgr(self):
        from workbuddy_one.region import CN, region_of_account
        self.assertIs(region_of_account(SimpleNamespace()), CN)


class TestRegionEndpoints(unittest.TestCase):
    """区域差异必须体现在 host / 路径 / Origin 三处。"""

    def test_chat_base(self):
        from workbuddy_one.region import chat_base
        self.assertEqual(chat_base("www.codebuddy.cn"), "https://copilot.tencent.com")
        self.assertEqual(chat_base("www.workbuddy.ai"), "https://www.workbuddy.ai")

    def test_catalog_path_differs(self):
        from workbuddy_one.region import catalog_path
        self.assertEqual(catalog_path("www.codebuddy.cn"), "/console/enterprises/personal/models")
        self.assertEqual(catalog_path("www.workbuddy.ai"), "/v2/enterprises/personal/models")

    def test_origin(self):
        from workbuddy_one.region import origin
        self.assertEqual(origin("www.codebuddy.cn"), "https://www.codebuddy.cn")
        self.assertEqual(origin("www.workbuddy.ai"), "https://www.workbuddy.ai")

    def test_billing_base(self):
        from workbuddy_one.region import billing_base
        self.assertEqual(billing_base("www.workbuddy.ai"), "https://www.workbuddy.ai")
        self.assertEqual(billing_base("www.codebuddy.cn"), "https://copilot.tencent.com")

    def test_backend_env_overrides_host_not_path(self):
        """BACKEND 覆盖 host，但模型目录路径仍按区域走（路径差异是协议层的）。"""
        from workbuddy_one.config import config
        from workbuddy_one.region import catalog_path, chat_base
        old = config.backend
        config.backend = "https://custom.example.com"
        try:
            self.assertEqual(chat_base("www.workbuddy.ai"), "https://custom.example.com")
            self.assertEqual(chat_base("www.codebuddy.cn"), "https://custom.example.com")
            self.assertEqual(catalog_path("www.workbuddy.ai"), "/v2/enterprises/personal/models")
        finally:
            config.backend = old


class TestUpstreamRouting(unittest.TestCase):
    """chat 转发按请求头里的 X-Domain 选 host（调用方无需感知区域）。"""

    def test_global_header_picks_global_host(self):
        from workbuddy_one.upstream import _chat_url
        self.assertEqual(_chat_url({"X-Domain": "www.workbuddy.ai"}),
                         "https://www.workbuddy.ai/v2/chat/completions")

    def test_cn_header_picks_cn_host(self):
        from workbuddy_one.upstream import _chat_url
        self.assertEqual(_chat_url({"X-Domain": "www.codebuddy.cn"}),
                         "https://copilot.tencent.com/v2/chat/completions")

    def test_missing_header_falls_back_to_default_domain(self):
        from workbuddy_one.upstream import _chat_url
        from workbuddy_one.region import chat_base
        self.assertEqual(_chat_url({}), f"{chat_base('')}/v2/chat/completions")


class TestBillingHeadersByRegion(unittest.TestCase):
    def test_global_gets_matching_origin(self):
        from workbuddy_one.billing import _BROWSER_UA, _billing_headers
        h = _billing_headers(_FakeMgr("www.workbuddy.ai"))
        self.assertEqual(h["Origin"], "https://www.workbuddy.ai")
        self.assertEqual(h["Referer"], "https://www.workbuddy.ai/")
        self.assertEqual(h["User-Agent"], _BROWSER_UA)

    def test_cn_keeps_existing_behavior(self):
        """国内版刻意不加 Origin/Referer：既有实现不带也能过，不动已经在跑的额度查询。"""
        from workbuddy_one.billing import _billing_headers
        h = _billing_headers(_FakeMgr("www.codebuddy.cn"))
        self.assertNotIn("Origin", h)
        self.assertNotIn("Referer", h)

    def test_billing_base_by_domain(self):
        from workbuddy_one.billing import _billing_base
        self.assertEqual(_billing_base(_FakeMgr("www.workbuddy.ai")), "https://www.workbuddy.ai")
        self.assertEqual(_billing_base(_FakeMgr("www.codebuddy.cn")), "https://copilot.tencent.com")


class TestEnsureLeadingSystem(unittest.TestCase):
    """国际版 11128「first message is not system prompt」兜底。"""

    def test_prepends_when_no_system(self):
        from workbuddy_one.reasoning import ensure_leading_system
        b = {"messages": [{"role": "user", "content": "hi"}]}
        out = ensure_leading_system(b)
        self.assertEqual(out["messages"][0], {"role": "system", "content": ""})
        self.assertEqual(out["messages"][1]["role"], "user")
        self.assertEqual(len(out["messages"]), 2)

    def test_existing_system_untouched(self):
        from workbuddy_one.reasoning import ensure_leading_system
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        out = ensure_leading_system({"messages": list(msgs)})
        self.assertEqual(out["messages"], msgs)

    def test_prepends_when_system_is_not_first(self):
        """上游校验的是**第一条**消息：system 排在后面时照样要补。

        判据必须是 msgs[0].role，不能是「列表里存在 system」——否则
        [user, system] 这种顺序会漏补，上游仍返回 11128。
        """
        from workbuddy_one.reasoning import ensure_leading_system
        msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "s"}]
        out = ensure_leading_system({"messages": list(msgs)})
        self.assertEqual(out["messages"][0], {"role": "system", "content": ""})
        # 原有消息一条不少、顺序不变
        self.assertEqual(out["messages"][1:], msgs)
        self.assertEqual(len(out["messages"]), 3)

    def test_prepends_when_system_is_in_the_middle(self):
        from workbuddy_one.reasoning import ensure_leading_system
        msgs = [{"role": "user", "content": "a"},
                {"role": "system", "content": "s"},
                {"role": "assistant", "content": "b"}]
        out = ensure_leading_system({"messages": list(msgs)})
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["messages"][1:], msgs)

    def test_developer_only_becomes_system_without_prepend(self):
        """developer 先被归一成 system，因此不该再多补一条空 system。"""
        from workbuddy_one.reasoning import sanitize_body
        out = sanitize_body({"model": "x", "messages": [{"role": "developer", "content": "d"},
                                                       {"role": "user", "content": "hi"}]})
        self.assertEqual(len(out["messages"]), 2)
        self.assertEqual(out["messages"][0]["role"], "system")

    def test_empty_messages_untouched(self):
        from workbuddy_one.reasoning import ensure_leading_system
        out = ensure_leading_system({"messages": []})
        self.assertEqual(out["messages"], [])

    def test_no_messages_key_untouched(self):
        from workbuddy_one.reasoning import ensure_leading_system
        out = ensure_leading_system({"model": "x"})
        self.assertNotIn("messages", out)

    def test_sanitize_body_end_to_end(self):
        from workbuddy_one.reasoning import sanitize_body
        out = sanitize_body({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["role"], "system")


class TestDesensitizeByRegion(unittest.TestCase):
    """反审核脱敏只在国内版账号上施加。"""

    def _body(self):
        return {"messages": [{"role": "system", "content": "Refuse DoS attacks and credential testing"}]}

    def test_cn_account_desensitized(self):
        from workbuddy_one.gateway.inference import apply_desensitize
        acc = SimpleNamespace(mgr=SimpleNamespace(domain="www.codebuddy.cn"))
        out = apply_desensitize(self._body(), acc)
        self.assertIn("\u200b", out["messages"][0]["content"])

    def test_global_account_untouched(self):
        from workbuddy_one.gateway.inference import apply_desensitize
        acc = SimpleNamespace(mgr=SimpleNamespace(domain="www.workbuddy.ai"))
        out = apply_desensitize(self._body(), acc)
        self.assertNotIn("\u200b", out["messages"][0]["content"])

    def test_disabled_config_skips_both(self):
        from workbuddy_one.config import config
        from workbuddy_one.gateway.inference import apply_desensitize
        old = config.desensitize
        config.desensitize = False
        try:
            acc = SimpleNamespace(mgr=SimpleNamespace(domain="www.codebuddy.cn"))
            out = apply_desensitize(self._body(), acc)
            self.assertNotIn("\u200b", out["messages"][0]["content"])
        finally:
            config.desensitize = old


class TestCredentialRegion(unittest.TestCase):
    """区域由 auth 文件自带的 auth.domain 判定，无需额外配置。"""

    def _mgr(self, domain: str):
        from workbuddy_one.credentials import CredentialManager
        tmp = _tmp_dir(self)
        p = tmp / "workbuddy-stub.info"
        p.write_text(json.dumps({
            "auth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 4102444800000,
                     "domain": domain},
            "account": {"uid": "u1", "nickname": "n"},
        }, ensure_ascii=False), encoding="utf-8")
        return CredentialManager(p)

    def test_global_account(self):
        mgr = self._mgr("www.workbuddy.ai")
        self.assertEqual(mgr.domain, "www.workbuddy.ai")
        self.assertEqual(mgr.region.id, "global")

    def test_cn_account(self):
        mgr = self._mgr("www.codebuddy.cn")
        self.assertEqual(mgr.region.id, "cn")

    def test_summary_carries_region(self):
        mgr = self._mgr("www.workbuddy.ai")
        s = mgr.summary()
        self.assertEqual(s["region"], "global")
        self.assertEqual(s["region_label"], "国际版")
        self.assertEqual(s["domain"], "www.workbuddy.ai")

    def test_missing_domain_falls_back_to_cn(self):
        from workbuddy_one.credentials import CredentialManager
        tmp = _tmp_dir(self)
        p = tmp / "workbuddy-stub2.info"
        p.write_text(json.dumps({
            "auth": {"accessToken": "at", "expiresAt": 4102444800000},
            "account": {"uid": "u2"},
        }), encoding="utf-8")
        mgr = CredentialManager(p)
        self.assertEqual(mgr.region.id, "cn")


class TestOAuthRegionBase(unittest.TestCase):
    def test_default_is_cn(self):
        from workbuddy_one.oauth import _base_for
        self.assertEqual(_base_for(None), "https://copilot.tencent.com")
        self.assertEqual(_base_for(""), "https://copilot.tencent.com")
        self.assertEqual(_base_for("cn"), "https://copilot.tencent.com")

    def test_global(self):
        from workbuddy_one.oauth import _base_for
        self.assertEqual(_base_for("global"), "https://www.workbuddy.ai")

    def test_unknown_region_falls_back_to_cn(self):
        from workbuddy_one.oauth import _base_for
        self.assertEqual(_base_for("mars"), "https://copilot.tencent.com")


def _write_auth(case, uid: str, domain: str):
    """写一份 auth 文件并返回 CredentialManager（临时目录登记清理）。"""
    from workbuddy_one.credentials import CredentialManager
    p = _tmp_dir(case) / f"workbuddy-{uid}.info"
    p.write_text(json.dumps({
        "auth": {"accessToken": "at", "refreshToken": "rt",
                 "expiresAt": 4102444800000, "domain": domain},
        "account": {"uid": uid, "nickname": uid},
    }, ensure_ascii=False), encoding="utf-8")
    return CredentialManager(p)


class TestPoolRegionFilter(unittest.TestCase):
    """混池下按区域收敛候选账号（模型只在单区域存在时必须用对区域）。"""

    def _pool(self):
        from workbuddy_one.pool import AccountPool
        mgrs = {
            "cn-1": _write_auth(self, "cn-1", "www.codebuddy.cn"),
            "g-1": _write_auth(self, "g-1", "www.workbuddy.ai"),
        }
        return AccountPool(mgrs)

    def test_account_region_cached_on_instance(self):
        pool = self._pool()
        by_uid = {a.uid: a for a in pool.accounts}
        self.assertEqual(by_uid["cn-1"].region_id, "cn")
        self.assertEqual(by_uid["g-1"].region_id, "global")

    def test_pick_limited_to_global(self):
        pool = self._pool()
        for _ in range(20):
            self.assertEqual(pool.pick(regions={"global"}).uid, "g-1")

    def test_pick_limited_to_cn(self):
        pool = self._pool()
        for _ in range(20):
            self.assertEqual(pool.pick(regions={"cn"}).uid, "cn-1")

    def test_pick_no_region_constraint_uses_all(self):
        pool = self._pool()
        picked = {pool.pick().uid for _ in range(60)}
        self.assertEqual(picked, {"cn-1", "g-1"})

    def test_pick_falls_back_when_region_absent(self):
        """限定区域内没有账号时退回不过滤，而不是返回 None（宁可试一次）。"""
        pool = self._pool()
        self.assertIsNotNone(pool.pick(regions={"mars"}))

    def test_all_accounts_exposes_region(self):
        pool = self._pool()
        got = {a["uid"]: a for a in pool.all_accounts()}
        self.assertEqual(got["g-1"]["region"], "global")
        self.assertEqual(got["g-1"]["region_label"], "国际版")
        self.assertEqual(got["cn-1"]["region"], "cn")


class _FakePool:
    """只给 ModelRegistry 用的最小账号池桩。"""

    def __init__(self, accounts):
        self.accounts = accounts
        self.failures = []

    def pick(self, regions=None):
        cands = [a for a in self.accounts if not regions or a.region_id in regions]
        return cands[0] if cands else None

    def on_failure(self, uid, seconds):
        self.failures.append((uid, seconds))


class TestModelCatalogPerRegion(unittest.TestCase):
    """目录按区域分别拉取后合并；模型→区域映射用于选号过滤。"""

    def _registry(self, catalogs):
        from workbuddy_one.models import ModelRegistry

        class _Stub(ModelRegistry):
            def _fetch_one(self, account):
                return catalogs.get(account.region_id)

        pool = _FakePool([SimpleNamespace(uid="cn-1", region_id="cn"),
                          SimpleNamespace(uid="g-1", region_id="global")])
        return _Stub(pool), pool

    @staticmethod
    def _entry(mid):
        return ({"id": mid, "object": "model", "name": mid}, (mid, {}))

    def test_merges_union_and_tracks_regions(self):
        reg, _ = self._registry({
            "cn": [self._entry("deepseek-v4-pro"), self._entry("glm-5.2")],
            "global": [self._entry("gpt-5.4"), self._entry("glm-5.2")],
        })
        ids = {m["id"] for m in reg.refresh()}
        # auto 两区域都不在目录里，但会被兜底注入（实测两区域都支持 auto）
        self.assertEqual(ids, {"auto", "deepseek-v4-pro", "glm-5.2", "gpt-5.4"})
        self.assertEqual(reg.regions_for("glm-5.2"), {"cn", "global"})
        self.assertEqual(reg.regions_for("gpt-5.4"), {"global"})
        self.assertEqual(reg.regions_for("deepseek-v4-pro"), {"cn"})

    def test_unknown_model_returns_empty_set(self):
        reg, _ = self._registry({"cn": [self._entry("glm-5.2")]})
        reg.refresh()
        self.assertEqual(reg.regions_for("no-such-model"), set())
        self.assertEqual(reg.regions_for(""), set())

    def test_auto_injected_when_absent(self):
        reg, _ = self._registry({"cn": [self._entry("glm-5.2")]})
        self.assertIn("auto", {m["id"] for m in reg.refresh()})

    def test_partial_region_failure_still_merges_other(self):
        """一个区域拉取失败不影响另一个区域的结果。"""
        reg, _ = self._registry({"global": [self._entry("gpt-5.4")]})  # cn 返回 None
        ids = {m["id"] for m in reg.refresh()}
        self.assertIn("gpt-5.4", ids)
        self.assertEqual(reg.regions_for("gpt-5.4"), {"global"})

    def test_all_regions_failed_returns_none(self):
        reg, _ = self._registry({})
        self.assertIsNone(reg._fetch_from_upstream())


class TestModelRegionsHelper(unittest.TestCase):
    """model_regions() 决定是否给选号加区域约束。"""

    def _ctx(self, regions):
        models = SimpleNamespace(regions_for=lambda m: set(regions.get(m, set())))
        return SimpleNamespace(models=models)

    def test_known_region_specific_model(self):
        from workbuddy_one.gateway.inference import model_regions
        ctx = self._ctx({"gpt-5.4": {"global"}})
        self.assertEqual(model_regions(ctx, {"model": "gpt-5.4"}), {"global"})

    def test_unknown_model_not_restricted(self):
        from workbuddy_one.gateway.inference import model_regions
        ctx = self._ctx({})
        self.assertIsNone(model_regions(ctx, {"model": "some-alias"}))

    def test_missing_model_not_restricted(self):
        from workbuddy_one.gateway.inference import model_regions
        ctx = self._ctx({})
        self.assertIsNone(model_regions(ctx, {}))
        self.assertIsNone(model_regions(ctx, {"model": "   "}))


class TestConfigOverrides(unittest.TestCase):
    """部署级配置（BACKEND / PROXY / WORKBUDDY_EXE）可在 WebUI 里改：DB 值优先于环境变量。

    这三项以前只能改环境变量，用户根本不知道它们存在；现在放到设置页后，
    "留空"必须严格等于"回落环境变量/自动判定"，否则会出现"界面清空了但值还在"的歧义。
    """

    def setUp(self):
        from workbuddy_one.config import config
        self.config = config
        self._saved = dict(config._overrides)
        self.addCleanup(self._restore)

    def _restore(self):
        self.config._overrides = self._saved

    def test_db_value_wins_over_env(self):
        with patch.object(self.config, "backend", "https://from-env.example.com"):
            self.config.load_overrides({"backend": "https://from-db.example.com"})
            self.assertEqual(self.config.backend_effective, "https://from-db.example.com")

    def test_empty_db_value_falls_back_to_env(self):
        with patch.object(self.config, "backend", "https://from-env.example.com"):
            self.config.load_overrides({"backend": ""})
            self.assertEqual(self.config.backend_effective, "https://from-env.example.com")
            # override_of 要如实反映"用户没设过"，设置页据此回显空输入框
            self.assertEqual(self.config.override_of("backend"), "")

    def test_missing_key_falls_back_to_env(self):
        with patch.object(self.config, "proxy", "http://env-host:1080"):
            self.config.load_overrides({})
            self.assertEqual(self.config.proxy_effective, "http://env-host:1080")

    def test_whitespace_treated_as_unset(self):
        with patch.object(self.config, "workbuddy_exe", "/env/exe"):
            self.config.load_overrides({"workbuddy_exe": "   "})
            self.assertEqual(self.config.workbuddy_exe_effective, "/env/exe")

    def test_reload_replaces_previous_overrides(self):
        """保存设置时会整份重载；旧覆盖必须被清掉，否则清空输入框改不回去。"""
        self.config.load_overrides({"backend": "https://a.example.com"})
        self.config.load_overrides({"backend": "https://b.example.com"})
        self.assertEqual(self.config.backend_effective, "https://b.example.com")
        self.config.load_overrides({})
        self.assertEqual(self.config.override_of("backend"), "")

    def test_region_uses_effective_backend(self):
        """区域 host 覆盖必须走 backend_effective，否则 WebUI 里改了不生效。"""
        from workbuddy_one.region import chat_base
        with patch.object(self.config, "backend", ""):
            self.config.load_overrides({"backend": "https://from-db.example.com"})
            self.assertEqual(chat_base("www.codebuddy.cn"), "https://from-db.example.com")
            self.assertEqual(chat_base("www.workbuddy.ai"), "https://from-db.example.com")

    def test_region_override_does_not_touch_catalog_path(self):
        """覆盖的是 host，不是协议路径——国际版目录路径仍按区域走。"""
        from workbuddy_one.region import catalog_path
        with patch.object(self.config, "backend", ""):
            self.config.load_overrides({"backend": "https://from-db.example.com"})
            self.assertEqual(catalog_path("www.workbuddy.ai"), "/v2/enterprises/personal/models")
            self.assertEqual(catalog_path("www.codebuddy.cn"), "/console/enterprises/personal/models")

    def test_proxy_override_reaches_net(self):
        from workbuddy_one import net
        with patch.object(self.config, "proxy", ""):
            self.config.load_overrides({"proxy": "socks5://127.0.0.1:1080"})
            kw = net.client_kwargs()
        self.assertEqual(kw["proxy"], "socks5://127.0.0.1:1080")
        self.assertFalse(kw["trust_env"])

    def test_exe_override_is_first_candidate(self):
        from workbuddy_one import atrest
        with patch.object(self.config, "workbuddy_exe", ""):
            self.config.load_overrides({"workbuddy_exe": "/custom/WorkBuddy"})
            candidates = atrest.official_exe_candidates()
        self.assertEqual(candidates[0], Path("/custom/WorkBuddy"))

    # ---- 整数型覆盖项（在途并发上限）----
    # 这两项与上面三项有一个关键差别：**"0" 是一个有意义的值**（= 不限制），
    # 不是"未设置"。所以不能用 falsy 判断来决定是否回落环境变量，
    # 否则用户显式设的"0 = 不限制"会被静默当成"没设过"而失效。

    def test_int_override_zero_is_a_real_value(self):
        with patch.object(self.config, "max_in_flight", 3):
            self.config.load_overrides({"max_in_flight": "0"})
            self.assertEqual(self.config.max_in_flight_effective, 0)
            # override_of 也要如实回显 "0"，否则设置页输入框会显示成空
            self.assertEqual(self.config.override_of("max_in_flight"), "0")

    def test_int_override_empty_falls_back_to_env(self):
        with patch.object(self.config, "max_in_flight", 3):
            self.config.load_overrides({"max_in_flight": ""})
            self.assertEqual(self.config.max_in_flight_effective, 3)
            self.assertEqual(self.config.override_of("max_in_flight"), "")

    def test_int_override_bad_value_falls_back_not_raises(self):
        """配置坏掉不该让进程起不来：非法值回落环境变量，不抛异常。"""
        with patch.object(self.config, "max_in_flight", 3):
            self.config.load_overrides({"max_in_flight": "abc"})
            self.assertEqual(self.config.max_in_flight_effective, 3)

    def test_global_override_is_separate_from_general(self):
        """国际版那一档必须独立生效——两区域风控档位不同，共用一个值就没意义了。"""
        with patch.object(self.config, "max_in_flight", 3), \
             patch.object(self.config, "max_in_flight_global", 2):
            self.config.load_overrides({"max_in_flight": "8"})
            self.assertEqual(self.config.max_in_flight_effective, 8)
            self.assertEqual(self.config.max_in_flight_global_effective, 2)
            self.config.load_overrides({"max_in_flight_global": "1"})
            self.assertEqual(self.config.max_in_flight_global_effective, 1)
            self.assertEqual(self.config.max_in_flight_effective, 3)


class TestSettingsWhitelist(unittest.TestCase):
    """设置白名单：没登记进 DEFAULT_SETTINGS 的 key 会被 save_settings 静默丢弃。

    keepalive_enabled 就踩过这个坑——WebUI 保存"每日 token 保活"开关后值没落库，
    界面永远显示"开启"、实际根本改不动。新增设置项必须同时登记白名单。
    """

    def _db(self):
        from workbuddy_one.db import Database
        d = _tmp_dir(self)
        db = Database(str(d / "settings.db"))
        self.addCleanup(db._conn.close)
        return db

    def test_keepalive_enabled_persists(self):
        db = self._db()
        self.assertEqual(db.get_settings()["keepalive_enabled"], "1")
        db.save_settings(keepalive_enabled="0")
        self.assertEqual(db.get_settings()["keepalive_enabled"], "0")

    def test_region_and_network_keys_persist(self):
        db = self._db()
        db.save_settings(backend="https://b.example.com",
                         proxy="http://127.0.0.1:7890",
                         workbuddy_exe="/opt/wb")
        s = db.get_settings()
        self.assertEqual(s["backend"], "https://b.example.com")
        self.assertEqual(s["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(s["workbuddy_exe"], "/opt/wb")

    def test_region_network_defaults_are_empty(self):
        """默认必须为空 = 自动判定 / 直连，不能给老部署带进意外的强制值。"""
        db = self._db()
        s = db.get_settings()
        self.assertEqual(s["backend"], "")
        self.assertEqual(s["proxy"], "")
        self.assertEqual(s["workbuddy_exe"], "")

    # ---- P1~P3 新增设置项的落库（同样要登记白名单，否则界面改不动）----

    def test_in_flight_keys_persist(self):
        db = self._db()
        db.save_settings(max_in_flight="5", max_in_flight_global="1")
        s = db.get_settings()
        self.assertEqual(s["max_in_flight"], "5")
        self.assertEqual(s["max_in_flight_global"], "1")

    def test_in_flight_defaults_are_empty_not_zero(self):
        """默认必须是**空串**而不是 "0"。

        空串 = "用户没设过" → 回落环境变量（3 / 2）；"0" = 显式要求不限制。
        默认给 "0" 会让所有部署在升级后静默失去并发保护。
        """
        db = self._db()
        s = db.get_settings()
        self.assertEqual(s["max_in_flight"], "")
        self.assertEqual(s["max_in_flight_global"], "")

    def test_prompt_mode_keys_persist(self):
        db = self._db()
        db.save_settings(prompt_mode="append", prompt_text="你是助手")
        s = db.get_settings()
        self.assertEqual(s["prompt_mode"], "append")
        self.assertEqual(s["prompt_text"], "你是助手")

    def test_prompt_mode_default_is_passthrough(self):
        """默认必须是"不改动"——这个功能对老用户应当是零影响。"""
        db = self._db()
        s = db.get_settings()
        self.assertEqual(s["prompt_mode"], "passthrough")
        self.assertEqual(s["prompt_text"], "")

    def test_makeup_defaults_are_safe(self):
        """补签默认必须是"总开关关 + 演练开"。

        判据链里有一环未验证（活跃地图分数 ≠ 签到状态），而补签花的是**用户自己的卡**。
        两个默认值任一被改成"激进"，都可能在用户没确认判据前就把卡花掉。
        """
        db = self._db()
        s = db.get_settings()
        self.assertEqual(s["makeup_enabled"], "0")
        self.assertEqual(s["makeup_dry_run"], "1")

    def test_travel_defaults_are_safe(self):
        """猫猫旅行默认必须是"总开关关 + 演练开"。

        旅行本身不消耗任何资产（纯收益），但它每天会**替用户向上游写两次状态**
        （派出 / 领取）。默认演练是为了先让人确认状态机判断正确，而不是出于风险——
        但两个默认值任一被改成"激进"，都会让用户在没看过判断结果前就被代跑。
        """
        db = self._db()
        s = db.get_settings()
        self.assertEqual(s["travel_enabled"], "0")
        self.assertEqual(s["travel_dry_run"], "1")


class TestTravelWiring(unittest.TestCase):
    """猫猫旅行的**接线**守卫（配置项登记 + 路由 + 定时注册）。

    这一节存在的理由：本项目**两次**因为"配置项没登记全"导致 WebUI 里改不动、
    功能静默不生效——`DEFAULT_SETTINGS` 白名单外 `save_settings` **静默丢弃**，
    消费端键名拼错又落进"空值=不改"分支，两个方向都是全绿静默失效。

    `test_travel_defaults_are_safe` 只挡住白名单那一侧；**路由 GET/POST 是否接上**
    必须另有守卫，否则键登记了但界面拿不到、也存不进去。
    """

    def test_settings_route_exposes_and_accepts_travel_keys(self):
        import inspect
        from workbuddy_one.routes import settings as settings_route
        src = inspect.getsource(settings_route.register)
        for key in ("travel_enabled", "travel_dry_run"):
            self.assertIn(f'"{key}"', src,
                          f"GET /admin/settings 没返回 {key}，界面拿不到当前值")
            self.assertIn(f'"{key}" in body', src,
                          f"POST /admin/settings 没接 {key}，保存会被静默忽略")

    def test_admin_travel_routes_exist(self):
        import inspect
        from workbuddy_one.routes import accounts as accounts_route
        src = inspect.getsource(accounts_route.register)
        self.assertIn('"/admin/travel"', src, "缺少手动触发入口，演练结果无处可看")
        self.assertIn('"travel": travel', src, "/admin/streak 没带上旅行状态，界面无法显示")

    def test_scheduler_registers_travel_task(self):
        """定时巡检必须真注册进 `_run`——否则"加了功能但永远不会自己跑"。"""
        import inspect
        from workbuddy_one.scheduler import Scheduler
        src = inspect.getsource(Scheduler._run)
        self.assertIn("do_travel", src)
        self.assertIn("_travel_enabled", src)
        # 去重槽位必须精确到小时：只用日期的话当天第二轮会被整体跳过，
        # 而行程要 1~4 小时才到站 —— 早上派出、晚上领取全靠这两个窗口。
        self.assertIn("_last_travel_slot", src)


class TestActiveMapWiring(unittest.TestCase):
    """活跃地图提醒的**接线**守卫（配置项登记 + 路由 + 只读接口）。

    与 `TestTravelWiring` 同构，理由也一样：`DEFAULT_SETTINGS` 是白名单，
    未登记的 key 被 `save_settings` **静默丢弃**，消费端键名拼错又落进"空值=不改"
    分支——两个方向都会全绿静默失效。这个项目已经因此踩过**两次**。
    """

    def test_settings_route_exposes_and_accepts_active_map_keys(self):
        import inspect
        from workbuddy_one.routes import settings as settings_route
        src = inspect.getsource(settings_route.register)
        for key in ("active_map_enabled", "active_map_hour"):
            self.assertIn(f'"{key}"', src,
                          f"GET /admin/settings 没返回 {key}，界面拿不到当前值")
            self.assertIn(f'"{key}" in body', src,
                          f"POST /admin/settings 没接 {key}，保存会被静默忽略")

    def test_default_settings_register_active_map_keys(self):
        """白名单里必须有这两个 key，否则界面保存时被静默丢弃。"""
        from workbuddy_one.db import Database
        defaults = Database.DEFAULT_SETTINGS
        self.assertIn("active_map_enabled", defaults)
        self.assertIn("active_map_hour", defaults)
        # 默认开（全项目唯一）——见 TestActiveMapPilot.test_default_is_on_unlike_makeup_and_travel
        self.assertEqual(defaults["active_map_enabled"], "1")
        self.assertEqual(defaults["active_map_hour"], "23")

    def test_check_route_exists(self):
        import inspect
        from workbuddy_one.routes import accounts as accounts_route
        src = inspect.getsource(accounts_route.register)
        self.assertIn('"/admin/active-map/check"', src, "缺少手动检查入口，判据无处可看")
        self.assertIn("do_active_map_check", src, "路由没有调调度器的检查方法")

    def test_streak_route_reports_todays_heat(self):
        """`/admin/streak` 必须带上**今天**的地图分数。

        它是用户唯一能看到"今天到底亮没亮"的地方（定时任务只推 webhook，界面看不到），
        而 heatmap 那个请求**本来就已经拉过了**，顺手带出来零成本。
        """
        import inspect
        from workbuddy_one.routes import accounts as accounts_route
        src = inspect.getsource(accounts_route.register)
        self.assertIn('"heat_today"', src, "/admin/streak 没带今天的地图分数，界面只能显示空白")

    def test_overview_surfaces_active_map_alert(self):
        """概览页横幅必须读活跃地图快照。

        这是**没配 webhook 时唯一能看到提醒的地方**：`do_active_map_check` 的 webhook
        只在 `alert_webhook_url` 非空时才推，而容器日志用户不会去看。
        少了这一步，整条功能在默认配置下（`alert_webhook_url=""`）就是**完全静默**的 ——
        测试全绿、日志里也有 WARNING，但用户永远不知道。
        """
        import inspect
        from workbuddy_one.routes import overview as overview_route
        src = inspect.getsource(overview_route.register)
        self.assertIn("active_map_snapshot", src,
                      "概览没读活跃地图快照 → 默认配置下提醒完全静默")
        self.assertIn("alerts.append", src, "读了快照却没往 alerts 里加，界面照样看不到")

    def test_overview_never_hits_upstream(self):
        """概览是**高频轮询端点**，绝不能在这里打上游。

        活跃地图判据需要 heatmap + streak 两个上游请求 —— 放进概览就是每秒几十个请求
        （前端每隔几秒拉一次）。所以查询只在调度器里发生（每天一次，或用户手动一次），
        概览只读内存里的快照。这条守的是**架构不变量**，不只是这一次改动。
        """
        import inspect
        from workbuddy_one.routes import overview as overview_route
        src = inspect.getsource(overview_route.register)
        for forbidden in ("fetch_heatmap", "fetch_streak", "fetch_credits",
                          "collect_upstream", "stream_upstream"):
            self.assertNotIn(forbidden, src,
                             f"概览端点里出现了 {forbidden}：高频轮询端点禁止上游网络请求")


class TestCostRegionWiring(unittest.TestCase):
    """成本展示的接线守卫：**区域维度必须真的走到界面**。

    两处成本是完全不同的东西，别混：
      - 模型页的 `credits` = 上游标称的成本系数（倍率），按区域可能不同
        → `credits_by_region` 明细（行为用例见 `TestCreditsByRegion`）；
      - 用量页的「实测积分单价」= 我们自己按真实请求算出的 `积分 / token × 1000`，
        台账是 (账号, 模型) 维度 → 每行必须带 `region`，否则混池时分不清国内/国际。
    """

    def test_cost_route_exists_and_reports_its_window(self):
        import inspect
        from workbuddy_one.routes import usage as usage_route
        src = inspect.getsource(usage_route.register)
        self.assertIn('"/admin/usage/costs"', src, "缺少实测积分单价的只读接口")
        self.assertIn("cost_table", src, "接口没有读台账")
        self.assertIn("ttl_seconds", src, "没告诉前端观测窗口有多长，界面无法解释「为什么这行会消失」")

    def test_cost_route_registered_before_int_converter(self):
        """字面路由必须排在 `{record_id:int}` 之前。

        这个文件里已经因为注册顺序踩过一次（`/admin/usage/filters` 被 `{record_id}`
        吞成 422）。`{record_id:int}` 的 int 转换器让字面路径侥幸不冲突，但顺序是
        文件里唯一显式的防线，别让后来者把新路由随手加到最后。
        """
        import inspect
        from workbuddy_one.routes import usage as usage_route
        src = inspect.getsource(usage_route.register)
        self.assertLess(src.index('"/admin/usage/costs"'), src.index("{record_id:int}"),
                        "字面路由被排到了 {record_id:int} 之后")


if __name__ == "__main__":
    unittest.main()
