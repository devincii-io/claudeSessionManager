"""QWebChannel bridge: the single object exposed to the JavaScript frontend.

Slots return JSON strings (parsed in JS) to sidestep nested-QVariant conversion
quirks. Filesystem changes are debounced into a single ``dataChanged`` signal so
the UI can refresh live without redrawing on every byte appended to a transcript.
"""

from __future__ import annotations

import json
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from . import actions, assistant, paths
from .scanner import Scanner
from .watcher import Watcher


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Bridge(QObject):
    dataChanged = Signal(str)      # emitted (debounced) when the filesystem changes
    assistantEvent = Signal(str)   # async result of a claude-CLI job (JSON)

    def __init__(self, scanner: Scanner, watcher: Watcher) -> None:
        super().__init__()
        self._scanner = scanner
        self._watcher = watcher
        self._window = None  # set by the application shell
        self._open_session: tuple[str, str] | None = None
        self._pending_reason: str | None = None
        self._jobs: dict[str, tuple[QProcess, str]] = {}  # job_id -> (proc, kind)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._flush)
        self._watcher.fileEvent.connect(self._on_fs_event)

    # -- filesystem → UI ---------------------------------------------------- #

    def _on_fs_event(self, path: str) -> None:
        # Statusline capture rewrites constantly while Claude runs; route it as
        # a cheap "statusline" tick so the UI doesn't do a full data refresh.
        if path.endswith(".csm-statusline.json"):
            if self._pending_reason is None:
                self._pending_reason = "statusline"
        else:
            self._pending_reason = "fs"
        self._debounce.start()

    def _flush(self) -> None:
        reason = self._pending_reason or "fs"
        self._pending_reason = None
        self.dataChanged.emit(reason)

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
        data = self._scanner.detail(jsonl) if jsonl.is_file() else {"events": [], "error": "not found"}
        data["tasks"] = self._scanner.get_tasks(session_id)
        scratch = self._scanner.get_scratchpad(project_path, session_id)
        data["scratchpad"] = scratch
        data["images"] = self._scanner.get_images(session_id)
        data["file_history"] = self._scanner.get_file_history(session_id)
        data["session_id"] = session_id
        data["project_id"] = project_id
        # Live-watch this session's workspace while it's open.
        self._open_session = (project_id, session_id)
        self._watcher.watch_scratchpad(scratch.get("dir") or None)
        return _j(data)

    @Slot(str, str, int, int, result=str)
    def getTranscriptBefore(self, project_id: str, session_id: str, before: int, count: int) -> str:
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_before(jsonl, before, count))

    @Slot(str, str, int, result=str)
    def getTranscriptAfter(self, project_id: str, session_id: str, after: int) -> str:
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_after(jsonl, after))

    @Slot(str, result=str)
    def searchAll(self, query: str) -> str:
        return _j(self._scanner.search_all(query))

    @Slot(result=str)
    def getShells(self) -> str:
        return _j(self._scanner.get_shells())

    @Slot(result=str)
    def getGlobalStats(self) -> str:
        return _j(self._scanner.global_stats())

    @Slot(result=str)
    def getAllSessions(self) -> str:
        return _j(self._scanner.all_sessions())

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

    @Slot(str, bool, result=str)
    def deleteSessions(self, items_json: str, purge: bool) -> str:
        try:
            items = json.loads(items_json)
        except json.JSONDecodeError:
            items = []
        return _j(actions.delete_sessions(items, purge))

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

    @Slot(str, result=str)
    def updateSettings(self, items_json: str) -> str:
        """Apply many edits at once (each ``{"key","value"}``; null value = delete)."""
        try:
            items = json.loads(items_json)
        except json.JSONDecodeError:
            items = []
        return _j(actions.update_settings(items))

    @Slot(result=str)
    def listConfigFiles(self) -> str:
        return _j({"files": actions.list_config_files()})

    @Slot(str, str, result=str)
    def writeClaudeFile(self, path: str, content: str) -> str:
        return _j(actions.write_claude_file(path, content))

    # -- window controls (WSLg title bars can be nearly invisible) ---------- #

    @Slot()
    def windowMinimize(self) -> None:
        if self._window is not None:
            self._window.showMinimized()

    @Slot()
    def windowClose(self) -> None:
        if self._window is not None:
            self._window.close()

    # -- CLAUDE.md guidance & consolidation -------------------------------- #

    @Slot(str, str, result=str)
    def getGuidance(self, scope: str, project_path: str) -> str:
        return _j(actions.read_guidance(scope, project_path))

    @Slot(str, str, str, result=str)
    def saveGuidance(self, scope: str, content: str, project_path: str) -> str:
        return _j(actions.write_guidance(scope, content, project_path))

    @Slot(str, str, result=str)
    def writeMemoryNotes(self, project_id: str, notes_json: str) -> str:
        try:
            notes = json.loads(notes_json)
        except json.JSONDecodeError:
            notes = []
        return _j(actions.write_memory_notes(project_id, notes))

    @Slot(str, result=str)
    def startAssistant(self, req_json: str) -> str:
        """Launch a headless ``claude`` job. Returns a job_id immediately; the
        result arrives later via the ``assistantEvent`` signal so the UI never
        blocks on the (multi-second) model call."""
        try:
            req = json.loads(req_json)
        except json.JSONDecodeError:
            return _j({"ok": False, "error": "bad request"})
        binp = assistant.claude_bin()
        if not binp:
            return _j({"ok": False, "error": "The 'claude' CLI was not found. Install Claude Code and sign in."})

        kind = req.get("kind", "tune")
        summaries = self._scanner.summaries_for(req.get("sessions") or [])
        if kind == "consolidate":
            prompt = assistant.build_consolidate_prompt(req, summaries)
        else:
            prompt = assistant.build_tune_prompt(req, summaries)

        job_id = uuid4().hex
        proc = QProcess(self)
        proc.setProgram(binp)
        proc.setArguments(["-p", prompt, "--output-format", "json"])
        proc.finished.connect(lambda code, _status, jid=job_id: self._assistant_finished(jid, code))
        proc.errorOccurred.connect(lambda _e, jid=job_id: self._assistant_error(jid))
        self._jobs[job_id] = (proc, kind)
        proc.start()
        return _j({"ok": True, "job_id": job_id})

    def _assistant_finished(self, job_id: str, code: int) -> None:
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            return
        proc, kind = entry
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        result = assistant.parse_result(out, err, code)
        # Consolidate returns a JSON array of notes — parse it here (tolerant of
        # code fences / stray prose) so the UI receives ready-to-write records.
        if kind == "consolidate" and result.get("ok"):
            result["notes"] = assistant.parse_memory_notes(result.get("text") or "")
        result["kind"] = kind
        result["job_id"] = job_id
        result["status"] = "done" if result.get("ok") else "error"
        self.assistantEvent.emit(_j(result))

    def _assistant_error(self, job_id: str) -> None:
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            return
        proc, _kind = entry
        self.assistantEvent.emit(_j({
            "job_id": job_id, "status": "error",
            "ok": False, "error": proc.errorString() or "failed to start claude",
        }))

    @Slot(result=str)
    def statuslineStatus(self) -> str:
        return _j(actions.statusline_capture_status())

    @Slot(result=str)
    def installStatusline(self) -> str:
        return _j(actions.install_statusline_capture())

    @Slot(result=str)
    def uninstallStatusline(self) -> str:
        return _j(actions.uninstall_statusline_capture())
