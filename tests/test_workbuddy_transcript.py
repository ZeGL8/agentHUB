import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agenthub.model import is_chat
from agenthub.readers import read_record, workbuddy


class WorkBuddyTranscriptTests(unittest.TestCase):
    """正文来自 projects/<ws>/<sid>.jsonl；标题优先级与注入过滤。"""

    def setUp(self):
        root = tempfile.mkdtemp()
        db = sqlite3.connect(os.path.join(root, "workbuddy.db"))
        db.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, title TEXT, custom_title TEXT, "
                   "created_at INT, updated_at INT, model TEXT, status TEXT, deleted_at INT)")
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", [
            ("a", "/w", "数据库标题", None, 1000, 2000, "m", "done", None),
            ("b", "/w", "", None, 1000, 2000, "m", "done", None),
        ])
        db.commit()
        proj = os.path.join(root, "projects", "w")
        os.makedirs(proj)
        rows = [
            {"type": "message", "role": "user", "content": "<system-reminder>注入</system-reminder>"},
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "<user_query>真实问题</user_query>"}]},
            {"type": "reasoning", "content": "想"},
            {"type": "function_call", "name": "ls", "arguments": {"p": "."}, "callId": "c"},
            {"type": "function_call_result", "name": "ls", "output": {"text": "a.txt"}, "callId": "c"},
            {"type": "message", "role": "assistant", "content": "回答"},
        ]
        for sid in ("a", "b"):
            with open(os.path.join(proj, sid + ".jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        for target, value in (("WB", root), ("DB", os.path.join(root, "workbuddy.db")),
                              ("PROJECTS_ROOT", os.path.join(root, "projects")),
                              ("TRACES_ROOT", os.path.join(root, "traces"))):
            p = patch.object(workbuddy, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_transcript_messages_and_titles(self):
        recs = {r.session_id: r for r in workbuddy.WorkBuddyReader().scan()}
        self.assertEqual(recs["a"].title, "数据库标题")  # 有数据库标题时不被首条消息覆盖
        self.assertEqual(recs["b"].title, "真实问题")    # 无标题才用首条用户消息
        msgs = read_record(recs["a"])
        self.assertEqual([m.text for m in msgs if is_chat(m)], ["真实问题", "回答"])
        self.assertEqual([m.kind for m in msgs], ["text", "thinking", "tool_call", "tool_result", "text"])
        self.assertEqual(msgs[3].tool_output, "a.txt")


if __name__ == "__main__":
    unittest.main()
