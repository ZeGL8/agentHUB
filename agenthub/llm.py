# -*- coding: utf-8 -*-
"""AI 总结：调用任意 OpenAI 兼容接口（/v1/models、/v1/chat/completions）。

配置存于 DATA_DIR/config.json 的 "llm" 段（文件权限 600）；API Key 只在后端使用，不回传前端。
长内容按模型上下文切段：先逐段提要（map），再合并成最终总结（reduce，流式输出）。
"""

import json
import os
import time
import socket
import threading
import urllib.error
import urllib.request
import urllib.parse
import tempfile

from . import DATA_DIR
from .model import split_think

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DEFAULT_CONTEXT = 32000  # 用户未配置上下文窗口时按 32K token 估算
TIMEOUT = 30
MAX_MAP_ROUNDS = 4
_config_lock = threading.Lock()
SOURCE_PROMPT = "保留每条结论的原始来源标识与完整 Markdown 来源链接；区分已做、已验证、待办和未确认，不能把计划或尝试写成已完成。"

TEMPLATES = {
    "handover": (
        "你是资深工程师。请基于下面的对话记录写一份交接摘要，让另一个 AI agent 或同事能直接接手。"
        "用 Markdown，按以下小节组织：## 目标、## 已完成、## 关键结论与决策、## 未完成 / 待办、"
        "## 涉及的文件与路径、## 注意事项。只写记录中有依据的内容，不要编造；"
        "保留关键数字、路径、命令与报错原文。记录可能来自多个工具的多个会话，请合并归纳。"),
    "decisions": (
        "请用 Markdown 整理记录中的关键决策：决策内容、依据、考虑过的方案、影响与尚待确认的问题。"
        "区分已作出的决策与建议，不要把计划或尝试当成已完成结果。"),
    "brief": (
        "请用 Markdown 提炼下面对话的要点：先用一句话概括，再列 5~10 条要点。"
        "保留关键数字、路径、命令；不要编造记录中没有的内容。"),
}
MAP_PROMPT = ("下面是一段较长对话记录中的第 {k}/{n} 段。请提取这一段里的事实要点：做了什么、得出什么结论、"
              "改了哪些文件、遗留什么问题。用简洁的 Markdown 列表，保留路径、命令、数字，不要编造。")


class LLMError(Exception):
    pass


# ---------- 配置 ----------

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("llm", {})
    except FileNotFoundError:
        return {}


def save_config(update: dict) -> dict:
    with _config_lock:
        return _save_config(update)


def _save_config(update: dict) -> dict:
    """合并保存。api_key 为 None 表示不修改，空串表示清除。"""
    cfg = load_config()
    for k in ("base_url", "model"):
        if k in update:
            if not isinstance(update[k], str):
                raise LLMError(f"{k} 必须是字符串")
            cfg[k] = (update[k] or "").strip()
    if update.get("api_key") is not None:
        if not isinstance(update["api_key"], str):
            raise LLMError("API Key 必须是字符串")
        cfg["api_key"] = update["api_key"].strip()
    if cfg.get("base_url"):
        _endpoint(cfg["base_url"], "/models")
    if "context_window" in update:
        value = update["context_window"]
        cfg["context_window"] = _context_value(value) if value not in (None, "") else None
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            whole = json.load(f)
    except FileNotFoundError:
        whole = {}
    whole["llm"] = cfg
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix="config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(whole, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return public_config(cfg)


def public_config(cfg: dict = None) -> dict:
    """给前端看的配置：不含 Key 本身，只给末 4 位提示。"""
    cfg = load_config() if cfg is None else cfg
    key = cfg.get("api_key") or ""
    return {"base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "context_window": cfg.get("context_window"), "has_key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else ("已设置" if key else "")}


def _resolve(overrides: dict = None) -> dict:
    """已保存配置 + 本次请求临时覆盖（设置页「测试」时还没保存）。"""
    cfg = dict(load_config())
    if overrides is not None and not isinstance(overrides, dict):
        raise LLMError("模型配置必须是对象")
    for k, v in (overrides or {}).items():
        if k not in ("base_url", "model", "api_key", "context_window"):
            raise LLMError(f"未知模型配置项：{k}")
        if v is not None:
            if k != "context_window" and not isinstance(v, str):
                raise LLMError(f"{k} 必须是字符串")
            cfg[k] = v.strip() if isinstance(v, str) else v
    if not cfg.get("base_url"):
        raise LLMError("还没有配置模型接口地址，请先在「设置」里填写")
    _endpoint(cfg["base_url"], "/models")
    return cfg


