"""Application shell: a Qt main window hosting the HTML/CSS/JS frontend in a
QWebEngineView, wired to the Python backend over QWebChannel.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _prepare_native_env() -> None:
    """Make QtWebEngine start cleanly on Linux/WSL without system changes.

    - Chromium's sandbox and GPU paths are unreliable under WSL, so default to
      software rendering with the sandbox off (users can override the env var).
    - Preload ``libxkbfile.so.1`` from the vendored copy if the system lacks it,
      so ``import QtWebEngineWidgets`` resolves. Must run *before* that import.
    """
    if sys.platform != "linux":
        return
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    try:
        ctypes.CDLL("libxkbfile.so.1")
    except OSError:
        here = Path(__file__).resolve().parent
        for vendored in (here.parent / "vendor" / "linux-x86_64", here / "vendor" / "linux-x86_64"):
            if (vendored / "libxkbfile.so.1").is_file():
                # Preload for this process AND expose the dir on LD_LIBRARY_PATH so the
                # separately-spawned QtWebEngineProcess subprocess can resolve it too.
                try:
                    ctypes.CDLL(str(vendored / "libxkbfile.so.1"))
                except OSError:
                    pass
                existing = os.environ.get("LD_LIBRARY_PATH", "")
                if str(vendored) not in existing.split(":"):
                    os.environ["LD_LIBRARY_PATH"] = f"{vendored}:{existing}" if existing else str(vendored)
                break


_prepare_native_env()

from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from .bridge import Bridge
from .scanner import Scanner
from .watcher import Watcher

APP_NAME = "Claude Session Manager"


def asset_dir() -> Path:
    """Locate the bundled web frontend across dev / installed / frozen layouts."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "web",  # installed package (csm/web)
        here.parent / "web",  # dev checkout (repo/web)
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / "web")
        candidates.insert(1, Path(meipass) / "csm" / "web")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return candidates[0]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 920)
        self.setMinimumSize(1040, 680)

        self._scanner = Scanner()
        self._watcher = Watcher()
        self._bridge = Bridge(self._scanner, self._watcher)
        self._bridge._window = self

        self._view = QWebEngineView(self)
        self._view.page().setBackgroundColor(QColor("#1b1a17"))
        ws = self._view.settings()
        ws.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
        ws.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.setCentralWidget(self._view)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("backend", self._bridge)
        self._view.page().setWebChannel(self._channel)

        index = asset_dir() / "index.html"
        self._view.load(QUrl.fromLocalFile(str(index)))

        self._watcher.start()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._watcher.stop()
        super().closeEvent(event)


def main() -> int:
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("claude-session-manager")

    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1b1a17"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1b1a17"))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
