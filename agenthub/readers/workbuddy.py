# -*- coding: utf-8 -*-
"""WorkBuddy Desktop 会话读取器。

存储位置：
  ~/.workbuddy/workbuddy.db → sessions 表（元数据）
  ~/.workbuddy/projects/<cwd转义>/<sessionId>.jsonl → 会话 transcript（正文首选）
    逐行事件流：session-meta / message(user|assistant) / reasoning /
    function_call / function_call_result / file-history-snapshot / ai-title
  ~/.workbuddy/traces/<pid>/trace_*.json(l) → 每轮 LLM 调用的原始 trace（回退）
  ~/.workbuddy/changes-detail/<convId>/cd_*.json → 文件变更检查点

正文优先从 projects transcript 读取：文件名即 sessionId，归属精确、内容完整。
旧会话若没有 transcript，再回退到 traces（新版按 sessionId 归属，旧版按工作目录
与创建时间区间归属）。两者都没有时显式报错，不伪造正文。
"""

import functools
import glob
import json
import os
import re
from typing import List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import open_ro, tool_home, iter_jsonl

WB = tool_home(".workbuddy", "AGENTHUB_WORKBUDDY_HOME")
DB = os.path.join(WB, "workbuddy.db")
PROJECTS_ROOT = os.path.join(WB, "projects")
TRACES_ROOT = os.path.join(WB, "traces")

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
WORKSPACE_RE = re.compile(r"Workspace Folder:\s*([^\n]+)")

# transcript 里属于系统注入而非用户输入的前缀
SKIP_PREFIXES = (
    "<system-reminder", "<task-notification", "<command-name>",
    "<local-command", "<connector-status", "<user_info",
)


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


# ---------------------------------------------------------------- transcript

def transcript_path(session_id: str) -> str:
    """projects/<cwd转义>/<sessionId>.jsonl；找不到返回空串。"""
    hits = glob.glob(os.path.join(PROJECTS_ROOT, "*", f"{session_id}.jsonl"))
    if not hits:
        return ""
    return max(hits, key=os.path.getmtime)


def _user_text(o: dict) -> str:
    """transcript 中 role=user 行的真人输入；系统注入返回空串。"""
    text = _content_full_text(o.get("content")).strip()
    if not text:
        return ""
    m = USER_QUERY_RE.search(text)
    if m:
        return m.group(1).strip()
    if text.startswith(SKIP_PREFIXES):
        return ""
    return text


def _result_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return _content_full_text(output.get("text") or output.get("content"))
    return ""


@functools.lru_cache(maxsize=4096)
def _transcript_meta(path: str, signature) -> dict:
    """轻量扫描 transcript：只统计与归属信息，不构造 Message，便于 scan 快速跑完。"""
    n_user = n_asst = n_tool = 0
    first_ts = last_ts = 0
    cwd = title = first_user = model = ""
    for o in iter_jsonl(path):
        t = o.get("type")
        if not cwd and o.get("cwd"):
            cwd = str(o["cwd"])
        ts = ts_to_ms(o.get("timestamp"))
        if ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        if t == "message":
            role = o.get("role")
            if role == "user":
                txt = _user_text(o)
                if txt:
                    n_user += 1
                    if not first_user:
                        first_user = txt
            elif role == "assistant":
                if _content_full_text(o.get("content")).strip():
                    n_asst += 1
                m = (o.get("providerData") or {}).get("model")
                if m:
                    model = str(m)
        elif t == "function_call":
            n_tool += 1
        elif t in ("ai-title", "custom-title"):
            title = str(o.get("title") or o.get("customTitle") or title)
    return {
        "n_messages": n_user + n_asst, "n_tool_calls": n_tool,
        "started_at": first_ts, "updated_at": last_ts, "cwd": cwd,
        "title": title, "first_user": first_user, "model": model,
    }


def _transcript_messages(path: str) -> List[Message]:
    msgs: List[Message] = []
    for o in iter_jsonl(path):
        t = o.get("type")
        ts = ts_to_ms(o.get("timestamp")) / 1000.0
        mid = str(o.get("id") or "")
        base = {"message_id": mid, "source": path}
        if t == "message":
            role = o.get("role")
            text = _content_full_text(o.get("content")).strip()
            if role == "user":
                txt = _user_text(o)
                if txt:
                    msgs.append(Message(role="user", text=txt, timestamp=ts, **base))
            elif role == "assistant" and text:
                msgs.append(Message(role="assistant", text=text, timestamp=ts, **base))
        elif t == "reasoning":
            txt = _content_full_text(o.get("content")).strip()
            if txt:
                msgs.append(Message(role="assistant", kind="thinking", text=txt,
                                    timestamp=ts, **base))
        elif t == "function_call":
            args = o.get("arguments")
            msgs.append(Message(
                role="assistant", kind="tool_call",
                tool_name=str(o.get("name") or ""),
                tool_args=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                call_id=str(o.get("callId") or ""), timestamp=ts, **base))
        elif t == "function_call_result":
            msgs.append(Message(
                role="assistant", kind="tool_result",
                tool_name=str(o.get("name") or ""),
                tool_output=_result_text(o.get("output")),
                call_id=str(o.get("callId") or ""), timestamp=ts, **base))
    return msgs


