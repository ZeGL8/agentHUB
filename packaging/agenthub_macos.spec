# -*- mode: python ; coding: utf-8 -*-
"""agentHUB macOS 桌面版打包配置（.app，WKWebView 原生窗口）。

只能在 macOS 上构建（PyInstaller 不能交叉编译），产物架构与构建所用 Python 一致：
在 Apple Silicon 上构建得到 arm64 版，在 Intel Mac 上得到 x86_64 版。
推荐用 packaging/build_macos.sh，它负责装依赖、生成图标、签名并出 dmg。
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = "0.1.0"

a = Analysis(
    [os.path.join(ROOT, "packaging", "desktop.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "agenthub", "web"), os.path.join("agenthub", "web"))],
    hiddenimports=[
        "agenthub.readers.claude",
        "agenthub.readers.codex",
        "agenthub.readers.dsh",
        "agenthub.readers.hermes",
        "agenthub.readers.opencode",
        "agenthub.readers.workbuddy",
        # pywebview 运行时按平台动态 import 后端，PyInstaller 静态分析看不到
        "webview.platforms.cocoa",
    ],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "PIL", "numpy", "pandas", "matplotlib", "IPython",
        "webview.platforms.gtk", "webview.platforms.qt", "webview.platforms.cef",
        "webview.platforms.winforms", "webview.platforms.edgechromium",
        "webview.platforms.mshtml", "webview.platforms.android",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agentHUB",
    console=False,
    upx=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="agentHUB", upx=False)

app = BUNDLE(
    coll,
    name="agentHUB.app",
    icon=os.path.join(ROOT, "packaging", "agenthub.icns"),
    bundle_identifier="io.github.zegl8.agenthub",
    version=VERSION,
    info_plist={
        "CFBundleName": "agentHUB",
        "CFBundleDisplayName": "agentHUB",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": "agentHUB",
        # 窗口只加载 http://127.0.0.1 的内置服务；pywebview 的 cocoa 后端运行时也会放行本地 http
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
