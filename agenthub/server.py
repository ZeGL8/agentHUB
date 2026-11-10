# -*- coding: utf-8 -*-
"""agentHUB Web UI 服务器 — 纯标准库 http.server 实现，只监听本机。

启动：agenthub serve [--port 8322]   →   http://127.0.0.1:8322

API：
  GET  /api/stats                       统计（按工具）
  GET  /api/sessions?tool=&q=&cwd=      会话列表（过滤 + 分页）
  GET  /api/cwd                         按工作目录聚合
  GET  /api/session/{tool}/{id}         会话消息（offset 分页）
  GET  /api/export/{tool}/{id}          导出 Markdown 交接文档（落盘 + 返回全文）
  POST /api/excerpt/{tool}/{id}         按消息序号导出节选 {"indices": [...]}
  POST /api/scan                        重扫全部工具并重建索引
  POST /api/export-multi                多会话合并导出 {"items": [{tool, id}], "mode"}
  GET  /api/llm/config                  模型配置（不含 Key）；POST 保存
  POST /api/llm/models                  模型列表；POST /api/llm/test 测试连接
  POST /api/summary                     AI 总结（SSE 流）{"items": [{tool, id, indices?}], "template", "prompt"}
"""

import functools
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .indexer import (INDEX_DIR, find_record as _find, load_index, load_problems, scan_all,
                      write_index, live_records, session_key)
from . import llm
from .exporter import export_session, message_md, render_markdown, _safe_name
from .model import is_chat, VIEWS
from .readers import read_record

WEB_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("AGENTHUB_PORT", "8322"))
MAX_MSG_PAGE = 200     # 消息接口分页大小
RESCAN_MINUTES = int(os.environ.get("AGENTHUB_RESCAN_MINUTES", "30"))
# 只接受这些 Host / Origin，防止 DNS rebinding 与跨站请求读取本地对话（main() 按实际端口设置）
ALLOWED_HOSTS = set()
# 显式指定常用静态类型：Windows 上 mimetypes 读注册表，.js 可能被识别成 text/plain
STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".woff2": "font/woff2", ".svg": "image/svg+xml"}

_records = []          # 内存缓存（索引快照）
_last_scan_at = 0      # 上次扫描时间戳（ms）
_summary_jobs = {}
_summary_recent = {}  # request_id -> (过期时间, 已取消)，保留5分钟防止迟到请求重启任务
SUMMARY_REQUEST_TTL = 300
_summary_lock = threading.Lock()
_problems = []         # 上次扫描发现的问题（读不了的文件 / 工具）
_scan_lock = threading.Lock()  # 定时重扫与手动重扫互斥，避免并发写 index


def _do_scan() -> int:
    """全量重扫并刷新缓存，返回会话数。"""
    global _records, _last_scan_at
    with _scan_lock:
        try:
            records, problems = scan_all(previous=_records)
        except RuntimeError as e:
            _problems[:] = [str(e)]  # 原索引保留不动，问题在页面顶部可见
            raise
        write_index(records, problems=problems)
        _records, _problems[:] = records, problems
        _cached_messages.cache_clear()
        _last_scan_at = int(time.time() * 1000)
    return len(records)


def _auto_rescan_loop():
    """后台定时重扫（默认 30 分钟，AGENTHUB_RESCAN_MINUTES 可调）。"""
    while True:
        time.sleep(RESCAN_MINUTES * 60)
        try:
            n = _do_scan()
            print(f"[auto-rescan {time.strftime('%H:%M:%S')}] {n} 个会话")
        except Exception as e:  # noqa: BLE001
            print(f"[auto-rescan] 失败: {e}")


def refresh_cache():
    global _records
    _records = load_index()
    _problems[:] = load_problems()


def find_record(tool: str, sid: str):
    return _find(current_records(), tool, sid)


def current_records():
    return live_records(_records)