def _endpoint(base_url: str, path: str) -> str:
    """base_url 既可以写到 /v1，也可以只写到主机端口。"""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMError("接口地址必须是 HTTP/HTTPS 地址，不能包含账号、密码、查询或锚点")
    try:
        parsed.port
    except ValueError as exc:
        raise LLMError("接口地址端口无效") from exc
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + path


def _request(cfg: dict, path: str, body: dict = None, stream: bool = False):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_endpoint(cfg["base_url"], path), data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"模型接口返回 HTTP {e.code}：{detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"连接模型接口失败：{getattr(e, 'reason', e)}") from None


# ---------- 模型列表 / 测试 ----------

def list_models(overrides: dict = None) -> list:
    """返回可用于对话的模型。网关若扩展了 type / ready / max_context 字段则据此过滤与标注。"""
    cfg = _resolve(overrides)
    with _request(cfg, "/models") as r:
        data = json.load(r).get("data", [])
    out = []
    for m in data:
        if m.get("type") not in (None, "LLM", "chat") or m.get("ready") is False:
            continue
        out.append({"id": m.get("id", ""), "max_context": m.get("max_context"),
                    "owned_by": m.get("owned_by", "")})
    return sorted(out, key=lambda m: m["id"])


def test(overrides: dict = None) -> dict:
    cfg = _resolve(overrides)
    if not cfg.get("model"):
        raise LLMError("请先选择模型")
    t = time.time()
    # 不限 max_tokens：推理模型会先输出思考，限得太小正文会是空的
    reply = complete(cfg, [{"role": "user", "content": "请只回复两个字：连通"}])
    return {"ok": True, "reply": reply[:200], "ms": int((time.time() - t) * 1000)}


# ---------- 调用 ----------

