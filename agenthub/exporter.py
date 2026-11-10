# -*- coding: utf-8 -*-
"""通用导出器：任一工具的会话 → 标准 Markdown 交接文档。

产出的 Markdown 可直接投喂给任何 agent（"请接手这段工作"）。
"""

import os
import re
import time
from typing import List

from .model import SessionRecord, Message, is_chat, VIEWS
from .readers import get_reader, read_record

from . import DATA_DIR

DEFAULT_EXPORT_DIR = os.path.join(DATA_DIR, "exports")


def _safe_name(s: str, n: int = 40) -> str:
    s = re.sub(r"[\s/\\:*?\"<>|\u3000]+", "_", (s or "").strip())
    return s[:n] or "untitled"


def render_markdown(rec: SessionRecord, msgs: List[Message],
                    mode: str = "final", excerpt_of: int = 0) -> str:
    """渲染会话为 Markdown 交接文档。

    mode:
      final — 仅用户消息 + 助手最终文本回复（交接首选，无中间过程噪音）
      full  — 含工具调用与工具结果全文的完整过程
    excerpt_of > 0 表示 msgs 是用户从全部 excerpt_of 条记录中手工选出的节选，原样全部保留。
    """
    if excerpt_of:
        mode = "full"
    out = []
    a = out.append
    a(f"# {'会话节选' if excerpt_of else '会话交接文档'}：{rec.title}")
    a("")
    desc = (f"节选：从 {excerpt_of} 条记录中手工选出 {len(msgs)} 条，按原顺序排列。"
            if excerpt_of else MODE_DESC[mode])
    a(f"> 由 agentHUB 从 **{rec.tool}** 导出。{desc}")
    a("")
    a("| 项目 | 值 |")
    a("|---|---|")
    a(f"| 来源工具 | {rec.tool} |")
    a(f"| 会话 ID | `{rec.session_id}` |")
    if rec.cwd:
        a(f"| 原工作目录 | `{rec.cwd}` |")
    if rec.started_at:
        a(f"| 开始时间 | {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.started_at/1000))} |")
    if rec.updated_at:
        a(f"| 最近活动 | {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.updated_at/1000))} |")
    a(f"| 对话规模 | {rec.n_messages} 条消息、{rec.n_tool_calls} 次工具调用 |")
    if rec.extra.get("model"):
        a(f"| 模型 | {rec.extra['model']} |")
    a("")
    a("---")
    a("")

    shown = [m for m in msgs if _keep(m, mode)]
    texts = [m for m in shown if m.kind == "text" and m.text]
    n_user = sum(1 for m in texts if m.role == "user")
    n_asst = len(texts) - n_user
    a("## 对话记录" + ("（节选）" if excerpt_of else MODE_TITLE[mode]))
    a("")
    for m in shown:
        block = message_md(m)
        if block:
            a(block)
            a("")

    a("---")
    a("")
    a("## 接手指令（可直接使用）")
    a("")
    a("把本文档连同下面这段话发给接手的 AI agent：")
    a("")
    a("```text")
    a(f"你将接手一段已在其他工具（{rec.tool}）中进行的对话工作。"
      f"请先阅读本交接文档中的对话记录，理解任务目标与已达成结论；"
      f"再检查工作目录 {rec.cwd or '（见元数据）'} 的当前文件状态，"
      f"对齐实际进度后继续完成任务。不要重做已完成的部分。")
    a("```")
    a("")
    a(f"共 {n_user} 条用户消息、{n_asst} 条助手回复。")
    return "\n".join(out)


def message_md(m: Message) -> str:
    """单条消息的 Markdown 片段：交接文档、AI 总结输入共用同一格式。"""
    if m.kind == "text" and m.text:
        who = "用户" if m.role == "user" else "助手"
        ts = f"  \n*{m.display_ts()}*" if m.timestamp else ""
        return f"### {who}{ts}\n\n{m.text.strip()}"
    if m.kind == "event":
        return f"### 协作事件 · {m.source or '系统'}\n\n{m.text.strip()}"
    if m.kind == "thinking":
        return f"<details><summary>思考</summary>\n\n{m.text.strip()}\n\n</details>"
    if m.kind == "tool_call":
        return f"### 工具调用 `{m.tool_name}`\n\n" + "\n".join("> " + line for line in m.tool_args.splitlines())
    if m.kind == "tool_result" and m.tool_output:
        return f"### 工具结果 `{m.tool_name or m.call_id}`\n\n" + "\n".join("> " + line for line in m.tool_output.splitlines())
    return ""


MODE_DESC = {
    "final": "已按「仅最终输出」模式导出：只含用户消息与每轮助手的最终回复，省略思考、过程说明、工具调用与结果。",
    "full": "按完整过程导出：含思考、过程说明、工具调用与结果全文。",
    "user": "已按「只看用户」导出：只含用户输入。",
    "ai": "已按「只看 AI」导出：只含每轮 AI 的最终回答。",
}
MODE_TITLE = {"final": "（仅最终输出）", "full": "（完整过程）", "user": "（只看用户）", "ai": "（只看 AI）"}


def _keep(m: Message, mode: str) -> bool:
    """full 保留全部；user / ai 按视图过滤；其余（final）只保留用户消息与每轮最终回复。"""
    return mode == "full" or VIEWS.get(mode, is_chat)(m)


def export_session(rec: SessionRecord, out_dir: str = None,
                   mode: str = "final") -> str:
    """导出一条会话，返回生成的 Markdown 文件路径。"""
    out_dir = out_dir or DEFAULT_EXPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    md = render_markdown(rec, read_record(rec), mode)
    fname = f"{rec.tool}__{_safe_name(rec.session_id, 12)}__{_safe_name(rec.title)}__{mode}.md"
    fp = os.path.join(out_dir, fname)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(md)
    return fp


def export_by_id(tool: str, session_id: str, out_dir: str = None,
                 index_dir: str = None, mode: str = "final") -> str:
    """按工具名 + 会话 ID（支持前缀匹配）导出。"""
    from .indexer import load_index, find_record
    rec = find_record(load_index(index_dir), tool, session_id)
    return export_session(rec, out_dir, mode=mode)


def export_by_path(tool: str, path: str, out_dir: str = None,
                   mode: str = "final") -> str:
    """按会话文件路径直接导出（不经过索引）。"""
    path = os.path.abspath(os.path.expanduser(path))
    recs = [r for r in get_reader(tool).scan() if r.file_path == path]
    if recs:
        rec = recs[0]
    else:
        raise KeyError("该文件不属于当前可见会话，不能导出已删除或未识别的记录")
    return export_session(rec, out_dir, mode=mode)
