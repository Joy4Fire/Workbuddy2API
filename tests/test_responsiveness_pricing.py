"""外部推理期间管理端响应，以及跨区域低价优先的行为回归。"""
import asyncio
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from workbuddy_one.config import config
from workbuddy_one.db import Database
from workbuddy_one.gateway import inference
from workbuddy_one.gateway.session import SessionRouter
from workbuddy_one.models import ModelRegistry
from workbuddy_one.pool import AccountPool


class TestPriceRouting(unittest.TestCase):
    def setUp(self):
        self.pool = AccountPool({'cn': None, 'global': None})
        self.cn, self.global_ = self.pool.accounts
        self.cn.region_id = 'cn'
        self.global_.region_id = 'global'
        self.models = ModelRegistry(self.pool)
        self.models._models = [{'id': 'm', 'credits_by_region': {'cn': 3, 'global': 1}}]
        self.models._model_regions = {'m': {'cn', 'global'}}
        self.ctx = SimpleNamespace(pool=self.pool, models=self.models, session_router=SessionRouter())

    def choose(self):
        return inference.pick_account(self.ctx, regions={'cn', 'global'}, model='m').uid

    def test_lower_global_price_always_wins(self):
        self.assertEqual({self.choose() for _ in range(30)}, {'global'})

    def test_lower_cn_price_always_wins(self):
        self.models._models[0]['credits_by_region'] = {'cn': 0.1, 'global': 4}
        self.assertEqual({self.choose() for _ in range(30)}, {'cn'})

    def test_expiry_and_priority_do_not_override_price(self):
        self.cn.priority = 999
        self.cn.credits_expire_at = time.time() + 3600
        self.assertEqual(self.choose(), 'global')

    def test_unavailable_low_price_falls_back(self):
        self.pool.set_enabled('global', False)
        self.assertEqual(self.choose(), 'cn')

    def test_low_price_exhausted_falls_back(self):
        self.global_.credits_remaining = 0
        self.assertEqual(self.choose(), 'cn')

    def test_low_price_cooling_falls_back(self):
        self.pool.on_failure('global', 300)
        self.assertEqual(self.choose(), 'cn')

    def test_low_price_model_blocked_falls_back(self):
        self.pool.cooldown_model('global', 'm', 300)
        self.assertEqual(self.choose(), 'cn')

    def test_busy_low_price_falls_back(self):
        self.global_.capacity_limit = 1
        self.global_.in_flight = 1
        self.assertEqual(self.choose(), 'cn')

    def test_sticky_high_price_moves_to_cheaper_account(self):
        body = {'model': 'm', 'prompt_cache_key': 'session-a'}
        key = inference.session.extract_session_key(body)
        self.ctx.session_router.bind(key, 'cn')
        acc, _ = inference.acquire_account(self.ctx, body)
        self.assertEqual(acc.uid, 'global')
        self.assertEqual(self.ctx.session_router.lookup(key, self.pool), 'global')

    def test_sticky_same_price_is_preserved(self):
        self.models._models[0]['credits_by_region'] = {'cn': 1, 'global': 1}
        body = {'model': 'm', 'prompt_cache_key': 'session-a'}
        self.ctx.session_router.bind(inference.session.extract_session_key(body), 'cn')
        self.assertEqual(inference.acquire_account(self.ctx, body)[0].uid, 'cn')

    def test_price_change_releases_old_sticky_account(self):
        body = {'model': 'm', 'prompt_cache_key': 'session-a'}
        self.assertEqual(inference.acquire_account(self.ctx, body)[0].uid, 'global')
        self.models._models[0]['credits_by_region'] = {'cn': 0, 'global': 1}
        self.assertEqual(inference.acquire_account(self.ctx, body)[0].uid, 'cn')

    def test_missing_tariff_not_treated_as_free(self):
        self.models._models[0]['credits_by_region'] = {'cn': 2}
        self.assertEqual(self.choose(), 'cn')

    def test_paid_observations_are_compared_when_tariffs_equal(self):
        self.models._models[0]['credits_by_region'] = {'cn': 1, 'global': 1}
        self.pool.record_cost('cn', 'm', 4, 1000)
        self.pool.record_cost('global', 'm', 2, 1000)
        self.assertEqual(self.choose(), 'global')

    def test_paid_observations_are_compared_without_tariffs(self):
        self.models._models = []
        self.pool.record_cost('cn', 'm', 2, 1000)
        self.pool.record_cost('global', 'm', 4, 1000)
        self.assertEqual(self.choose(), 'cn')

    def test_invalid_tariffs_are_excluded_and_snapshot_is_copied(self):
        self.models._models[0]['credits_by_region'] = {'cn': float('nan'), 'global': 0, 'other': -1,
                                                      'bad': '0', 'infinity': float('inf'), 'bool': False}
        prices = self.models.prices_for('m')
        self.assertEqual(prices, {'global': 0})
        prices['global'] = 8
        self.assertEqual(self.models.prices_for('m'), {'global': 0})

    def test_missing_credit_does_not_create_false_free_observation(self):
        db = Mock()
        inference.log_usage(SimpleNamespace(pool=self.pool, db=db), 'responses', 'm', self.cn,
                            time.time(), 'ok', usage={'prompt_tokens': 100, 'completion_tokens': 20})
        self.assertNotIn('m', self.cn.model_costs)