def complete(cfg: dict, messages: list, max_tokens: int = None) -> str:
    """非流式调用，返回去掉 <think> 的正文。"""
    body = {"model": cfg["model"], "messages": messages, "stream": False}
    if max_tokens:
        body["max_tokens"] = max_tokens
    with _request(cfg, "/chat/completions", body) as r:
        o = json.load(r)
    try:
        text = o["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"模型返回格式无法识别：{json.dumps(o, ensure_ascii=False)[:300]}") from None
    _check_finish(o["choices"][0].get("finish_reason"))
    text = split_think(text)[1]
    if not text.strip():
        raise LLMError("模型返回空正文")
    return text


def _check_cancel(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise LLMError("总结已取消；已关闭本地上游连接，服务端计算是否停止取决于模型供应商")


def _check_finish(reason):
    if reason not in (None, "stop"):
        raise LLMError(f"模型未正常完成（finish_reason={reason}），请调整输出预算或缩小输入范围")


def stream(cfg: dict, messages: list, max_tokens: int = None, cancelled=None):
    """[DONE] 或 finish_reason=stop 表示完成；EOF 本身不是完成。"""
    _check_cancel(cancelled)
    body = {"model": cfg["model"], "messages": messages, "stream": True}
    if max_tokens:
        body["max_tokens"] = max_tokens
    finished = False
    pieces = []
    with _request(cfg, "/chat/completions", body, stream=True) as r:
        stopped = threading.Event()

        def watch_cancel():
            while not stopped.wait(0.05):
                if cancelled.is_set():
                    # shutdown 先唤醒阻塞读取，避免 HTTPResponse.close 等待读锁。
                    sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    r.close()
                    return

        watcher = None
        if cancelled is not None:
            watcher = threading.Thread(target=watch_cancel, daemon=True)
            watcher.start()
        try:
            _check_cancel(cancelled)
            for raw in r:
                _check_cancel(cancelled)
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    break
                try:
                    o = json.loads(payload)
                except json.JSONDecodeError as e:
                    raise LLMError("模型流包含无效 JSON") from e
                if o.get("error"):
                    raise LLMError(f"模型返回错误：{json.dumps(o['error'], ensure_ascii=False)[:300]}")
                for ch in o.get("choices") or []:
                    reason = ch.get("finish_reason")
                    _check_finish(reason)
                    if reason == "stop":
                        finished = True
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        pieces.append(piece)
                        yield piece
                if finished:
                    break
            _check_cancel(cancelled)
        except (OSError, ValueError) as e:
            _check_cancel(cancelled)
            raise LLMError(f"模型流读取失败：{e}") from e
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join(timeout=0.2)
    if not finished:
        raise LLMError("模型流意外结束，结果未完成")
    if not split_think("".join(pieces))[1].strip():
        raise LLMError("模型返回空正文")


def _context_value(value):
    try:
        context = int(value)
    except (ValueError, TypeError):
        raise LLMError("上下文窗口必须是正整数") from None
    if context <= 0 or isinstance(value, bool) or str(value).strip() != str(context):
        raise LLMError("上下文窗口必须是正整数")
    return context


def summary_budget(cfg: dict, instruction: str = "") -> dict:
    """无联网预算。字符按 UTF-8 字节上界估算 token，含输出、提示和消息开销。"""
    configured = cfg.get("context_window")
    context = _context_value(configured) if configured not in (None, "") else DEFAULT_CONTEXT
    prompts = [instruction or TEMPLATES["handover"], MAP_PROMPT.format(k=999999, n=999999)]
    prompt_tokens = max(len((p + SOURCE_PROMPT).encode("utf-8")) for p in prompts) + 128
    output = min(4096, context // 4)
    # 每个 Unicode 字符最多四字节；这是可解释的保守上界，非精确 tokenizer。
    input_chars = (context - output - prompt_tokens) // 4
    if input_chars < 32 or output < 32:
        raise LLMError("上下文窗口不足以容纳提示与输出，请增大上下文窗口或缩短自定义提示")
    return {"context_window": context, "estimated": not bool(configured),
            "input_chars": input_chars, "max_tokens": output, "prompt_tokens": prompt_tokens}


def _model_context(cfg: dict) -> int:
    return summary_budget(cfg)["context_window"]


def _chunks(blocks: list, limit: int) -> list:
    """按块边界把文本切成不超过 limit 字符的段；单块超长时硬切。"""
    if limit <= 0:
        raise LLMError("分段预算必须大于零")
    out, cur = [], ""
    for b in blocks:
        header = ""
        if b.startswith("[来源："):
            lines = b.split("\n", 2)
            if len(lines) == 3 and lines[1].startswith("会话："):
                header = "\n".join(lines[:2]) + "\n"
                if len(header) >= limit:
                    raise LLMError("来源信息超出分段预算，请增大上下文窗口或缩短会话标题")
        while len(b) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(b[:limit])
            b = header + b[limit:]
        if len(cur) + len(b) + 2 > limit and cur:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        out.append(cur)
    return out


def summary_instruction(template="handover", prompt=""):
    instruction = prompt.strip() if template == "custom" else TEMPLATES.get(template, TEMPLATES["handover"])
    if not instruction:
        raise LLMError("自定义模板需要填写总结要求")
    return instruction


def summarize(blocks: list, template: str = "handover", prompt: str = "", overrides: dict = None, cancelled=None):
    """对若干文本块做总结，产出事件 dict：status / delta / done。

    blocks 是已按会话、消息排好的文本块；过长时先分段提要再合并。
    """
    cfg = _resolve(overrides)
    if not cfg.get("model"):
        raise LLMError("还没有选择模型，请先在「设置」里选择")
    instruction = summary_instruction(template, prompt)
    _check_cancel(cancelled)
    budget = summary_budget(cfg, instruction)
    instruction += SOURCE_PROMPT
    limit = budget["input_chars"]
    parts = _chunks(blocks, limit)
    rounds = 0
    while len(parts) > 1:
        _check_cancel(cancelled)
        if rounds >= MAX_MAP_ROUNDS:
            raise LLMError("分段归并超过轮数上限，请缩小输入范围或增大上下文窗口")
        previous_size = sum(map(len, parts))
        notes = []
        for k, part in enumerate(parts, 1):
            _check_cancel(cancelled)
            yield {"type": "status", "text": f"内容较长，分段提取要点 {k}/{len(parts)}…"}
            note = "".join(stream(cfg, [
                {"role": "system", "content": MAP_PROMPT.format(k=k, n=len(parts)) + SOURCE_PROMPT},
                {"role": "user", "content": part}], max_tokens=budget["max_tokens"], cancelled=cancelled))
            notes.append(split_think(note)[1])
        parts = _chunks(notes, limit)
        rounds += 1
        if len(parts) > 1 and sum(map(len, parts)) >= previous_size:
            raise LLMError("分段提要未缩减，已停止归并；请缩小输入范围或修改总结要求")
    _check_cancel(cancelled)
    yield {"type": "status", "text": "生成总结中…"}
    for piece in stream(cfg, [{"role": "system", "content": instruction},
                              {"role": "user", "content": parts[0] if parts else "（无内容）"}],
                        max_tokens=budget["max_tokens"], cancelled=cancelled):
        yield {"type": "delta", "text": piece}
    _check_cancel(cancelled)
    yield {"type": "done"}
