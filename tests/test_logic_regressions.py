"""逻辑审查发现的并发、换号、终态和设置一致性边界。"""
import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from workbuddy_one.config import config
from workbuddy_one.db import Database
from workbuddy_one.gateway import inference
from workbuddy_one.gateway.sse import with_keepalive
from workbuddy_one.pool import AccountPool
from workbuddy_one.routes.inference import register
from workbuddy_one.routes.settings import register as register_settings
from workbuddy_one.upstream import UpstreamError

FIRST = 'data: {"choices":[{"delta":{"content":"partial"}}]}'
PAYLOAD = {'model':'m','messages':[{'role':'user','content':'synthetic'}]}

async def tail(finish='stop'):
    if finish:
        yield 'data: ' + json.dumps({'choices':[{'delta':{},'finish_reason':finish}],
                                    'usage':{'prompt_tokens':10,'completion_tokens':2,'credit':0.25}})
        yield 'data: [DONE]'

class TestSlotOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_does_not_send_or_release_another_slot(self):
        pool = AccountPool({'a':None}); a = pool.accounts[0]; a.capacity_limit = 1
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        async def upstream(headers, body):
            sent.append(body['id'])
            if body['id'] == 1:
                entered.set(); await release.wait()
            yield FIRST
        with patch.object(config,'ratelimit',False), patch.object(inference,'get_headers',return_value={}), patch.object(inference,'stream_upstream',upstream):
            first = asyncio.create_task(inference.open_upstream_once(SimpleNamespace(pool=pool),a,{'id':1}))
            await entered.wait()
            second = asyncio.create_task(inference.open_upstream_once(SimpleNamespace(pool=pool),a,{'id':2}))
            await asyncio.sleep(0.1)
            self.assertEqual(sent,[1]); self.assertEqual(a.in_flight,1)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError): await second
            self.assertEqual(a.in_flight,1)
            release.set(); it, _ = await first; await it.aclose()
            self.assertEqual(a.in_flight,0)

    async def test_cancel_active_connection_closes_stream_and_releases(self):
        pool = AccountPool({'a':None}); a = pool.accounts[0]; a.capacity_limit = 1
        entered, closed = asyncio.Event(), asyncio.Event()
        async def upstream(*args):
            try:
                entered.set(); await asyncio.Event().wait(); yield FIRST
            finally: closed.set()
        with patch.object(config,'ratelimit',False), patch.object(inference,'get_headers',return_value={}), patch.object(inference,'stream_upstream',upstream):
            task=asyncio.create_task(inference.open_upstream_once(SimpleNamespace(pool=pool),a,{}))
            await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed.is_set()); self.assertEqual(a.in_flight,0)

    async def test_busy_slot_timeout_does_not_release(self):
        pool=AccountPool({'a':None}); a=pool.accounts[0]; a.capacity_limit=1; pool.acquire_slot('a')
        upstream=Mock()
        with patch.object(config,'ratelimit',False), patch.object(inference,'get_headers',return_value={}), patch.object(inference,'stream_upstream',upstream), patch.object(inference,'time',SimpleNamespace(monotonic=Mock(side_effect=[0,11]))):
            with self.assertRaises(HTTPException) as error:
                await inference.open_upstream_once(SimpleNamespace(pool=pool),a,{})
        self.assertEqual(error.exception.status_code,503); self.assertEqual(a.in_flight,1); upstream.assert_not_called()

    def test_unlimited_slots_remain_owned_across_limit_change(self):
        pool=AccountPool({'a':None}); a=pool.accounts[0]; a.capacity_limit=0
        pool.acquire_slot('a'); a.capacity_limit=1
        self.assertFalse(pool.acquire_slot('a'))
        pool.release_slot('a'); self.assertEqual(a.in_flight,0)

class TestHealthyFallback(unittest.TestCase):
    def test_exhausted_account_is_rejected(self):
        pool=AccountPool({'a':None}); pool.accounts[0].credits_remaining=0
        with self.assertRaises(HTTPException) as error:
            inference.pick_account(SimpleNamespace(pool=pool,models=None),model='m')
        self.assertEqual(error.exception.status_code,503)

    def test_degraded_account_is_rejected(self):
        pool=AccountPool({'a':None}); pool.accounts[0].degrade_until=time.time()+600
        self.assertIsNone(pool.pick(model='m'))

    def test_short_cooldown_still_allows_probe(self):
        pool=AccountPool({'a':None}); a=pool.accounts[0]; a.cooldown_until=time.time()+5
        self.assertIs(inference.pick_account(SimpleNamespace(pool=pool,models=None),model='m'),a)

    def test_retry_excludes_initial_account(self):
        pool=AccountPool({'a':None,'b':None})
        self.assertEqual(pool.pick(model='m',exclude={'a'}).uid,'b')

