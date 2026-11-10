import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agenthub.model import is_chat
from agenthub.readers import codex, read_record


class CodexHistoryTests(unittest.TestCase):
    """rollout 文件已被清理时，从 thread_history_1.sqlite 读取线程。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        st = sqlite3.connect(os.path.join(self.root, "state_5.sqlite"))
        st.execute("CREATE TABLE backfill_state (status TEXT)")
        st.execute("INSERT INTO backfill_state VALUES ('complete')")
        st.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT, title TEXT, cwd TEXT, "
                   "created_at_ms INT, updated_at_ms INT, model TEXT, first_user_message TEXT, "
                   "thread_source TEXT, source TEXT, agent_nickname TEXT, agent_path TEXT)")
        spawn = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "p"}}})
        st.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("p", "/gone/rollout-p.jsonl", "主线程", "", "/w", 1000, 2000, "gpt", "", "user", "vscode", None, None),
            ("c", "/gone/rollout-c.jsonl", "", "子线程", "/w", 1500, 1600, "gpt", "", "subagent", spawn, "Boole", "/root/x"),
        ])
        st.commit()
        h = sqlite3.connect(os.path.join(self.root, "thread_history_1.sqlite"))
        h.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INT, "
                  "created_at_ms INT, item_json TEXT, item_type TEXT)")
        items = [
            ("p", "t1", {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "问题"}]}),
            ("p", "t1", {"type": "agentMessage", "id": "a1", "text": "我先看看"}),
            ("p", "t1", {"type": "commandExecution", "id": "x", "command": "ls", "aggregatedOutput": "f", "exitCode": 1}),
            ("p", "t1", {"type": "subAgentActivity", "id": "s", "kind": "started", "agentThreadId": "c", "agentPath": "/root/x"}),
            ("p", "t1", {"type": "agentMessage", "id": "a2", "text": "结论"}),
            ("c", "t9", {"type": "agentMessage", "id": "a3", "text": "子线程结果"}),
            ("gone", "t0", {"type": "agentMessage", "id": "a4", "text": "线程表里已删除"}),
        ]
        h.executemany("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)",
                      [(tid, turn, it["id"], n, 1000 + n, json.dumps(it), it["type"])
                       for n, (tid, turn, it) in enumerate(items)])
        h.commit()
        for target, value in (("ROOT", self.root), ("HISTORY_DB", os.path.join(self.root, "thread_history_1.sqlite"))):
            p = patch.object(codex, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_history_threads_scanned_and_read(self):
        recs = {r.session_id: r for r in codex.CodexReader().scan()}
        self.assertEqual(set(recs), {"p", "c"})  # 不在线程表中的 gone 不显示
        self.assertEqual((recs["p"].title, recs["p"].n_messages, recs["p"].n_tool_calls), ("主线程", 3, 1))
        self.assertEqual((recs["c"].extra["origin"], recs["c"].extra["parent_session_id"]), ("subagent", "p"))
        msgs = read_record(recs["p"])
        self.assertEqual([m.text for m in msgs if is_chat(m)], ["问题", "结论"])
        self.assertEqual(next(m for m in msgs if m.kind == "event").link, "c")
        self.assertTrue(next(m for m in msgs if m.kind == "tool_result").tool_output.startswith("[exit 1]"))


if __name__ == "__main__":
    unittest.main()
