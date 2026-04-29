# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all

# PyInstaller runs inside the venv and can't find the Tcl/Tk libraries on its
# own. Point it explicitly to the base Python installation so the tkinter hook
# works correctly.
_base = sys.base_prefix
_tcl  = os.path.join(_base, 'tcl', 'tcl8.6')
_tk   = os.path.join(_base, 'tcl', 'tk8.6')
if os.path.isdir(_tcl):
    os.environ['TCL_LIBRARY'] = _tcl
    os.environ['TK_LIBRARY']  = _tk

datas = []
binaries = []
hiddenimports = []

# argostranslate, ctranslate2, and sentencepiece all have native DLLs / data
# files that PyInstaller can't discover through static import analysis alone.
for pkg in ('argostranslate', 'ctranslate2', 'sentencepiece'):
    d, b, h = collect_all(pkg)
    datas         += d
    binaries      += b
    hiddenimports += h

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        # pywin32 — win32timezone is not discovered by static analysis
        'win32timezone',
        'win32com.server.policy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SAMP-Translate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SAMP-Translate',
)