class TestRetryAndTerminals(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, *, finish='stop', stream=False, second_error=None, close_terminal=False):
        pool=AccountPool({'a':None,'b':None}); a,b=pool.accounts; db=Mock()
        ctx=SimpleNamespace(pool=pool,models=None,db=db)
        app=FastAPI(); register(app,ctx)
        payload={**PAYLOAD,'stream':stream}
        if path.endswith('responses'): payload={'model':'m','input':'synthetic','stream':stream}
        it=tail(finish)
        attempts=AsyncMock(side_effect=[UpstreamError(429,b'{"message":"rate limit"}',{'Retry-After':'300'}), second_error if second_error else (it,FIRST)])
        with ExitStack() as stack:
            for target, kwargs in [(config,{'desensitize':False}), (inference,{'check_api_key':Mock(return_value='test'),'enhance_body':Mock(side_effect=lambda ctx,b:b),'acquire_account':Mock(return_value=(a,None))})]:
                stack.enter_context(patch.multiple(target,**kwargs))
            stack.enter_context(patch.object(inference,'open_upstream_once',attempts))
            if close_terminal:
                stack.enter_context(patch('workbuddy_one.routes.inference.with_keepalive',side_effect=lambda gen,interval:gen))
                async def receive(): return {'type':'http.request','body':json.dumps(payload).encode(),'more_body':False}
                request=Request({'type':'http','method':'POST','path':path,'headers':[]},receive)
                endpoint=next(r.endpoint for r in app.routes if r.path==path)
                response=await endpoint(request,authorization=None,x_api_key=None)
                async for part in response.body_iterator:
                    text=part.decode() if isinstance(part,bytes) else part
                    if ('finish_reason' in text or 'message_stop' in text):
                        await response.body_iterator.aclose(); break
            else:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                    response=await client.post(path,json=payload)
        return response,db,pool,attempts

    async def test_nonstream_chat_rotates_and_logs_success_to_actual_account(self):
        response,db,pool,attempts=await self.call('/v1/chat/completions')
        self.assertEqual(response.status_code,200); self.assertEqual(attempts.await_count,2)
        row=db.log_usage.call_args.kwargs
        self.assertEqual(row['account_uid'],'b'); self.assertEqual(row['credits'],0.25)
        self.assertEqual(row['output_content'],'partial')

    async def test_second_business_error_is_attributed_to_actual_account_all_protocols(self):
        for path in ('/v1/chat/completions','/v1/messages','/v1/responses'):
            with self.subTest(path=path):
                response,db,pool,_=await self.call(path,second_error=UpstreamError(400,b'{"code":11115,"message":"prompt too long"}'))
                self.assertEqual(response.status_code,400); self.assertEqual(db.log_usage.call_args.kwargs['account_uid'],'b')
                self.assertEqual(pool.accounts[0].failure_count,1); self.assertEqual(pool.accounts[1].failure_count,0)

    async def test_second_transport_error_does_not_penalize_first_again(self):
        for path in ('/v1/chat/completions','/v1/messages','/v1/responses'):
            with self.subTest(path=path):
                response,db,pool,_=await self.call(path,second_error=httpx.ConnectError('synthetic'))
                self.assertEqual(response.status_code,502); self.assertEqual(db.log_usage.call_args.kwargs['account_uid'],'b')
                self.assertEqual([a.failure_count for a in pool.accounts],[1,1])
                self.assertGreater(pool.accounts[0].cooldown_until-time.time(),200)

    async def test_truncated_chat_stream_emits_error_and_logs_error(self):
        response,db,_,_=await self.call('/v1/chat/completions',finish=None,stream=True)
        self.assertIn('upstream stream ended without a finish reason',response.text)
        self.assertEqual(db.log_usage.call_args.kwargs['status'],'error')
        self.assertEqual(db.log_usage.call_args.kwargs['output_content'],'partial')

    async def test_truncated_messages_returns_502(self):
        response,db,_,_=await self.call('/v1/messages',finish=None)
        self.assertEqual(response.status_code,502); self.assertEqual(db.log_usage.call_args.kwargs['status'],'error')

    async def test_length_is_incomplete_all_protocols(self):
        for path in ('/v1/chat/completions','/v1/messages','/v1/responses'):
            with self.subTest(path=path):
                response,db,_,_=await self.call(path,finish='length')
                self.assertEqual(response.status_code,200); self.assertEqual(db.log_usage.call_args.kwargs['status'],'incomplete')

    async def test_close_after_chat_terminal_keeps_single_success(self):
        _,db,_,_=await self.call('/v1/chat/completions',stream=True,close_terminal=True)
        self.assertEqual(db.log_usage.call_count,1); self.assertEqual(db.log_usage.call_args.kwargs['status'],'ok')

    async def test_close_after_messages_terminal_keeps_single_success(self):
        _,db,_,_=await self.call('/v1/messages',stream=True,close_terminal=True)
        self.assertEqual(db.log_usage.call_count,1); self.assertEqual(db.log_usage.call_args.kwargs['status'],'ok')

