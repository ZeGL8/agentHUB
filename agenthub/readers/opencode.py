# -*- coding: utf-8 -*-
"""OpenCode (Desktop / CLI) 会话读取器。

存储位置：~/.local/share/opencode/opencode.db（SQLite，WAL）
  XDG：$XDG_DATA_HOME/opencode/opencode.db，可用 AGENTHUB_OPENCODE_HOME 覆盖。

表结构（v2，桌面版 2.x 使用；也兼容只有 v1 表的旧版本）：
  session_v2        会话元数据（title / directory / model / parent_id / 时间戳，epoch ms）
  session_message   消息，data 为 JSON：user 在 data.text，assistant 在 data.content 内容块内联
      content[]: {type: reasoning|text|tool, ...}
        - reasoning / text：text 字段（流式写入时可能是逐 token 的相邻小块，需按序合并；
          部分 provider 还把同一段推理内嵌进正文的 <think>…</think>，需去重）
        - tool：name / id（或 tool / callID），state {status, input, content|error}

会话删除由原生外键级联（行被移除，无墓碑）；time_archived 只表示归档，不隐藏。
"""

import json
import os
import re
from typing import List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import open_ro

# 部分 provider 把推理既写成 reasoning 块、又内嵌进正文的 <think>…</think>，需识别后去重。
THINK_RE = re.compile(r"<think\b[^>]*>(.*?)</think\s*>", re.S | re.I)
THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.I)


def _home() -> str:
    if os.environ.get("AGENTHUB_OPENCODE_HOME"):
        return os.path.expanduser(os.environ["AGENTHUB_OPENCODE_HOME"])
    xdg = os.environ.get("XDG_DATA_HOME")
    base = os.path.expanduser(xdg) if xdg else os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "opencode")


DB = os.path.join(_home(), "opencode.db")


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _model_name(raw) -> str:
    """session_v2.model 是 JSON 文本（{"id":..,"providerID":..}），取模型 ID。"""
    if not raw:
        return ""
    try:
        o = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(o, dict):
        return str(o.get("id") or o.get("modelID") or "")
    return str(o)


def _user_text(o: dict) -> str:
    """user 消息正文：data.text（displayText 是同一内容的展示兜底）。"""
    text = o.get("text")
    if not isinstance(text, str) or not text.strip():
        meta = o.get("metadata")
        if isinstance(meta, dict):
            text = meta.get("displayText")
    return text.strip() if isinstance(text, str) else ""


def _tool_output(state) -> str:
    """工具结果文本：优先 content 文本块，其次 error / output。"""
    if not isinstance(state, dict):
        return ""
    content = state.get("content")
    if isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    if state.get("error"):
        return str(state["error"])
    if state.get("output") is not None:
        out = state["output"]
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if content is not None and not isinstance(content, list):
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


def _normalized(s: str) -> str:
    """去掉 <think> 标记与全部空白，用于判断两段推理是否同一内容。"""
    return re.sub(r"\s+", "", THINK_TAG_RE.sub("", s or ""))


def group_content(content) -> list:
    """assistant 内容块 → 有序分组。返回 [(kind, payload)]，kind ∈ {thinking, text, tool}。

    - 所有 reasoning 块合并为一段思考，放在首个 reasoning 的位置（原生中推理恒在正文/工具之前）。
    - 若正文内嵌的 <think> 与 reasoning 是同一段内容，则去掉正文里的 <think>，避免思考被重复渲染。
    - 相邻 text 片段合并（流式写入时正文可能是逐 token 的小块）。
    """
    blocks = [b for b in (content or []) if isinstance(b, dict)]
    reasoning = "".join(b.get("text") or "" for b in blocks if b.get("type") == "reasoning").strip()
    groups, first_reason, seen = [], None, ""
    for b in blocks:
        t = b.get("type")
        if t == "reasoning":
            if first_reason is None:
                first_reason = len(groups)
        elif t == "text":
            piece = b.get("text") or ""
            if not piece:
                continue
            seen += piece
            if groups and groups[-1][0] == "text":
                groups[-1][1] += piece
            else:
                groups.append(["text", piece])
        elif t == "tool":
            groups.append(["tool", b])
    if reasoning:
        a, b = _normalized(reasoning), _normalized(seen)
        if "<think" in seen and a and b and (a in b or b in a):
            for g in groups:  # 推理已单独成段：正文里的 <think> 是重复副本，去掉
                if g[0] == "text":
                    g[1] = THINK_RE.sub("", g[1]).strip()
        groups.insert(first_reason if first_reason is not None else 0, ["thinking", reasoning])
    return [g for g in groups if not (g[0] in ("thinking", "text") and not g[1].strip())]


