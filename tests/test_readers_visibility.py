import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub.readers import claude, codex, hermes, workbuddy


class ReaderVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def jsonl(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        return str(path)

    def test_claude_deleted_marker_and_same_id(self):
        for proj in ('a', 'b'):
            self.jsonl(self.root / proj / (proj + '.jsonl'), [{'type': 'user', 'sessionId': 'live', 'message': {'content': 'same'}}])
        self.jsonl(self.root / 'a/dead.jsonl', [{'type': 'user', 'message': {'content': 'same'}}])
        (self.root / 'a/dead.desktop-released.json').write_text(json.dumps({'v': 1, 'reason': 'delete', 'releasedAt': '2026-01-01T00:00:00Z'}))
        with patch.object(claude, 'ROOT', str(self.root)):
            self.assertEqual([r.session_id for r in claude.ClaudeReader().scan()], ['live'])

    def test_claude_full_tools(self):
        p = self.jsonl(self.root / 'c.jsonl', [
            {'type':'assistant','uuid':'msg1','message':{'content':[{'type':'tool_use','id':'call1','name':'tool','input':{'a':'x'*900}}]}},
            {'type':'user','uuid':'msg2','message':{'content':[{'type':'tool_result','tool_use_id':'call1','content':'y'*900}]}}])
        msgs = claude.ClaudeReader().read_messages(p)
        self.assertIn('x'*900, msgs[0].tool_args)
        self.assertEqual(msgs[1].tool_output, 'y'*900)
        self.assertEqual([m.call_id for m in msgs], ['call1','call1'])

    def codex_db(self, complete=True):
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as c, c:
            c.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER); CREATE TABLE backfill_state(status TEXT);')
            c.execute('INSERT INTO backfill_state VALUES (?)', ('complete' if complete else 'pending',))
            c.execute('INSERT INTO threads VALUES (?,?,1)', ('live', str(self.root / 'archived_sessions/rollout-live.jsonl')))

    def test_codex_authority_keeps_archive_excludes_deleted(self):
        self.codex_db()
        for folder, sid in [('sessions','dead'), ('sessions','live'), ('archived_sessions','live')]:
            self.jsonl(self.root / folder / f'rollout-{sid}.jsonl', [{'type':'session_meta','payload':{'id':sid}}])
        with patch.object(codex, 'ROOT', str(self.root)):
            reader = codex.CodexReader()
            rows = reader.scan()
            self.assertEqual([r.session_id for r in rows], ['live'])
            self.assertIn('archived_sessions', rows[0].file_path)
            self.assertEqual(reader.visible_session_ids(), {'live'})

    def test_codex_incomplete_db_not_authority(self):
        self.codex_db(False)
        self.jsonl(self.root / 'sessions/rollout-new.jsonl', [{'type':'session_meta','payload':{'id':'new'}}])
        with patch.object(codex, 'ROOT', str(self.root)):
            self.assertIsNone(codex.CodexReader().visible_session_ids())
            self.assertEqual([r.session_id for r in codex.CodexReader().scan()], ['new'])

    def test_codex_full_custom_tools(self):
        p = self.jsonl(self.root / 'c.jsonl', [
            {'type':'response_item','payload':{'type':'custom_tool_call','name':'patch','call_id':'call','input':'x'*900}},
            {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'call','output':'y'*900}}])
        msgs = codex.CodexReader().read_messages(p)
        self.assertEqual([(m.kind,m.call_id) for m in msgs], [('tool_call','call'),('tool_result','call')])
        self.assertEqual(msgs[0].tool_args, 'x'*900)
        self.assertEqual(msgs[1].tool_output, 'y'*900)

    def hermes_db(self, reasoning):
        p = str(self.root / 'h.db')
        with closing(sqlite3.connect(p)) as c, c:
            c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, started_at REAL, hidden INTEGER, archived INTEGER, end_reason TEXT)')
            c.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?)', [('live','same',1,0,0,None),('archived','same',1,0,1,None),('hidden','same',1,1,0,None),('ended','same',1,0,0,'completed')])
            c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, tool_call_id TEXT'+reasoning+')')
            c.execute("INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (1,'live','user','hello',1)")
        return p

    def test_hermes_reasoning_missing_or_single_column(self):
        for column in ('', ', reasoning TEXT', ', reasoning_content TEXT'):
            with self.subTest(column=column):
                p = self.hermes_db(column)
                with patch.object(hermes, 'DB', p):
                    reader = hermes.HermesReader()
                    self.assertEqual(reader.read_messages('live')[0].text, 'hello')
                    self.assertEqual(reader.visible_session_ids(), {'live','archived','ended'})
                    self.assertEqual({r.session_id for r in reader.scan()}, {'live','archived','ended'})
                Path(p).unlink()

    def test_workbuddy_deleted_and_checkpoint_not_call(self):
        p = str(self.root / 'w.db')
        with closing(sqlite3.connect(p)) as c, c:
            c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT,title TEXT,custom_title TEXT,created_at INT,updated_at INT,model TEXT,deleted_at INT,status TEXT)')
            c.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)', [('live','/tmp','same',None,1,2,'m',None,'ok'),('dead','/tmp','same',None,1,2,'m',3,'ok')])
        cp = self.root / 'changes-detail/live/cd_1.json'
        cp.parent.mkdir(parents=True); cp.write_text('{}')
        with patch.object(workbuddy, 'DB', p), patch.object(workbuddy, 'WB', str(self.root)):
            reader = workbuddy.WorkBuddyReader()
            rows = reader.scan()
            self.assertEqual([r.session_id for r in rows], ['live'])
            self.assertEqual(rows[0].n_tool_calls, 0)
            self.assertEqual(rows[0].extra['n_checkpoints'], 1)
            self.assertEqual(reader.visible_session_ids(), {'live'})
            self.assertEqual(reader.read_messages('dead'), [])

if __name__ == '__main__':
    unittest.main()
