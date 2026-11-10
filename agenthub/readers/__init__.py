# -*- coding: utf-8 -*-
"""读取器注册表：工具名 → Reader。"""

from dataclasses import replace
import hashlib
import json
from collections import Counter

from ..model import Message, split_think

from .claude import ClaudeReader
from .codex import CodexReader
from .dsh import DshReader
from .hermes import HermesReader
from .opencode import OpenCodeReader
from .workbuddy import WorkBuddyReader

READERS = {
    ClaudeReader.tool: ClaudeReader,
    CodexReader.tool: CodexReader,
    DshReader.tool: DshReader,
    HermesReader.tool: HermesReader,
    OpenCodeReader.tool: OpenCodeReader,
    WorkBuddyReader.tool: WorkBuddyReader,
}


def get_reader(tool: str):
    cls = READERS.get(tool)
    if not cls:
        raise KeyError(f"未知工具: {tool}，支持: {', '.join(READERS)}")
    return cls()


def read_record(rec):
    """读取一条索引记录的全部消息（数据库型工具按 session_id，文件型按路径），并统一后处理。"""
    reader = get_reader(rec.tool)
    by_id = getattr(reader, "uses_session_id", False) or rec.extra.get("read_by_id")
    key = rec.session_id if by_id else rec.file_path
    return normalize(reader.read_messages(key))


def normalize(msgs):
    """保留原生轮次与身份；没有轮次的工具按用户消息划分。"""
    out = []
    for m in msgs:
        if m.kind == "text" and m.role == "assistant" and "<think" in m.text:
            thought, visible = split_think(m.text)
            if thought:
                out.append(replace(m, kind="thinking", text=thought, final=False,
                                   message_id=(m.message_id + ":thinking") if m.message_id else ""))
            if visible:
                out.append(replace(m, text=visible, final=False))
        elif m.kind in ("text", "thinking", "event") and not m.text.strip():
            continue
        else:
            out.append(replace(m, final=False))
    # 工具结果常只带调用 ID（如 DSH）：按 call_id 回填工具名，界面上才看得出是谁的结果
    names = {m.call_id: m.tool_name for m in out if m.kind == "tool_call" and m.call_id}
    for m in out:
        if m.kind == "tool_result" and not m.tool_name and m.call_id in names:
            m.tool_name = names[m.call_id]
    last_by_turn = {}
    turn = 0
    occurrences = Counter()
    for m in out:
        # 内容指纹加出现次数：追加消息不会改变原有选择，同文重复发送也不会被合并。
        if not m.message_id:
            raw = json.dumps([m.role, m.kind, m.timestamp, m.text, m.tool_name,
                              m.tool_args, m.tool_output, m.call_id], ensure_ascii=False)
            m.message_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
        identity = m.message_id
        occurrences[identity] += 1
        if occurrences[identity] > 1:
            m.message_id += ":" + str(occurrences[identity])
        if m.kind != "text":
            continue
        if m.role == "user":
            turn += 1
        elif m.role == "assistant":
            key = ("native", m.turn_id) if m.turn_id else ("inferred", turn)
            last_by_turn[key] = m
    for m in last_by_turn.values():
        m.final = True
    return out
