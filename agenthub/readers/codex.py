# -*- coding: utf-8 -*-
"""Codex (Desktop/CLI) 会话读取器。

存储位置：
  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl   （活跃）
  ~/.codex/archived_sessions/rollout-*.jsonl               （归档）
  ~/.codex/session_index.jsonl                             （id→中文标题索引）

rollout 格式：
  首行 type=session_meta（payload 含 cwd/originator/model）
  type=response_item：payload.type = message / function_call / function_call_output
  message 的 role=user 时 content 数组 [{type:input_text, text}]，
  其中 <environment_context> / <permissions 等开头的是系统注入，需过滤。

新版 Codex 还把每个线程的条目投影到 ~/.codex/thread_history_1.sqlite（thread_items，
每条一个类型化 JSON）。rollout 文件被清理后线程仍在 state_5.sqlite 中可见，此时从这里读取。
"""

import glob
import json
import os
import sqlite3
from typing import Dict, List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import iter_jsonl as _iter_jsonl, tool_home, open_ro

ROOT = tool_home(".codex", "AGENTHUB_CODEX_HOME", "CODEX_HOME")
HISTORY_DB = os.path.join(ROOT, "thread_history_1.sqlite")
# thread_history 中算作工具调用的条目类型
TOOL_ITEMS = ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall",
              "imageView", "imageGeneration", "collabAgentToolCall")

# 系统注入的用户上下文（不是真实用户说的话）
INJECT_PREFIXES = ("<environment_context>", "<permissions", "<user_instructions>",
                   "<turn_context>", "<AGENTS", "<sandbox", "<runtime",
                   "<recommended_plugins>", "# AGENTS.md instructions", "<turn_aborted>",
                   "<in-app-browser-context", "<skill>", "# In app browser:")


