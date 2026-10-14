# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_all


rapidocr_datas, rapidocr_binaries, rapidocr_hiddenimports = collect_all("rapidocr")
onnxruntime_datas, onnxruntime_binaries, onnxruntime_hiddenimports = collect_all(
    "onnxruntime"
)

a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=rapidocr_binaries + onnxruntime_binaries,
    datas=[
        ("web", "web"),
        ("configs/default.json", "configs"),
        ("configs/standard-visual-table.json", "configs"),
        ("configs/custom-modules.json", "configs"),
    ]
    + rapidocr_datas
    + onnxruntime_datas,
    hiddenimports=rapidocr_hiddenimports
    + onnxruntime_hiddenimports
    + ["mss.windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=os.getenv("RPA_EXE_NAME", "RPA流程配置器"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
