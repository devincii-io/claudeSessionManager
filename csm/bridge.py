"""QWebChannel bridge: the single object exposed to the JavaScript frontend.

Slots return JSON strings (parsed in JS) to sidestep nested-QVariant conversion
quirks. Filesystem changes are debounced into a single ``dataChanged`` signal so
the UI can refresh live without redrawing on every byte appended to a transcript.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import actions, paths
from .scanner import Scanner
from .session_parser import detail
from .watcher import Watcher


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Bridge(QObject):
    dataChanged = Signal(str)  # emitted (debounced) when the filesystem changes

    def __init__(self, scanner: Scanner, watcher: Watcher) -> None:
        super().__init__()
        self._scanner = scanner
        self._watcher = watcher
        self._open_session: tuple[str, str] | None = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._flush)
        self._watcher.fileEvent.connect(self._on_fs_event)

    # -- filesystem → UI ---------------------------------------------------- #

    def _on_fs_event(self, path: str) -> None:
        self._debounce.start()

    def _flush(self) -> None:
        self.dataChanged.emit("fs")

    # -- read slots --------------------------------------------------------- #

    @Slot(result=str)
    def getOverview(self) -> str:
        return _j({"projects": self._scanner.scan_projects(), "home": str(paths.claude_home())})

    @Slot(str, result=str)
    def getSessions(self, project_id: str) -> str:
        return _j({"sessions": self._scanner.list_sessions(project_id), "project": project_id})

    @Slot(str, str, result=str)
    def getSessionDetail(self, project_id: str, session_id: str) -> str:
        project_path = self._scanner.project_path(project_id)
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        data = detail(jsonl) if jsonl.is_file() else {"events": [], "error": "not found"}
        data["tasks"] = self._scanner.get_tasks(session_id)
        scratch = self._scanner.get_scratchpad(project_path, session_id)
        data["scratchpad"] = scratch
        data["session_id"] = session_id
        data["project_id"] = project_id
        # Live-watch this session's scratchpad while it's open.
        self._open_session = (project_id, session_id)
        self._watcher.watch_scratchpad(scratch.get("dir") or None)
        return _j(data)

    @Slot(str, result=str)
    def getMemory(self, project_id: str) -> str:
        return _j(self._scanner.get_memory(project_id))

    @Slot(result=str)
    def getSettings(self) -> str:
        return _j(self._scanner.get_settings())

    @Slot(result=str)
    def getStatuslineLive(self) -> str:
        return _j(self._scanner.get_statusline_live() or {})

    @Slot(str, result=str)
    def readFile(self, path: str) -> str:
        return _j(actions.read_text_file(path))

    # -- write / action slots ---------------------------------------------- #

    @Slot(str, str, result=str)
    def saveMemory(self, path: str, content: str) -> str:
        return _j(actions.save_memory(path, content))

    @Slot(str, result=str)
    def deleteMemory(self, path: str) -> str:
        return _j(actions.delete_memory(path))

    @Slot(str, str, bool, result=str)
    def deleteSession(self, project_id: str, session_id: str, purge: bool) -> str:
        return _j(actions.delete_session(project_id, session_id, purge))

    @Slot(str, result=str)
    def deleteScratchpadFile(self, path: str) -> str:
        return _j(actions.delete_scratchpad_file(path))

    @Slot(str, result=str)
    def openInEditor(self, path: str) -> str:
        return _j(actions.open_in_editor(path))

    @Slot(str, result=str)
    def openPath(self, path: str) -> str:
        return _j(actions.open_path(path))

    @Slot(str, str, result=str)
    def updateSetting(self, key: str, value_json: str) -> str:
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError:
            value = value_json
        return _j(actions.update_setting(key, value))

    @Slot(result=str)
    def listConfigFiles(self) -> str:
        return _j({"files": actions.list_config_files()})

    @Slot(str, str, result=str)
    def writeClaudeFile(self, path: str, content: str) -> str:
        return _j(actions.write_claude_file(path, content))

    @Slot(result=str)
    def statuslineStatus(self) -> str:
        return _j(actions.statusline_capture_status())

    @Slot(result=str)
    def installStatusline(self) -> str:
        return _j(actions.install_statusline_capture())

    @Slot(result=str)
    def uninstallStatusline(self) -> str:
        return _j(actions.uninstall_statusline_capture())