class CodexReader:
    tool = "codex"

    def _live_threads(self):
        db = os.path.join(ROOT, "state_5.sqlite")
        if not os.path.exists(db):
            return None
        with open_ro(db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"threads", "backfill_state"} <= tables:
                return None
            status = conn.execute("SELECT status FROM backfill_state LIMIT 1").fetchone()
            if not status or status[0] != "complete":
                return None
            return dict(conn.execute("SELECT id, rollout_path FROM threads"))

    def visible_session_ids(self):
        live = self._live_threads()
        return set(live) if live is not None else None

    def _thread_meta(self) -> Dict[str, dict]:
        """state_5.sqlite 中各线程的元数据（标题、目录、时间、模型、父线程）。"""
        db = os.path.join(ROOT, "state_5.sqlite")
        if not os.path.exists(db):
            return {}
        with open_ro(db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM threads").fetchall()
        meta = {}
        for r in rows:
            r = dict(r)
            try:
                spawn = (json.loads(r.get("source") or "{}").get("subagent") or {})
            except (ValueError, AttributeError):
                spawn = {}
            spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) else None
            r["parent"] = (spawn or {}).get("parent_thread_id") or ""
            r["is_subagent"] = r.get("thread_source") == "subagent"
            meta[r["id"]] = r
        return meta

    def _history_records(self, meta: Dict[str, dict], skip) -> List[SessionRecord]:
        """rollout 文件已不存在、但 thread_history 中有条目的线程。"""
        if not os.path.exists(HISTORY_DB):
            return []
        with open_ro(HISTORY_DB) as conn:
            rows = conn.execute(
                "SELECT thread_id, item_type, count(*), sum(length(item_json)), "
                "min(created_at_ms), max(created_at_ms) FROM thread_items GROUP BY thread_id, item_type"
            ).fetchall()
        stats = {}
        for tid, typ, n, size, first, last in rows:
            st = stats.setdefault(tid, {"msg": 0, "tool": 0, "size": 0, "first": first, "last": last})
            st["msg"] += n if typ in ("userMessage", "agentMessage") else 0
            st["tool"] += n if typ in TOOL_ITEMS else 0
            st["size"] += size or 0
            st["first"], st["last"] = min(st["first"], first), max(st["last"], last)
        out = []
        for tid, st in stats.items():
            m = meta.get(tid)
            if tid in skip or m is None:  # 不在线程表里的视为已被 Codex 删除
                continue
            title = m.get("name") or m.get("title") or clip(m.get("first_user_message") or "", 60)
            out.append(SessionRecord(
                tool=self.tool, session_id=tid, title=title or "(无标题)", cwd=m.get("cwd") or "",
                file_path=HISTORY_DB,
                started_at=m.get("created_at_ms") or st["first"],
                updated_at=max(m.get("updated_at_ms") or 0, st["last"] or 0),
                n_messages=st["msg"], n_tool_calls=st["tool"], size_bytes=st["size"],
                extra={"model": m.get("model") or "", "read_by_id": True,
                       "visibility_source": "thread_history"}))
        return out

    def scan(self) -> List[SessionRecord]:
        titles = self._load_title_index()
        records, self.errors = {}, []
        live = self._live_threads()
        patterns = [
            os.path.join(ROOT, "sessions", "**", "rollout-*.jsonl"),
            os.path.join(ROOT, "archived_sessions", "rollout-*.jsonl"),
        ]
        files = []
        for pat in patterns:
            files.extend(glob.glob(pat, recursive=True))
        for fp in sorted(set(files)):
            try:
                record = self._scan_file(fp, titles)
            except Exception as e:  # 单个文件坏了只跳过它并报告，不连累其他会话
                self.errors.append(f"{fp}: {type(e).__name__}: {e}")
                continue
            if live is not None and record.session_id not in live:
                continue
            previous = records.get(record.session_id)
            preferred = live.get(record.session_id) if live is not None else None
            rank = lambda r: (r.file_path == preferred, r.updated_at, r.size_bytes)
            if previous is None or rank(record) > rank(previous):
                record.extra["visibility_source"] = "state_threads" if live is not None else "rollout_files_unverified"
                records[record.session_id] = record
        meta = self._thread_meta()
        out = list(records.values()) + self._history_records(meta, set(records))
        for r in out:  # 子代理线程挂到父线程下（与 DSH 共用任务树）
            m = meta.get(r.session_id) or {}
            if m.get("is_subagent"):
                r.extra.update(origin="subagent", parent_session_id=m["parent"],
                               label=m.get("agent_nickname") or m.get("agent_path") or "")
        return out

    def _load_title_index(self) -> Dict[str, str]:
        titles: Dict[str, str] = {}
        idx = os.path.join(ROOT, "session_index.jsonl")
        if not os.path.exists(idx):
            return titles
        for o in _iter_jsonl(idx):
            if o.get("id") and o.get("thread_name"):
                titles[o["id"]] = o["thread_name"]
        return titles

    def _scan_file(self, fp: str, titles: Dict[str, str]) -> SessionRecord:
        sid, cwd, first_ts, last_ts = "", "", 0, 0
        n_user, n_asst, n_tool = 0, 0, 0
        first_user_text, model = "", ""
        for o in _iter_jsonl(fp):
            ts = ts_to_ms(o.get("timestamp"))
            if ts:
                last_ts = ts
                if not first_ts:
                    first_ts = ts
            p = o.get("payload", {}) or {}
            t = p.get("type")
            if o.get("type") == "turn_context" and p.get("model"):
                model = p["model"]  # 取最后使用的模型
            if o.get("type") == "session_meta":
                sid = p.get("session_id") or p.get("id") or ""
                cwd = p.get("cwd") or ""
            elif t == "message":
                role = p.get("role")
                if role == "user":
                    txt = self._msg_text(p)
                    if txt and not txt.startswith(INJECT_PREFIXES):
                        n_user += 1
                        if not first_user_text:
                            first_user_text = txt
                elif role == "assistant":
                    n_asst += 1
            elif t in ("function_call", "custom_tool_call"):
                n_tool += 1
        if not sid:
            sid = os.path.basename(fp).replace("rollout-", "")[:-6]
        title = titles.get(sid) or clip(first_user_text, 60)
        return SessionRecord(
            tool=self.tool, session_id=sid, title=title or "(无标题)",
            cwd=cwd, file_path=fp, started_at=first_ts, updated_at=last_ts,
            n_messages=n_user + n_asst, n_tool_calls=n_tool,
            size_bytes=os.path.getsize(fp),
            extra={"model": model} if model else {},
        )

    def read_messages(self, path: str) -> List[Message]:
        """path 为 rollout 文件；若不是文件，则视为线程 ID，从 thread_history 读取。"""
        if not os.path.isfile(path):
            return self._read_history(path)
        msgs: List[Message] = []
        for o in _iter_jsonl(path):
            ts = ts_to_ms(o.get("timestamp")) / 1000.0
            if o.get("type") != "response_item":
                continue
            p = o.get("payload", {}) or {}
            t = p.get("type")
            identity = {"message_id": str(p.get("id") or ""), "call_id": str(p.get("call_id") or ""), "source": path}
            if t == "message":
                role = p.get("role")
                if role == "user":
                    txt = self._msg_text(p)
                    if txt and not txt.startswith(INJECT_PREFIXES):
                        msgs.append(Message(role="user", text=txt, timestamp=ts, **identity))
                elif role == "assistant":
                    txt = self._msg_text(p)
                    if txt:
                        msgs.append(Message(role="assistant", text=txt, timestamp=ts, **identity))
            elif t == "reasoning":
                # 推理正文是加密的，只有 summary 可读（常为空）
                txt = "\n".join(x.get("text", "") for x in p.get("summary") or []
                                if isinstance(x, dict))
                if txt.strip():
                    msgs.append(Message(role="assistant", kind="thinking", text=txt, timestamp=ts, **identity))
            elif t in ("function_call", "custom_tool_call"):
                msgs.append(Message(
                    role="assistant", kind="tool_call",
                    tool_name=p.get("name", ""),
                    tool_args=str(p.get("arguments", p.get("input", ""))),
                    timestamp=ts, **identity))
            elif t in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output", "")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                msgs.append(Message(role="assistant", kind="tool_result",
                                    tool_output=str(out), timestamp=ts, **identity))
        return msgs

    def _read_history(self, thread_id: str) -> List[Message]:
        with open_ro(HISTORY_DB) as conn:
            rows = conn.execute(
                "SELECT turn_id, item_json, created_at_ms FROM thread_items WHERE thread_id = ? "
                "ORDER BY rollout_ordinal, created_at_ms", (thread_id,)).fetchall()
        if not rows:
            raise KeyError(f"thread_history 中没有线程 {thread_id}")
        msgs: List[Message] = []
        for turn_id, raw, ms in rows:
            it = json.loads(raw)
            msgs.extend(_history_item(it, turn_id, (ms or 0) / 1000.0))
        return msgs

    @staticmethod
    def _msg_text(p: dict) -> str:
        parts = []
        for b in p.get("content") or []:
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
                parts.append(b.get("text", ""))
        return "\n".join(parts).strip()


