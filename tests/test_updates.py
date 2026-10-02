"""版本查询必须只读、限流、并发合并，且网络失败不能伪装成最新。"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from workbuddy_one import __version__
from workbuddy_one.updates import UpdateChecker, parse_version, SOURCE_URL


class TestUpdateParsing(unittest.TestCase):
    def test_version_compares_numerically(self):
        self.assertGreater(parse_version('__version__ = "0.10.0"')[1],
                           parse_version('__version__ = "0.9.0"')[1])

    def test_pre_release_or_code_is_not_a_stable_version(self):
        for text in ('__version__ = "0.7.0rc1"', '__version__ = get_version()', '<html>502</html>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_version(text)


class TestUpdateChecker(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_get_cached_never_opens_network(self):
        with patch('workbuddy_one.updates.net.async_client') as client:
            checker = UpdateChecker()
            self.assertEqual(checker.cached()['status'], 'unchecked')
            client.assert_not_called()

    async def test_current_version_no_credentials_and_cached(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), SOURCE_URL)
            self.assertNotIn('Authorization', request.headers)
            return httpx.Response(200, text=f'__version__ = "{__version__}"\n')
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(handler)) as factory:
            result = await checker.check()
            self.assertEqual(result['status'], 'up_to_date')
            self.assertEqual(await checker.check(), result)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(requests), 1)

    async def test_update_available(self):
        checker = UpdateChecker()
        major, minor, _ = map(int, __version__.split('.'))
        newer = f'{major}.{minor + 1}.0'
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                lambda request: httpx.Response(200, text=f'__version__ = "{newer}"'))):
            result = await checker.check()
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['latest_version'], newer)

    async def test_local_ahead(self):
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                lambda request: httpx.Response(200, text='__version__ = "0.0.0"'))):
            self.assertEqual((await checker.check())['status'], 'local_ahead')

    async def test_failure_is_not_up_to_date_and_has_cooldown(self):
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                lambda request: httpx.Response(503))) as factory:
            result = await checker.check()
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['latest_version'])
            self.assertEqual(await checker.check(), result)
            self.assertEqual(factory.call_count, 1)

    async def test_failed_recheck_preserves_prior_version_but_marks_unavailable(self):
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                lambda request: httpx.Response(200, text=f'__version__ = "{__version__}"'))):
            await checker.check()
        with patch('workbuddy_one.updates.time.monotonic', return_value=checker._last_attempt + 21601), \
             patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                 lambda request: httpx.Response(404))):
            result = await checker.check()
        self.assertEqual(result['latest_version'], __version__)
        self.assertEqual(result['status'], 'unavailable')

    async def test_oversized_or_invalid_source_rejected(self):
        for body in ('x' * 32769, '<html>not a version</html>'):
            checker = UpdateChecker()
            with self.subTest(size=len(body)), patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                    lambda request: httpx.Response(200, text=body))):
                self.assertEqual((await checker.check())['status'], 'unavailable')

    async def test_concurrent_checks_merge(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, text=f'__version__ = "{__version__}"')
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(handler)):
            tasks = [asyncio.create_task(checker.check()) for _ in range(3)]
            await entered.wait()
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(calls, 1)
        self.assertEqual(results[0], results[2])

    async def test_cancel_propagates_without_caching_success(self):
        def handler(request):
            raise asyncio.CancelledError()
        checker = UpdateChecker()
        with patch('workbuddy_one.updates.net.async_client', return_value=self.client(handler)):
            with self.assertRaises(asyncio.CancelledError):
                await checker.check()
        self.assertEqual(checker.cached()['status'], 'unchecked')

    async def test_management_route_get_is_cache_only_post_checks(self):
        from fastapi import FastAPI
        from types import SimpleNamespace
        from workbuddy_one.routes.updates import register
        checker = UpdateChecker()
        app = FastAPI()
        register(app, SimpleNamespace(updates=checker))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            with patch('workbuddy_one.updates.net.async_client', return_value=self.client(
                    lambda request: httpx.Response(200, text=f'__version__ = "{__version__}"'))) as factory:
                self.assertEqual((await client.get('/admin/updates')).json()['status'], 'unchecked')
                factory.assert_not_called()
                self.assertEqual((await client.post('/admin/updates/check')).json()['status'], 'up_to_date')
                self.assertEqual(factory.call_count, 1)
