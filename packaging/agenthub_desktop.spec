# -*- mode: python ; coding: utf-8 -*-
"""agentHUB 桌面版打包配置（原生窗口，无控制台）。

构建：
  .venv-build\\Scripts\\pyinstaller.exe --noconfirm --clean packaging\\agenthub_desktop.spec

产物：dist-desktop\\agentHUB\\agentHUB.exe —— 双击直接出一个应用窗口。
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SP = os.path.join(ROOT, ".venv-build", "Lib", "site-packages")


def _collect_webview():
    """pywebview 在 Windows 上依赖 .NET 程序集，手工收全。"""
    import glob

    bins, datas = [], []
    lib_dir = os.path.join(SP, "webview", "lib")
    if os.path.isdir(lib_dir):
        for name in ("Microsoft.Web.WebView2.Core.dll",
                     "Microsoft.Web.WebView2.WinForms.dll",
                     "WebBrowserInterop.x64.dll",
                     "WebBrowserInterop.x86.dll"):
            p = os.path.join(lib_dir, name)
            if os.path.isfile(p):
                datas.append((p, os.path.join("webview", "lib")))
        runtimes = os.path.join(lib_dir, "runtimes")
        if os.path.isdir(runtimes):
            for root, _dirs, files in os.walk(runtimes):
                for f in files:
                    src = os.path.join(root, f)
                    rel = os.path.relpath(root, lib_dir)
                    datas.append((src, os.path.join("webview", "lib", rel)))
    return bins, datas


wv_bins, wv_datas = _collect_webview()

web_src = os.path.join(ROOT, "agenthub", "web")
datas = [(os.path.normpath(web_src), os.path.join("agenthub", "web"))] + wv_datas

hiddenimports = [
    "agenthub.readers.claude",
    "agenthub.readers.codex",
    "agenthub.readers.dsh",
    "agenthub.readers.hermes",
    "agenthub.readers.opencode",
    "agenthub.readers.workbuddy",
    # pywebview 的 Windows 后端走 pythonnet
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
]

excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "test",
    # 注意：不要 exclude distutils / setuptools，它们被 setuptools 的 hook 改名引用，
    # 排掉会触发 "Target module already imported as ExcludedModule" 报错
    "PIL", "numpy", "pandas",
    "matplotlib", "IPython",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.cocoa",
    "webview.platforms.cef",
    "webview.platforms.android",
    "webview.platforms.mshtml",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "desktop.py")],
    pathex=[ROOT],
    binaries=wv_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agentHUB",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 桌面程序：不弹控制台窗口
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "agenthub.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt")
    if os.path.isfile(os.path.join(ROOT, "packaging", "version_info.txt")) else None,
)