def _texts(items) -> str:
    """[{type:text, text}, ...] 或字符串 → 纯文本。"""
    if isinstance(items, str):
        return items
    return "\n".join(x.get("text", "") for x in items or [] if isinstance(x, dict)).strip()


def _dump(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _history_item(it: dict, turn_id: str, ts: float) -> List[Message]:
    """thread_history 的一条类型化条目 → 统一消息（工具类拆成调用 + 结果两条）。"""
    t, iid = it.get("type", ""), str(it.get("id") or "")
    base = dict(timestamp=ts, turn_id=turn_id, message_id=iid, call_id=iid, source="thread_history")
    call = lambda name, args: Message(role="assistant", kind="tool_call", tool_name=name,
                                      tool_args=_dump(args), **base)
    result = lambda name, out: Message(role="assistant", kind="tool_result", tool_name=name,
                                       tool_output=_dump(out), **dict(base, message_id=iid + ":result"))
    if t == "userMessage":
        text = _texts(it.get("content"))
        return [Message(role="user", text=text, **base)] if text else []
    if t == "agentMessage":
        return [Message(role="assistant", text=it.get("text") or "", **base)]
    if t == "reasoning":
        text = _texts(it.get("summary")) or _texts(it.get("content"))
        return [Message(role="assistant", kind="thinking", text=text, **base)] if text else []
    if t == "commandExecution":
        out = it.get("aggregatedOutput") or ""
        if it.get("exitCode") not in (None, 0):
            out = f"[exit {it.get('exitCode')}]\n{out}"
        return [call("shell", it.get("command", "")), result("shell", out)]
    if t == "fileChange":
        paths = [c.get("path", "") for c in it.get("changes") or [] if isinstance(c, dict)]
        return [call("apply_patch", "\n".join(paths)), result("apply_patch", it.get("status", ""))]
    if t == "mcpToolCall":
        name = f"{it.get('server', '')}.{it.get('tool', '')}"
        return [call(name, it.get("arguments", "")), result(name, it.get("error") or it.get("result") or "")]
    if t == "dynamicToolCall":
        name = f"{it.get('namespace', '')}.{it.get('tool', '')}"
        return [call(name, it.get("arguments", "")), result(name, _texts(it.get("contentItems")))]
    if t == "webSearch":
        return [call("web_search", it.get("query", ""))]
    if t == "imageView":
        return [call("view_image", it.get("path", ""))]
    if t == "imageGeneration":  # result 是整张图片的 base64，只保留保存路径
        return [call("image_generation", it.get("revisedPrompt", "")),
                result("image_generation", it.get("savedPath") or it.get("status", ""))]
    if t == "collabAgentToolCall":
        return [call("agent." + str(it.get("tool", "")), it.get("prompt") or "")]
    if t == "subAgentActivity":
        return [Message(role="system", kind="event", link=str(it.get("agentThreadId") or ""),
                        text=f"子代理{ {'started': '启动'}.get(it.get('kind'), it.get('kind') or '')}："
                             f"{it.get('agentPath') or ''}", **dict(base, source="codex:subagent-activity"))]
    if t == "contextCompaction":
        return [Message(role="system", kind="event", text="上下文已压缩",
                        **dict(base, source="codex:context-compaction"))]
    return []  # sleep 等无内容条目
