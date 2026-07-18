"""Filesystem watcher that turns Claude Code's on-disk changes into Qt signals.

A single watchdog observer monitors the Claude home tree (projects, tasks,
settings, image cache, statusline capture). A caller can additionally watch a
specific scratchpad directory while a session is open. Raw events are coalesced
downstream (the bridge debounces before re-scanning).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import paths


class _Handler(FileSystemEventHandler):
    def __init__(self, emit) -> None:
        super().__init__()
        self._emit = emit

    def on_any_event(self, event) -> None:  # noqa: ANN001
        try:
            src = getattr(event, "src_path", "") or ""
            self._emit(str(src))
        except Exception:
            pass


class Watcher(QObject):
    """Emits :attr:`fileEvent` (queued to the GUI thread) on any watched change."""

    fileEvent = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._observer = Observer()
        self._handler = _Handler(self._on_event)
        self._extra_watch = None

    def _on_event(self, path: str) -> None:
        # Signal emission is thread-safe; delivery is queued to the GUI thread.
        self.fileEvent.emit(path)

    def start(self) -> None:
        home = paths.claude_home()
        if home.is_dir():
            self._observer.schedule(self._handler, str(home), recursive=True)
        self._observer.start()

    def watch_scratchpad(self, scratchpad_dir: str | None) -> None:
        """Watch a scratchpad directory in addition to the Claude home tree."""
        if self._extra_watch is not None:
            try:
                self._observer.unschedule(self._extra_watch)
            except Exception:
                pass
            self._extra_watch = None
        if scratchpad_dir:
            p = Path(scratchpad_dir)
            if p.is_dir():
                try:
                    self._extra_watch = self._observer.schedule(self._handler, str(p), recursive=True)
                except Exception:
                    self._extra_watch = None

    def stop(self) -> None:
        try:
            self._observer.stop()
            self._observer.join(timeout=2)
        except Exception:
            pass
