import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub.model import Message, SessionRecord, is_chat
from agenthub.readers import normalize
from agenthub import indexer, server


class WorkspaceTests(unittest.TestCase):
    def test_native_turns_and_stable_identity(self):
        a = Message(role='assistant', text='first')
        b = Message(role='assistant', text='<think>reason</think>second')
        a.turn_id, b.turn_id = '1', '2'
        a.message_id, b.message_id = 'a', 'b'
        result = normalize([a, b])
        self.assertEqual([m.text for m in result if is_chat(m)], ['first', 'second'])
        self.assertEqual(len({m.message_id for m in result}), 3)
        self.assertEqual(result[-1].turn_id, '2')
        original = normalize([Message(role='user', text='one'), Message(role='assistant', text='two')])
        extended = normalize([Message(role='user', text='one'), Message(role='assistant', text='two'), Message(role='user', text='three')])
        self.assertEqual([m.message_id for m in original], [m.message_id for m in extended[:2]])

    def test_identity_dedup_and_deleted_descendants(self):
        records = [SessionRecord(tool='dsh', session_id='a', title='same', updated_at=1),
                   SessionRecord(tool='dsh', session_id='a', title='new', updated_at=2),
                   SessionRecord(tool='dsh', session_id='b', title='same'),
                   SessionRecord(tool='dsh', session_id='dead', extra={'deleted': True}),
                   SessionRecord(tool='dsh', session_id='child', extra={'origin': 'subagent', 'parent_session_id': 'dead'}),
                   SessionRecord(tool='claude', session_id='a', title='same')]
        out = indexer.visible_records(records)
        self.assertEqual({(r.tool, r.session_id) for r in out}, {('dsh', 'a'), ('dsh', 'b'), ('claude', 'a')})
        self.assertEqual(next(r.title for r in out if r.tool == 'dsh' and r.session_id == 'a'), 'new')

    def test_failed_scan_does_not_publish_previous_records(self):
        class Broken:
            def scan(self):
                raise ValueError('corrupt fixture')
        with patch.object(indexer, 'get_reader', return_value=Broken()):
            with self.assertRaisesRegex(RuntimeError, '原索引未替换'):
                indexer.scan_all(['dsh'], [SessionRecord(tool='dsh', session_id='old')])

    def test_exact_directory_and_root_tree(self):
        records = [SessionRecord(tool='dsh', session_id='root', cwd='/work'),
                   SessionRecord(tool='dsh', session_id='child', cwd='/work', title='needle', extra={'origin':'subagent', 'parent_session_id':'root'}),
                   SessionRecord(tool='dsh', session_id='other', cwd='/workspace'),
                   SessionRecord(tool='claude', session_id='unknown')]
        handler = object.__new__(server.Handler)
        with patch.object(server, '_records', records):
            out = handler._list_sessions({'cwd':['/work'], 'group':['roots']})
            self.assertEqual([s['session_id'] for s in out['sessions']], ['root'])
            self.assertEqual(out['sessions'][0]['children'][0]['session_id'], 'child')
            matched = handler._list_sessions({'q':['needle'], 'group':['roots']})
            self.assertEqual([s['session_id'] for s in matched['sessions']], ['root'])
            unknown = handler._list_sessions({'cwd':['']})
            self.assertEqual([s['session_id'] for s in unknown['sessions']], ['unknown'])

    def test_message_search_and_selection_ids(self):
        rec = SessionRecord(tool='dsh', session_id='a', title='test')
        msgs = normalize([Message(role='user', text='question'), Message(role='assistant', text='needle result')])
        with patch.object(server, '_records', [rec]), patch.object(server, '_cached_messages', return_value=msgs):
            payload = server.session_payload('dsh', 'a', 0, view='chat', query='needle')
            self.assertEqual(payload['matches'], [msgs[1].message_id])
            blocks = server.summary_blocks({'items':[{'tool':'dsh', 'id':'a', 'message_ids':[msgs[1].message_id]}]})
            self.assertIn('needle result', '\n'.join(blocks))
            self.assertNotIn('question', '\n'.join(blocks))
            self.assertIn('#dsh/a', '\n'.join(blocks))
            with self.assertRaises(ValueError):
                server.summary_blocks({'items':[{'tool':'dsh', 'id':'a', 'message_ids':[]}]})

    def test_failed_rescan_keeps_records_and_reports(self):
        old = [SessionRecord(tool='dsh', session_id='a')]
        with patch.object(server, '_records', old), \
             patch.object(server, 'scan_all', side_effect=RuntimeError('扫描未完成：坏文件')):
            with self.assertRaises(RuntimeError):
                server._do_scan()
            self.assertIs(server._records, old)  # 原索引不动
            self.assertEqual(server._problems, ['扫描未完成：坏文件'])  # 页面顶部可见
        server._problems.clear()

    def test_user_and_ai_views(self):
        rec = SessionRecord(tool='dsh', session_id='a', title='test')
        msgs = normalize([Message(role='user', text='问1'), Message(role='assistant', text='过程'),
                          Message(role='assistant', kind='thinking', text='想'),
                          Message(role='assistant', text='答1'), Message(role='user', text='问2'),
                          Message(role='assistant', text='答2')])
        with patch.object(server, '_records', [rec]), patch.object(server, '_cached_messages', return_value=msgs):
            texts = lambda view: [m['text'] for m in server.session_payload('dsh', 'a', 0, view=view)['messages']]
            self.assertEqual(texts('user'), ['问1', '问2'])
            self.assertEqual(texts('ai'), ['答1', '答2'])  # 只含每轮最终回答，不含过程与思考
            self.assertEqual(len(texts('full')), 6)
        from agenthub.exporter import render_markdown
        doc = render_markdown(rec, msgs, 'user')
        self.assertIn('问2', doc)
        self.assertNotIn('答1', doc)
        self.assertIn('只看用户', doc)


if __name__ == '__main__':
    unittest.main()