def _source_sig(path: str, tool: str = "") -> tuple:
    """会话源文件（及 SQLite 的 -wal）的修改时间，源数据一变缓存即失效。"""
    signature = (path,) + tuple((os.stat(p).st_mtime_ns, os.stat(p).st_size) if os.path.exists(p) else (0, 0)
                               for p in (path, path + "-wal"))
    if tool == "workbuddy":
        from .readers.workbuddy import trace_signature
        signature += trace_signature()
    return signature


# 最近8个会话，数据库与 WorkBuddy trace 文件变化均触发失效。
@functools.lru_cache(maxsize=8)
def _cached_messages(tool: str, session_id: str, sig: tuple):
    return read_record(find_record(tool, session_id))


def msg_dict(i: int, m) -> dict:
    d = {
        "i": i,  # 在整个会话中的序号：多选 / 节选导出按它定位，与视图和分页无关
        "role": m.role, "kind": m.kind, "text": m.text or "",
        "tool_name": m.tool_name or "", "tool_args": m.tool_args or "",
        "tool_output": m.tool_output or "",
        "ts": m.display_ts(),
        "final": m.final, "id": m.message_id, "turn_id": m.turn_id,
        "call_id": m.call_id, "source": m.source, "link": m.link,
    }
    return d


def record_dict(rec):
    sd = rec.to_dict()
    sd.update(iso=rec.iso("updated") or rec.iso("start"), iso2=rec.iso("start"))
    return sd


def session_payload(tool: str, sid: str, offset: int, order: str = "asc",
                    view: str = "full", query: str = "", anchor: str = "") -> dict:
    if offset < 0:
        raise ValueError("offset 不能为负数")
    rec = find_record(tool, sid)
    msgs = list(enumerate(_cached_messages(rec.tool, rec.session_id, _source_sig(rec.file_path, rec.tool))))
    if view in VIEWS:
        msgs = [(i, m) for i, m in msgs if VIEWS[view](m)]
    if order == "desc":
        msgs.reverse()
    matches = [m.message_id for _, m in msgs if query and query.casefold() in
               " ".join((m.text, m.tool_name, m.tool_args, m.tool_output)).casefold()]
    if anchor or (matches and offset == 0):
        target = anchor or matches[0]
        position = next((i for i, (_, m) in enumerate(msgs) if m.message_id == target), None)
        if position is not None:
            offset = (position // MAX_MSG_PAGE) * MAX_MSG_PAGE
    session = record_dict(rec)
    # 直接子任务（DSH 子代理 / 派生会话），供头部列表跳转；父 ID 可能带或不带 session- 前缀
    bare = lambda x: (x or "").replace("session-", "", 1)
    session["children"] = [
        {"tool": c.tool, "session_id": c.session_id, "title": c.title, "iso": c.iso("updated"),
         "origin": c.extra.get("origin", ""), "n_messages": c.n_messages}
        for c in sorted(current_records(), key=lambda c: c.started_at or 0)
        if c.tool == rec.tool and c.extra.get("parent_session_id")
        and bare(c.extra["parent_session_id"]) == bare(rec.session_id)]
    return {"session": session, "total": len(msgs), "offset": offset,
            "order": order, "matches": matches,
            "messages": [msg_dict(i, m) for i, m in msgs[offset:offset + MAX_MSG_PAGE]]}


def selected_messages(msgs, selection, default_chat=True):
    if "message_ids" in selection:
        ids = selection["message_ids"]
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError("message_ids 必须为字符串数组")
        found = {m.message_id: m for m in msgs}
        if not ids or any(i not in found for i in ids):
            raise ValueError("所选消息已变化或不存在，请重新选择")
        wanted = set(ids)
        return [m for m in msgs if m.message_id in wanted]
    if "indices" in selection:
        indices = selection["indices"]
        if not isinstance(indices, list) or not indices or any(
                type(i) is not int or i < 0 or i >= len(msgs) for i in indices):
            raise ValueError("没有选中有效消息，请重新选择")
        return [msgs[i] for i in sorted(set(indices))]
    return [m for m in msgs if is_chat(m)] if default_chat else msgs


def excerpt_payload(tool: str, sid: str, indices=None, message_ids=None) -> dict:
    rec = find_record(tool, sid)
    msgs = _cached_messages(rec.tool, rec.session_id, _source_sig(rec.file_path, rec.tool))
    selection = {"message_ids": message_ids} if message_ids is not None else {"indices": indices}
    chosen = selected_messages(msgs, selection)
    return {"filename": f"{rec.tool}__{_safe_name(rec.session_id, 12)}__"
                        f"{_safe_name(rec.title)}__节选{len(chosen)}条.md",
            "mode": "excerpt", "count": len(chosen),
            "markdown": render_markdown(rec, chosen, excerpt_of=len(msgs))}


def _items(body: dict) -> list:
    items = body.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(i, dict) for i in items):
        raise ValueError("没有选择有效会话")
    selected = {}
    for it in items:
        rec = find_record(str(it.get("tool", "")), str(it.get("id", "")))
        key = session_key(rec.tool, rec.session_id)
        if key in selected:
            raise ValueError("会话重复，请刷新后重新选择")
        selected[key] = (rec, it)
    if body.get("include_children"):
        if any("message_ids" in it or "indices" in it for _, it in selected.values()):
            raise ValueError("选中消息时不能同时包含子任务")
        pending = list(current_records())
        while pending:
            added = []
            for rec in pending:
                key = session_key(rec.tool, rec.session_id)
                if key not in selected and rec.extra.get("origin") == "subagent" and session_key(rec.tool, rec.extra.get("parent_session_id")) in selected:
                    selected[key] = (rec, {})
                    added.append(rec)
            if not added:
                break
            pending = [r for r in pending if session_key(r.tool, r.session_id) not in selected]
    return list(selected.values())


