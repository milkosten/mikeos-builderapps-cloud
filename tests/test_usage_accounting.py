"""Offline regression tests: output budgets and every provider's usage reach accounting."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from server import gpu


class Response:
    status_code = 200
    def __init__(self, data, lines=()):
        self.data, self.lines = data, lines
    def raise_for_status(self): pass
    def json(self): return self.data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def aiter_lines(self):
        for line in self.lines: yield line


class Client:
    def __init__(self, response):
        self.response, self.calls = response, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def post(self, url, **kwargs):
        self.calls.append(kwargs); return self.response
    def stream(self, *args, **kwargs):
        self.calls.append(kwargs); return self.response


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_chat_respects_small_budget(self):
        c = Client(Response({'choices': [{'message': {'content': 'ok'}}],
                             'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}))
        with patch.object(gpu.httpx, 'AsyncClient', return_value=c), patch.object(gpu, '_report_tokens') as report:
            self.assertEqual(await gpu._openrouter_chat([], None, .2, 1600, 5, 1), 'ok')
        self.assertEqual(c.calls[0]['json']['max_tokens'], 1600)
        self.assertEqual(report.call_count, 1)

    async def test_openrouter_stream_usage_and_budget(self):
        c = Client(Response({}, ['data: '+json.dumps({'choices': [{'delta': {'content': 'ok'}}]}),
                                'data: '+json.dumps({'choices': [], 'usage': {'prompt_tokens': 10}}),
                                'data: [DONE]']))
        with patch.object(gpu.httpx, 'AsyncClient', return_value=c), patch.object(gpu, '_report_usage') as report:
            self.assertEqual([x async for x in gpu._openrouter_stream([], .2, 1000, 5)], ['ok'])
        self.assertEqual(report.call_count, 1)
        self.assertEqual(c.calls[0]['json']['max_tokens'], 1000)
        self.assertTrue(c.calls[0]['json']['stream_options']['include_usage'])

    async def test_ollama_stream_final_counts(self):
        c = Client(Response({}, [json.dumps({'done': True, 'prompt_eval_count': 12, 'eval_count': 4})]))
        with patch.object(gpu.httpx, 'AsyncClient', return_value=c), patch.object(gpu, '_report_usage') as report:
            self.assertEqual([x async for x in gpu._ollama_stream([], .2, 8192, 1000, 5, '1m')], [])
        self.assertEqual(report.call_args.args[0], {'prompt_tokens': 12, 'completion_tokens': 4, 'cost': 0})

    async def test_telemetry_cached_and_reasoning(self):
        c = Client(Response({}))
        with patch.dict(os.environ, {'SVC_TELEMETRY_KEY': 'test'}), patch.object(gpu.httpx, 'AsyncClient', return_value=c):
            gpu._report_tokens({'prompt_tokens': 100, 'completion_tokens': 20,
                                'prompt_tokens_details': {'cached_tokens': 60},
                                'completion_tokens_details': {'reasoning_tokens': 5}}, 'test-model')
            await asyncio.gather(*gpu._telemetry_tasks)
        self.assertEqual(c.calls[0]['json']['usage'], {'input_tokens': 100, 'output_tokens': 20,
                         'cached_input_tokens': 60, 'reasoning_output_tokens': 5})

    async def test_anthropic_cache_input_is_additive(self):
        c = Client(Response({}))
        with patch.dict(os.environ, {'SVC_TELEMETRY_KEY': 'test'}), patch.object(gpu.httpx, 'AsyncClient', return_value=c):
            gpu._report_tokens({'input_tokens': 10, 'output_tokens': 2,
                                'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 20}, 'claude')
            await asyncio.gather(*gpu._telemetry_tasks)
        self.assertEqual(c.calls[0]['json']['usage']['input_tokens'], 90)


if __name__ == '__main__': unittest.main()
