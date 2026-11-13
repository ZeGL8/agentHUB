# -*- coding: utf-8 -*-
"""打包版入口：启动 agentHUB 页面并在浏览器里打开。

与 server.py 的区别只有两点：参数更宽松（双击运行不带参数也能起），
以及启动成功后自动打开浏览器。真正的工作仍由 agenthub.server.main 承担。
"""

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文日志会变成乱码。

    优先切控制台代码页到 65001；再给 stdout/stderr 套 UTF-8，
    两者都失败时退回 errors="replace"，至少不因编码问题崩掉。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int) -> int:
    """首选端口被占用时向后找一个空闲的，避免双击启动直接报错。"""
    if _port_free(preferred):
        return preferred
    for p in range(preferred + 1, min(preferred + 40, 65535)):
        if _port_free(p):
            return p
    raise RuntimeError(f"端口 {preferred} 及后续 40 个端口都被占用")


def _open_browser_when_ready(port: int, timeout: float = 30.0) -> None:
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                time.sleep(0.3)  # 让首屏静态资源准备好
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return
        time.sleep(0.4)


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentHUB", description="启动 agentHUB 本地页面")
    parser.add_argument("--port", type=int, default=0, help="端口，默认 8322；被占用时自动顺延")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args, _unknown = parser.parse_known_args()

    from agenthub.server import DEFAULT_PORT, main as serve

    port = args.port or DEFAULT_PORT
    if not args.port:
        try:
            port = _pick_port(port)
        except RuntimeError as exc:
            print(f"启动失败：{exc}", file=sys.stderr)
            return 1

    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    try:
        serve(port)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