def summary_blocks(body: dict) -> list:
    blocks = []
    for rec, selection in _items(body):
        msgs = _cached_messages(rec.tool, rec.session_id, _source_sig(rec.file_path, rec.tool))
        for m in selected_messages(msgs, selection):
            block = message_md(m)
            if not block:
                continue
            url = "#" + urllib.parse.quote(rec.tool, safe="") + "/" + urllib.parse.quote(rec.session_id, safe="")
            url += "?message=" + urllib.parse.quote(m.message_id, safe="")
            blocks.append(f"[来源：{rec.tool}/{rec.session_id}/{m.message_id}]({url})\n"
                          f"会话：{rec.title}｜目录：{rec.cwd or '未知'}\n{block}")
    if not blocks:
        raise ValueError("所选范围没有可总结内容")
    return blocks


def _summary_request_id(job_id):
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 100:
        raise ValueError("缺少有效 request_id")
    return job_id


def _prune_summary_requests():
    now = time.monotonic()
    for job_id, (expires, _) in list(_summary_recent.items()):
        if expires <= now:
            del _summary_recent[job_id]


def register_summary(job_id):
    job_id = _summary_request_id(job_id)
    with _summary_lock:
        _prune_summary_requests()
        if job_id in _summary_jobs:
            raise ValueError("总结请求重复")
        if job_id in _summary_recent:
            raise ValueError("总结请求已取消" if _summary_recent[job_id][1] else "总结请求重复")
        event = threading.Event()
        _summary_jobs[job_id] = event
        return event


def cancel_summary(job_id):
    job_id = _summary_request_id(job_id)
    with _summary_lock:
        _prune_summary_requests()
        event = _summary_jobs.get(job_id)
        if event is not None:
            event.set()
        # 即使取消先抵达，也记住它，阻止迟到的总结请求发起模型调用。
        _summary_recent[job_id] = (time.monotonic() + SUMMARY_REQUEST_TTL, True)
    return True


def finish_summary(job_id):
    with _summary_lock:
        _summary_jobs.pop(job_id, None)
        was_cancelled = _summary_recent.get(job_id, (0, False))[1]
        _summary_recent[job_id] = (time.monotonic() + SUMMARY_REQUEST_TTL, was_cancelled)


