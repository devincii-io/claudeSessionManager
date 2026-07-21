"""Scanner for Codex's date-partitioned rollout archive.

This backend is intentionally isolated from :mod:`asm.scanner`: callers can
adopt it provider-by-provider without changing the stable Claude implementation.
Public records use the same field names as the current frontend and always carry
``provider="codex"``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path

from . import paths
from .codex_session_parser import DetailBuilder, SessionSummary, SummaryBuilder
from .session_parser import iter_file_records, read_new_lines

ACTIVE_WINDOW_SECONDS = 120
PROTECTED_WINDOW_SECONDS = 600
MAX_DETAIL_STATES = 2


def _project_id(cwd: str) -> str:
    """Return a compact deterministic id without exposing a path as an API id."""
    normalized = os.path.normcase(os.path.normpath(cwd or "<unknown>"))
    digest = hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"codex-{digest}"


def _add_usage(target: dict, source: dict) -> None:
    for key in ("input", "output", "cache_read", "cache_write", "reasoning_output", "total"):
        target[key] = int(target.get(key, 0)) + int(source.get(key, 0) or 0)


def _usage_zero() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning_output": 0, "total": 0}


class CodexScanner:
    """Incrementally index root Codex sessions and reconstruct their details."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home is not None else paths.codex_home()
        self.sessions_root = self.home / "sessions"
        self.index_file = self.home / "session_index.jsonl"
        self._sum_states: dict[str, dict] = {}
        self._detail_states: dict[str, dict] = {}
        self._locators: dict[tuple[str, str], Path] = {}
        self._session_locators: dict[str, Path] = {}
        self._projects: dict[str, str] = {}
        self._root_summaries: dict[str, SessionSummary] = {}
        self._child_summaries: dict[str, list[SessionSummary]] = {}
        self._titles: dict[str, str] = {}
        self._title_stamp: tuple[int, int] | None = None
        self._archived: dict[str, float] = {}
        self._archive_state_known = False
        self._archive_stamp: tuple[str, int, int] | None = None

    # -- incremental parsing -------------------------------------------------

    @staticmethod
    def _feed_lines(builder, path: Path, offset: int) -> int:
        lines, new_offset = read_new_lines(path, offset)
        for line in lines:
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            try:
                builder.feed(record)
            except Exception:
                # A future record variant must not hide the rest of the archive.
                continue
        return new_offset

    def _summary(self, rollout: Path) -> SessionSummary | None:
        try:
            stat = rollout.stat()
        except OSError:
            return None
        key = str(rollout)
        state = self._sum_states.get(key)
        if state is None or state["offset"] > stat.st_size:
            state = {"builder": SummaryBuilder(), "offset": 0}
            self._sum_states[key] = state
        state["offset"] = self._feed_lines(state["builder"], rollout, state["offset"])
        builder: SummaryBuilder = state["builder"]
        # Until the first complete session_meta line is available there is no
        # safe identity.  In particular, the rollout filename is not the API id.
        if not builder.session_id:
            return None
        return builder.result(builder.session_id, key, stat.st_size, stat.st_mtime)

    def _load_titles(self) -> None:
        try:
            stat = self.index_file.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            self._titles = {}
            self._title_stamp = None
            return
        if stamp == self._title_stamp:
            return
        titles: dict[str, str] = {}
        for record in iter_file_records(self.index_file):
            if not isinstance(record, dict):
                continue
            sid = str(record.get("id") or "")
            title = str(record.get("thread_name") or record.get("title") or "").strip()
            if sid and title:
                titles[sid] = title
        self._titles = titles
        self._title_stamp = stamp

    def _discover(self) -> None:
        self._load_titles()
        self._load_archived_state()
        summaries: list[SessionSummary] = []
        if self.sessions_root.is_dir():
            for rollout in self.sessions_root.rglob("*.jsonl"):
                summary = self._summary(rollout)
                if summary is not None:
                    summaries.append(summary)

        # Prefer the newest duplicate if an imported archive repeats an id.
        by_id: dict[str, SessionSummary] = {}
        for summary in summaries:
            previous = by_id.get(summary.session_id)
            if previous is None or summary.mtime >= previous.mtime:
                by_id[summary.session_id] = summary

        roots: dict[str, SessionSummary] = {}
        children: dict[str, list[SessionSummary]] = {}
        projects: dict[str, str] = {}
        locators: dict[tuple[str, str], Path] = {}
        session_locators: dict[str, Path] = {}

        for summary in by_id.values():
            session_locators[summary.session_id] = Path(summary.path)
            if summary.is_subagent:
                if summary.parent_session_id:
                    children.setdefault(summary.parent_session_id, []).append(summary)
                continue
            pid = _project_id(summary.cwd)
            projects[pid] = summary.cwd
            summary.title = self._titles.get(summary.session_id, summary.title)
            roots[summary.session_id] = summary
            locators[(pid, summary.session_id)] = Path(summary.path)

        for parent_id, child_items in children.items():
            parent = roots.get(parent_id)
            if parent is not None:
                parent.has_subagents = True
                parent.subagent_calls = max(parent.subagent_calls, len(child_items))

        self._projects = projects
        self._locators = locators
        self._session_locators = session_locators
        self._root_summaries = roots
        self._child_summaries = children

    def _load_archived_state(self) -> None:
        """Read Codex's versioned state database without ever mutating it."""
        candidates = sorted(self.home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        if not candidates:
            self._archived = {}
            self._archive_state_known = False
            self._archive_stamp = None
            return
        try:
            stat = candidates[0].stat()
            stamp = (str(candidates[0]), stat.st_size, stat.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp is not None and stamp == self._archive_stamp:
            return
        self._archived = {}
        self._archive_state_known = False
        db = None
        try:
            uri = candidates[0].resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=1)
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if not {"id", "archived"}.issubset(columns):
                return
            archived_at = "archived_at" if "archived_at" in columns else "NULL"
            rows = db.execute(f"SELECT id, {archived_at} FROM threads WHERE archived = 1").fetchall()
            self._archived = {str(sid): float(archived_at or 0) for sid, archived_at in rows}
            self._archive_state_known = True
            self._archive_stamp = stamp
        except (OSError, sqlite3.Error, ValueError):
            return
        finally:
            if db is not None:
                db.close()

    def _visible_roots(self):
        return (summary for sid, summary in self._root_summaries.items() if sid not in self._archived)

    # -- projects and summaries ---------------------------------------------

    def scan_projects(self) -> list[dict]:
        self._discover()
        now = time.time()
        grouped: dict[str, list[SessionSummary]] = {}
        for summary in self._visible_roots():
            grouped.setdefault(_project_id(summary.cwd), []).append(summary)
        result: list[dict] = []
        for pid, summaries in grouped.items():
            cwd = self._projects.get(pid, "")
            last_activity = max((item.mtime for item in summaries), default=0.0)
            result.append({
                "provider": "codex",
                "id": pid,
                "path": cwd,
                "name": Path(cwd).name if cwd else "Unknown project",
                "session_count": len(summaries),
                "active_count": sum(1 for item in summaries if now - item.mtime <= ACTIVE_WINDOW_SECONDS),
                "total_cost": 0.0,
                "cost_available": False,
                "total_tokens": sum(int(item.usage.get("total", 0)) for item in summaries),
                "last_activity": last_activity,
                "memory_count": 0,
                "exists": bool(cwd) and Path(cwd).is_dir(),
            })
        result.sort(key=lambda item: item["last_activity"], reverse=True)
        return result

    def list_sessions(self, project_id: str) -> list[dict]:
        self._discover()
        now = time.time()
        result: list[dict] = []
        for summary in self._visible_roots():
            if _project_id(summary.cwd) != project_id:
                continue
            item = summary.to_dict()
            item["provider"] = "codex"
            item["active"] = now - summary.mtime <= ACTIVE_WINDOW_SECONDS
            item["protected"] = now - summary.mtime <= PROTECTED_WINDOW_SECONDS
            item["child_session_count"] = len(self._child_summaries.get(summary.session_id, []))
            result.append(item)
        result.sort(key=lambda item: item["mtime"], reverse=True)
        return result

    def summaries_for(self, items: list) -> list[dict]:
        self._discover()
        result = []
        for item in items or []:
            if not isinstance(item, dict) or item.get("provider", "codex") != "codex":
                continue
            summary = self._root_summaries.get(str(item.get("session_id") or ""))
            if summary is not None:
                result.append(summary.to_dict())
        return result

    def project_path(self, project_id: str) -> str:
        self._discover()
        return self._projects.get(project_id, "")

    # -- locator-backed detail API ------------------------------------------

    def session_path(self, project_id: str, session_id: str | None = None) -> Path | None:
        """Resolve a canonical id; accepts ``(session_id)`` or ``(pid, sid)``."""
        self._discover()
        if session_id is None:
            return self._session_locators.get(project_id)
        return self._locators.get((project_id, session_id))

    def _detail_builder(self, rollout: Path) -> DetailBuilder | None:
        key = str(rollout)
        try:
            size = rollout.stat().st_size
        except OSError:
            return None
        state = self._detail_states.get(key)
        if state is None or state["offset"] > size:
            state = {"builder": DetailBuilder(), "offset": 0, "used": 0.0}
            self._detail_states[key] = state
            if len(self._detail_states) > MAX_DETAIL_STATES:
                candidates = [item for item in self._detail_states if item != key]
                if candidates:
                    oldest = min(candidates, key=lambda item: self._detail_states[item]["used"])
                    del self._detail_states[oldest]
        state["offset"] = self._feed_lines(state["builder"], rollout, state["offset"])
        state["used"] = time.time()
        return state["builder"]

    def detail(self, project_id: str, session_id: str | None = None, *, tail: int = 60) -> dict:
        """Return details; accepts ``detail(session_id)`` or ``detail(pid, sid)``."""
        rollout = self.session_path(project_id, session_id)
        if rollout is None:
            return {"provider": "codex", "events": [], "error": "not found"}
        builder = self._detail_builder(rollout)
        if builder is None:
            return {"provider": "codex", "events": [], "error": "not found"}
        result = builder.meta()
        result["events"], result["events_start"] = builder.tail(tail)
        sid = session_id if session_id is not None else project_id
        result["session_id"] = sid
        if session_id is not None:
            result["project_id"] = project_id
        child_items = self._child_summaries.get(sid, [])
        result["child_sessions"] = [
            {
                "provider": "codex",
                "session_id": child.session_id,
                "parent_session_id": child.parent_session_id,
                "agent_path": child.agent_path,
                "title": child.first_prompt or child.agent_path or "Subagent",
                "mtime": child.mtime,
                "models": child.models,
            }
            for child in sorted(child_items, key=lambda item: item.mtime)
        ]
        if child_items:
            result["subagents"]["count"] = max(result["subagents"].get("count", 0), len(child_items))
        return result

    def transcript_before(self, project_id: str, session_id: str, before: int, count: int) -> dict:
        rollout = self.session_path(project_id, session_id)
        builder = self._detail_builder(rollout) if rollout is not None else None
        if builder is None:
            return {"provider": "codex", "events": [], "start": 0, "total": 0}
        events, start = builder.page_before(before, count)
        return {"provider": "codex", "events": events, "start": start, "total": len(builder.events)}

    def transcript_after(self, project_id: str, session_id: str, after: int) -> dict:
        rollout = self.session_path(project_id, session_id)
        builder = self._detail_builder(rollout) if rollout is not None else None
        if builder is None:
            return {"provider": "codex", "events": [], "start": 0, "total": 0}
        events, start = builder.page_after(after)
        return {"provider": "codex", "events": events, "start": start, "total": len(builder.events)}

    def release_detail(self, project_id: str, session_id: str | None = None) -> None:
        rollout = self.session_path(project_id, session_id)
        if rollout is not None:
            self._detail_states.pop(str(rollout), None)

    # -- aggregate/search views ---------------------------------------------

    def all_sessions(self) -> dict:
        self._discover()
        now = time.time()
        records: list[dict] = []
        for summary in self._root_summaries.values():
            records.append({
                "provider": "codex",
                "project_id": _project_id(summary.cwd),
                "project_name": Path(summary.cwd).name if summary.cwd else "Unknown project",
                "project_path": summary.cwd,
                "session_id": summary.session_id,
                "title": summary.title or summary.first_prompt or "Untitled session",
                "first_prompt": summary.first_prompt,
                "cost": 0.0,
                "cost_available": False,
                "tokens": int(summary.usage.get("total", 0)),
                "user_messages": summary.user_messages,
                "assistant_messages": summary.assistant_messages,
                "tool_calls": summary.tool_calls,
                "size_bytes": summary.size_bytes,
                "extra_bytes": 0,
                "mtime": summary.mtime,
                "created": summary.created,
                "active": now - summary.mtime <= ACTIVE_WINDOW_SECONDS,
                "protected": now - summary.mtime <= PROTECTED_WINDOW_SECONDS,
                "has_subagents": summary.has_subagents,
                "child_session_count": len(self._child_summaries.get(summary.session_id, [])),
                "models": list(summary.models),
                "archived": summary.session_id in self._archived,
                "archived_at": self._archived.get(summary.session_id, 0),
                "archive_state_known": self._archive_state_known,
            })
        records.sort(key=lambda item: item["size_bytes"], reverse=True)
        return {
            "provider": "codex",
            "sessions": records,
            "home": str(self.home),
            "total_bytes": sum(item["size_bytes"] for item in records),
            "cost_available": False,
        }

    def global_stats(self) -> dict:
        self._discover()
        usage = _usage_zero()
        by_model: dict[str, dict] = {}
        tools: Counter[str] = Counter()
        days: Counter[str] = Counter()
        now = time.time()
        active = prompts = turns = tool_calls = subagent_sessions = 0
        for summary in self._visible_roots():
            _add_usage(usage, summary.usage)
            prompts += summary.user_messages
            turns += summary.assistant_messages
            tool_calls += summary.tool_calls
            if summary.has_subagents:
                subagent_sessions += 1
            if now - summary.mtime <= ACTIVE_WINDOW_SECONDS:
                active += 1
            for model, model_usage in summary.usage_by_model.items():
                bucket = by_model.setdefault(model, _usage_zero())
                _add_usage(bucket, model_usage)
            tools.update(summary.tool_counts)
            if summary.mtime:
                days[time.strftime("%Y-%m-%d", time.localtime(summary.mtime))] += 1
        by_day = []
        for offset in range(13, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
            by_day.append([day, days.get(day, 0)])
        return {
            "provider": "codex",
            "cost": 0.0,
            "cost_available": False,
            "usage": usage,
            "sessions": sum(1 for _ in self._visible_roots()),
            "active": active,
            "prompts": prompts,
            "turns": turns,
            "tool_calls": tool_calls,
            "subagent_sessions": subagent_sessions,
            "by_model": {model: {**item, "cost": 0.0, "cost_available": False} for model, item in by_model.items()},
            "tool_counts": dict(tools.most_common(14)),
            "sessions_by_day": by_day,
        }

    def search_all(self, query: str) -> dict:
        self._discover()
        needle = query.lower().strip()
        if not needle:
            return {"provider": "codex", "sessions": [], "prompts": []}
        sessions: list[dict] = []
        prompts: list[dict] = []
        for summary in self._visible_roots():
            haystack = " ".join((summary.title, summary.first_prompt, summary.cwd, summary.session_id)).lower()
            if needle in haystack:
                sessions.append({
                    "provider": "codex",
                    "project_id": _project_id(summary.cwd),
                    "project_name": Path(summary.cwd).name if summary.cwd else "Unknown project",
                    "session_id": summary.session_id,
                    "title": summary.title or summary.first_prompt,
                    "cost": 0.0,
                    "cost_available": False,
                    "mtime": summary.mtime,
                })
            if needle in summary.first_prompt.lower():
                prompts.append({
                    "provider": "codex",
                    "display": summary.first_prompt[:220],
                    "project": summary.cwd,
                    "project_name": Path(summary.cwd).name if summary.cwd else "",
                    "project_id": _project_id(summary.cwd),
                    "session_id": summary.session_id,
                    "timestamp": summary.created or summary.mtime,
                })
        sessions.sort(key=lambda item: item["mtime"], reverse=True)
        return {"provider": "codex", "sessions": sessions[:50], "prompts": prompts[:50]}
