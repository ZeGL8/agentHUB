# -*- coding: utf-8 -*-
"""Hermes Desktop 会话读取器。

存储位置：~/.hermes/state.db（SQLite，WAL 模式）
  sessions 表：id / display_name / model / 时间戳（epoch 秒）/ cwd（若存在）
  messages 表：session_id / role / content / tool_calls / timestamp

"""

import json
import os
from typing import List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import open_ro, tool_home

DB = os.path.join(tool_home(".hermes", "AGENTHUB_HERMES_HOME"), "state.db")


def _table_columns(conn, table: str):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _tool_call_names(tool_calls: str) -> List[tuple]:
    """tool_calls 为 OpenAI 格式 JSON 数组 → [(name, arguments, call_id)]；解析失败原样返回。"""
    try:
        calls = json.loads(tool_calls)
    except (TypeError, ValueError):
        return [("", str(tool_calls), "")]
    out = []
    for c in calls if isinstance(calls, list) else [calls]:
        c = c if isinstance(c, dict) else {}
        fn = c.get("function") or {}
        out.append((fn.get("name", ""), str(fn.get("arguments", "")), str(c.get("id") or "")))
    return out


class HermesReader:
    tool = "hermes"
    uses_session_id = True  # read_messages 的参数是 session_id 而非文件路径

    @staticmethod
    def _visible_where(cols):
        # hidden means absent from the native default list, not deleted.
        if "hidden" not in cols:
            return "1"
        archived = "COALESCE(archived, 0) <> 0" if "archived" in cols else "0"
        return f"(COALESCE(hidden, 0) = 0 OR {archived})"

    def visible_session_ids(self):
        if not os.path.exists(DB):
            return None
        with open_ro(DB) as conn:
            where = self._visible_where(_table_columns(conn, "sessions"))
            return {str(r[0]) for r in conn.execute(f"SELECT id FROM sessions WHERE {where}")}

    def scan(self) -> List[SessionRecord]:
        if not os.path.exists(DB):
            return []
        with open_ro(DB) as conn:
            cols = _table_columns(conn, "sessions")
            wanted = {
                "id": ["id"],
                # 同一语义可能分散在多列（display_name 常为空，title 才有值），按序取首个非空值
                "title": ["display_name", "title", "name"],
                "model": ["model"],
                "start": ["created_at", "started_at", "created"],
                "upd": ["last_activity_at", "updated_at", "updated", "ended_at"],
                "cwd": ["cwd", "workdir"],
            }
            sel = [c for names in wanted.values() for c in names if c in cols]
            where = self._visible_where(cols)
            rows = conn.execute(f"SELECT {', '.join(sel)} FROM sessions WHERE {where}").fetchall()
            counts = dict(conn.execute(
                "SELECT session_id, count(*) FROM messages "
                "WHERE role IN ('user','assistant') GROUP BY session_id").fetchall())
            tool_counts = {}
            for sid, calls in conn.execute("SELECT session_id, tool_calls FROM messages WHERE tool_calls IS NOT NULL"):
                tool_counts[sid] = tool_counts.get(sid, 0) + len(_tool_call_names(calls))
        records = []
        for r in rows:
            d = dict(zip(sel, r))
            first = lambda key: next((d[c] for c in wanted[key] if d.get(c)), None)
            sid = str(first("id") or "")
            started = ts_to_ms(first("start") or 0)
            records.append(SessionRecord(
                tool=self.tool, session_id=sid,
                title=clip(str(first("title") or ""), 60) or "(无标题)",
                cwd=str(first("cwd") or ""),
                file_path=DB,
                started_at=started, updated_at=ts_to_ms(first("upd") or 0) or started,
                n_messages=counts.get(sid, 0),
                n_tool_calls=tool_counts.get(sid, 0),
                size_bytes=os.path.getsize(DB),
                extra={"model": str(first("model") or "")},
            ))
        return records

    def read_messages(self, path: str) -> List[Message]:
        # path 传 session_id（数据库型工具）
        with open_ro(DB) as conn:
            cols = _table_columns(conn, "messages")
            # 思考内容可能在 reasoning_content 或 reasoning 列（按版本不同），取第一个非空
            think_cols = [f"NULLIF({c}, '')" for c in ("reasoning_content", "reasoning") if c in cols]
            thinking_sql = "COALESCE(" + ", ".join(think_cols + ["NULL"]) + ")" if think_cols else "NULL"
            call_id_sql = "tool_call_id" if "tool_call_id" in cols else "NULL"
            where = self._visible_where(_table_columns(conn, "sessions"))
            if not conn.execute(f"SELECT 1 FROM sessions WHERE id = ? AND {where}", (path,)).fetchone():
                return []
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_name, timestamp, "
                f"{thinking_sql}, id, {call_id_sql} "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (path,)).fetchall()
        msgs = []
        for role, content, tool_calls, tool_name, ts, thinking, mid, call_id in rows:
            identity = {"message_id": str(mid), "source": path}
            t = ts_to_ms(ts) / 1000.0
            if role == "tool":
                msgs.append(Message(role="assistant", kind="tool_result",
                                    tool_name=tool_name or "",
                                    tool_output=str(content or ""), call_id=str(call_id or ""), timestamp=t, **identity))
                continue
            if thinking and str(thinking).strip():
                msgs.append(Message(role="assistant", kind="thinking",
                                    text=str(thinking), timestamp=t, **identity))
            # 同一条 assistant 消息可能既有正文又有工具调用，两者都要保留
            if content:
                msgs.append(Message(role=role or "user", text=str(content), timestamp=t, **identity))
            if tool_calls:
                for name, args, call_id in _tool_call_names(tool_calls):
                    msgs.append(Message(role="assistant", kind="tool_call",
                                        tool_name=name, tool_args=args, call_id=call_id,
                                        timestamp=t, **identity))
        return msgs
