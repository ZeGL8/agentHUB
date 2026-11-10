import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub.readers import normalize
from agenthub.readers import opencode


def block(**kw):
    return kw


class OpenCodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / 'opencode.db')
        self._build()

    def _build(self):
        with sqlite3.connect(self.db) as c:
            c.executescript('''
                CREATE TABLE session_v2(id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
                    title TEXT, directory TEXT, model TEXT,
                    time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
                CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
                    seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
            ''')
            c.executemany('INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?)', [
                ('ses_parent', 'p', None, '主任务', '/work', '{"id":"deepseek-v4.1-flash-free"}', 1000, 5000, None),
                ('ses_child', 'p', 'ses_parent', '子代理 (@explore subagent)', '/work', '{"id":"m"}', 2000, 6000, None),
                ('ses_archived', 'p', None, '归档但保留', '/work', '{"id":"m"}', 1000, 9000, 4000),
            ])
            msg = lambda mid, sid, t, seq, tc, data: (mid, sid, t, seq, tc, tc, json.dumps(data))
            c.executemany('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', [
                msg('m1', 'ses_parent', 'user', 1, 1000, {'text': '  你好  ', 'time': {'created': 1000}}),
                # 流式写入：reasoning 逐 token 的相邻碎片必须合成一段思考
                msg('m2', 'ses_parent', 'assistant', 2, 1100, {'time': {'created': 1100}, 'content': [
                    {'type': 'reasoning', 'text': 'Let'}, {'type': 'reasoning', 'text': ' me'},
                    {'type': 'reasoning', 'text': ' think'}, {'type': 'text', 'text': '答案是 42'},
                    {'type': 'tool', 'id': 'c1', 'name': 'read',
                     'state': {'status': 'completed', 'input': {'path': '/x'},
                               'content': [{'type': 'text', 'text': 'file body'}]}}]}),
                msg('m3', 'ses_parent', 'assistant', 3, 1200, {'time': {'created': 1200}, 'content': [
                    {'type': 'reasoning', 'text': 'again'},
                    {'type': 'tool', 'name': 'shell', 'callID': 'c2',
                     'state': {'status': 'error', 'input': 'ls', 'error': 'boom'}}]}),
                msg('m4', 'ses_parent', 'system', 4, 1300, {'text': '工具已变更', 'time': {'created': 1300}}),
                msg('m5', 'ses_child', 'user', 1, 2000, {'text': '子任务输入', 'time': {'created': 2000}}),
            ])

    def reader(self):
        return opencode.OpenCodeReader()

    def test_scan_metadata_tree_and_counts(self):
        with patch.object(opencode, 'DB', self.db):
            records = {r.session_id: r for r in self.reader().scan()}
        self.assertEqual(set(records), {'ses_parent', 'ses_child', 'ses_archived'})
        parent = records['ses_parent']
        self.assertEqual(parent.title, '主任务')
        self.assertEqual(parent.cwd, '/work')
        self.assertEqual(parent.extra['model'], 'deepseek-v4.1-flash-free')
        self.assertEqual(parent.extra['origin'], '')
        # 1 user + 1 assistant 正文（m3 只有思考与工具，不计正文）；2 次工具调用
        self.assertEqual((parent.n_messages, parent.n_tool_calls), (2, 2))
        child = records['ses_child']
        self.assertEqual(child.extra['origin'], 'subagent')
        self.assertEqual(child.extra['parent_session_id'], 'ses_parent')
        # 归档不是删除：仍然可见
        self.assertTrue(records['ses_archived'].extra['archived'])
        with patch.object(opencode, 'DB', self.db):
            self.assertEqual(self.reader().visible_session_ids(),
                             {'ses_parent', 'ses_child', 'ses_archived'})

    def test_read_messages_maps_blocks_and_merges_fragments(self):
        with patch.object(opencode, 'DB', self.db):
            msgs = normalize(self.reader().read_messages('ses_parent'))
        user = [m for m in msgs if m.kind == 'text' and m.role == 'user']
        self.assertEqual([m.text for m in user], ['你好'])  # strip + 无注入
        thinking = [m.text for m in msgs if m.kind == 'thinking']
        self.assertEqual(thinking, ['Let me think', 'again'])
        calls = [(m.tool_name, m.call_id, m.tool_args) for m in msgs if m.kind == 'tool_call']
        self.assertEqual(calls[0], ('read', 'c1', '{"path": "/x"}'))
        self.assertEqual(calls[1][0], 'shell')
        results = [(m.tool_name, m.call_id, m.tool_output) for m in msgs if m.kind == 'tool_result']
        self.assertEqual(results[0], ('read', 'c1', 'file body'))
        self.assertEqual(results[1][2], 'boom')  # error 作为结果正文
        event = [m.text for m in msgs if m.kind == 'event']
        self.assertEqual(event, ['工具已变更'])
        # 稳定且唯一的消息标识（多选 / 节选依赖它）
        ids = [m.message_id for m in msgs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_chat_view_keeps_only_final_reply(self):
        with patch.object(opencode, 'DB', self.db):
            msgs = normalize(self.reader().read_messages('ses_parent'))
        chat = [m.text for m in msgs if m.kind == 'text' and (m.role == 'user' or m.final)]
        self.assertEqual(chat[0], '你好')
        self.assertIn('答案是 42', chat)  # 该轮最后的助手正文
        self.assertNotIn('file body', chat)

    def test_reasoning_embedded_in_text_is_deduplicated(self):
        # 部分 provider 把同一段推理既写成 reasoning 碎片、又内嵌进正文的 <think>：
        # 读取器应合并成一段思考，并去掉正文里重复的 <think>。
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)',
                      ('m9', 'ses_parent', 'assistant', 9, 1400, 1400, json.dumps({
                          'time': {'created': 1400}, 'content': [
                              {'type': 'reasoning', 'text': 'Let'},
                              {'type': 'text', 'text': '<think>Let me plan the work</think>\n开始执行'},
                              {'type': 'reasoning', 'text': ' me plan'},
                              {'type': 'reasoning', 'text': ' the work'},
                          ]})))
        with patch.object(opencode, 'DB', self.db):
            msgs = normalize(self.reader().read_messages('ses_parent'))
        mine = [m for m in msgs if m.message_id.startswith('m9')]
        thinking = [m.text for m in mine if m.kind == 'thinking']
        self.assertEqual(thinking, ['Let me plan the work'])  # 合并成一段，不重复
        self.assertEqual([m.text for m in mine if m.kind == 'text'], ['开始执行'])

    def test_missing_schema_is_reported_as_no_sessions(self):
        empty = str(self.root / 'empty.db')
        sqlite3.connect(empty).close()
        with patch.object(opencode, 'DB', empty):
            self.assertEqual(self.reader().scan(), [])
            self.assertIsNone(self.reader().visible_session_ids())
            self.assertEqual(self.reader().read_messages('ses_parent'), [])


if __name__ == '__main__':
    unittest.main()
