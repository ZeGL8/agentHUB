# -*- coding: utf-8 -*-
"""agentHUB 桌面版入口。

内置 HTTP 服务只监听 127.0.0.1（端口自动挑选），前面套一个原生窗口
（Windows 走 WebView2，macOS 走 WKWebView）。用户看到的是一个独立应用窗口，没有地址栏。

打包成无控制台程序后有两点必须处理：
  1. sys.stdout / sys.stderr 会是 None，任何 print 都会抛异常 —— 统一重定向到日志文件；
  2. 首轮扫描可能耗时数秒，不能让窗口干等 —— 先显示一个轻量启动页，服务就绪后切换。
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import traceback

# 直接以脚本方式运行时（python packaging/desktop.py），把仓库根目录加进搜索路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if os.path.isdir(os.path.join(_ROOT, "agenthub")) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

APP_NAME = "agentHUB"
APP_TITLE = "agentHUB — 会话索引与交接"
WIN_W, WIN_H = 1360, 880
MIN_W, MIN_H = 960, 620

_LOG_STREAM = None


# ---------------------------------------------------------------- 日志

def _log_path() -> str:
    base = os.environ.get("AGENTHUB_HOME") or os.path.join(os.path.expanduser("~"), ".agenthub")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        import tempfile

        base = tempfile.gettempdir()
    return os.path.join(base, "desktop.log")


def _redirect_streams():
    """无控制台打包运行时，print 会写到无效句柄或直接抛异常。

    这里不判断 sys.stdout 是否为 None —— 无控制台时它可能是个无效对象，
    只在真正写入时才报错。干脆一律把 stdout/stderr 换到日志文件。
    """
    global _LOG_STREAM
    target = _log_path()
    try:
        _LOG_STREAM = open(target, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = _LOG_STREAM
    sys.stderr = _LOG_STREAM


def log(msg: str) -> None:
    # stdout 已被重定向到日志文件，print 一次即可（原先再写一遍会让每行重复）
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def _alert(msg: str) -> None:
    """系统级错误对话框；窗口都没起来时也能让用户看到原因。"""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display alert (item 1 of argv) message (item 2 of argv) as critical",
                            "-e", "end run", APP_NAME, msg], timeout=120, check=False)
    except Exception:
        pass


def _install_crash_logger():
    """未捕获异常写日志，并弹一个系统对话框，避免「双击没反应」。"""

    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log("UNCAUGHT\n" + text)
        _alert(f"agentHUB 启动失败：\n\n{exc_value}\n\n详细信息见：\n{_log_path()}")

    sys.excepthook = hook


_redirect_streams()


# ---------------------------------------------------------------- 端口 / 服务

def pick_port(preferred: int = 0) -> int:
    for candidate in ([preferred] if preferred else []) + [0]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


class ServerThread(threading.Thread):
    """后台跑 agentHUB 的服务（复用 agenthub.server.create_server）。"""

    def __init__(self, port: int):
        super().__init__(daemon=True, name="agenthub-http")
        self.port = port
        self.error = None
        self._httpd = None
        self._ready = threading.Event()

    def run(self):
        try:
            from agenthub.server import create_server

            log(f"正在建立本地服务 :{self.port} …")
            self._httpd = create_server(self.port)
            self._httpd.daemon_threads = True
            self._ready.set()
            log(f"服务已就绪 → http://127.0.0.1:{self.port}/")
            self._httpd.serve_forever(poll_interval=0.4)
        except BaseException as exc:  # noqa: BLE001 - 要把错误带回主线程
            self.error = exc
            log("服务启动异常：\n" + traceback.format_exc())
            self._ready.set()

    def wait_ready(self, timeout: float = 180.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self):
        if self._httpd is not None:
            for fn in (self._httpd.shutdown, self._httpd.server_close):
                try:
                    fn()
                except Exception:
                    pass


# ---------------------------------------------------------------- 前端桥接

class Api:
    """暴露给前端的原生能力（window.pywebview.api.*）。

    注意：pywebview 注入时会递归遍历本对象的所有非下划线属性
    （见 webview/util.py 的 get_functions）。Window 对象持有 .native（WinForms Form），
    递归进去会撞上 Rectangle.Empty.Empty... / AnchorStyles.Bottom.Bottom...
    这类自我返回的 .NET 属性，导致 RecursionError 并让 UI 卡死。
    所以窗口引用必须以下划线开头。
    """

    def __init__(self):
        self._window = None
        self.port = 0
        self._last_dir = os.path.expanduser("~")

    def save_text(self, filename: str, content: str) -> dict:
        """另存为：WebView 里 a.download 不可靠，改走系统保存对话框。"""
        import webview

        safe = (filename or "agentHUB.md").replace("/", "_").replace("\\", "_")
        if not safe.lower().endswith(".md"):
            safe += ".md"
        try:
            result = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=self._last_dir,
                save_filename=safe,
                file_types=("Markdown (*.md)", "所有文件 (*.*)"),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"保存对话框失败：{exc}")
            return {"ok": False, "error": f"打开保存对话框失败：{exc}"}

        if not result:
            return {"ok": False, "cancelled": True}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content or "")
            self._last_dir = os.path.dirname(path) or self._last_dir
            log(f"已保存：{path}")
            return {"ok": True, "path": path}
        except OSError as exc:
            return {"ok": False, "error": f"写入失败：{exc}"}

    def open_data_dir(self) -> dict:
        from agenthub import DATA_DIR

        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(DATA_DIR)  # noqa: S606
            else:
                subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", DATA_DIR], check=True)
            return {"ok": True, "path": DATA_DIR}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def app_info(self) -> dict:
        from agenthub import __version__

        return {"name": APP_NAME, "version": __version__, "port": self.port}


# 服务就绪前的启动页，避免窗口空白或显示「未响应」
SPLASH_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>agentHUB</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;
       background:#f7f8fa;color:#2b2f36;
       font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei","Segoe UI",system-ui,sans-serif}
  .box{text-align:center}
  .dot{width:10px;height:10px;border-radius:50%;background:#6cb0ff;display:inline-block;
       margin:0 3px;animation:b 1.2s infinite ease-in-out}
  .dot:nth-child(2){animation-delay:.15s;background:#8fc4ff}
  .dot:nth-child(3){animation-delay:.3s;background:#ffb058}
  @keyframes b{0%,80%,100%{transform:translateY(0);opacity:.5}
               40%{transform:translateY(-6px);opacity:1}}
  h1{font-size:17px;font-weight:600;margin:0 0 6px}
  p{margin:0;color:#7a828e;font-size:13px}
</style></head>
<body><div class="box">
  <h1>agentHUB</h1>
  <p>正在扫描本地会话记录…</p>
  <p style="margin-top:14px">
    <span class="dot"></span><span class="dot"></span><span class="dot"></span>
  </p>
</div></body></html>
"""


