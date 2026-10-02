"""参考项目增量的行为回归：终态、缓存、多模态历史与签到处理中。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from unittest.mock import Mock

import httpx

from workbuddy_one.adapters.anthropic import anthropic_request_to_chat
from workbuddy_one.adapters.responses import ResponsesStreamConverter, responses_request_to_chat
from workbuddy_one import billing
from workbuddy_one.gateway.errors import conv_usage


def feed(converter, *, delta=None, finish=None, usage=None):
    chunk = {'choices': [{'delta': delta or {}, 'finish_reason': finish}]}
    if usage is not None:
        chunk['usage'] = usage
    return converter.feed_line('data: ' + json.dumps(chunk))


class TestResponsesTerminal(unittest.TestCase):
    def test_length_is_incomplete_in_stream_and_json(self):
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': '半截'}, finish='length')
        terminal = converter.finish()
        self.assertIn('response.incomplete', terminal)
        self.assertNotIn('response.completed', terminal)
        response = converter.get_nonstream_response()
        self.assertEqual(response['status'], 'incomplete')
        self.assertEqual(response['incomplete_details'], {'reason': 'max_output_tokens'})
        self.assertEqual(response['output'][0]['status'], 'incomplete')

    def test_content_filter_is_incomplete(self):
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': '部分'}, finish='content_filter')
        self.assertEqual(converter.get_nonstream_response()['incomplete_details']['reason'], 'content_filter')

    def test_stop_completes(self):
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': '完整'}, finish='stop')
        self.assertIn('response.completed', converter.finish())
        self.assertEqual(converter.get_nonstream_response()['status'], 'completed')

    def test_eof_without_finish_must_not_complete(self):
        from workbuddy_one.upstream import UpstreamError
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': '断流'})
        with self.assertRaises(UpstreamError):
            converter.finish()

    def test_done_alone_must_not_complete(self):
        from workbuddy_one.upstream import UpstreamError
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': '断流'})
        converter.feed_line('data: [DONE]')
        with self.assertRaises(UpstreamError):
            converter.finish()

    def test_empty_stream_must_not_complete(self):
        from workbuddy_one.upstream import UpstreamError
        with self.assertRaises(UpstreamError):
            ResponsesStreamConverter().finish()


class TestUsageDetails(unittest.TestCase):
    def response_usage(self, usage):
        converter = ResponsesStreamConverter()
        feed(converter, delta={'content': 'ok'}, finish='stop', usage=usage)
        return converter.get_nonstream_response()['usage']

    def test_nested_cache_and_reasoning_details(self):
        usage = self.response_usage({'prompt_tokens': 100, 'completion_tokens': 20,
                                     'prompt_tokens_details': {'cached_tokens': 80},
                                     'completion_tokens_details': {'reasoning_tokens': 12}})
        self.assertEqual(usage['input_tokens_details']['cached_tokens'], 80)
        self.assertEqual(usage['output_tokens_details']['reasoning_tokens'], 12)
        self.assertEqual(usage['total_tokens'], 120)

    def test_workbuddy_flat_cache_details(self):
        usage = self.response_usage({'prompt_tokens': 100, 'completion_tokens': 20,
                                     'prompt_cache_hit_tokens': 60})
        self.assertEqual(usage['input_tokens_details']['cached_tokens'], 60)

    def test_explicit_zero_cache_beats_fallback(self):
        usage = self.response_usage({'prompt_tokens': 100, 'completion_tokens': 1,
                                     'prompt_tokens_details': {'cached_tokens': 0},
                                     'prompt_cache_hit_tokens': 60})
        self.assertEqual(usage['input_tokens_details']['cached_tokens'], 0)

    def test_converted_usage_preserves_credit(self):
        self.assertEqual(conv_usage({'input_tokens': 10, 'output_tokens': 4, 'credit': 1.2})['credit'], 1.2)


class TestMultimodalHistory(unittest.TestCase):
    def test_responses_preserves_user_order_and_tool_images(self):
        blocks = [{'type': 'input_text', 'text': '前'},
                  {'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='},
                  {'type': 'input_text', 'text': '后'}]
        chat = responses_request_to_chat({'input': [
            {'role': 'user', 'content': blocks},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'shot', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': blocks}]})
        for message in (chat['messages'][0], chat['messages'][-1]):
            self.assertEqual([b['type'] for b in message['content']], ['text', 'image_url', 'text'])
            self.assertEqual(message['content'][1]['image_url']['url'], 'data:image/png;base64,AA==')

    def test_responses_assistant_history_keeps_images(self):
        chat = responses_request_to_chat({'input': [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '历史'}, {'type': 'input_image', 'image_url': 'https://example.com/a.png'}]}]})
        self.assertEqual(chat['messages'][0]['content'][1]['type'], 'image_url')

    def test_anthropic_tool_result_then_user_order(self):
        image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='}}
        chat = anthropic_request_to_chat({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '继续'},
            {'type': 'tool_result', 'tool_use_id': 'call_1', 'content': [image]},
            image, {'type': 'text', 'text': '结束'}]}]})
        messages = chat['messages']
        self.assertEqual([m['role'] for m in messages], ['tool', 'user'])
        self.assertEqual(messages[0]['content'][0]['image_url']['url'], 'data:image/png;base64,AA==')
        self.assertEqual([b['type'] for b in messages[1]['content']], ['text', 'image_url', 'text'])

    def test_anthropic_url_image_supported(self):
        chat = anthropic_request_to_chat({'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}}]}]})
        self.assertEqual(chat['messages'][0]['content'][0]['image_url']['url'], 'https://example.com/a.png')

    def test_text_only_remains_string(self):
        chat = responses_request_to_chat({'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'a'}, {'type': 'input_text', 'text': 'b'}]}]})
        self.assertEqual(chat['messages'][0]['content'], 'ab')


class TestCheckinProcessing(unittest.IsolatedAsyncioTestCase):
    async def run_checkin(self, responses):
        pending = list(responses)
        def handler(request):
            status, payload = pending.pop(0)
            return httpx.Response(status, json=payload)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(billing.net, 'async_client', return_value=client), \
             patch.object(billing, '_billing_headers', return_value={'X-User-Id': 'u1'}), \
             patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await billing.daily_checkin(SimpleNamespace(domain='copilot.tencent.com'))
        return result, pending, sleep

    async def test_processing_retries_before_success(self):
        result, pending, sleep = await self.run_checkin([
            (429, {'code': 10001, 'msg': '请求处理中，请勿重复操作'}),
            (429, {'code': 10001, 'msg': 'request is being processed'}),
            (200, {'code': 0})])
        self.assertTrue(result['ok'])
        self.assertFalse(pending)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2, 5])

    async def test_processing_is_bounded(self):
        result, pending, sleep = await self.run_checkin([(429, {'code': 10001, 'msg': 'request processing'})] * 4)
        self.assertFalse(result['ok'])
        self.assertFalse(pending)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2, 5, 10])

    async def test_regular_429_not_retried(self):
        result, _, sleep = await self.run_checkin([(429, {'code': 6004, 'msg': 'rate limit'})])
        self.assertFalse(result['ok'])
        sleep.assert_not_awaited()

    async def test_checkin_word_is_not_already_signed(self):
        result, _, _ = await self.run_checkin([(400, {'code': 1, 'msg': 'checkin activity disabled'})])
        self.assertFalse(result.get('already', False))

    async def test_already_signed_is_idempotent(self):
        result, _, sleep = await self.run_checkin([(400, {'code': 1, 'msg': '今日已签到'})])
        self.assertTrue(result['already'])
        sleep.assert_not_awaited()

    async def test_cancel_during_delay_propagates(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={'code': 10001, 'msg': '请求处理中'})))
        with patch.object(billing.net, 'async_client', return_value=client), \
             patch.object(billing, '_billing_headers', return_value={}), \
             patch('asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await billing.daily_checkin(SimpleNamespace(domain='copilot.tencent.com'))


class TestResponsesRoute(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, stream, finish, close_on_event=None):
        from fastapi import FastAPI, Request
        from workbuddy_one.routes.inference import register
        from workbuddy_one.gateway import inference
        account = SimpleNamespace(uid='u1')
        ctx = SimpleNamespace(db=None, models=None)
        app = FastAPI()
        register(app, ctx)
        first = 'data: ' + json.dumps({'choices': [{'delta': {'content': '部分'}}]})
        async def tail():
            if finish is not None:
                yield 'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}],
                                            'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                                                      'prompt_cache_hit_tokens': 80, 'credit': 0.2}})
                yield 'data: [DONE]'
        with patch.object(inference, 'check_api_key', return_value='test'), \
             patch.object(inference, 'enhance_body', side_effect=lambda ctx, body: body), \
             patch.object(inference, 'acquire_account', return_value=(account, None)), \
             patch.object(inference, 'open_upstream', new_callable=AsyncMock, return_value=(tail(), first, account)), \
             patch.object(inference, 'log_usage') as log, \
             patch.object(inference, 'penalize', return_value=SimpleNamespace(cooldown=0)), \
             patch('workbuddy_one.routes.inference.with_keepalive', side_effect=lambda gen, interval: gen):
            payload = {'model': 'auto', 'input': 'hi', 'stream': stream}
            if close_on_event:
                async def receive():
                    return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
                request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses', 'headers': []}, receive)
                endpoint = next(r.endpoint for r in app.routes if r.path == '/v1/responses')
                result = await endpoint(request, authorization=None, x_api_key=None)
                parts = []
                async for part in result.body_iterator:
                    parts.append(part.decode())
                    if close_on_event in parts[-1]:
                        await result.body_iterator.aclose()
                        break
                response = SimpleNamespace(text=''.join(parts))
            else:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                    response = await client.post('/v1/responses', json=payload)
        self.assertEqual(log.call_count, 1)
        return response, log.call_args

    async def test_nonstream_incomplete_and_credit_logged(self):
        response, logged = await self.request(stream=False, finish='length')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'incomplete')
        self.assertEqual(response.json()['usage']['input_tokens_details']['cached_tokens'], 80)
        self.assertEqual(logged.args[5], 'incomplete')
        self.assertEqual(logged.kwargs['usage']['credit'], 0.2)

    async def test_stream_incomplete_terminal_and_log(self):
        response, logged = await self.request(stream=True, finish='length')
        self.assertIn('response.incomplete', response.text)
        self.assertNotIn('response.completed', response.text)
        self.assertEqual(logged.args[5], 'incomplete')

    async def test_nonstream_missing_terminal_is_502(self):
        response, logged = await self.request(stream=False, finish=None)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(logged.args[5], 'error')

    async def test_stream_missing_terminal_is_error_not_completed(self):
        response, logged = await self.request(stream=True, finish=None)
        self.assertIn('upstream_error', response.text)
        self.assertNotIn('response.completed', response.text)
        self.assertEqual(logged.args[5], 'error')

    async def test_close_after_terminal_does_not_double_log_or_mark_aborted(self):
        _, logged = await self.request(stream=True, finish='stop', close_on_event='response.completed')
        self.assertEqual(logged.args[5], 'ok')

    async def test_close_before_terminal_logs_aborted(self):
        _, logged = await self.request(stream=True, finish='stop', close_on_event='response.output_text.delta')
        self.assertEqual(logged.args[5], 'aborted')
        self.assertFalse(logged.kwargs['update_pool'])


class TestIncompleteAccountHealth(unittest.TestCase):
    def test_output_limit_does_not_penalize_healthy_account(self):
        from workbuddy_one.gateway.inference import log_usage
        pool = Mock()
        pool.clear_model_cooldown.return_value = False
        pool.record_cost.return_value = None
        db = Mock()
        log_usage(SimpleNamespace(pool=pool, db=db), 'responses', 'auto', SimpleNamespace(uid='u1'),
                  0, 'incomplete', usage={'prompt_tokens': 100, 'completion_tokens': 10, 'credit': 0.2})
        pool.on_success.assert_called_once_with('u1')
        pool.on_failure.assert_not_called()
        self.assertEqual(db.log_usage.call_args.kwargs['status'], 'incomplete')
        self.assertEqual(db.log_usage.call_args.kwargs['credits'], 0.2)
