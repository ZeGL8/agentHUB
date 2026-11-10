# -*- coding: utf-8 -*-
"""读取器共用工具：JSONL 逐行解析、SQLite（含 WAL）只读打开。"""

import json
import os
import pathlib
import sqlite3
from contextlib import contextmanager


def tool_home(default: str, *env_names: str) -> str:
    """工具的数据目录：依次取环境变量（AGENTHUB_<TOOL>_HOME 或工具官方变量），否则 ~/<default>。

    所有路径都经 expanduser / os.path 拼接，macOS / Linux / Windows 通用。
    """
    for name in env_names:
        if os.environ.get(name):
            return os.path.expanduser(os.environ[name])
    return os.path.join(os.path.expanduser("~"), default)


def iter_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if not line.endswith("\n"):  # 末行没有换行符：对方工具正在写入，不是损坏
                    return
                raise ValueError(f"{path}:{number}: JSONL 记录损坏") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{number}: JSONL 记录必须是对象")
            yield record


@contextmanager
def open_ro(path: str):
    """直接以只读模式打开 SQLite；失败时显式报错，不复制活跃 WAL。"""
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()
