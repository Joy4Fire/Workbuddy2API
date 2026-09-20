"""$wbEncrypted 加密登录态 + 容量字段 + onlyReasoning 锁定 的单元测试。

覆盖本次从参考项目吸收的能力：
- atrest 信封识别 / 解密降级 / 错误信息（workbuddy-account-hub v0.6.7）
- 区域判定后缀匹配加固（Buddy2api fingerprint.py）
- 模型容量字段多拼写兼容、max_completion_tokens 裁剪（Buddy2api model_capacity.py）
- onlyReasoning 模型不放开 off 档（cli2api 目录优先规则）

运行：python -m unittest discover -s tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _tmp_dir(case) -> Path:
    """每个用例一个独立临时目录，并登记退出时删除。

    用**系统临时目录**而非仓库内 tests/_tmp/：仓库可能位于网络盘，网络盘没有回收站，
    删除会退化成失败的 SHFileOperationW 并耗时数十秒（见 test_region._tmp_dir 说明）。
    """
    d = Path(tempfile.mkdtemp(prefix="wb_atrest_"))
    case.addCleanup(shutil.rmtree, d, True)
    return d


def _envelope(payload: str = "enc") -> dict:
    """构造一个形状正确的 $wbEncrypted 信封（内容不参与识别，只测形状）。"""
    return {"$wbEncrypted": 1, "envelope": payload}


def _write_auth(case, name: str, *, access="tok", refresh="rtok",
                access_env=None, refresh_env=None) -> Path:
    d = _tmp_dir(case)
    p = d / f"workbuddy-{name}.info"
    p.write_text(json.dumps({
        "account": {"uid": f"uid-{name}", "nickname": f"nick-{name}"},
        "auth": {
            "accessToken": access_env if access_env is not None else access,
            "refreshToken": refresh_env if refresh_env is not None else refresh,
            "expiresAt": 4102444800000,  # 2100 年，避免测试触发刷新
            "domain": "www.codebuddy.cn",
        },
    }, ensure_ascii=False), encoding="utf-8")
    return p


class TestEnvelopeDetect(unittest.TestCase):
    """信封识别：必须零依赖、任何环境都能给出准确判断。"""

    def test_is_envelope_true(self):
        from workbuddy_one.atrest import is_envelope
        self.assertTrue(is_envelope(_envelope()))

    def test_is_envelope_rejects_plain_string(self):
        from workbuddy_one.atrest import is_envelope
        self.assertFalse(is_envelope("eyJhbGciOi..."))

    def test_is_envelope_rejects_missing_envelope_key(self):
        from workbuddy_one.atrest import is_envelope
        self.assertFalse(is_envelope({"$wbEncrypted": 1}))

    def test_is_envelope_rejects_wrong_marker(self):
        from workbuddy_one.atrest import is_envelope
        self.assertFalse(is_envelope({"$wbEncrypted": 2, "envelope": "x"}))

    def test_is_envelope_rejects_scheme_variant(self):
        """带 scheme 的是另一种（非信封）结构，不应被当成信封去解密。"""
        from workbuddy_one.atrest import is_envelope
        self.assertFalse(is_envelope({"$wbEncrypted": 1, "envelope": "x", "scheme": "v2"}))

    def test_is_envelope_rejects_non_dict(self):
        from workbuddy_one.atrest import is_envelope
        for v in (None, 1, [], True):
            self.assertFalse(is_envelope(v))

    def test_envelope_fields_collects_all_locations(self):
        from workbuddy_one.atrest import envelope_fields
        session = {
            "auth": {"accessToken": _envelope(), "refreshToken": _envelope()},
            "account": {"nickname": _envelope(), "phoneNumber": "123"},
            "allAccounts": [{"nickname": _envelope()}],
        }
        self.assertEqual(
            envelope_fields(session),
            ["auth.accessToken", "auth.refreshToken", "account.nickname", "allAccounts.0.nickname"],
        )

    def test_envelope_fields_empty_for_plaintext(self):
        from workbuddy_one.atrest import envelope_fields
        session = {"auth": {"accessToken": "eyJ..."}, "account": {"nickname": "张三"}}
        self.assertEqual(envelope_fields(session), [])

    def test_is_encrypted(self):
        from workbuddy_one.atrest import is_encrypted
        self.assertTrue(is_encrypted({"auth": {"accessToken": _envelope()}}))
        self.assertFalse(is_encrypted({"auth": {"accessToken": "eyJ..."}}))
        self.assertFalse(is_encrypted({}))

    def test_tolerates_malformed_session(self):
        from workbuddy_one.atrest import envelope_fields, is_encrypted
        self.assertEqual(envelope_fields(None), [])
        self.assertEqual(envelope_fields({"auth": "not-a-dict"}), [])
        self.assertFalse(is_encrypted({"allAccounts": "not-a-list"}))


class TestDecryptDegradation(unittest.TestCase):
    """没有官方客户端时必须优雅降级（返回 False），不能抛也不能猜。"""

    def test_decrypt_returns_false_without_exe(self):
        from workbuddy_one import atrest
        session = {"auth": {"accessToken": _envelope()}}
        with mock.patch.object(atrest, "find_official_exe", return_value=None):
            self.assertFalse(atrest.decrypt_session(session))

    def test_decrypt_returns_false_when_nothing_encrypted(self):
        from workbuddy_one import atrest
        session = {"auth": {"accessToken": "eyJ..."}}
        # 无信封字段时压根不该去找 exe
        with mock.patch.object(atrest, "find_official_exe") as m:
            self.assertFalse(atrest.decrypt_session(session))
            m.assert_not_called()

    def test_decrypt_applies_worker_output(self):
        """worker 返回明文时就地替换，且只改内存 dict。"""
        from workbuddy_one import atrest
        session = {
            "auth": {"accessToken": _envelope()},
            "account": {"nickname": _envelope()},
            "allAccounts": [{"nickname": _envelope()}],
        }
        worker_out = {
            "accessToken": {"ok": True, "value": "plain-token"},
            "account.nickname": {"ok": True, "value": "和光同尘"},
            "allAccounts.0.nickname": {"ok": True, "value": "和光同尘"},
        }
        with mock.patch.object(atrest, "find_official_exe", return_value=Path("fake.exe")), \
             mock.patch.object(atrest, "_run_worker", return_value=worker_out):
            self.assertTrue(atrest.decrypt_session(session))
        self.assertEqual(session["auth"]["accessToken"], "plain-token")
        self.assertEqual(session["account"]["nickname"], "和光同尘")
        self.assertEqual(session["allAccounts"][0]["nickname"], "和光同尘")

    def test_decrypt_partial_failure_keeps_envelope(self):
        """单个字段解密失败时保持原样，由上层报错，不写入半成品。"""
        from workbuddy_one import atrest
        session = {"auth": {"accessToken": _envelope(), "refreshToken": _envelope()}}
        worker_out = {
            "accessToken": {"ok": True, "value": "plain"},
            "refreshToken": {"ok": False, "err": "bad tag"},
        }
        with mock.patch.object(atrest, "find_official_exe", return_value=Path("fake.exe")), \
             mock.patch.object(atrest, "_run_worker", return_value=worker_out):
            self.assertTrue(atrest.decrypt_session(session))
        self.assertEqual(session["auth"]["accessToken"], "plain")
        self.assertTrue(atrest.is_envelope(session["auth"]["refreshToken"]))

    def test_error_message_mentions_path_and_hint(self):
        from workbuddy_one import atrest
        with mock.patch.object(atrest, "find_official_exe", return_value=None):
            err = atrest.encrypted_auth_error(Path("a.info"))
        text = str(err)
        self.assertIn("a.info", text)
        self.assertIn("$wbEncrypted", text)
        self.assertIn("WORKBUDDY_EXE", text)


class TestCredentialEncrypted(unittest.TestCase):
    """凭据层：加密登录态必须报明确错误，绝不发出 Bearer {dict}。"""

    def test_plaintext_unaffected(self):
        from workbuddy_one.credentials import CredentialManager
        p = _write_auth(self, "plain")
        mgr = CredentialManager(p)
        self.assertEqual(mgr.get_headers()["Authorization"], "Bearer tok")
        self.assertEqual(mgr.encrypted_fields(), [])
        self.assertFalse(mgr.summary()["auth_encrypted"])

    def test_encrypted_raises_on_get_headers(self):
        from workbuddy_one import atrest
        from workbuddy_one.credentials import CredentialManager
        p = _write_auth(self, "enc", access_env=_envelope(), refresh_env=_envelope())
        mgr = CredentialManager(p)
        with mock.patch.object(atrest, "find_official_exe", return_value=None):
            with self.assertRaises(atrest.EncryptedAuthError):
                mgr.get_headers()

    def test_encrypted_summary_flags_without_decrypting(self):
        """列表/摘要只做展示，不该触发解密子进程。"""
        from workbuddy_one import atrest
        from workbuddy_one.credentials import CredentialManager
        p = _write_auth(self, "enc2", access_env=_envelope())
        mgr = CredentialManager(p)
        with mock.patch.object(atrest, "decrypt_session") as m:
            summary = mgr.summary()
            m.assert_not_called()
        self.assertTrue(summary["auth_encrypted"])
        self.assertEqual(summary["auth_encrypted_fields"], ["auth.accessToken"])

    def test_encrypted_get_headers_ok_after_decrypt(self):
        """能解密时正常返回明文 token。"""
        from workbuddy_one import atrest
        from workbuddy_one.credentials import CredentialManager
        p = _write_auth(self, "enc3", access_env=_envelope())
        mgr = CredentialManager(p)
        with mock.patch.object(atrest, "find_official_exe", return_value=Path("fake.exe")), \
             mock.patch.object(atrest, "_run_worker",
                               return_value={"accessToken": {"ok": True, "value": "decrypted"}}):
            self.assertEqual(mgr.get_headers()["Authorization"], "Bearer decrypted")

    def test_encrypted_file_is_never_rewritten(self):
        """加密文件绝不回写成明文（否则官方客户端认不出自己的登录态）。

        构造一次**成功**的 token 刷新，再断言磁盘文件一个字节都没变——
        新 token 只应存在于内存里。这比"刷新失败所以没写"更能证明短路逻辑生效。
        """
        from workbuddy_one import atrest
        from workbuddy_one.credentials import CredentialManager
        p = _write_auth(self, "enc4", access_env=_envelope())
        before = p.read_text(encoding="utf-8")
        mgr = CredentialManager(p)

        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "code": 0,
            "data": {"accessToken": "new-plain", "expiresIn": 3600},
        }
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value.post.return_value = fake_resp

        with mock.patch.object(atrest, "find_official_exe", return_value=Path("fake.exe")), \
             mock.patch.object(atrest, "_run_worker",
                               return_value={"accessToken": {"ok": True, "value": "decrypted"}}), \
             mock.patch("workbuddy_one.credentials.net.client", return_value=fake_client):
            mgr._ensure_decrypted()
            mgr._refresh()

        # 内存里确实换成了新 token
        self.assertEqual(mgr._session()["auth"]["accessToken"], "new-plain")
        # 但磁盘文件原封不动（仍是加密信封）
        self.assertEqual(p.read_text(encoding="utf-8"), before)
        self.assertTrue(atrest.is_envelope(json.loads(before)["auth"]["accessToken"]))


class TestRegionSuffixMatching(unittest.TestCase):
    """区域判定加固：后缀匹配而非子串包含。"""

    def test_known_hosts(self):
        from workbuddy_one.region import CN, GLOBAL, detect_region
        self.assertIs(detect_region("www.workbuddy.ai"), GLOBAL)
        self.assertIs(detect_region("workbuddy.ai"), GLOBAL)
        self.assertIs(detect_region("www.codebuddy.cn"), CN)
        self.assertIs(detect_region("codebuddy.cn"), CN)

    def test_lookalike_domains_are_not_global(self):
        """仿冒域不能被判成国际版（旧实现用 'workbuddy' in domain，会误判）。"""
        from workbuddy_one.region import CN, detect_region
        for d in ("workbuddy.evil.com", "notworkbuddy.ai.example.com", "workbuddy.ai.evil.com"):
            self.assertIs(detect_region(d), CN, d)

    def test_normalizes_scheme_port_path_case(self):
        from workbuddy_one.region import GLOBAL, detect_region, host_of
        self.assertEqual(host_of("https://WWW.WorkBuddy.AI:443/x/y"), "www.workbuddy.ai")
        self.assertIs(detect_region("https://WWW.WorkBuddy.AI:443/x"), GLOBAL)

    def test_empty_and_unknown_fall_back_to_cn(self):
        from workbuddy_one.region import CN, detect_region
        for d in ("", None, "example.com"):
            self.assertIs(detect_region(d), CN)


class TestCapacityFields(unittest.TestCase):
    """容量字段多拼写兼容 + 只认正整数。"""

    def test_camel_case(self):
        from workbuddy_one.models import _capacity
        self.assertEqual(_capacity({"maxInputTokens": 200000, "maxOutputTokens": 32000}),
                         (200000, 32000))

    def test_snake_case(self):
        from workbuddy_one.models import _capacity
        self.assertEqual(_capacity({"max_input_tokens": 100000, "max_output_tokens": 8000}),
                         (100000, 8000))

    def test_context_window_alias(self):
        from workbuddy_one.models import _capacity
        self.assertEqual(_capacity({"context_window": 64000}), (64000, 0))

    def test_invalid_values_treated_as_unknown(self):
        """0/负数/字符串都算「未提供」——0 会让裁剪逻辑关闭，正是想要的保守行为。"""
        from workbuddy_one.models import _capacity
        self.assertEqual(_capacity({"maxInputTokens": 0, "maxOutputTokens": -1}), (0, 0))
        self.assertEqual(_capacity({"maxInputTokens": "200000"}), (0, 0))
        self.assertEqual(_capacity({}), (0, 0))

    def test_prefers_first_present_spelling(self):
        from workbuddy_one.models import _capacity
        self.assertEqual(_capacity({"maxInputTokens": 1, "max_input_tokens": 2}), (1, 0))


class TestOnlyReasoningLock(unittest.TestCase):
    """onlyReasoning 模型不能关闭思考：不提供 off 档。"""

    def _reg(self, cfg):
        from workbuddy_one.models import ModelRegistry
        reg = ModelRegistry.__new__(ModelRegistry)
        import threading
        reg._lock = threading.Lock()
        reg._reasoning = {"m": cfg}
        return reg

    def test_only_reasoning_excludes_off(self):
        reg = self._reg({"supportedEfforts": ["low", "medium", "high"],
                         "canDisableThinking": True, "onlyReasoning": True})
        self.assertNotIn("off", reg.reasoning_efforts("m"))

    def test_disable_thinking_still_allows_off(self):
        reg = self._reg({"supportedEfforts": ["low", "medium", "high"],
                         "canDisableThinking": True, "onlyReasoning": False})
        self.assertIn("off", reg.reasoning_efforts("m"))

    def test_unknown_model_returns_none(self):
        reg = self._reg({})
        self.assertIsNone(reg.reasoning_efforts("other"))

    def test_off_request_is_lifted_to_lowest(self):
        """客户端仍请求 off 时，降级逻辑把它抬到该模型最低支持档。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        body = {"model": "m", "reasoning_effort": "off"}
        out = normalize_reasoning_effort(body, efforts={"m": ["low", "medium", "high"]})
        self.assertEqual(out["reasoning_effort"], "low")

    def test_none_is_recognized_as_lowest(self):
        """none 与 off 同义（OpenAI 系客户端常用 none），不能被原样透传。"""
        from workbuddy_one.reasoning import normalize_reasoning_effort
        body = {"model": "m", "reasoning_effort": "none"}
        out = normalize_reasoning_effort(body, efforts={"m": ["low", "medium", "high"]})
        self.assertEqual(out["reasoning_effort"], "low")


