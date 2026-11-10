# -*- coding: utf-8 -*-
"""核心逻辑回归检查：uv run python tests/test_core.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.indexer import find_record
from agenthub.model import Message, SessionRecord, is_chat, split_think
from agenthub.readers import normalize
from agenthub.readers.claude import ClaudeReader, _user_text
from agenthub.readers.codex import INJECT_PREFIXES
from agenthub.readers.hermes import _tool_call_names


def test_claude_user_text():
    u = lambda c, **kw: dict(type="user", message={"role": "user", "content": c}, **kw)
    assert _user_text(u("你好")) == "你好"
    # 带图片的输入是数组形态，文字不能丢
    assert _user_text(u([{"type": "image"}, {"type": "text", "text": "问题依然存在"}])) == "问题依然存在"
    assert _user_text(u([{"type": "tool_result", "content": "x"}])) == ""
    assert _user_text(u("技能注入", isMeta=True)) == ""
    assert _user_text(u("压缩摘要", isCompactSummary=True)) == ""
    assert _user_text(u("<task-notification>done")) == ""


def test_claude_scan_counts():
    lines = [
        # assistant 在任何 user 行之前出现：旧代码在此处 UnboundLocalError
        {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}},
                                 {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:02Z", "cwd": "/w",
         "message": {"content": [{"type": "image"}, {"type": "text", "text": "看图"}]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    try:
        r = ClaudeReader()._scan_file(f.name, "sid")
        assert r.n_tool_calls == 2, r.n_tool_calls
        assert r.n_messages == 2 and r.title == "看图" and r.cwd == "/w", r
        texts = [m.text for m in ClaudeReader().read_messages(f.name) if m.kind == "text"]
        assert texts == ["看图"], texts
    finally:
        os.unlink(f.name)


def test_jsonl_partial_last_line():
    from agenthub.readers.base import iter_jsonl
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write('{"a": 1}\n{"b": ')  # 正在写入的末行
    assert list(iter_jsonl(f.name)) == [{"a": 1}]
    with open(f.name, "w") as g:
        g.write('{"a": 1}\n{bad}\n{"c": 3}\n')  # 中间行损坏仍要报错
    try:
        list(iter_jsonl(f.name))
        raise AssertionError("损坏行应报错")
    except ValueError:
        pass
    os.unlink(f.name)


def test_find_record():
    rs = [SessionRecord(tool="dsh", session_id="session-abc123"),
          SessionRecord(tool="dsh", session_id="session-abd456"),
          SessionRecord(tool="claude", session_id="abc")]
    assert find_record(rs, "dsh", "abc").session_id == "session-abc123"
    assert find_record(rs, "dsh", "session-abd").session_id == "session-abd456"
    assert find_record(rs, "claude", "abc").session_id == "abc"
    for bad in ("ab", "zzz"):  # 歧义前缀 / 不存在都应报错，而不是静默挑一条
        try:
            find_record(rs, "dsh", bad)
            raise AssertionError(bad)
        except KeyError:
            pass


def test_codex_and_hermes():
    assert "# AGENTS.md instructions for /x".startswith(INJECT_PREFIXES)
    assert "<recommended_plugins>\n".startswith(INJECT_PREFIXES)
    calls = json.dumps([{"id": "call_1", "function": {"name": "terminal", "arguments": "{\"c\":1}"}}, None])
    assert _tool_call_names(calls) == [("terminal", "{\"c\":1}", "call_1"), ("", "", "")]


def test_think_and_final():
    assert split_think("<think>想</think>答") == ("想", "答")
    assert split_think("前<think>未闭合的思考") == ("未闭合的思考", "前")
    u = lambda t: Message(role="user", text=t)
    a = lambda t, **kw: Message(role="assistant", text=t, **kw)
    out = normalize([u("问1"), a("我先看看"), Message(role="assistant", kind="tool_call"),
                     a("<think>推理</think>结论1"), u("问2"), a(""), a("结论2")])
    chat = [m.text for m in out if is_chat(m)]
    assert chat == ["问1", "结论1", "问2", "结论2"], chat
    assert [m.text for m in out if m.kind == "thinking"] == ["推理"]
    assert not any(m.text == "" for m in out if m.kind == "text")  # 空正文被丢弃
    out = normalize([Message(role="assistant", kind="tool_call", tool_name="read", call_id="c1"),
                     Message(role="assistant", kind="tool_result", call_id="c1", tool_output="ok")])
    assert out[1].tool_name == "read"  # 结果按 call_id 回填工具名


def test_llm_against_mock_server():
    """用本地模拟的 OpenAI 兼容服务验证：模型过滤、<think> 去除、流式解析、长内容分段。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agenthub import llm

    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # /v1/models
            body = json.dumps({"data": [{"id": "chat-a", "type": "LLM", "max_context": 8000},
                                        {"id": "emb", "type": "Embedding"},
                                        {"id": "off", "type": "LLM", "ready": False},
                                        {"id": "plain"}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # /v1/chat/completions
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(req)
            self.send_response(200)
            self.end_headers()
            if req.get("stream"):
                for piece in ["<think>想", "</think>总", "结"]:
                    chunk = {"choices": [{"delta": {"content": piece}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                msg = {"content": "<think>推理</think>要点"}
                self.wfile.write(json.dumps({"choices": [{"message": msg}]}).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "config.json")  # 不读写真实配置
    cfg = {"base_url": f"http://127.0.0.1:{srv.server_address[1]}", "model": "chat-a", "context_window": 8000}
    try:
        assert [m["id"] for m in llm.list_models(cfg)] == ["chat-a", "plain"]
        assert llm.test(cfg)["reply"] == "要点"
        events = list(llm.summarize(["短内容"], "brief", overrides=cfg))
        assert "".join(e["text"] for e in events if e["type"] == "delta") == "<think>想</think>总结"
        assert events[-1]["type"] == "done"
        calls.clear()  # 保守预算分段：所有提取与最终总结均流式并限制输出
        list(llm.summarize(["甲" * 3000, "乙" * 3000, "丙" * 3000], overrides=cfg))
        assert len(calls) > 1 and all(c["stream"] for c in calls), calls
        assert all(c.get("max_tokens", 0) > 0 for c in calls), calls
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
