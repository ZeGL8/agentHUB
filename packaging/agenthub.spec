# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— agentHUB 桌面版。

构建：
  .venv-build\\Scripts\\pyinstaller.exe --noconfirm --clean packaging\\agenthub.spec

产物：dist\\agentHUB\\agentHUB.exe（onedir，启动快）与 dist\\agentHUB.exe（onefile，单文件）
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# 静态资源：整体带上 agenthub/web（含 KaTeX 字体），保持目录结构
web_src = os.path.join(ROOT, "agenthub", "web")
datas = [
    (web_src, os.path.join("agenthub", "web")),
]
datas = [(os.path.normpath(src), dst) for src, dst in datas if os.path.isdir(src)]

hiddenimports = [
    "agenthub.readers.claude",
    "agenthub.readers.codex",
    "agenthub.readers.dsh",
    "agenthub.readers.hermes",
    "agenthub.readers.opencode",
    "agenthub.readers.workbuddy",
]

excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "test", "distutils",
    "setuptools", "pip", "wheel", "PyInstaller", "PIL", "numpy", "pandas",
    "matplotlib", "IPython", "sqlite3.test",
]

# ---- onedir：目录式，启动快，便于排障 ----
a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
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

exe_dir = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agentHUB",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台：双击可见日志，Ctrl+C / 关窗即停
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "agenthub.ico"),
)
coll = COLLECT(
    exe_dir,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="agentHUB",
)
