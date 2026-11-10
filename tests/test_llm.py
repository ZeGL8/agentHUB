import io
import json
import threading
import unittest
from unittest.mock import patch

from agenthub import llm


def response(*events):
    return io.BytesIO(b''.join(('data: ' + (e if isinstance(e, str) else json.dumps(e)) + '\n\n').encode() for e in events))


def delta(text='', finish=None):
    return {'choices': [{'delta': {'content': text}, 'finish_reason': finish}]}


class LLMTests(unittest.TestCase):
    cfg = {'base_url': 'http://localhost:1', 'model': 'mock', 'context_window': 2048}

    def test_incomplete_empty_and_length_streams(self):
        for events in [(delta('partial'),), ('[DONE]',), (delta('partial', 'length'), '[DONE]')]:
            with self.subTest(events=events), patch.object(llm, '_request', return_value=response(*events)):
                with self.assertRaises(llm.LLMError):
                    list(llm.stream(self.cfg, []))

    def test_done_or_stop_is_valid(self):
        for events in [(delta('正文'), '[DONE]'), (delta('正文', 'stop'),)]:
            with patch.object(llm, '_request', return_value=response(*events)):
                self.assertEqual(''.join(llm.stream(self.cfg, [])), '正文')

    def test_complete_rejects_empty_and_truncated(self):
        for text, finish in [('', 'stop'), ('partial', 'length'), ('<think>x</think>', 'stop')]:
            raw = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': finish}]}).encode()
            with patch.object(llm, '_request', return_value=io.BytesIO(raw)):
                with self.assertRaises(llm.LLMError):
                    llm.complete(self.cfg, [])

    def test_cancel_prevents_requests(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(llm, '_resolve', return_value=self.cfg), patch.object(llm, '_request') as request:
            with self.assertRaises(llm.LLMError):
                list(llm.summarize(['text'], cancelled=cancelled))
            request.assert_not_called()

    def test_budget_small_context_and_unknown(self):
        budget = llm.summary_budget(self.cfg)
        self.assertLess(budget['input_chars'] + budget['max_tokens'], 2048)
        self.assertFalse(budget['estimated'])
        self.assertTrue(llm.summary_budget({})['estimated'])
        with self.assertRaises(llm.LLMError):
            llm.summary_budget(self.cfg, 'x' * 3000)

    def test_nonconvergent_map_fails(self):
        with patch.object(llm, '_resolve', return_value=self.cfg), patch.object(llm, 'stream', side_effect=lambda cfg, messages, **kw: iter([messages[-1]['content']])) as request:
            with self.assertRaisesRegex(llm.LLMError, '缩减'):
                list(llm.summarize(['x' * 5000]))
            self.assertEqual(request.call_count, len(llm._chunks(['x' * 5000], llm.summary_budget(self.cfg)['input_chars'])))

    def test_cancel_interrupts_real_local_blocked_stream(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        entered = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
                self.wfile.flush()
                entered.set()
                release.wait(3)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        cancelled = threading.Event()
        errors = []

        def consume():
            try:
                list(llm.stream(dict(self.cfg, base_url='http://127.0.0.1:' + str(server.server_port)), [], cancelled=cancelled))
            except llm.LLMError as e:
                errors.append(str(e))

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            cancelled.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors and '取消' in errors[0])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_cancel_during_map_stops_next_request(self):
        cancelled = threading.Event()

        def cancel_map(*args, **kwargs):
            cancelled.set()
            return iter(['note'])

        with patch.object(llm, '_resolve', return_value=self.cfg), patch.object(llm, 'stream', side_effect=cancel_map) as request:
            with self.assertRaisesRegex(llm.LLMError, '取消'):
                list(llm.summarize(['x' * 5000], cancelled=cancelled))
            self.assertEqual(request.call_count, 1)

    def test_shrinking_map_still_has_round_limit(self):
        def shrink(cfg, messages, **kwargs):
            return iter([messages[-1]['content'][:-1]])

        with patch.object(llm, '_resolve', return_value=self.cfg), patch.object(llm, 'stream', side_effect=shrink), patch.object(llm, 'MAX_MAP_ROUNDS', 2):
            with self.assertRaisesRegex(llm.LLMError, '轮数上限'):
                list(llm.summarize(['x' * 5000]))

    def test_summary_does_not_emit_done_after_broken_stream(self):
        events = []
        with patch.object(llm, '_resolve', return_value=self.cfg), patch.object(llm, '_request', return_value=response(delta('partial'))):
            with self.assertRaises(llm.LLMError):
                for event in llm.summarize(['test']):
                    events.append(event)
        self.assertTrue(any(e['type'] == 'delta' for e in events))
        self.assertNotIn('done', [e['type'] for e in events])

    def test_config_context_is_public_without_key(self):
        cfg = dict(self.cfg, api_key='secret-123456789')
        self.assertEqual(llm.public_config(cfg)['context_window'], 2048)
        self.assertNotIn('api_key', llm.public_config(cfg))

    def test_context_window_saved_and_invalid_rejected(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory, patch.object(llm, 'DATA_DIR', directory), patch.object(llm, 'CONFIG_PATH', os.path.join(directory, 'config.json')):
            self.assertEqual(llm.save_config({'context_window': '8192'})['context_window'], 8192)
            self.assertEqual(llm.load_config()['context_window'], 8192)
            with self.assertRaises(llm.LLMError):
                llm.save_config({'context_window': -1})
            self.assertIsNone(llm.save_config({'context_window': ''})['context_window'])

    def test_long_source_block_repeats_source_and_context_within_budget(self):
        header = '[来源：codex/session/message](#codex/session?message=message)\n会话：标题｜目录：/tmp\n'
        body = '正文' * 300
        chunks = llm._chunks([header + body], 180)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith(header) and len(chunk) <= 180 for chunk in chunks))
        self.assertEqual(''.join(chunk[len(header):] for chunk in chunks), body)

    def test_source_header_larger_than_budget_is_explicit_error(self):
        with self.assertRaisesRegex(llm.LLMError, '来源'):
            llm._chunks(['[来源：tool/session/message](#link)\n会话：' + 'x' * 200 + '\nbody'], 100)

    def test_decisions_template_and_custom_validation(self):
        self.assertIn('决策', llm.summary_instruction('decisions'))
        self.assertEqual(llm.summary_instruction('custom', '  要求  '), '要求')
        with self.assertRaises(llm.LLMError):
            llm.summary_instruction('custom', '   ')


