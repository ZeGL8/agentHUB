import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import exporter, indexer, llm, server
from agenthub.model import SessionRecord
from agenthub.readers import workbuddy, normalize
from agenthub.readers.base import iter_jsonl, open_ro


class AuditTests(unittest.TestCase):
    def test_workbuddy_native_identity_survives_compression_and_cache_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'w.db'
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute('CREATE TABLE sessions(id TEXT, cwd TEXT, created_at REAL)')
                conn.executemany('INSERT INTO sessions VALUES (?,?,?)', [('live', '', 1), ('other', '', 1)])
            path = root / 'traces/pid/trace_a.json'
            path.parent.mkdir(parents=True)
            def write(text):
                path.write_text(json.dumps({'trace': {'sessionId': 'live', 'startedAt': '2026-09-27T10:11:59Z'}, 'spans': [
                    {'spanId': 'a', 'type': 'generation', 'startedAt': '2026-09-27T10:11:59Z',
                     'toolInput': '[{"role":"user","content":"conversation_history_summary"}]',
                     'toolOutput': json.dumps({'choices': [{'message': {'content': [{'text': text}]}}]})}]}))
            write('complete')
            with patch.object(workbuddy, 'DB', str(db)), patch.object(workbuddy, 'TRACES_ROOT', str(path.parent.parent)):
                reader = workbuddy.WorkBuddyReader()
                meta = ('live', '', 'title', None, 1, 2, 'm', None)
                self.assertEqual(reader._messages_from_traces('live', meta)[0].text, 'complete')
                self.assertEqual(reader._messages_from_traces('other', ('other', *meta[1:])), [])
                before = server._source_sig(str(db), 'workbuddy')
                write('changed and longer')
                self.assertNotEqual(server._source_sig(str(db), 'workbuddy'), before)
                self.assertEqual(reader._messages_from_traces('live', meta)[0].text, 'changed and longer')
                second = path.with_name('trace_b.json')
                second.write_text(path.read_text().replace('changed and longer', 'next reply').replace('\"spanId\": \"a\"', '\"spanId\": \"b\"'))
                messages = normalize(reader._messages_from_traces('live', meta))
                self.assertEqual(sum(m.final for m in messages), 2)
                second.unlink()
                # ID inside a span must never be mistaken for trace.sessionId.
                path.write_text(json.dumps({'trace': {'startedAt': '2026-09-27T10:11:59Z'}, 'spans': [{'sessionId': 'live'}]}))
                self.assertEqual(reader._messages_from_traces('live', meta), [])

    def test_native_visibility_is_shared_and_deleted_subtree_stays_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source'
            path.touch()
            records = [SessionRecord(tool='codex', session_id='parent', file_path=str(path)),
                       SessionRecord(tool='codex', session_id='child', file_path=str(path),
                                     extra={'origin': 'subagent', 'parent_session_id': 'parent'})]
            class Reader:
                def visible_session_ids(self):
                    return {'child'}
            with patch.object(indexer, 'get_reader', return_value=Reader()):
                indexer.write_index(records, tmp)
                self.assertEqual(indexer.load_index(tmp), [])
                with patch.object(server, '_records', records):
                    self.assertEqual(server.current_records(), [])
                    with self.assertRaises(KeyError):
                        server.find_record('codex', 'child')

    def test_corrupt_jsonl_and_database_do_not_silently_degrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'broken'
            path.write_text('{}\n{broken}\n')
            with self.assertRaisesRegex(ValueError, ':2:'):
                list(iter_jsonl(str(path)))
            with self.assertRaises(sqlite3.DatabaseError):
                with open_ro(str(path)) as conn:
                    conn.execute('SELECT * FROM sqlite_master').fetchall()

    def test_path_export_cannot_bypass_deleted_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'deleted.jsonl'
            path.write_text('{}')
            class Reader:
                def scan(self):
                    return []
            with patch.object(exporter, 'get_reader', return_value=Reader()):
                with self.assertRaises(KeyError):
                    exporter.export_by_path('claude', str(path), tmp)

    def test_dsh_canonical_parent_and_cycle_are_visible(self):
        root = SessionRecord(tool='dsh', session_id='session-root')
        child = SessionRecord(tool='dsh', session_id='child', extra={'origin': 'subagent', 'parent_session_id': 'root'})
        handler = object.__new__(server.Handler)
        with patch.object(server, 'current_records', return_value=[root, child]):
            rows = handler._list_sessions({'group': ['roots']})['sessions']
            self.assertEqual(rows[0]['children'][0]['session_id'], 'child')
        root.extra = {'origin': 'subagent', 'parent_session_id': 'child'}
        with patch.object(server, 'current_records', return_value=[root, child]):
            with self.assertRaisesRegex(ValueError, '循环'):
                handler._list_sessions({'group': ['roots']})

    def test_clear_key_and_blank_model_are_explicit_overrides(self):
        with patch.object(llm, 'load_config', return_value={'base_url': 'http://localhost:1', 'model': 'old', 'api_key': 'secret'}):
            self.assertEqual(llm._resolve({'api_key': ''})['api_key'], '')
            self.assertEqual(llm._resolve({'api_key': None})['api_key'], 'secret')
            self.assertEqual(llm._resolve({'model': ''})['model'], '')
            for value in ('file:///tmp/key', 'http://user:password@localhost', 'https://host/?key=secret'):
                with self.assertRaises(llm.LLMError):
                    llm._resolve({'base_url': value})
            with self.assertRaises(llm.LLMError):
                llm._resolve({'model': []})


if __name__ == '__main__':
    unittest.main()
