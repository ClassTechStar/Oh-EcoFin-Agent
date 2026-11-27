# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：Windows onedir 打包（单文件自解压拖慢冷启动，弃用 onefile）。

构建：pyinstaller desktop/finagent.spec --noconfirm
产物：dist/finagent/finagent.exe（含 backend + frontend 静态资源）
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "desktop", "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # 前端构建产物（先在 frontend/ 执行 npm run build）
        (os.path.join(ROOT, "frontend", "dist"), "frontend/dist"),
        (os.path.join(ROOT, "config.yaml.example"), "."),
    ],
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "backend.app", "backend.config",
        "pywebview.edgechromium",
    ],
    excludes=["tkinter", "matplotlib", "jupyter", "notebook", "torch"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="finagent",
    debug=False,
    strip=False,
    upx=False,          # UPX 触发杀软误报，答辩机风险，禁用
    console=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="finagent",
)
