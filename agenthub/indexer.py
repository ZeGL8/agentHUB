# -*- coding: utf-8 -*-
"""统一索引器：扫描全部工具 → index/index.json + index/index.md。"""

import json
import os
import time
from collections import defaultdict
from typing import List

from .model import SessionRecord
from dataclasses import replace
from .readers import READERS, get_reader

from . import DATA_DIR

INDEX_DIR = os.path.join(DATA_DIR, "index")


def scan_all(tools=None, previous: List[SessionRecord] = None):
    """扫描全部（或指定）工具；任意读取失败均报告且不发布新索引。"""
    tools = tools or list(READERS)
    records: List[SessionRecord] = []
    problems = []
    for t in tools:
        reader = get_reader(t)
        try:
            rs = reader.scan()
            records.extend(rs)
            problems += [f"[{t}] {e}" for e in getattr(reader, "errors", [])]
            print(f"  [{t}] {len(rs)} 个会话")
        except Exception as e:
            problems.append(f"[{t}] 扫描失败：{type(e).__name__}: {e}")
    for p in problems:
        print("  ⚠", p)
    if problems:
        raise RuntimeError("扫描未完成，原索引未替换：" + "; ".join(problems))
    return visible_records(records), problems


def session_key(tool, sid):
    return tool, sid.removeprefix("session-") if tool == "dsh" and isinstance(sid, str) else sid


def visible_records(records):
    """身份去重；删除标记优先，已删除子树不重新浮到顶层。"""
    chosen = {}
    hidden = {session_key(r.tool, r.session_id) for r in records
              if r.extra.get("deleted") or r.extra.get("hidden")}
    for r in records:
        key = session_key(r.tool, r.session_id)
        prior = chosen.get(key)
        if prior is None or (r.updated_at, r.file_path) > (prior.updated_at, prior.file_path):
            chosen[key] = r
    changed = True
    while changed:
        changed = False
        for key, r in chosen.items():
            if key not in hidden and r.extra.get("origin") == "subagent" and session_key(r.tool, r.extra.get("parent_session_id")) in hidden:
                hidden.add(key)
                changed = True
    return [r for key, r in chosen.items() if key not in hidden]


def live_records(records):
    """Web 与 CLI 共用原生可见性检查；过滤父任务时一起过滤其子代理。"""
    readers, allowed, checked = {}, {}, []
    for rec in visible_records(records):
        hidden = False
        if rec.file_path:
            hidden = not os.path.exists(rec.file_path)
            if not hidden:
                if rec.tool not in readers:
                    reader = readers[rec.tool] = get_reader(rec.tool)
                    allowed[rec.tool] = reader.visible_session_ids() if hasattr(reader, "visible_session_ids") else None
                reader = readers[rec.tool]
                ids = allowed[rec.tool]
                hidden = ids is not None and rec.session_id not in ids
                if rec.tool == "claude":
                    hidden = hidden or reader.is_deleted(rec.file_path)
        checked.append(replace(rec, extra={**rec.extra, "deleted": True}) if hidden else rec)
    return visible_records(checked)