class TestMaxOutputClamp(unittest.TestCase):
    """max_tokens 与 max_completion_tokens 都要按模型上限裁剪。"""

    class _Ctx:
        def __init__(self, limit):
            self._limit = limit
            self.db = _FakeDb()

        @property
        def models(self):
            return self

        def max_output_tokens(self, model):
            return self._limit

        def reasoning_efforts(self, model):
            return None

    def test_both_fields_clamped(self):
        from workbuddy_one.gateway.inference import enhance_body
        ctx = self._Ctx(4096)
        body = {"model": "m", "max_tokens": 32000, "max_completion_tokens": 99999}
        out = enhance_body(ctx, body)
        self.assertEqual(out["max_tokens"], 4096)
        self.assertEqual(out["max_completion_tokens"], 4096)

    def test_unknown_limit_does_not_clamp(self):
        from workbuddy_one.gateway.inference import enhance_body
        ctx = self._Ctx(None)
        body = {"model": "m", "max_tokens": 32000}
        out = enhance_body(ctx, body)
        self.assertEqual(out["max_tokens"], 32000)

    def test_non_int_left_alone(self):
        from workbuddy_one.gateway.inference import enhance_body
        ctx = self._Ctx(4096)
        body = {"model": "m", "max_tokens": "32000"}
        out = enhance_body(ctx, body)
        self.assertEqual(out["max_tokens"], "32000")


class _FakeDb:
    def get_settings(self):
        return {}


if __name__ == "__main__":
    unittest.main()
