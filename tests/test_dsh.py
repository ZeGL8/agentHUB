import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zstandard
from agenthub.readers.dsh import DshReader


class DshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch('agenthub.readers.dsh.ROOT', str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, dirname, events, version=''):
        p = self.root / 'project' / dirname / ('session' + version + '.jsonl.zstd')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(zstandard.ZstdCompressor().compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode()))
        return str(p)

    def test_versions_and_relationships(self):
        ask = {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'hi'}]}}
        self.write('a', [{'type': 'session', 'id': 'a'}, ask])
        self.write('a', [{'type': 'session', 'id': 'a', 'origin': 'subagent', 'parentSession': 'p', 'delegationDepth': 2}, ask], '.v3')
        self.write('fork', [{'type': 'session', 'id': 'fork', 'parentSession': 'p', 'seedLength': 4}, ask])
        records = {r.session_id: r for r in DshReader().scan()}
        self.assertEqual(len(records), 2)
        self.assertIn('.v3.', records['a'].file_path)
        self.assertEqual(records['a'].extra['parent_session_id'], 'p')
        self.assertEqual(records['a'].extra['delegation_depth'], 2)
        self.assertEqual(records['fork'].extra['parent_session_id'], 'p')
        self.assertEqual(records['fork'].extra['origin'], 'fork')

    def test_full_messages_and_unique_calls(self):
        long = 'hello\n' * 500
        p = self.write('a', [
            {'type': 'session', 'id': 'a'},
            {'type': 'turn/start', 'data': {'turn': 2}},
            {'type': 'user/message', 'seq': 1, 'data': {'id': 'u', 'source': {'kind': 'agent'}, 'content': [{'type': 'text', 'text': long}]}},
            {'type': 'assistant/message', 'seq': 2, 'data': {'turn': 2, 'step': 3, 'message': {'id': 'm', 'content': [{'type': 'tool-call', 'id': 'c', 'name': 'exec', 'arguments': {'cmd': long}}]}}},
            {'type': 'tool/call', 'seq': 3, 'data': {'turn': 2, 'step': 3, 'callId': 'c', 'name': 'exec', 'arguments': {'cmd': long}}},
            {'type': 'tool/call', 'seq': 4, 'data': {'callId': 'standalone', 'name': 'exec', 'arguments': long}},
            {'type': 'tool/result', 'seq': 5, 'data': {'turn': 2, 'step': 3, 'message': {'id': 'r', 'content': [{'type': 'tool-result', 'toolCallId': 'c', 'content': [{'type': 'text', 'text': long}]}]}}},
            {'type': 'goal/change', 'seq': 6, 'data': {'goal': long}},
        ])
        reader = DshReader()
        self.assertEqual(reader.scan()[0].n_tool_calls, 2)
        msgs = reader.read_messages(p)
        self.assertEqual(len([m for m in msgs if m.kind == 'tool_call']), 2)
        call = next(m for m in msgs if m.call_id == 'c' and m.kind == 'tool_call')
        self.assertEqual(json.loads(call.tool_args)['cmd'], long)
        self.assertTrue(call.turn_id.startswith('2#'))  # 轮次号 + 启动序号（区分重试）
        self.assertIn('step:3', call.source)
        self.assertEqual(next(m for m in msgs if m.kind == 'tool_result').tool_output, long)
        agent_msg = next(m for m in msgs if m.source.endswith(':agent'))
        self.assertEqual((agent_msg.role, agent_msg.kind, agent_msg.text), ('system', 'event', long))
        self.assertTrue(any(m.source.startswith('goal/change') for m in msgs))

    def test_noise_filtered_and_children_linked(self):
        child = '11111111-2222-3333-4444-555555555555'
        p = self.write('a', [
            {'type': 'session', 'id': 'a'},
            {'type': 'user/message', 'data': {'source': {'kind': 'plugin'}, 'content': [{'type': 'text', 'text': 'runtime context'}]}},
            {'type': 'user/message', 'data': {'source': {'kind': 'agent-instructions'}, 'content': [{'type': 'text', 'text': 'workspace rules'}]}},
            {'type': 'agent/inbox/spliced', 'data': {'inserted': []}},
            {'type': 'subagent/catalog', 'data': {'childId': child, 'label': '读测试'}},
            {'type': 'user/message', 'data': {'source': {'kind': 'agent-message'},
                                              'content': [{'type': 'text', 'text': f'Agent {child} sent a message:'},
                                                          {'type': 'text', 'text': '# 报告'}]}},
            {'type': 'user/message', 'data': {'source': {'kind': 'subagent-report', 'senderSessionId': 'session-x'},
                                              'content': [{'type': 'text', 'text': 'done'}]}},
        ])
        events = [m for m in DshReader().read_messages(p)]
        self.assertEqual([m.link for m in events], [child, child, 'session-x'])
        self.assertEqual(events[0].text, '创建子任务：读测试')
        self.assertFalse(any('runtime context' in m.text or 'workspace rules' in m.text for m in events))

    def test_title_without_think(self):
        self.write('a', [{'type': 'session', 'id': 'a'},
                         {'type': 'session/title', 'data': {'title': '<think>The user asks: x'}},
                         {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': '真实问题'}]}}])
        self.assertEqual(DshReader().scan()[0].title, '真实问题')

    def test_corruption_is_reported_and_no_old_fallback(self):
        ask = {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'hi'}]}}
        self.write('a', [{'type': 'session', 'id': 'a'}, ask])
        p = self.write('a', [], '.v3')
        Path(p).write_bytes(b'broken')
        self.write('b', [{'type': 'session', 'id': 'b'}, ask])
        reader = DshReader()
        # 新版坏了：不回退到同目录旧版，只报告；其他会话照常
        self.assertEqual([r.session_id for r in reader.scan()], ['b'])
        self.assertTrue(any(p in e for e in reader.errors))

    def test_invalid_json_is_reported(self):
        p = self.write('a', [])
        Path(p).write_bytes(zstandard.ZstdCompressor().compress(b'{bad}\n'))
        with self.assertRaises(ValueError):
            DshReader().read_messages(p)

    def test_restarted_turns_and_descriptor(self):
        p = self.write('a', [
            {'type': 'session', 'id': 'a'},
            {'type': 'subagent/descriptor', 'data': {'label': 'reader'}},
            {'type': 'turn/start', 'data': {'turn': 1}},
            {'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text', 'text': 'first'}]}}},
            {'type': 'turn/start', 'data': {'turn': 1}},
            {'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text', 'text': 'second'}]}}},
        ])
        messages = [m for m in DshReader().read_messages(p) if m.kind == 'text']
        self.assertNotEqual(messages[0].turn_id, messages[1].turn_id)
        self.assertEqual(DshReader().scan()[0].extra['label'], 'reader')

    def test_v4_desktop_format_preferred(self):
        # 桌面版 DSH 改为 session.v4.jsonl.zstd；同目录同时存在 v3 时必须选 v4
        self.write('a', [{'type': 'session', 'version': 3, 'id': 'a'}], '.v3')
        p = self.write('a', [
            {'type': 'session', 'version': 4, 'id': 'a', 'createdAt': 1000, 'cwd': '/w'},
            {'type': 'turn/start', 'data': {'turn': 1}},
            {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'hi'}]}},
            {'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text', 'text': 'v4 reply'}]}}},
        ], '.v4')
        records = DshReader().scan()
        self.assertEqual(len(records), 1)
        self.assertIn('.v4.', records[0].file_path)
        self.assertEqual(records[0].cwd, '/w')
        self.assertIn('v4 reply', [m.text for m in DshReader().read_messages(p)])

    def test_v4_injection_sources_filtered(self):
        p = self.write('a', [
            {'type': 'session', 'id': 'a'},
            {'type': 'user/message', 'data': {'source': {'kind': 'runtime-context'}, 'content': [{'type': 'text', 'text': 'runtime ctx'}]}},
            {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'real ask'}]}},
            {'type': 'user/message', 'data': {'source': {'kind': 'skill-catalog'}, 'content': [{'type': 'text', 'text': 'skills'}]}},
            {'type': 'user/message', 'data': {'source': {'kind': 'compact-checkpoint'}, 'content': [{'type': 'text', 'text': 'checkpoint'}]}},
        ], '.v4')
        self.assertEqual([m.text for m in DshReader().read_messages(p)], ['real ask'])

    def test_blank_session_is_skipped(self):
        # 桌面版会给“开了但从没发过话”的工作区留下一个只含初始配置事件的空会话（其自身元数据也标记 blank）。
        # 这种没有任何对话/协作内容的会话不应建索引。
        self.write('blank', [
            {'type': 'session', 'id': 'blank'},
            {'type': 'permission/preset', 'data': {'preset': 'workspace-write'}},
            {'type': 'sandbox/mode', 'data': {'mode': 'workspace-write'}},
            {'type': 'approval/policy', 'data': {'policy': 'ask'}},
            {'type': 'session/end-seed', 'data': {}},
        ], '.v4')
        self.write('real', [
            {'type': 'session', 'id': 'real'},
            {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'hi'}]}},
        ], '.v4')
        records = {r.session_id: r for r in DshReader().scan()}
        self.assertEqual(list(records), ['real'])

    def test_session_with_only_event_is_kept(self):
        # 只有协作事件、没有对话的会话不算空（可能是有意义的子任务记录），仍要保留
        self.write('evt', [
            {'type': 'session', 'id': 'evt'},
            {'type': 'subagent/catalog', 'data': {'childId': 'c', 'label': '读文件'}},
        ], '.v4')
        self.assertEqual([r.session_id for r in DshReader().scan()], ['evt'])
