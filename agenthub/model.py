# -*- coding: utf-8 -*-
"""统一数据模型：所有工具的会话与消息都归一到这两个结构。"""

import re
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Message:
    """一条归一化的对话消息。"""

    role: str = ""            # user / assistant
    kind: str = "text"        # text / thinking / tool_call / tool_result
    text: str = ""            # 正文（text 类消息的完整文本）
    tool_name: str = ""       # 工具调用名
    tool_args: str = ""       # 工具调用参数全文
    tool_output: str = ""     # 工具结果全文
    timestamp: float = 0.0    # epoch 秒
    final: bool = False       # 该轮对话中助手的最后一条正文（对话视图 / 仅最终输出只显示它）

    message_id: str = ""
    turn_id: str = ""
    call_id: str = ""
    source: str = ""
    link: str = ""            # 关联的另一个会话 ID（如 DSH 子任务），前端据此提供跳转

    def display_ts(self) -> str:
        if not self.timestamp:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))


@dataclass
class SessionRecord:
    """一条会话的索引记录。"""

    tool: str = ""
    session_id: str = ""
    title: str = ""
    cwd: str = ""
    file_path: str = ""           # 会话文件（或数据库）路径
    started_at: int = 0           # epoch ms
    updated_at: int = 0           # epoch ms
    n_messages: int = 0           # 归一化消息数（user+assistant 文本）
    n_tool_calls: int = 0
    size_bytes: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SessionRecord":
        return SessionRecord(**d)

    def iso(self, which: str = "updated") -> str:
        ts = self.updated_at if which == "updated" else self.started_at
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000.0))

    def human_size(self) -> str:
        n = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB"]:
            if n < 1024 or unit == "GB":
                return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}GB"


def ts_to_ms(ts) -> int:
    """把各种时间戳（秒/毫秒/ISO 字符串）统一成 epoch 毫秒。"""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts) if ts > 1e12 else int(ts * 1000)
    s = str(ts)
    try:
        # ISO 8601: 2026-04-27T13:23:35.712Z
        s2 = s.replace("Z", "+00:00")
        import datetime
        dt = datetime.datetime.fromisoformat(s2)
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
    try:
        f = float(s)
        return int(f) if f > 1e12 else int(f * 1000)
    except Exception:
        return 0


def is_chat(m: Message) -> bool:
    """对话视图 / 「仅最终输出」只保留：用户正文 + 每轮助手最终正文。"""
    return m.kind == "text" and (m.role == "user" or m.final)


# 消息视图：对话（用户 + 每轮最终回答）/ 只看用户 / 只看 AI 最终回答；不在表中的视图为全程
VIEWS = {"chat": is_chat,
         "user": lambda m: is_chat(m) and m.role == "user",
         "ai": lambda m: is_chat(m) and m.role != "user"}


THINK_RE = re.compile(r"<(think|thinking)>(.*?)(?:</\1>|$)", re.S)


def split_think(text: str):
    """把正文里内嵌的 <think>…</think> 拆出来，返回 (思考, 可见正文)。未闭合的标签视为思考到结尾。"""
    thoughts = [m.group(2).strip() for m in THINK_RE.finditer(text)]
    return "\n\n".join(t for t in thoughts if t), THINK_RE.sub("", text).strip()


def clip(s: Optional[str], n: int = 200) -> str:
    """截断字符串用于摘要展示。"""
    if not s:
        return ""
    s = " ".join(str(s).split())  # 压平空白
    return s if len(s) <= n else s[: n - 1] + "…"
