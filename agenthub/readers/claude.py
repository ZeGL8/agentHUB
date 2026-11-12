# -*- coding: utf-8 -*-
"""Claude Code 会话读取器。

存储位置：~/.claude/projects/<cwd转义>/<sessionId>.jsonl
格式：JSONL 事件流，每行 {type, sessionId, cwd, timestamp, message{role, content}}。
  - type=user    content 为字符串 → 真实用户输入；为数组 → tool_result 回传
  - type=assistant  content 数组含 thinking / text / tool_use 块
  - type=custom-title  会话标题
  - type=queue-operation  用户排队消息（与随后 user 行重复，跳过）
"""

import json
import re
import os
from typing import List

from ..model import SessionRecord, Message, ts_to_ms, clip
from .base import iter_jsonl as _iter_jsonl, tool_home

ROOT = os.path.join(tool_home(".claude", "AGENTHUB_CLAUDE_HOME", "CLAUDE_CONFIG_DIR"), "projects")

# 这些块/行在导出时跳过
SKIP_PREFIXES = ("<system-reminder", "<command-name>", "<local-command",
                 "<task-notification", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
BASH_RE = re.compile(r"<bash-(input|stdout|stderr)>(.*?)</bash-\1>", re.S)


def _bash_text(o: dict) -> str:
    """用户在 Claude Code 里用「!」直接执行的终端命令（不是对 AI 说的话）→ 可读文本；否则空串。"""
    c = (o.get("message") or {}).get("content")
    if not isinstance(c, str) or not c.startswith(("<bash-input>", "<bash-stdout>", "<bash-stderr>")):
        return ""
    parts = {k: v.strip() for k, v in BASH_RE.findall(c)}
    out = "\n".join(x for x in (parts.get("stdout"), parts.get("stderr")) if x)
    return "\n".join(x for x in (parts.get("input") and "$ " + parts["input"], out) if x)


def _user_text(o: dict) -> str:
    """type=user 行中真人输入的正文；工具结果 / 系统注入 / 压缩摘要返回空串。

    content 可能是字符串，也可能是 [{type:text}, {type:image}] 数组（带图片的输入）。
    """
    if o.get("isMeta") or o.get("isCompactSummary"):
        return ""
    c = (o.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        c = "\n".join(b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or c.startswith(SKIP_PREFIXES):
        return ""
    return c


class ClaudeReader:
    tool = "claude"

    def scan(self) -> List[SessionRecord]:
        records, self.errors, deleted_ids = {}, [], set()
        if not os.path.isdir(ROOT):
            return []
        for proj in sorted(os.listdir(ROOT)):
            pdir = os.path.join(ROOT, proj)
            if not os.path.isdir(pdir):
                continue
            for fn in sorted(os.listdir(pdir)):
                if not fn.endswith(".jsonl"):
                    continue
                fp = os.path.join(pdir, fn)
                try:
                    record = self._scan_file(fp, fn[:-6])
                    if self.is_deleted(fp):
                        deleted_ids.add(record.session_id)
                        continue
                except Exception as e:  # 单个文件坏了只跳过它并报告，不连累其他会话
                    self.errors.append(f"{fp}: {type(e).__name__}: {e}")
                    continue
                previous = records.get(record.session_id)
                if previous is None or (record.updated_at, record.size_bytes) > (previous.updated_at, previous.size_bytes):
                    records[record.session_id] = record
        return [r for sid, r in records.items() if sid not in deleted_ids]

    def is_deleted(self, path: str) -> bool:
        marker = path.removesuffix(".jsonl") + ".desktop-released.json"
        if not os.path.exists(marker):
            return False
        with open(marker, encoding="utf-8") as f:
            released = json.load(f)
        # Desktop writes this marker when deletion cannot safely reap the transcript.
        return released.get("v") == 1 and released.get("reason") == "delete"

    def _scan_file(self, fp: str, sid: str) -> SessionRecord:
        title, cwd, first_ts, last_ts = "", "", 0, 0
        n_user, n_asst, n_tool = 0, 0, 0
        first_user_text, model = "", ""
        for o in _iter_jsonl(fp):
            t = o.get("type")
            if t in ("user", "assistant") and o.get("sessionId"):
                sid = o["sessionId"]
            ts = ts_to_ms(o.get("timestamp"))
            if ts:
                last_ts = ts
                if not first_ts:
                    first_ts = ts
            if not cwd and o.get("cwd"):
                cwd = o["cwd"]
            if t == "custom-title" and o.get("customTitle"):
                title = o["customTitle"]
            elif t == "user":
                txt = _user_text(o)
                if txt:
                    n_user += 1
                    if not first_user_text:
                        first_user_text = txt
            elif t == "assistant":
                n_asst += 1
                m = (o.get("message") or {}).get("model")
                if m and m != "<synthetic>":  # <synthetic> 是中断/报错等合成消息
                    model = m  # 取最后使用的模型
                n_tool += sum(
                    1 for b in ((o.get("message") or {}).get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "tool_use")
        if not title:
            title = clip(first_user_text, 60)
        return SessionRecord(
            tool=self.tool, session_id=sid, title=title or "(无标题)",
            cwd=cwd, file_path=fp, started_at=first_ts, updated_at=last_ts,
            n_messages=n_user + n_asst, n_tool_calls=n_tool,
            size_bytes=os.path.getsize(fp),
            extra={"model": model} if model else {},
        )

    def read_messages(self, path: str) -> List[Message]:
        if self.is_deleted(path):
            return []
        msgs: List[Message] = []
        for o in _iter_jsonl(path):
            t = o.get("type")
            ts = ts_to_ms(o.get("timestamp")) / 1000.0
            msg = o.get("message", {}) or {}
            identity = {"message_id": str(o.get("uuid") or msg.get("id") or ""), "source": path}
            if t == "user":
                c = msg.get("content")
                txt = _user_text(o)
                if txt:
                    msgs.append(Message(role="user", text=txt, timestamp=ts, **identity))
                elif _bash_text(o):
                    msgs.append(Message(role="system", kind="event", text=_bash_text(o), timestamp=ts,
                                        **dict(identity, source="claude:bash")))
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            out = self._tool_result_text(b)
                            msgs.append(Message(
                                role="user", kind="tool_result",
                                tool_output=out, call_id=b.get("tool_use_id", ""), timestamp=ts, **identity))
            elif t == "assistant":
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        msgs.append(Message(role="assistant", text=b.get("text", ""),
                                            timestamp=ts, **identity))
                    elif bt == "thinking" and b.get("thinking"):
                        msgs.append(Message(role="assistant", kind="thinking",
                                            text=b["thinking"], timestamp=ts, **identity))
                    elif bt == "tool_use":
                        msgs.append(Message(
                            role="assistant", kind="tool_call",
                            tool_name=b.get("name", ""),
                            tool_args=json.dumps(b.get("input", {}), ensure_ascii=False),
                            call_id=b.get("id", ""),
                            timestamp=ts, **identity))
        return msgs

    @staticmethod
    def _tool_result_text(b: dict) -> str:
        c = b.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [x.get("text", "") for x in c
                     if isinstance(x, dict) and x.get("type") == "text"]
            return "\n".join(parts)
        return ""