class TestSettingsAndStatistics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Database(str(Path(self.tmp.name)/'temporary.db'))
        self.addCleanup(self.tmp.cleanup); self.addCleanup(self.db._conn.close)

    async def test_invalid_later_field_does_not_save_earlier_field(self):
        self.db.save_settings(checkin_hours='9,21')
        app=FastAPI(); register_settings(app,SimpleNamespace(db=self.db,pool=Mock()))
        with patch.object(config,'load_overrides') as reload_config:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                response=await client.post('/admin/settings',json={'checkin_hours':'8','credit_refresh_min':0})
        self.assertEqual(response.status_code,400); self.assertEqual(self.db.get_settings()['checkin_hours'],'9,21')
        reload_config.assert_not_called()

    async def test_numeric_zero_capacity_is_saved_as_unlimited(self):
        pool=Mock(); pool.all_accounts.return_value=[]
        app=FastAPI(); register_settings(app,SimpleNamespace(db=self.db,pool=pool))
        with patch.object(config,'load_overrides'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                response=await client.post('/admin/settings',json={'max_in_flight':0})
        self.assertEqual(response.status_code,200); self.assertEqual(self.db.get_settings()['max_in_flight'],'0')

    def test_sql_failure_rolls_back_whole_settings_batch(self):
        self.db.save_settings(checkin_hours='9,21')
        self.db._conn.execute("CREATE TRIGGER fail_setting BEFORE INSERT ON settings WHEN NEW.key='credit_refresh_min' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(Exception): self.db.save_settings(checkin_hours='8',credit_refresh_min='5')
        self.assertEqual(self.db.get_settings()['checkin_hours'],'9,21')
        self.assertFalse(self.db._conn.in_transaction)

    def test_content_statistics_matches_unicode_content(self):
        self.db.log_usage(model='m',protocol='chat',account_uid='a',input_content='中文'*2200,output_content='😀'*3,reasoning_content='abc')
        self.db.log_usage(model='m',protocol='chat',account_uid='a')
        self.assertEqual(self.db.usage_content_stats(),{'rows':2,'total_chars':4406,'over_limit_rows':1})

class TestBoundedKeepalive(unittest.IsolatedAsyncioTestCase):
    async def test_slow_consumer_applies_backpressure_and_close_finishes(self):
        produced=[]; closed=asyncio.Event()
        async def fast():
            try:
                for i in range(1000): produced.append(i); yield str(i)
            finally: closed.set()
        wrapped=with_keepalive(fast(),1)
        await wrapped.__anext__(); await asyncio.sleep(0.05)
        self.assertLessEqual(len(produced),34)
        await asyncio.wait_for(wrapped.aclose(),1)
        self.assertTrue(closed.is_set())

    async def test_empty_preflight_is_502_and_slot_released(self):
        pool=AccountPool({'a':None}); a=pool.accounts[0]
        with patch.object(config,'ratelimit',False), patch.object(inference,'get_headers',return_value={}), patch.object(inference,'stream_upstream',side_effect=lambda *args:tail(None)):
            with self.assertRaises(UpstreamError) as error:
                await inference.open_upstream_once(SimpleNamespace(pool=pool),a,{})
        self.assertEqual(error.exception.status_code,502); self.assertEqual(a.in_flight,0)