def summary_preview(body):
    blocks = summary_blocks(body)
    cfg = llm._resolve(body.get("llm"))
    if not cfg.get("model"):
        raise llm.LLMError("还没有选择模型，请先在「设置」里选择")
    instruction = llm.summary_instruction(body.get("template", "handover"), body.get("prompt", ""))
    budget = llm.summary_budget(cfg, instruction)
    return {"sessions": len(_items(body)), "messages": len(blocks),
            "characters": sum(map(len, blocks)), "model": cfg.get("model", ""),
            "parts": len(llm._chunks(blocks, budget["input_chars"])), "budget": budget}


def export_multi(body: dict) -> dict:
    """多个会话（可跨工具）合并成一份交接文档。"""
    mode = "full" if body.get("mode") == "full" else "final"
    recs = [rec for rec, _ in _items(body)]
    docs = [render_markdown(r, _cached_messages(r.tool, r.session_id, _source_sig(r.file_path, r.tool)), mode)
            for r in recs]
    toc = "\n".join(f"{k}. [{r.tool}] {r.title}（`{r.cwd or '未知目录'}`）" for k, r in enumerate(recs, 1))
    md = (f"# 多会话交接文档（{len(recs)} 个会话）\n\n> 由 agentHUB 合并导出，"
          f"各会话按选择顺序排列。\n\n{toc}\n\n---\n\n" + "\n\n---\n\n".join(docs))
    return {"filename": f"multi__{len(recs)}个会话__{time.strftime('%Y%m%d-%H%M')}.md",
            "mode": "multi", "count": len(recs), "markdown": md}


