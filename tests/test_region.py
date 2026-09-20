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


if __name__ == "__main__":
    unittest.main()
