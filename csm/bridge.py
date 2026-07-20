"""QWebChannel bridge: the single object exposed to the JavaScript frontend.

Slots return JSON strings (parsed in JS) to sidestep nested-QVariant conversion
quirks. Filesystem changes are debounced into a single ``dataChanged`` signal so
the UI can refresh live without redrawing on every byte appended to a transcript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import time
from collections import Counter
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from . import actions, assistant, paths
from .scanner import Scanner
from .codex_scanner import CodexScanner
from .watcher import Watcher


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Bridge(QObject):
    dataChanged = Signal(str)      # emitted (debounced) when the filesystem changes
    assistantEvent = Signal(str)   # async result of a claude-CLI job (JSON)

    def __init__(self, scanner: Scanner, watcher: Watcher) -> None:
        super().__init__()
        self._scanner = scanner
        self._codex = CodexScanner()
        self._watcher = watcher
        self._window = None  # set by the application shell
        self._open_session: tuple[str, str, str] | None = None
        self._pending_reason: str | None = None
        self._jobs: dict[str, tuple[QProcess, str, QTimer]] = {}

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self._flush)
        # A trailing debounce alone can starve forever while a busy session
        # writes continuously. This timer guarantees a bounded update cadence.
        self._max_wait = QTimer(self)
        self._max_wait.setSingleShot(True)
        self._max_wait.setInterval(1000)
        self._max_wait.timeout.connect(self._flush)
        self._watcher.fileEvent.connect(self._on_fs_event)

    # -- filesystem → UI ---------------------------------------------------- #

    def _on_fs_event(self, path: str) -> None:
        # Statusline capture rewrites constantly while Claude runs; route it as
        # a cheap "statusline" tick so the UI doesn't do a full data refresh.
        reason = self._classify_path(path)
        if self._pending_reason is None:
            self._pending_reason = reason
        elif self._pending_reason != reason and reason != "statusline":
            self._pending_reason = "fs"
        self._debounce.start()

        if not self._max_wait.isActive():
            self._max_wait.start()

    @staticmethod
    def _classify_path(path: str) -> str:
        if path.endswith(".csm-statusline.json"):
            return "statusline"
        try:
            rel = Path(path).resolve().relative_to(paths.projects_dir().resolve())
            if len(rel.parts) == 2 and rel.suffix == ".jsonl":
                return f"session:{rel.parts[0]}:{rel.stem}"
            if len(rel.parts) >= 2:
                return f"project:{rel.parts[0]}"
        except (OSError, ValueError):
            pass
        try:
            Path(path).resolve().relative_to(paths.codex_sessions_dir().resolve())
            return "codex"
        except (OSError, ValueError):
            pass
        return "fs"

    def _flush(self) -> None:
        self._debounce.stop()
        self._max_wait.stop()
        reason = self._pending_reason or "fs"
        self._pending_reason = None
        self.dataChanged.emit(reason)

    # -- read slots --------------------------------------------------------- #

    @Slot(result=str)
    def getOverview(self) -> str:
        return _j({"projects": self._scanner.scan_projects(), "home": str(paths.claude_home())})

    @Slot(str, result=str)
    def getProviderOverview(self, provider: str) -> str:
        if provider == "codex":
            projects = self._codex.scan_projects()
            home = paths.codex_home()
        elif provider == "all":
            claude_projects = self._scanner.scan_projects()
            for item in claude_projects:
                item["provider"] = "claude"
            projects = claude_projects + self._codex.scan_projects()
            projects.sort(key=lambda item: item.get("last_activity", 0), reverse=True)
            home = ""
        else:
            projects = self._scanner.scan_projects()
            for item in projects:
                item["provider"] = "claude"
            home = paths.claude_home()
        return _j({
            "provider": provider, "projects": projects, "home": str(home),
            "claude_home": str(paths.claude_home()), "codex_home": str(paths.codex_home()),
        })

    @Slot(result=str)
    def getAppInfo(self) -> str:
        return _j({
            "platform": platform.system().lower(),
            "custom_window_controls": platform.system() == "Linux" and bool(os.environ.get("WSL_DISTRO_NAME")),
        })

    @Slot(str, result=str)
    def getSessions(self, project_id: str) -> str:
        return _j({"sessions": self._scanner.list_sessions(project_id), "project": project_id})

    @Slot(str, str, result=str)
    def getProviderSessions(self, provider: str, project_id: str) -> str:
        if provider == "codex":
            sessions = self._codex.list_sessions(project_id)
        else:
            sessions = self._scanner.list_sessions(project_id)
            for item in sessions:
                item["provider"] = "claude"
        return _j({"provider": provider, "sessions": sessions, "project": project_id})

    @Slot(str, str, result=str)
    def getSessionDetail(self, project_id: str, session_id: str) -> str:
        if self._open_session and self._open_session != ("claude", project_id, session_id):
            self._release_open_session()
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
        self._open_session = ("claude", project_id, session_id)
        self._watcher.watch_scratchpad(scratch.get("dir") or None)
        return _j(data)

    @Slot(str, str, str, result=str)
    def getProviderSessionDetail(self, provider: str, project_id: str, session_id: str) -> str:
        if self._open_session and self._open_session != (provider, project_id, session_id):
            self._release_open_session()
        if provider == "codex":
            data = self._codex.detail(project_id, session_id)
            path = self._codex.session_path(project_id, session_id)
            data.update({
                "path": str(path) if path else "", "tasks": [], "scratchpad": {"files": []},
                "images": [], "file_history": [], "session_id": session_id, "project_id": project_id,
            })
            self._watcher.watch_scratchpad(None)
        else:
            project_path = self._scanner.project_path(project_id)
            jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
            data = self._scanner.detail(jsonl) if jsonl.is_file() else {"events": [], "error": "not found"}
            data.update({
                "provider": "claude", "path": str(jsonl),
                "tasks": self._scanner.get_tasks(session_id),
                "scratchpad": self._scanner.get_scratchpad(project_path, session_id),
                "images": self._scanner.get_images(session_id),
                "file_history": self._scanner.get_file_history(session_id),
                "session_id": session_id, "project_id": project_id,
            })
            self._watcher.watch_scratchpad(data["scratchpad"].get("dir") or None)
        self._open_session = (provider, project_id, session_id)
        return _j(data)

    def _release_open_session(self) -> None:
        if not self._open_session:
            return
        provider, project_id, session_id = self._open_session
        if provider == "codex":
            self._codex.release_detail(project_id, session_id)
        else:
            self._scanner.release_detail(project_id, session_id)

    @Slot()
    def leaveSession(self) -> None:
        """Release session-specific watches when the inspector is closed."""
        if self._open_session:
            self._release_open_session()
        self._open_session = None
        self._watcher.watch_scratchpad(None)

    @Slot(str, str, int, int, result=str)
    def getTranscriptBefore(self, project_id: str, session_id: str, before: int, count: int) -> str:
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_before(jsonl, before, count))

    @Slot(str, str, int, result=str)
    def getTranscriptAfter(self, project_id: str, session_id: str, after: int) -> str:
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_after(jsonl, after))

    @Slot(str, str, str, int, int, result=str)
    def getProviderTranscriptBefore(self, provider: str, project_id: str, session_id: str, before: int, count: int) -> str:
        if provider == "codex":
            return _j(self._codex.transcript_before(project_id, session_id, before, count))
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_before(jsonl, before, count))

    @Slot(str, str, str, int, result=str)
    def getProviderTranscriptAfter(self, provider: str, project_id: str, session_id: str, after: int) -> str:
        if provider == "codex":
            return _j(self._codex.transcript_after(project_id, session_id, after))
        jsonl = paths.projects_dir() / project_id / f"{session_id}.jsonl"
        return _j(self._scanner.transcript_after(jsonl, after))

    @Slot(str, result=str)
    def searchAll(self, query: str) -> str:
        return _j(self._scanner.search_all(query))

    @Slot(str, str, result=str)
    def searchProvider(self, provider: str, query: str) -> str:
        if provider == "codex":
            return _j(self._codex.search_all(query))
        claude = self._scanner.search_all(query)
        for kind in ("sessions", "prompts"):
            for item in claude.get(kind, []):
                item["provider"] = "claude"
        if provider == "claude":
            return _j(claude)
        codex = self._codex.search_all(query)
        sessions = claude.get("sessions", []) + codex.get("sessions", [])
        sessions.sort(key=lambda item: item.get("mtime", 0), reverse=True)
        prompts = claude.get("prompts", []) + codex.get("prompts", [])
        return _j({"provider": "all", "sessions": sessions[:100], "prompts": prompts[:100]})

    @Slot(result=str)
    def getShells(self) -> str:
        return _j(self._scanner.get_shells())

    @Slot(result=str)
    def getGlobalStats(self) -> str:
        return _j(self._scanner.global_stats())

    @Slot(str, result=str)
    def getProviderGlobalStats(self, provider: str) -> str:
        if provider == "codex":
            return _j(self._codex.global_stats())
        claude = self._scanner.global_stats()
        claude["provider"] = "claude"
        claude["cost_available"] = True
        if provider == "claude":
            return _j(claude)
        codex = self._codex.global_stats()
        usage = {}
        for key in set(claude.get("usage", {})) | set(codex.get("usage", {})):
            usage[key] = int(claude.get("usage", {}).get(key, 0) or 0) + int(codex.get("usage", {}).get(key, 0) or 0)
        by_model = {}
        for source in (claude.get("by_model", {}), codex.get("by_model", {})):
            for model, values in source.items():
                bucket = by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0, "cost": 0.0})
                for key in ("input", "output", "cache_read", "cache_write", "reasoning_output", "total"):
                    bucket[key] = bucket.get(key, 0) + int(values.get(key, 0) or 0)
                bucket["cost"] += float(values.get("cost", 0) or 0)
        tools = Counter(claude.get("tool_counts", {})); tools.update(codex.get("tool_counts", {}))
        days = Counter(dict(claude.get("sessions_by_day", []))); days.update(dict(codex.get("sessions_by_day", [])))
        result = {"provider": "all", "cost": claude.get("cost", 0), "cost_available": False, "usage": usage,
                  "by_model": by_model, "tool_counts": dict(tools.most_common(14)), "sessions_by_day": sorted(days.items())[-14:]}
        for key in ("sessions", "active", "prompts", "turns", "tool_calls", "subagent_sessions"):
            result[key] = int(claude.get(key, 0) or 0) + int(codex.get(key, 0) or 0)
        return _j(result)

    @Slot(result=str)
    def getAllSessions(self) -> str:
        return _j(self._scanner.all_sessions())

    @Slot(str, result=str)
    def getProviderAllSessions(self, provider: str) -> str:
        if provider == "codex":
            return _j(self._codex.all_sessions())
        claude = self._scanner.all_sessions()
        for item in claude.get("sessions", []):
            item["provider"] = "claude"
        if provider == "claude":
            return _j(claude)
        codex = self._codex.all_sessions()
        sessions = claude.get("sessions", []) + codex.get("sessions", [])
        sessions.sort(key=lambda item: item.get("size_bytes", 0) + item.get("extra_bytes", 0), reverse=True)
        return _j({"provider": "all", "sessions": sessions, "home": "", "total_bytes": claude.get("total_bytes", 0) + codex.get("total_bytes", 0), "cost_available": False})

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
    def launchClaude(self, project_path: str, session_id: str) -> str:
        return _j(actions.launch_claude(project_path, session_id))

    @Slot(str, str, str, str, result=str)
    def launchAgent(self, provider: str, project_path: str, session_id: str, mode: str) -> str:
        return _j(actions.launch_session(provider, project_path, session_id, mode))

    @Slot(str, result=str)
    def archiveCodexSession(self, session_id: str) -> str:
        path = self._codex.session_path(session_id)
        if path is None:
            return _j({"ok": False, "error": "Session not found"})
        try:
            if path.is_file() and time.time() - path.stat().st_mtime <= 600:
                return _j({"ok": False, "error": "Session had activity in the last 10 minutes; archive is temporarily blocked"})
        except OSError:
            pass
        return _j(actions.archive_codex_session(session_id))

    @Slot(str, bool, result=str)
    def cleanupSessions(self, items_json: str, purge: bool) -> str:
        try:
            items = json.loads(items_json)
        except json.JSONDecodeError:
            items = []
        results = []
        for item in items if isinstance(items, list) else []:
            provider = (item or {}).get("provider", "claude")
            sid = (item or {}).get("session_id", "")
            pid = (item or {}).get("project_id", "")
            if provider == "codex":
                path = self._codex.session_path(pid, sid)
                try:
                    protected = path is not None and path.is_file() and time.time() - path.stat().st_mtime <= 600
                except OSError:
                    protected = False
                result = {"ok": False, "error": "Recently active"} if protected else actions.archive_codex_session(sid)
            else:
                result = actions.delete_session(pid, sid, purge)
            results.append({"provider": provider, "project_id": pid, "session_id": sid, **result})
        completed = sum(1 for item in results if item.get("ok"))
        return _j({"ok": completed > 0, "completed": completed, "count": len(results), "results": results})

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

    @Slot(result=str)
    def getCodexSettings(self) -> str:
        config = paths.codex_config_file()
        return _j({"provider": "codex", "home": str(paths.codex_home()), "config": str(config), "exists": config.is_file()})

    @Slot(str, result=str)
    def listCodexConfigFiles(self, project_path: str) -> str:
        return _j({"files": actions.list_codex_config_files(project_path)})

    @Slot(str, str, result=str)
    def writeClaudeFile(self, path: str, content: str) -> str:
        return _j(actions.write_claude_file(path, content))

    @Slot(str, str, str, result=str)
    def writeCodexFile(self, path: str, content: str, project_path: str) -> str:
        return _j(actions.write_codex_file(path, content, project_path))

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
        if len(self._jobs) >= 2:
            return _j({"ok": False, "error": "Two optimization jobs are already running"})
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
        # Read the prompt from stdin. Passing a full multi-session prompt as one
        # argv value exceeds Windows' command-line limit surprisingly quickly.
        proc.setArguments(["-p", "--output-format", "json"])
        proc.started.connect(lambda p=proc, data=prompt.encode("utf-8"): (p.write(data), p.closeWriteChannel()))
        proc.finished.connect(lambda code, _status, jid=job_id: self._assistant_finished(jid, code))
        proc.errorOccurred.connect(lambda _e, jid=job_id: self._assistant_error(jid))
        timer = QTimer(proc)
        timer.setSingleShot(True)
        timer.setInterval(15 * 60 * 1000)
        timer.timeout.connect(lambda jid=job_id: self._assistant_timeout(jid))
        self._jobs[job_id] = (proc, kind, timer)
        proc.start()
        timer.start()
        return _j({"ok": True, "job_id": job_id})

    def _assistant_finished(self, job_id: str, code: int) -> None:
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            return
        proc, kind, timer = entry
        timer.stop()
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
        proc, _kind, timer = entry
        timer.stop()
        self.assistantEvent.emit(_j({
            "job_id": job_id, "status": "error",
            "ok": False, "error": proc.errorString() or "failed to start claude",
        }))

    def _assistant_timeout(self, job_id: str) -> None:
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            return
        proc, _kind, _timer = entry
        proc.kill()
        self.assistantEvent.emit(_j({
            "job_id": job_id, "status": "error", "ok": False,
            "error": "Optimization timed out after 15 minutes",
        }))

    @Slot(str, result=str)
    def cancelAssistant(self, job_id: str) -> str:
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            return _j({"ok": False, "error": "job not found"})
        proc, _kind, timer = entry
        timer.stop()
        proc.kill()
        return _j({"ok": True, "job_id": job_id})

    @Slot(result=str)
    def statuslineStatus(self) -> str:
        return _j(actions.statusline_capture_status())

    @Slot(result=str)
    def installStatusline(self) -> str:
        return _j(actions.install_statusline_capture())

    @Slot(result=str)
    def uninstallStatusline(self) -> str:
        return _j(actions.uninstall_statusline_capture())
