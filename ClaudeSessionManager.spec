# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec — single-file (onefile) build.

Produces one self-contained ``ClaudeSessionManager`` executable (no ``_internal``
folder beside it). Onefile extracts its payload to a temp dir on each launch, so
a native splash screen is shown by the bootloader during that extraction to keep
cold start feeling responsive; the app closes it once the first page paints.

Build:  uv run pyinstaller --noconfirm ClaudeSessionManager.spec
        (PyInstaller cannot cross-compile — run this on the target OS.)
"""

# Trim clearly-unused heavy libraries so there's less to extract on launch.
# NB: kept conservative — nothing QtWebEngineWidgets/WebChannel depends on.
EXCLUDES = [
    "matplotlib", "numpy", "scipy", "pandas", "PIL", "IPython",
    "pytest", "notebook",
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
    datas=[("web", "web"), ("vendor", "vendor")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

# The onefile splash needs Tcl/Tk (bundled with Windows Python; may be absent on
# a bare Linux box). Build it only when available so the spec stays portable —
# the app's splash-close hook is a no-op when no splash was bundled.
try:
    import tkinter  # noqa: F401

    splash = Splash(
        "web/icons/app-512.png",
        binaries=a.binaries,
        datas=a.datas,
        text_pos=(10, 20),
        text_size=9,
        text_color="#8a8378",
        minify_script=True,
        always_on_top=True,
    )
    _splash_toc = [splash, splash.binaries]
except Exception:
    _splash_toc = []

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    *_splash_toc,
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