# 页面加载后注入的垫片：只做浏览器里多余行为的抑制。
# 下载改走 app.js 的 saveText()：它检测到 window.pywebview.api.save_text 就直接调原生对话框，
# 不再依赖改写 document.createElement 那种做法。
DESKTOP_SHIM = r"""
(() => {
  if (window.__agenthubDesktop) return;
  window.__agenthubDesktop = true;
  // 中键在 WebView 里会触发「新窗口打开」，本地应用没有标签页概念，直接吞掉
  document.addEventListener('auxclick', e => { if (e.button === 1) e.preventDefault(); }, true);
})();
"""


# ---------------------------------------------------------------- 主流程

def _storage_dir() -> str:
    from agenthub import DATA_DIR

    path = os.path.join(DATA_DIR, "webview")
    os.makedirs(path, exist_ok=True)
    return path


def main(argv=None) -> int:
    _install_crash_logger()
    parser = argparse.ArgumentParser(prog=APP_NAME, description="agentHUB 桌面版")
    parser.add_argument("--port", type=int, default=0, help="内部服务端口，默认自动挑选")
    parser.add_argument("--debug", action="store_true", help="打开 WebView 开发者工具")
    args = parser.parse_args(argv)

    log("=" * 50)
    log(f"{APP_NAME} 桌面版启动（frozen={getattr(sys, 'frozen', False)}）")

    try:
        import webview
    except ImportError:
        log("缺少 pywebview")
        return 2

    try:
        port = pick_port(args.port)
        log(f"选定端口 {port}")
        server = ServerThread(port)

        api = Api()
        api.port = port

        # 先开窗口显示启动页，扫描在后台跑，避免启动时界面像卡死
        window = webview.create_window(
            APP_TITLE,
            html=SPLASH_HTML,
            js_api=api,
            width=WIN_W,
            height=WIN_H,
            min_size=(MIN_W, MIN_H),
            background_color="#f7f8fa",
            text_select=True,
            easy_drag=False,
        )
        api._window = window

        url = f"http://127.0.0.1:{port}/"

        def boot():
            server.start()
            ok = server.wait_ready(180)
            if server.error is not None:
                log(f"启动失败：{server.error}")
                _show_error(window, f"服务启动失败：{server.error}")
                return
            if not ok:
                log("启动超时")
                _show_error(window, "扫描超时，请查看日志：" + _log_path())
                return
            try:
                window.load_url(url)
                log("已切换主界面")
            except Exception as exc:  # noqa: BLE001
                log(f"切换主界面失败：{exc}")

        threading.Thread(target=boot, daemon=True, name="agenthub-boot").start()

        def on_loaded():
            def inject():
                time.sleep(0.5)
                try:
                    window.evaluate_js(DESKTOP_SHIM)
                    log("垫片注入完成")
                except Exception as exc:  # noqa: BLE001
                    log(f"垫片注入失败：{exc}")

            threading.Thread(target=inject, daemon=True).start()

        window.events.loaded += on_loaded

        webview.start(debug=args.debug, private_mode=False, storage_path=_storage_dir())

    except Exception:
        log("主流程异常：\n" + traceback.format_exc())
        raise
    finally:
        try:
            server.stop()
        except Exception:
            pass
        log("已退出")
    return 0


def _show_error(window, msg: str) -> None:
    """把错误显示在窗口里，而不是留一片空白。"""
    try:
        import html as _html

        window.load_html(
            "<html><head><meta charset='utf-8'></head>"
            "<body style='font:14px/1.7 -apple-system,\"PingFang SC\",\"Microsoft YaHei\",sans-serif;padding:40px;color:#b4232c'>"
            f"<h3>启动失败</h3><p>{_html.escape(msg)}</p></body></html>"
        )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