def _counts(mtype: str, o: dict):
    """(正文消息数, 工具调用数)：与 read_messages 的口径一致。"""
    if mtype == "user":
        return (1 if _user_text(o) else 0, 0)
    if mtype != "assistant":
        return (0, 0)
    texts = tools = 0
    for kind, _ in group_content(o.get("content")):
        if kind == "tool":
            tools += 1
        elif kind == "text":
            texts += 1
    return (texts, tools)


class OpenCodeReader:
    tool = "opencode"
    uses_session_id = True  # read_messages 的参数是 session_id 而非文件路径

    def visible_session_ids(self):
        if not os.path.exists(DB):
            return None
        with open_ro(DB) as conn:
            if "session_v2" not in _tables(conn):
                return None
            return {str(r[0]) for r in conn.execute("SELECT id FROM session_v2")}

    def scan(self) -> List[SessionRecord]:
        if not os.path.exists(DB):
            return []
        with open_ro(DB) as conn:
            if "session_v2" not in _tables(conn):
                return []
            cols = [r[1] for r in conn.execute("PRAGMA table_info(session_v2)")]
            wanted = ["id", "parent_id", "title", "directory", "model",
                      "time_created", "time_updated", "time_archived"]
            sel = [c for c in wanted if c in cols]
            rows = conn.execute(f"SELECT {', '.join(sel)} FROM session_v2").fetchall()
            counts = {}
            for sid, mtype, data in conn.execute(
                    "SELECT session_id, type, data FROM session_message WHERE type IN ('user','assistant')"):
                try:
                    o = json.loads(data)
                except (TypeError, ValueError):
                    continue
                n_text, n_tool = _counts(mtype, o)
                agg = counts.setdefault(str(sid), [0, 0])
                agg[0] += n_text
                agg[1] += n_tool
        size = os.path.getsize(DB)
        records = []
        for r in rows:
            d = dict(zip(sel, r))
            sid = str(d.get("id") or "")
            started = ts_to_ms(d.get("time_created"))
            parent = str(d.get("parent_id") or "")
            n_text, n_tool = counts.get(sid, [0, 0])
            records.append(SessionRecord(
                tool=self.tool, session_id=sid,
                title=clip(str(d.get("title") or ""), 60) or "(无标题)",
                cwd=str(d.get("directory") or ""),
                file_path=DB,
                started_at=started,
                updated_at=ts_to_ms(d.get("time_updated")) or started,
                n_messages=n_text, n_tool_calls=n_tool,
                size_bytes=size,
                extra={"model": _model_name(d.get("model")),
                       "parent_session_id": parent,
                       "origin": "subagent" if parent else "",
                       "archived": bool(d.get("time_archived"))},
            ))
        return records

    def read_messages(self, path: str) -> List[Message]:
        # path 传 session_id（数据库型工具）
        with open_ro(DB) as conn:
            if "session_v2" not in _tables(conn):
                return []
            if not conn.execute("SELECT 1 FROM session_v2 WHERE id = ?", (path,)).fetchone():
                return []
            rows = conn.execute(
                "SELECT id, type, data FROM session_message WHERE session_id = ? ORDER BY seq",
                (path,)).fetchall()
        msgs: List[Message] = []
        for mid, mtype, data in rows:
            try:
                o = json.loads(data)
            except (TypeError, ValueError):
                continue
            ts = ts_to_ms((o.get("time") or {}).get("created")) / 1000.0
            mid = str(mid)
            if mtype == "user":
                text = _user_text(o)
                if text:
                    msgs.append(Message(role="user", text=text, timestamp=ts,
                                        message_id=mid, source=path))
            elif mtype == "system":
                text = o.get("text")
                if isinstance(text, str) and text.strip():
                    msgs.append(Message(role="system", kind="event", text=text.strip(),
                                        timestamp=ts, message_id=mid, source=path))
            elif mtype == "assistant":
                for index, (kind, payload) in enumerate(group_content(o.get("content"))):
                    if kind == "tool":
                        name = payload.get("name") or payload.get("tool") or ""
                        call_id = str(payload.get("id") or payload.get("callID") or "")
                        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
                        args = state.get("input")
                        tool_args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                        msgs.append(Message(role="assistant", kind="tool_call",
                                            tool_name=name, tool_args=tool_args, call_id=call_id,
                                            timestamp=ts, message_id=f"{mid}:{index}:call", source=path))
                        output = _tool_output(state)
                        if output.strip():
                            msgs.append(Message(role="assistant", kind="tool_result",
                                                tool_name=name, tool_output=output, call_id=call_id,
                                                timestamp=ts, message_id=f"{mid}:{index}:result", source=path))
                    else:
                        msgs.append(Message(role="assistant",
                                            kind="thinking" if kind == "thinking" else "text",
                                            text=payload, timestamp=ts,
                                            message_id=f"{mid}:{index}", source=path))
        return msgs
