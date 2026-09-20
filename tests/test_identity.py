"""出站身份（使用端 / User-Agent / 请求 id）单元测试。

覆盖：按区域选身份 UA、USER_AGENT 逃生口、身份头与聊天头的内容与边界、
聊天链路确实把身份头带到了上游。

背景（2026-09-20 真实双账号实测，见 workbuddy_one/identity.py 的模块注释）：
上游会从 UA 里解析客户端版本，解析不出直接 400 code=12403；同一个账号换 UA
会拿到不同的模型目录；不提供 X-Request-ID 时上游会替我们编一个 id。

运行：python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workbuddy_one import identity, region  # noqa: E402
from workbuddy_one.credentials import CredentialManager  # noqa: E402


class TestRegionUserAgent(unittest.TestCase):
    """UA 必须按区域给官方身份，不能自报网关名。"""

    def test_global_uses_desktop_identity(self):
        self.assertEqual(region.user_agent("www.workbuddy.ai"), "WorkBuddy/5.4.2")

    def test_cn_uses_cli_identity(self):
        self.assertEqual(region.user_agent("www.codebuddy.cn"), "CLI/2.139.0 CodeBuddy/2.139.0")

    def test_unknown_domain_falls_back_to_cn(self):
        # 未知识别不出区域时回落国内版，与 detect_region 的既有约定一致
        self.assertEqual(region.user_agent(""), region.CN.client_user_agent)
        self.assertEqual(region.user_agent("example.com"), region.CN.client_user_agent)

    def test_never_advertises_gateway_name(self):
        # 回归护栏：曾经是 "Workbuddy2API/0.4"，会被上游 12403 拒
        for domain in ("www.workbuddy.ai", "www.codebuddy.cn", ""):
            self.assertNotIn("workbuddy2api", region.user_agent(domain).lower())

    def test_env_override_wins(self):
        with patch.object(region.config, "user_agent", "MyCustom/9.9"):
            self.assertEqual(region.user_agent("www.workbuddy.ai"), "MyCustom/9.9")
            self.assertEqual(region.user_agent("www.codebuddy.cn"), "MyCustom/9.9")


class TestRequestId(unittest.TestCase):
    def test_format_is_32_lowercase_hex(self):
        rid = identity.new_request_id()
        self.assertEqual(len(rid), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in rid))

    def test_unique_per_call(self):
        ids = {identity.new_request_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)


class TestIdentityHeaders(unittest.TestCase):
    def test_identity_headers_carry_region_ua_and_product(self):
        h = identity.identity_headers("www.workbuddy.ai")
        self.assertEqual(h["User-Agent"], "WorkBuddy/5.4.2")
        self.assertEqual(h["X-Product"], "SaaS")
        self.assertEqual(h["X-IDE-Type"], "CLI")
        self.assertEqual(h["X-Requested-With"], "XMLHttpRequest")

    def test_identity_headers_have_no_request_id(self):
        # 身份头是静态的，不能混进每请求变化的 id（否则无法复用/比对）
        self.assertNotIn("X-Request-ID", identity.identity_headers("www.codebuddy.cn"))

    def test_chat_headers_add_request_id_and_agent(self):
        h = identity.chat_headers("www.workbuddy.ai")
        self.assertEqual(len(h["X-Request-ID"]), 32)
        self.assertEqual(h["X-Agent-Type"], "main")
        self.assertEqual(h["X-Agent-Intent"], "craft")

    def test_chat_headers_send_message_id(self):
        # 实测：X-Conversation-Message-ID 决定 SSE 里的消息 id（不回显则上游自己编）
        h = identity.chat_headers("www.workbuddy.ai")
        self.assertEqual(h["X-Conversation-Message-ID"], h["X-Request-ID"])

    def test_chat_headers_do_not_send_conversation_scope_ids(self):
        """刻意不发会话级 id：实测它们单独发送对响应没有任何影响，而
        `X-Conversation-ID` 很可能正是上游 prompt cache 的归属键——每请求随机
        有打散缓存、白烧额度的风险（见 identity.py 模块注释）。
        这条用例防止有人"顺手补齐"。"""
        h = identity.chat_headers("www.workbuddy.ai")
        for key in ("X-Conversation-ID", "X-Session-ID", "X-Conversation-Request-ID"):
            self.assertNotIn(key, h)

    def test_chat_headers_differ_per_call(self):
        a = identity.chat_headers("www.workbuddy.ai")
        b = identity.chat_headers("www.workbuddy.ai")
        self.assertNotEqual(a["X-Request-ID"], b["X-Request-ID"])


class TestCredentialHeaders(unittest.TestCase):
    """凭据层拼出来的头必须带上官方身份，且鉴权字段不被覆盖。"""

    def _headers(self, domain: str) -> dict:
        mgr = CredentialManager(Path("unused.json"))
        auth = {"domain": domain, "accessToken": "tok-123"}
        account = {"uid": "u-1", "enterpriseId": "e-1"}
        return mgr._build_headers_from(auth, account)

    def test_carries_region_identity(self):
        self.assertEqual(self._headers("www.workbuddy.ai")["User-Agent"], "WorkBuddy/5.4.2")
        self.assertEqual(self._headers("www.codebuddy.cn")["User-Agent"],
                         "CLI/2.139.0 CodeBuddy/2.139.0")

    def test_keeps_auth_and_routing_fields(self):
        h = self._headers("www.workbuddy.ai")
        self.assertEqual(h["Authorization"], "Bearer tok-123")
        self.assertEqual(h["X-Domain"], "www.workbuddy.ai")
        self.assertEqual(h["X-User-Id"], "u-1")
        self.assertEqual(h["X-Enterprise-Id"], "e-1")
        self.assertEqual(h["X-Tenant-Id"], "e-1")
        self.assertEqual(h["Content-Type"], "application/json")

    def test_encrypted_token_still_raises(self):
        # 身份改造不能顺手放过加密信封：非字符串 token 必须显式报错
        from workbuddy_one import atrest
        mgr = CredentialManager(Path("unused.json"))
        auth = {"domain": "www.workbuddy.ai", "accessToken": {"$wbEncrypted": 1, "envelope": "x"}}
        with self.assertRaises(atrest.EncryptedAuthError):
            mgr._build_headers_from(auth, {"uid": "u-1"})


class _FakeResp:
    status_code = 200

    def __init__(self, sink: dict):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        yield "data: [DONE]"


class _FakeClient:
    def __init__(self, sink: dict):
        self._sink = sink

    def stream(self, method, url, headers=None, json=None):
        self._sink["headers"] = dict(headers or {})
        self._sink["url"] = url
        return _FakeResp(self._sink)


class TestStreamUpstreamSendsIdentity(unittest.IsolatedAsyncioTestCase):
    """聊天出站必须真的带上身份头——只改 credentials 层不够。"""

    async def _capture(self, headers: dict) -> dict:
        sink: dict = {}
        with patch("workbuddy_one.upstream._get_client", lambda: _FakeClient(sink)):
            from workbuddy_one.upstream import stream_upstream
            async for _ in stream_upstream(headers, {"model": "m"}):
                pass
        return sink

    async def test_chat_request_carries_identity(self):
        sink = await self._capture({"X-Domain": "www.workbuddy.ai", "Authorization": "Bearer t"})
        h = sink["headers"]
        self.assertEqual(h["User-Agent"], "WorkBuddy/5.4.2")
        self.assertEqual(h["X-Product"], "SaaS")
        self.assertEqual(len(h["X-Request-ID"]), 32)
        # 原有鉴权/路由字段不能被身份头挤掉
        self.assertEqual(h["Authorization"], "Bearer t")
        self.assertIn("workbuddy.ai", sink["url"])

    async def test_cn_account_gets_cn_identity(self):
        sink = await self._capture({"X-Domain": "www.codebuddy.cn", "Authorization": "Bearer t"})
        self.assertEqual(sink["headers"]["User-Agent"], "CLI/2.139.0 CodeBuddy/2.139.0")
        self.assertIn("copilot.tencent.com", sink["url"])

    async def test_caller_headers_are_not_mutated(self):
        # stream_upstream 内部是拷贝后再补头，不能改到调用方传进来的 dict
        original = {"X-Domain": "www.workbuddy.ai"}
        await self._capture(original)
        self.assertEqual(original, {"X-Domain": "www.workbuddy.ai"})


if __name__ == "__main__":
    unittest.main()
