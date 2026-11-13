# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件配置 —— 供安装程序内嵌使用。

构建：
  .venv-build\\Scripts\\pyinstaller.exe --noconfirm --clean packaging\\agenthub_onefile.spec

产物：dist-onefile\\agentHUB.exe（单文件，首次运行自解压到临时目录）
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

web_src = os.path.join(ROOT, "agenthub", "web")
datas = [(os.path.normpath(web_src), os.path.join("agenthub", "web"))]

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
    "matplotlib", "IPython",
]

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
    console=True,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "agenthub.ico"),
)