class SummaryJobTests(unittest.TestCase):
    def setUp(self):
        from agenthub import server
        self.server = server
        self.jobs = patch.object(server, '_summary_jobs', {})
        self.history = patch.object(server, '_summary_recent', {})
        self.jobs.start()
        self.history.start()
        self.addCleanup(self.jobs.stop)
        self.addCleanup(self.history.stop)

    def test_cancel_before_registration_is_remembered(self):
        self.assertTrue(self.server.cancel_summary('early'))
        with self.assertRaisesRegex(ValueError, '取消'):
            self.server.register_summary('early')
        self.assertEqual(self.server._summary_jobs, {})

    def test_duplicate_active_and_finished_ids_rejected(self):
        event = self.server.register_summary('same')
        with self.assertRaisesRegex(ValueError, '重复'):
            self.server.register_summary('same')
        self.assertIs(self.server._summary_jobs['same'], event)
        self.server.finish_summary('same')
        with self.assertRaisesRegex(ValueError, '重复'):
            self.server.register_summary('same')

    def test_cancel_active_and_expiration(self):
        event = self.server.register_summary('active')
        self.server.cancel_summary('active')
        self.assertTrue(event.is_set())
        with patch.object(self.server.time, 'monotonic', return_value=1):
            self.server.cancel_summary('old')
        with patch.object(self.server.time, 'monotonic', return_value=1000):
            self.assertFalse(self.server.register_summary('old').is_set())

    def test_preview_matches_execution_budget(self):
        cfg = dict(LLMTests.cfg, context_window=8192)
        body = {'template': 'custom', 'prompt': '  请总结  '}
        with patch.object(self.server, 'summary_blocks', return_value=['test']), patch.object(self.server, '_items', return_value=[1]), patch.object(llm, '_resolve', return_value=cfg), patch.object(llm, 'stream', return_value=iter(['summary'])) as stream:
            preview = self.server.summary_preview(body)
            list(llm.summarize(['test'], body['template'], body['prompt']))
        self.assertEqual(preview['budget'], llm.summary_budget(cfg, '请总结'))
        self.assertEqual(stream.call_args.kwargs['max_tokens'], preview['budget']['max_tokens'])

    def test_local_routes_cancel_before_start_and_reject_duplicate(self):
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.server.Handler)
        address = '127.0.0.1:' + str(server.server_port)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()

        def post(path, body):
            request = urllib.request.Request('http://' + address + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=2) as result:
                    return result.status, result.read().decode()
            except urllib.error.HTTPError as error:
                return error.code, error.read().decode()

        try:
            with patch.object(self.server, 'ALLOWED_HOSTS', {address}), patch.object(self.server, 'summary_blocks', return_value=['synthetic']), patch.object(llm, 'summarize') as summarize:
                self.assertEqual(post('/api/summary/cancel', {'request_id': 'early-route'})[0], 200)
                self.assertEqual(post('/api/summary', {'request_id': 'early-route'})[0], 400)
                summarize.assert_not_called()
                summarize.side_effect = lambda *args, **kwargs: (event for event in [{'type': 'done'}])
                self.assertEqual(post('/api/summary', {'request_id': 'once'})[0], 200)
                self.assertEqual(post('/api/summary', {'request_id': 'once'})[0], 400)
                self.assertEqual(summarize.call_count, 1)
                self.assertEqual(post('/api/summary/cancel', {'request_id': []})[0], 400)
        finally:
            server.shutdown()
            server.server_close()
