# -*- coding: utf-8 -*-
"""WorkBuddy Desktop 会话读取器。

存储位置：
  ~/.workbuddy/workbuddy.db → sessions 表（元数据）
  ~/.workbuddy/traces/<pid>/trace_*.json → 每轮 LLM 调用的完整输入输出
    （generation span 的 toolInput 含截至该轮的完整消息历史（上限 100K 截断），
     toolOutput 为该步响应；用户正文在 user 项 content 的 <user_query> 标签内）
  ~/.workbuddy/changes-detail/<convId>/cd_*.json → 文件变更检查点

新版 trace 按原生 sessionId 归属；旧版按工作目录与创建时间区间归属。

只从原生 traces 重建对话；缺少 trace 时显式报错。
"""

import glob
import functools
import json
import os
import re
from typing import List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import open_ro, tool_home

WB = tool_home(".workbuddy", "AGENTHUB_WORKBUDDY_HOME")
DB = os.path.join(WB, "workbuddy.db")
TRACES_ROOT = os.path.join(WB, "traces")

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
WORKSPACE_RE = re.compile(r"Workspace Folder:\s*([^\n]+)")


def _content_full_text(content) -> str:
    """content 兼容 str / OpenAI content 数组 / 其他形态，拼出完整文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text", "") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return str(content)


def _iter_input_items(toolinput: str):
    """容错逐项解析（可能被截断的）toolInput JSON 数组。"""
    if not toolinput or not toolinput.lstrip().startswith("["):
        return []
    s = toolinput.strip()
    dec = json.JSONDecoder()
    idx = 1
    items = []
    while idx < len(s):
        while idx < len(s) and s[idx] in " \t\n\r,":
            idx += 1
        try:
            obj, end = dec.raw_decode(s, idx)
            items.append(obj)
            idx = end
        except json.JSONDecodeError:
            break
    return items


def _load_spans(trace_path: str):
    """读取 trace 文件，返回 (trace 开始时间 ms, 按时间排序的 spans)。"""
    try:
        with open(trace_path, "r", encoding="utf-8", errors="replace") as f:
            o = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 WorkBuddy trace: {trace_path}") from exc
    spans = [s for s in o.get("spans", []) or [] if isinstance(s, dict)]
    spans.sort(key=lambda s: s.get("startedAt") or "")
    return ts_to_ms((o.get("trace") or {}).get("startedAt")), spans


def _trace_workspace(spans) -> str:
    """trace 所属工作区：取首个 generation 输入里 user 上下文的 "Workspace Folder:"。"""
    for g in spans:
        if g.get("type") != "generation":
            continue
        for it in _iter_input_items(g.get("toolInput") or ""):
            if isinstance(it, dict) and it.get("role") == "user":
                m = WORKSPACE_RE.search(_content_full_text(it.get("content")))
                if m:
                    return m.group(1).strip().rstrip("/\\")
    return ""


def _norm(path):
    return os.path.normcase(path.replace("\\", "/").rstrip("/"))


def _file_sig(path):
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


@functools.lru_cache(maxsize=4096)
def _trace_meta(path, signature):
    # 仅缓存小型归属信息；按文件时间与大小失效，不把整份 spans 常驻内存。
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    trace = data.get("trace") or {}
    sid = trace.get("sessionId") or ""
    workspace = "" if sid else _trace_workspace(data.get("spans") or [])
    return sid, ts_to_ms(trace.get("startedAt")), _norm(workspace)


def trace_signature():
    return tuple((path, *_file_sig(path)) for path in sorted(
        glob.glob(os.path.join(TRACES_ROOT, "*", "trace_*.json"))))


def _function_output(tooloutput: str) -> str:
    """function span 的 toolOutput 形如 {"content": ...}；解析失败返回原文。"""
    try:
        o = json.loads(tooloutput)
    except (TypeError, ValueError):
        return str(tooloutput or "")
    return _content_full_text(o.get("content")) if isinstance(o, dict) else str(o)


def _output_text(tooloutput: str):
    """从 generation 的 toolOutput（OpenAI 响应）提取 assistant 文本。"""
    if not tooloutput:
        return ""
    try:
        r = json.loads(tooloutput)
    except (TypeError, ValueError):
        return ""
    items = r if isinstance(r, list) else [r]
    texts = []
    for item in items:
        for ch in item.get("choices", []) or []:
            msg = ch.get("message", {}) or {}
            c = msg.get("content")
            text = _content_full_text(c)
            if text.strip():
                texts.append(text)
    return "\n".join(texts)


class WorkBuddyReader:
    tool = "workbuddy"
    uses_session_id = True  # read_messages 的参数是 session_id

    def visible_session_ids(self):
        if not os.path.exists(DB):
            return None
        with open_ro(DB) as conn:
            return {str(r[0]) for r in conn.execute("SELECT id FROM sessions WHERE deleted_at IS NULL")}

    def scan(self) -> List[SessionRecord]:
        if not os.path.exists(DB):
            return []
        with open_ro(DB) as conn:
            rows = conn.execute(
                "SELECT id, cwd, title, custom_title, created_at, updated_at, model, "
                "CASE WHEN deleted_at IS NULL THEN 0 ELSE 1 END "
                "FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC"
            ).fetchall()
        records = []
        for sid, cwd, title, custom, created, updated, model, deleted in rows:
            n_cp = len(glob.glob(
                os.path.join(WB, "changes-detail", sid, "cd_*.json")))
            rec = SessionRecord(
                tool=self.tool, session_id=sid,
                title=clip(str(custom or title or ""), 60) or "(无标题)",
                cwd=str(cwd or ""), file_path=DB,
                started_at=ts_to_ms(created), updated_at=ts_to_ms(updated),
                n_messages=0, n_tool_calls=0,
                size_bytes=os.path.getsize(DB),
                extra={"model": str(model or ""),
                       "n_checkpoints": n_cp,
                       "tool_count_status": "unknown",
                       "trace_rebuild": True,
                       "deleted": bool(deleted)},
            )
            msgs = self._messages_from_traces(sid, (sid, cwd, title, custom, created, updated, model, None))
            if msgs:
                rec.n_messages = sum(m.kind == "text" for m in msgs)
                rec.n_tool_calls = sum(m.kind == "tool_call" for m in msgs)
                rec.extra["tool_count_status"] = "known"
                rec.extra["content_updated_at"] = int(max(m.timestamp for m in msgs) * 1000)
            records.append(rec)
        return records

    def read_messages(self, path: str) -> List[Message]:
        """path 传 session_id；缺少 trace 不伪造会话正文。"""
        meta = self._session_meta(path)
        if not meta:
            return []
        msgs = self._messages_from_traces(path, meta)
        if msgs:
            return msgs
        raise ValueError("本地没有可归属的 WorkBuddy trace，无法恢复该会话正文")

    # ---------- 内部 ----------

    def _session_meta(self, session_id: str):
        with open_ro(DB) as conn:
            return conn.execute(
                "SELECT id, cwd, title, custom_title, created_at, updated_at, model, status "
                "FROM sessions WHERE id = ? AND deleted_at IS NULL", (session_id,)).fetchone()

    def _messages_from_traces(self, session_id: str, meta) -> List[Message]:
        sid, cwd, title, custom, created, updated, model, status = meta
        cwd = _norm(cwd or "")
        created = ts_to_ms(created)
        # 同一 cwd 下的全部会话按创建时间切分时间线：trace 归属于开始时间之前最近创建的那个会话
        with open_ro(DB) as conn:
            siblings = sorted((ts_to_ms(c), i) for i, c, directory in conn.execute(
                "SELECT id, created_at, cwd FROM sessions") if _norm(directory or "") == cwd)

        def owner(t_ms):
            before = [i for c, i in siblings if c <= t_ms]
            return before[-1] if before else None

        msgs: List[Message] = []
        seen_users = set()
        seen_spans = set()
        for tf in sorted(glob.glob(os.path.join(TRACES_ROOT, "*", "trace_*.json"))):
            trace_sid, t_start, workspace = _trace_meta(tf, _file_sig(tf))
            if trace_sid:
                if trace_sid != sid:
                    continue
            elif not cwd or workspace != cwd or not t_start or owner(t_start) != sid:
                continue
            _, spans = _load_spans(tf)
            turn_id = os.path.basename(tf)
            for sp in spans:
                span_id = str(sp.get("id") or sp.get("spanId") or "")
                if span_id and span_id in seen_spans:
                    continue
                if span_id:
                    seen_spans.add(span_id)
                ts = ts_to_ms(sp.get("startedAt")) / 1000.0
                if sp.get("type") == "function":
                    # 工具实际执行记录：输出完整，不受 generation 输入 100K 截断影响
                    msgs.append(Message(role="assistant", kind="tool_result",
                                        tool_name=sp.get("toolName") or sp.get("name") or "",
                                        tool_output=_function_output(sp.get("toolOutput")),
                                        call_id=str(sp.get("toolCallId") or ""),
                                        message_id=span_id, source=tf, turn_id=turn_id,
                                        timestamp=ts))
                    continue
                if sp.get("type") != "generation":
                    continue
                # ---- user 消息：input 是截至该轮的累积历史，需去重 ----
                for position, it in enumerate(_iter_input_items(sp.get("toolInput") or "")):
                    if isinstance(it, dict) and it.get("role") == "user":
                        m = USER_QUERY_RE.search(_content_full_text(it.get("content")))
                        text = m.group(1).strip() if m else ""
                        # Cumulative snapshots repeat prior positions; equal text at distinct positions is a real repeat.
                        mid = str(it.get("id") or it.get("message_id") or "")
                        key = ("id", mid) if mid else ("position", position, text)
                        if text and key not in seen_users:
                            seen_users.add(key)
                            msgs.append(Message(role="user", text=text, timestamp=ts,
                                                message_id=mid, source=tf, turn_id=turn_id))
                # ---- assistant 输出与工具调用（来自当步响应，天然唯一）----
                out = _output_text(sp.get("toolOutput") or "")
                if out:
                    msgs.append(Message(role="assistant", text=out, timestamp=ts, message_id=span_id, source=tf, turn_id=turn_id))
                self._collect_tool_calls(sp.get("toolOutput"), msgs, ts, turn_id)
        # 按时间稳定排序（同 ts 保持插入序）
        msgs.sort(key=lambda m: m.timestamp or 0)
        return msgs

    @staticmethod
    def _collect_tool_calls(tooloutput: str, msgs: List[Message], ts: float, turn_id: str = ""):
        if not tooloutput:
            return
        try:
            r = json.loads(tooloutput)
        except (TypeError, ValueError):
            return
        items = r if isinstance(r, list) else [r]
        for item in items:
            for ch in item.get("choices", []) or []:
                for tc in (ch.get("message", {}) or {}).get("tool_calls") or []:
                    fn = tc.get("function", {}) or {}
                    msgs.append(Message(
                        role="assistant", kind="tool_call",
                        tool_name=fn.get("name", ""),
                        tool_args=str(fn.get("arguments", "")),
                        call_id=str(tc.get("id") or ""),
                        turn_id=turn_id,
                        timestamp=ts))
