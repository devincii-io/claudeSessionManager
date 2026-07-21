# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec — single-file (onefile) build.

Produces one self-contained ``ClaudeSessionManager`` executable (no ``_internal``
folder beside it). The build deliberately omits Tk/splash payloads and only
bundles the Linux compatibility library on Linux.

Build:  uv run pyinstaller --noconfirm ClaudeSessionManager.spec
        (PyInstaller cannot cross-compile — run this on the target OS.)
"""

import sys

# Trim clearly-unused heavy libraries so there's less to extract on launch.
# NB: kept conservative — nothing QtWebEngineWidgets/WebChannel depends on.
EXCLUDES = [
    "matplotlib", "numpy", "scipy", "pandas", "PIL", "IPython",
    "pytest", "notebook", "unittest", "pydoc", "tkinter", "_tkinter",
    # QtQml/QtQuick are intentionally NOT excluded — QtWebEngine can pull them in.
    "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSensors",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtSerialPort",
]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web")] + ([("vendor", "vendor")] if sys.platform.startswith("linux") else []),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=2,
)

# PySide's hooks collect every Qt translation and Chromium locale. The UI is
# English and the primary workstation locale is German, so retaining only
# those two avoids shipping dozens of never-used locale packs.
def _keep_locale(item):
    dest = item[0].replace("\\", "/")
    if "/translations/qtwebengine_locales/" in dest:
        return dest.endswith(("/en-US.pak", "/de.pak"))
    if "/translations/" in dest and dest.endswith(".qm"):
        return dest.endswith(("_en.qm", "_de.qm"))
    return True


a.datas = [item for item in a.datas if _keep_locale(item)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClaudeSessionManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is deliberately off: compressing Qt/WebEngine DLLs is a common cause of
    # silent startup failures, and its decompression would only slow cold start.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="web/icons/app.ico",
)