class TestResponsiveInference(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_protocols_keep_management_responsive_during_authentication(self):
        from workbuddy_one.routes.inference import register
        for path, payload in (
            ('/v1/chat/completions', {'model':'m','messages':[{'role':'user','content':'hi'}],'stream':False}),
            ('/v1/messages', {'model':'m','messages':[{'role':'user','content':'hi'}],'stream':False}),
            ('/v1/responses', {'model':'m','input':'hi','stream':False}),
        ):
            with self.subTest(path=path):
                entered, release = threading.Event(), threading.Event()
                def authenticate(*args):
                    entered.set(); release.wait(2); return 'test'
                async def tail():
                    yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                    yield 'data: [DONE]'
                app = FastAPI(); register(app, SimpleNamespace(db=None,models=None,pool=Mock()))
                @app.get('/health')
                async def health():return {'status':'ok'}
                account = SimpleNamespace(uid='a')
                with patch.object(inference,'check_api_key',side_effect=authenticate), \
                     patch.object(inference,'enhance_body',side_effect=lambda ctx,body:body), \
                     patch.object(inference,'acquire_account',return_value=(account,None)), \
                     patch.object(inference,'get_headers',return_value={}), \
                     patch.object(inference,'open_upstream',new=AsyncMock(return_value=(tail(),'data: {"choices":[{"delta":{"content":"OK"}}]}',account))), \
                     patch.object(inference,'log_usage'), \
                     patch.object(config,'ratelimit',False), \
                     patch('workbuddy_one.routes.inference.collect_upstream',new=AsyncMock(return_value={'choices':[{'message':{'content':'OK'}}]})):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
                        task=asyncio.create_task(c.post(path,json=payload))
                        try:
                            for _ in range(300):
                                if entered.is_set():break
                                await asyncio.sleep(0.005)
                            self.assertTrue(entered.is_set())
                            self.assertFalse(task.done())
                            self.assertEqual((await asyncio.wait_for(c.get('/health'),0.5)).status_code,200)
                        finally:release.set()
                        self.assertEqual((await task).status_code,200)

    async def test_health_responds_while_headers_are_refreshing(self):
        entered, release = threading.Event(), threading.Event()
        def headers():
            entered.set()
            release.wait(2)
            return {}
        async def upstream(*args):
            yield 'data: first'
        account = SimpleNamespace(uid='a', mgr=SimpleNamespace(get_headers=headers))
        ctx = SimpleNamespace(pool=Mock())
        app = FastAPI()
        @app.get('/health')
        async def health():
            return {'status': 'ok'}
        with patch.object(config, 'ratelimit', False), patch.object(inference, 'stream_upstream', upstream):
            task = asyncio.create_task(inference.open_upstream_once(ctx, account, {}))
            try:
                for _ in range(300):
                    if entered.is_set(): break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done(), '同步刷新阻塞了事件循环，直到刷新结束才执行验收')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
                    response = await asyncio.wait_for(c.get('/health'), 0.5)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_slow_usage_write_does_not_block_loop(self):
        from workbuddy_one.routes.inference import _usage_logger
        entered, release = threading.Event(), threading.Event()
        def slow_write(*args, **kwargs):
            entered.set(); release.wait(2)
        with patch.object(inference, 'log_usage', side_effect=slow_write):
            task = asyncio.create_task(_usage_logger(None, 'test')('responses', 'm', None, 0, 'ok'))
            try:
                for _ in range(300):
                    if entered.is_set(): break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done())
                await asyncio.wait_for(asyncio.sleep(0), 0.5)
            finally:
                release.set(); await task

    async def test_cancel_during_write_never_starts_second_record(self):
        from workbuddy_one.routes.inference import _usage_logger
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_write(*args, **kwargs):
            entered.set(); release.wait(2); finished.set()
        with patch.object(inference, 'log_usage', side_effect=slow_write) as write:
            logger = _usage_logger(None, 'test')
            task = asyncio.create_task(logger('responses', 'm', None, 0, 'ok'))
            try:
                for _ in range(300):
                    if entered.is_set(): break
                    await asyncio.sleep(0.005)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                await logger('responses', 'm', None, 0, 'aborted')
                self.assertEqual(write.call_count, 1)
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 2)


class TestDatabaseReadIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wb_responsive_')
        self.db = Database(str(Path(self.tmp.name)/'test.db'))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db._conn.close)

    def test_reads_continue_when_writer_lock_is_held(self):
        self.db.log_usage(model='m', protocol='chat', account_uid='a', input_tokens=12)
        with self.db._lock:
            self.assertEqual(self.db.usage_count(), 1)
            self.assertEqual(self.db.usage_summary()['total_tokens'], 12)
            self.assertIn('checkin_hours', self.db.get_settings())

    def test_reader_sees_only_committed_snapshot(self):
        self.db.log_usage(model='m', protocol='chat', account_uid='a', input_tokens=12)
        with self.db._lock:
            self.db._conn.execute('BEGIN IMMEDIATE')
            self.db._conn.execute("INSERT INTO usage_logs(model,protocol,total_tokens) VALUES ('m','chat',100)")
            self.assertEqual(self.db.usage_count(), 1)
            self.db._conn.rollback()

    def test_stats_queries_use_covering_indexes(self):
        with self.db._reader() as c:
            queries = ['SELECT SUM(total_tokens) FROM usage_logs',
                       'SELECT model,SUM(total_tokens),SUM(credits) FROM usage_logs GROUP BY model',
                       'SELECT protocol,SUM(total_tokens) FROM usage_logs GROUP BY protocol',
                       'SELECT app_name,SUM(total_tokens),SUM(credits) FROM usage_logs GROUP BY app_name']
            for query in queries:
                plan = ' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN '+query))
                self.assertIn('COVERING INDEX', plan)

    def test_v7_migration_preserves_complete_content_and_backups(self):
        content = '图片/base64/思考链' * 1000
        self.db.log_usage(model='m', protocol='chat', account_uid='a', input_content=content)
        self.db._conn.execute('PRAGMA user_version=7'); self.db._conn.commit()
        other = Database(str(self.db.path))
        try:
            self.assertEqual(other._user_version(), 8)
            self.assertEqual(other.migrated_from, 7)
            saved = other.usage_recent(1)[0]['input_content']
            self.assertEqual(hashlib.sha256(saved.encode()).digest(), hashlib.sha256(content.encode()).digest())
            self.assertTrue(list((self.db.path.parent/'backups').glob('*.bak')))
        finally:other._conn.close()