def write_index(records: List[SessionRecord], index_dir: str = None, problems=None) -> str:
    index_dir = index_dir or INDEX_DIR
    os.makedirs(index_dir, exist_ok=True)

    records = sorted(visible_records(records), key=lambda r: r.updated_at or 0, reverse=True)

    # ---- index.json（先写临时文件再原子替换，避免读到半截文件）----
    jp = os.path.join(index_dir, "index.json")
    with open(jp + ".tmp", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": int(time.time() * 1000),
            "total": len(records),
            "problems": problems or [],
            "sessions": [r.to_dict() for r in records],
        }, f, ensure_ascii=False, indent=1)
    os.replace(jp + ".tmp", jp)

    # ---- index.md ----
    mp = os.path.join(index_dir, "index.md")
    with open(mp, "w", encoding="utf-8") as f:
        w = f.write
        w(f"# agentHUB 统一会话索引\n\n")
        w(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  \n")
        w(f"会话总数：**{len(records)}**\n\n")

        # 按工具统计
        w("## 一、按工具统计\n\n| 工具 | 会话数 | 消息总数 | 工具调用总数 |\n|---|---:|---:|---:|\n")
        by_tool = defaultdict(list)
        for r in records:
            by_tool[r.tool].append(r)
        for t in sorted(by_tool):
            rs = by_tool[t]
            w(f"| {t} | {len(rs)} | {sum(x.n_messages for x in rs)} | "
              f"{sum(x.n_tool_calls for x in rs)} |\n")
        w("\n")

        # 按工作目录聚合（跨工具找对话的核心视图）
        w("## 二、按工作目录聚合（跨工具迁移工作用）\n\n")
        w("| 工作目录 | 涉及工具 | 会话数 | 最近活动 |\n|---|---|---:|---|\n")
        by_cwd = defaultdict(list)
        for r in records:
            key = r.cwd or "(未知目录)"
            by_cwd[key].append(r)
        for cwd in sorted(by_cwd, key=lambda k: max(r.updated_at or 0 for r in by_cwd[k]), reverse=True):
            rs = by_cwd[cwd]
            ts = max(r.updated_at or 0 for r in rs)
            latest = time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else "-"
            tools_used = ", ".join(sorted({r.tool for r in rs}))
            w(f"| `{cwd}` | {tools_used} | {len(rs)} | {latest} |\n")
        w("\n")

        # 明细
        w("## 三、会话明细\n")
        for t in sorted(by_tool):
            rs = by_tool[t]
            w(f"\n### {t}（{len(rs)}）\n\n")
            w("| 最近活动 | 标题 | 消息 | 工具调用 | 大小 | 会话ID |\n|---|---|---:|---:|---:|---|\n")
            for r in rs[:80]:
                w(f"| {r.iso('updated')} | {r.title} | {r.n_messages} | "
                  f"{r.n_tool_calls} | {r.human_size()} | `{r.session_id[:12]}` |\n")
            if len(rs) > 80:
                w(f"\n（仅显示最近 80 条，共 {len(rs)} 条，完整数据见 index.json）\n")
        w("\n---\n")
        w("> 导出任一会话：`uv run agenthub export --tool <工具> --id <会话ID>`\n")
    return mp


def find_record(records: List[SessionRecord], tool: str, sid: str) -> SessionRecord:
    """按工具 + 会话 ID 查记录：精确匹配优先，否则前缀匹配（兼容 dsh 的 session- 前缀）。

    前缀命中多条时报错，而不是静默挑一条。
    """
    strip = lambda s: s.replace("session-", "", 1)
    rs = [r for r in records if r.tool == tool]
    exact = [r for r in rs if r.session_id == sid]
    if exact:
        return exact[0]
    cand = [r for r in rs if strip(r.session_id).startswith(strip(sid))]
    if not cand:
        raise KeyError(f"找不到 {tool} 的会话 {sid}")
    if len(cand) > 1:
        ids = ", ".join(r.session_id for r in cand[:5])
        raise KeyError(f"{tool} 会话 ID 前缀 {sid} 有 {len(cand)} 条匹配（{ids}…），请给出更长的 ID")
    return cand[0]


def load_problems(index_dir: str = None) -> list:
    """上次扫描记录的问题列表（索引不存在时为空）。"""
    try:
        with open(os.path.join(index_dir or INDEX_DIR, "index.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("problems", [])
    except FileNotFoundError:
        return []


def load_index(index_dir: str = None) -> List[SessionRecord]:
    index_dir = index_dir or INDEX_DIR
    jp = os.path.join(index_dir, "index.json")
    if not os.path.exists(jp):
        raise FileNotFoundError("索引不存在，请先运行：uv run agenthub scan")
    with open(jp, "r", encoding="utf-8") as f:
        o = json.load(f)
    return live_records([SessionRecord.from_dict(d) for d in o.get("sessions", [])])