# ---------------------------------------------------------------- traces（回退）

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
    """读取 trace 文件，返回 (trace 元数据 dict, span 列表)。

    兼容两种落盘格式：整份 JSON 对象（trace_*.json），以及逐行事件流（trace_*.jsonl，
    其中 span_start/span_end 事件携带 span，只取已结束的 span_end 避免重复）。
    """
    trace = {}
    spans = []
    try:
        if trace_path.endswith(".jsonl"):
            for ev in iter_jsonl(trace_path):
                t = ev.get("type")
                if t == "trace_start":
                    trace = ev.get("trace") or {}
                elif t == "span_end" and isinstance(ev.get("span"), dict):
                    spans.append(ev["span"])
        else:
            with open(trace_path, "r", encoding="utf-8", errors="replace") as f:
                o = json.load(f)
            trace = o.get("trace") or {}
            spans = [s for s in o.get("spans", []) or [] if isinstance(s, dict)]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"无法读取 WorkBuddy trace: {trace_path}") from exc
    spans.sort(key=lambda s: s.get("startedAt") or "")
    return trace, spans


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
    return os.path.normcase(str(path).replace("\\", "/").rstrip("/"))


def _file_sig(path):
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


@functools.lru_cache(maxsize=4096)
def _trace_meta(path, signature):
    # 仅缓存小型归属信息；按文件时间与大小失效，不把整份 spans 常驻内存。
    trace, spans = _load_spans(path)
    sid = trace.get("sessionId") or ""
    workspace = "" if sid else _trace_workspace(spans)
    return sid, ts_to_ms(trace.get("startedAt")), _norm(workspace)


def trace_files():
    return sorted(glob.glob(os.path.join(TRACES_ROOT, "*", "trace_*.json")) +
                  glob.glob(os.path.join(TRACES_ROOT, "*", "trace_*.jsonl")))


def trace_signature():
    return tuple((path, *_file_sig(path)) for path in trace_files())


def session_signature(session_id: str) -> tuple:
    """单个会话的源文件签名，用于消息缓存失效。

    有 transcript 时只看它（改它就够）；否则回退到 traces，此时任何 trace 变化都要失效。
    """
    tp = transcript_path(session_id)
    if tp:
        try:
            return ((tp, *_file_sig(tp)),)
        except OSError:
            pass
    return trace_signature()


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
                       "deleted": bool(deleted)},
            )
            tp = transcript_path(sid)
            if tp:
                meta = _transcript_meta(tp, _file_sig(tp))
                rec.extra["source"] = "transcript"
                rec.extra["file_path"] = tp
                rec.size_bytes = os.path.getsize(tp)
                if meta["cwd"]:
                    rec.cwd = meta["cwd"]
                # 优先级：用户自定义标题 > transcript 的 AI 标题 > 数据库标题 > 首条用户消息
                if not custom and (meta["title"] or not title):
                    rec.title = clip(meta["title"] or meta["first_user"], 60) or rec.title
                if meta["model"]:
                    rec.extra["model"] = meta["model"]
                if meta["n_messages"] or meta["n_tool_calls"]:
                    rec.n_messages = meta["n_messages"]
                    rec.n_tool_calls = meta["n_tool_calls"]
                    rec.extra["tool_count_status"] = "known"
                if meta["updated_at"]:
                    rec.extra["content_updated_at"] = meta["updated_at"]
            records.append(rec)
        return records

    def read_messages(self, path: str) -> List[Message]:
        """path 传 session_id；缺少任何正文来源时显式报错，不伪造会话。"""
        meta = self._session_meta(path)
        if not meta:
            return []
        tp = transcript_path(path)
        if tp:
            msgs = _transcript_messages(tp)
            if msgs:
                return msgs
        msgs = self._messages_from_traces(path, meta)
        if msgs:
            return msgs
        raise ValueError("本地没有可归属的 WorkBuddy 会话正文（transcript 与 trace 均缺失）")

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
        for tf in trace_files():
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