class Handler(BaseHTTPRequestHandler):
    server_version = "agentHUB/0.1"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 2_000_000:
            raise ValueError("请求体大小无效")
        if not n:
            return {}
        try:
            o = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ValueError("请求体不是合法 JSON") from None
        if not isinstance(o, dict):
            raise ValueError("请求体应为 JSON 对象")
        return o

    def _sse(self, events, cancelled):
        """以 Server-Sent Events 逐条推送；客户端中途取消（断开）时静默结束。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        def send(ev):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        try:
            for ev in events:
                send(ev)
        except (BrokenPipeError, ConnectionResetError):
            cancelled.set()
        except Exception as e:
            try:
                send({"type": "error", "text": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                cancelled.set()
        finally:
            cancelled.set()
            events.close()

    def _forbidden(self) -> bool:
        """Host / Origin 不在白名单时拒绝（返回 True 表示已拒绝）。"""
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") not in ALLOWED_HOSTS or
                (origin and urllib.parse.urlparse(origin).netloc not in ALLOWED_HOSTS)):
            self._err(403, "forbidden host/origin")
            return True
        return False

    # ---------- 路由 ----------
    def do_GET(self):
        if self._forbidden():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

            if path in ("/", "/index.html"):
                fp = os.path.join(WEB_DIR, "index.html")
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path.startswith("/web/"):
                # 静态文件（含 vendor/ 下的前端库与字体）；realpath 校验防止 ../ 穿越
                fp = os.path.realpath(os.path.join(WEB_DIR, urllib.parse.unquote(path[5:])))
                if fp.startswith(WEB_DIR + os.sep) and os.path.isfile(fp):
                    ctype = (STATIC_TYPES.get(os.path.splitext(fp)[1].lower())
                             or mimetypes.guess_type(fp)[0] or "application/octet-stream")
                    if ctype.startswith("text/"):
                        ctype += "; charset=utf-8"
                    with open(fp, "rb") as f:
                        self._send(200, f.read(), ctype)
                else:
                    self._err(404, "not found")
            elif path == "/api/stats":
                by_tool = defaultdict(lambda: {"n": 0, "msg": 0, "tool": 0})
                for r in current_records():
                    d = by_tool[r.tool]
                    d["n"] += 1
                    d["msg"] += r.n_messages
                    d["tool"] += r.n_tool_calls
                self._json({
                    "total": sum(v["n"] for v in by_tool.values()),
                    "total_messages": sum(v["msg"] for v in by_tool.values()),
                    "total_tool_calls": sum(v["tool"] for v in by_tool.values()),
                    "by_tool": {k: dict(v) for k, v in by_tool.items()},
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_scan": _last_scan_at,
                    "auto_rescan_minutes": RESCAN_MINUTES,
                    "problems": _problems,
                })
            elif path == "/api/sessions":
                self._json(self._list_sessions(params))
            elif path == "/api/llm/config":
                self._json(llm.public_config())
            elif path == "/api/cwd":
                self._json(self._group_by_cwd())
            elif path.startswith("/api/session/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")]  # ['', 'api', 'session', tool, id]
                if len(parts) != 5:
                    self._err(400, "路径格式: /api/session/{tool}/{id}")
                    return
                offset = int(params.get("offset", ["0"])[0])
                order = "desc" if params.get("order", ["asc"])[0] == "desc" else "asc"
                try:
                    self._json(session_payload(parts[3], parts[4], offset, order,
                                               params.get("view", [""])[0],
                                               params.get("q", [""])[0], params.get("anchor", [""])[0]))
                except KeyError as e:
                    self._err(404, str(e.args[0]))
            elif path.startswith("/api/export/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")]
                if len(parts) != 5:
                    self._err(400, "路径格式: /api/export/{tool}/{id}")
                    return
                try:
                    rec = find_record(parts[3], parts[4])
                except KeyError as e:
                    self._err(404, str(e.args[0]))
                    return
                mode = params.get("mode", [""])[0]
                mode = mode if mode in ("full", "user", "ai") else "final"
                fp = export_session(rec, mode=mode)
                with open(fp, "r", encoding="utf-8") as f:
                    md = f.read()
                self._json({
                    "filename": os.path.basename(fp),
                    "path": fp,
                    "mode": mode,
                    "size": len(md.encode("utf-8")),
                    "markdown": md,
                })
            else:
                self._err(404, "not found")
        except BrokenPipeError:
            pass
        except ValueError as e:  # offset/limit 等参数非法
            self._err(400, f"参数错误: {e}")
        except Exception as e:  # noqa: BLE001
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        if self._forbidden():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/scan":
                n = _do_scan()
                self._json({"total": n, "index": os.path.join(INDEX_DIR, "index.md")})
            elif parsed.path == "/api/export-multi":
                self._json(export_multi(self._body_json()))
            elif parsed.path == "/api/llm/config":
                self._json(llm.save_config(self._body_json()))
            elif parsed.path == "/api/llm/models":
                self._json({"models": llm.list_models(self._body_json())})
            elif parsed.path == "/api/llm/test":
                self._json(llm.test(self._body_json()))
            elif parsed.path == "/api/summary/preview":
                self._json(summary_preview(self._body_json()))
            elif parsed.path == "/api/summary/cancel":
                job_id = self._body_json().get("request_id")
                self._json({"cancelled": cancel_summary(job_id)})
            elif parsed.path == "/api/summary":
                body = self._body_json()
                job_id = body.get("request_id")
                cancelled = register_summary(job_id)
                try:
                    blocks = summary_blocks(body)
                    self._sse(llm.summarize(blocks, body.get("template", "handover"),
                                           body.get("prompt", ""), body.get("llm"),
                                           cancelled=cancelled), cancelled)
                finally:
                    finish_summary(job_id)
            elif parsed.path.startswith("/api/excerpt/"):
                parts = [urllib.parse.unquote(x) for x in parsed.path.split("/")]
                if len(parts) != 5:
                    self._err(400, "路径格式: /api/excerpt/{tool}/{id}")
                    return
                body = self._body_json()
                self._json(excerpt_payload(parts[3], parts[4], body.get("indices"), body.get("message_ids")))
            else:
                self._err(404, "not found")
        except KeyError as e:
            self._err(404, str(e.args[0]))
        except (ValueError, llm.LLMError) as e:
            self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._err(500, f"{type(e).__name__}: {e}")

    # ---------- 业务 ----------
    def _list_sessions(self, params) -> dict:
        tool = params.get("tool", [""])[0].strip()
        q = params.get("q", [""])[0].strip().casefold()
        cwd = params.get("cwd", [None])[0]
        records = current_records()
        by_id = {session_key(r.tool, r.session_id): r for r in records}
        children = defaultdict(list)
        roots = []
        for r in records:
            parent = session_key(r.tool, r.extra.get("parent_session_id"))
            if r.extra.get("origin") == "subagent" and parent in by_id and parent != session_key(r.tool, r.session_id):
                children[parent].append(r)
            else:
                roots.append(r)
        def matches(r):
            return ((not tool or r.tool == tool) and (cwd is None or r.cwd == cwd) and
                    (not q or q in f"{r.title} {r.cwd} {r.session_id}".casefold()))
        def tree(r, seen):
            key = session_key(r.tool, r.session_id)
            if key in seen:
                return None
            descendants = [tree(c, seen | {key}) for c in sorted(children[key], key=lambda x: x.updated_at, reverse=True)]
            descendants = [d for d in descendants if d]
            if not matches(r) and not descendants:
                return None
            d = record_dict(r)
            d.update(children=descendants, matched=matches(r))
            return d
        if params.get("group", ["all"])[0] == "roots":
            reached, pending = set(), list(roots)
            while pending:
                rec = pending.pop()
                key = session_key(rec.tool, rec.session_id)
                if key not in reached:
                    reached.add(key)
                    pending.extend(children[key])
            if len(reached) != len(by_id):
                raise ValueError("任务父子关系存在循环，请切换全部平铺并检查原生记录")
            rows = [tree(r, set()) for r in sorted(roots, key=lambda x: x.updated_at, reverse=True)]
            rows = [r for r in rows if r]
        else:
            rows = [record_dict(r) for r in sorted(records, key=lambda x: x.updated_at, reverse=True) if matches(r)]
        offset = int(params.get("offset", ["0"])[0])
        limit = min(int(params.get("limit", ["100"])[0]), 500)
        if offset < 0 or limit < 1:
            raise ValueError("分页参数必须为非负 offset 和正数 limit")
        return {"total": len(rows), "offset": offset, "sessions": rows[offset:offset + limit]}

    def _group_by_cwd(self) -> dict:
        groups = defaultdict(list)
        for r in current_records():
            groups[r.cwd or "(未知目录)"].append(r)
        rows = []
        for cwd, rs in groups.items():
            rows.append({
                "cwd": "" if cwd == "(未知目录)" else cwd,
                "tools": sorted({r.tool for r in rs}),
                "n_sessions": len(rs),
                "n_messages": sum(r.n_messages for r in rs),
                "last_active": max(r.iso("updated") for r in rs) or "",
                "last_ts": max(r.updated_at or 0 for r in rs),
            })
        rows.sort(key=lambda x: x["last_ts"], reverse=True)
        return {"groups": rows}


def main(port: int = DEFAULT_PORT):
    global _last_scan_at
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    if RESCAN_MINUTES < 0:
        raise ValueError("扫描间隔不能为负数")
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    try:
        print("正在扫描本地会话…", flush=True)
        n = _do_scan()
        print(f"扫描完成：{n} 个会话", flush=True)
    except RuntimeError as e:
        # 扫描未完成：用上次的索引先把页面跑起来（问题照常显示），没有旧索引才算启动失败
        try:
            refresh_cache()
        except FileNotFoundError:
            httpd.server_close()
            raise e from None
        _problems[:] = [str(e)]
        print(f"⚠ 扫描未完成，先用上次的索引启动（{len(_records)} 个会话）：{e}", flush=True)
    except Exception:
        httpd.server_close()
        raise
    # 后台自动重扫线程；0 表示手动刷新。
    if RESCAN_MINUTES > 0:
        t = threading.Thread(target=_auto_rescan_loop, daemon=True)
        t.start()
    print(f"自动重扫：每 {RESCAN_MINUTES} 分钟（AGENTHUB_RESCAN_MINUTES 可调）")
    print(f"agentHUB Web UI → http://{HOST}:{port}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nagentHUB 已停止")
    finally:
        httpd.server_close()
