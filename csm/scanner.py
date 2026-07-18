"""Enumerate Claude Code's on-disk state: projects, sessions, memory, tasks,
scratchpads, images, shells, settings and the optional live statusline capture.

Performance:

* Session summaries persist in a disk cache keyed by mtime+size.
* For files that change while the app runs, an in-memory *incremental* builder
  (byte offset + aggregates) parses only appended bytes — live sessions cost
  microseconds per refresh instead of a full re-parse.
* The opened session's transcript uses the same incremental scheme.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from . import paths, pricing
from .session_parser import (
    DetailBuilder,
    SessionSummary,
    SummaryBuilder,
    _loads,
    read_new_lines,
)

ACTIVE_WINDOW_SECONDS = 120  # a session whose jsonl changed this recently is "live"
CACHE_VERSION = 2  # bump when SessionSummary's schema or computation changes
MAX_DETAIL_STATES = 4  # incremental transcript states kept in memory


class Scanner:
    def __init__(self) -> None:
        self._cache_path = paths.cache_dir() / "summaries.json"
        self._cache: dict[str, dict] = {}
        self._cache_dirty = False
        # path -> {"builder": SummaryBuilder, "offset": int}
        self._sum_states: dict[str, dict] = {}
        # path -> {"builder": DetailBuilder, "offset": int, "used": float}
        self._detail_states: dict[str, dict] = {}
        self._load_cache()

    # -- summary cache ------------------------------------------------------ #

    def _load_cache(self) -> None:
        try:
            blob = json.loads(self._cache_path.read_text("utf-8"))
            self._cache = blob.get("entries", {}) if blob.get("_v") == CACHE_VERSION else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            self._cache = {}

    def _save_cache(self) -> None:
        if not self._cache_dirty:
            return
        try:
            self._cache_path.write_text(json.dumps({"_v": CACHE_VERSION, "entries": self._cache}), "utf-8")
            self._cache_dirty = False
        except OSError:
            pass

    def _feed_lines(self, builder, path: Path, offset: int) -> int:
        lines, new_offset = read_new_lines(path, offset)
        feed = builder.feed
        for line in lines:
            if not line:
                continue
            try:
                feed(_loads(line))
            except Exception:
                continue
        return new_offset

    def _summary(self, jsonl: Path) -> SessionSummary:
        try:
            st = jsonl.stat()
        except OSError:
            return SessionSummary(session_id=jsonl.stem, path=str(jsonl))
        key = str(jsonl)
        cached = self._cache.get(key)
        if cached and cached.get("size_bytes") == st.st_size and cached.get("mtime") == st.st_mtime:
            return SessionSummary(**cached)

        state = self._sum_states.get(key)
        if state is None or state["offset"] > st.st_size:  # new or truncated/rewritten
            state = {"builder": SummaryBuilder(), "offset": 0}
            self._sum_states[key] = state
        state["offset"] = self._feed_lines(state["builder"], jsonl, state["offset"])
        summary = state["builder"].result(jsonl.stem, key, st.st_size, st.st_mtime)
        self._cache[key] = summary.to_dict()
        self._cache_dirty = True
        return summary

    # -- projects & sessions ------------------------------------------------ #

    def scan_projects(self) -> list[dict]:
        root = paths.projects_dir()
        if not root.is_dir():
            return []
        now = time.time()
        projects: list[dict] = []
        for pdir in sorted(root.iterdir()):
            if not pdir.is_dir():
                continue
            summaries = [self._summary(f) for f in pdir.glob("*.jsonl")]
            cwd = next((s.cwd for s in summaries if s.cwd), "")
            name = Path(cwd).name if cwd else pdir.name.lstrip("-").replace("-", "/")
            total_cost = sum(s.cost for s in summaries)
            total_tokens = sum(s.usage.get("total", 0) if isinstance(s.usage, dict) else 0 for s in summaries)
            last_activity = max((s.mtime for s in summaries), default=0.0)
            active = sum(1 for s in summaries if now - s.mtime <= ACTIVE_WINDOW_SECONDS)
            mem_dir = pdir / "memory"
            memory_count = len([p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"]) if mem_dir.is_dir() else 0
            projects.append(
                {
                    "id": pdir.name,
                    "path": cwd,
                    "name": name or pdir.name,
                    "session_count": len(summaries),
                    "active_count": active,
                    "total_cost": round(total_cost, 4),
                    "total_tokens": total_tokens,
                    "last_activity": last_activity,
                    "memory_count": memory_count,
                    "exists": bool(cwd) and Path(cwd).is_dir(),
                }
            )
        self._save_cache()
        projects.sort(key=lambda p: p["last_activity"], reverse=True)
        return projects

    def list_sessions(self, project_id: str) -> list[dict]:
        pdir = paths.projects_dir() / project_id
        if not pdir.is_dir():
            return []
        now = time.time()
        out: list[dict] = []
        for f in pdir.glob("*.jsonl"):
            s = self._summary(f).to_dict()
            s["active"] = now - s["mtime"] <= ACTIVE_WINDOW_SECONDS
            out.append(s)
        self._save_cache()
        out.sort(key=lambda s: s["mtime"], reverse=True)
        return out

    def all_sessions(self) -> dict:
        """Every session on the machine as a lightweight record, newest-heaviest
        first — powers the Cleanup helper and the "which sessions to include"
        picker. Built from cached summaries plus each session's on-disk footprint
        (transcript + ancillary tasks/history/image/env data)."""
        root = paths.projects_dir()
        now = time.time()
        out: list[dict] = []
        if root.is_dir():
            for pdir in sorted(root.iterdir()):
                if not pdir.is_dir():
                    continue
                summaries = [self._summary(f) for f in pdir.glob("*.jsonl")]
                cwd = next((s.cwd for s in summaries if s.cwd), "")
                pname = Path(cwd).name if cwd else pdir.name.lstrip("-").replace("-", "/")
                for s in summaries:
                    extra = self._session_footprint(s.session_id)
                    out.append({
                        "project_id": pdir.name,
                        "project_name": pname or pdir.name,
                        "project_path": cwd,
                        "session_id": s.session_id,
                        "title": s.title or s.first_prompt or "Untitled session",
                        "first_prompt": s.first_prompt,
                        "cost": round(s.cost, 4),
                        "tokens": (s.usage or {}).get("total", 0) if isinstance(s.usage, dict) else 0,
                        "user_messages": s.user_messages,
                        "assistant_messages": s.assistant_messages,
                        "tool_calls": s.tool_calls,
                        "size_bytes": s.size_bytes,
                        "extra_bytes": extra,
                        "mtime": s.mtime,
                        "created": s.created,
                        "active": now - s.mtime <= ACTIVE_WINDOW_SECONDS,
                        "has_subagents": s.has_subagents,
                        "models": [m for m in s.models if m != "<synthetic>"],
                    })
        self._save_cache()
        out.sort(key=lambda x: x["size_bytes"] + x["extra_bytes"], reverse=True)
        total_bytes = sum(x["size_bytes"] + x["extra_bytes"] for x in out)
        return {"sessions": out, "home": str(paths.claude_home()), "total_bytes": total_bytes}

    def summaries_for(self, items: list) -> list[dict]:
        """Cached summary dicts for a list of ``{project_id, session_id}`` — the
        context an assistant job digests (never full transcripts)."""
        out: list[dict] = []
        for it in items or []:
            pid = (it or {}).get("project_id")
            sid = (it or {}).get("session_id")
            if not pid or not sid:
                continue
            f = paths.projects_dir() / pid / f"{sid}.jsonl"
            if f.is_file():
                out.append(self._summary(f).to_dict())
        return out

    def _session_footprint(self, session_id: str) -> int:
        """Bytes of a session's ancillary data (tasks, file-history, image-cache,
        session-env) — the extra space a purge deletion reclaims."""
        home = paths.claude_home()
        total = 0
        for d in (
            paths.tasks_dir(session_id),
            paths.file_history_dir(session_id),
            paths.image_cache_dir(session_id),
            home / "session-env" / session_id,
        ):
            total += _dir_size(d)
        return total

    def project_path(self, project_id: str) -> str:
        pdir = paths.projects_dir() / project_id
        for f in pdir.glob("*.jsonl"):
            s = self._summary(f)
            if s.cwd:
                return s.cwd
        return ""

    # -- session detail (incremental, paged) --------------------------------- #

    def _detail_builder(self, jsonl: Path) -> DetailBuilder | None:
        """Return the up-to-date incremental builder for a transcript."""
        key = str(jsonl)
        try:
            size = jsonl.stat().st_size
        except OSError:
            return None
        state = self._detail_states.get(key)
        if state is None or state["offset"] > size:
            state = {"builder": DetailBuilder(), "offset": 0, "used": 0.0}
            self._detail_states[key] = state
            # Evict least-recently-used states beyond the cap.
            if len(self._detail_states) > MAX_DETAIL_STATES:
                oldest = min(
                    (k for k in self._detail_states if k != key),
                    key=lambda k: self._detail_states[k]["used"],
                    default=None,
                )
                if oldest:
                    del self._detail_states[oldest]
        state["offset"] = self._feed_lines(state["builder"], jsonl, state["offset"])
        state["used"] = time.time()
        return state["builder"]

    def detail(self, jsonl: Path, *, tail: int = 60) -> dict:
        """Aggregates + the last `tail` transcript events (paged elsewhere)."""
        b = self._detail_builder(jsonl)
        if b is None:
            return {"events": [], "error": "not found"}
        out = b.meta()
        out["events"], out["events_start"] = b.tail(tail)
        return out

    def transcript_before(self, jsonl: Path, before: int, count: int) -> dict:
        b = self._detail_builder(jsonl)
        if b is None:
            return {"events": [], "start": 0}
        events, start = b.page_before(before, count)
        return {"events": events, "start": start, "total": len(b.events)}

    def transcript_after(self, jsonl: Path, after: int) -> dict:
        b = self._detail_builder(jsonl)
        if b is None:
            return {"events": [], "start": 0}
        events, start = b.page_after(after)
        return {"events": events, "start": start, "total": len(b.events)}

    # -- memory ------------------------------------------------------------- #

    def get_memory(self, project_id: str) -> dict:
        mem_dir = paths.projects_dir() / project_id / "memory"
        index = ""
        files: list[dict] = []
        if mem_dir.is_dir():
            idx = mem_dir / "MEMORY.md"
            if idx.is_file():
                index = idx.read_text("utf-8", errors="replace")
            for f in sorted(mem_dir.glob("*.md")):
                if f.name == "MEMORY.md":
                    continue
                text = f.read_text("utf-8", errors="replace")
                meta = _parse_frontmatter(text)
                try:
                    st = f.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                files.append(
                    {
                        "name": f.name,
                        "path": str(f),
                        "title": meta.get("name", f.stem),
                        "description": meta.get("description", ""),
                        "type": meta.get("type", ""),
                        "size": size,
                        "mtime": mtime,
                        "content": text,
                    }
                )
        return {"index": index, "index_path": str(mem_dir / "MEMORY.md"), "dir": str(mem_dir), "files": files}

    # -- tasks -------------------------------------------------------------- #

    def get_tasks(self, session_id: str) -> list[dict]:
        tdir = paths.tasks_dir(session_id)
        if not tdir.is_dir():
            return []
        tasks: list[dict] = []
        for f in tdir.glob("*.json"):
            try:
                tasks.append(json.loads(f.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        tasks.sort(key=lambda t: str(t.get("id", "")))
        return tasks

    # -- workspace (scratchpad + background task outputs) ------------------- #

    def get_scratchpad(self, project_path: str, session_id: str) -> dict:
        base = paths.session_tmp_dir(project_path, session_id)
        result = {"dir": str(base) if base else "", "exists": bool(base), "files": []}
        if not base:
            return result
        files = []
        for f in base.rglob("*"):
            if f.is_dir() or "__pycache__" in f.parts:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": str(f.relative_to(base)),
                    "path": str(f),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ext": f.suffix.lstrip("."),
                }
            )
        files.sort(key=lambda x: x["mtime"], reverse=True)
        result["files"] = files
        return result

    # -- images ------------------------------------------------------------- #

    def get_images(self, session_id: str) -> list[dict]:
        d = paths.image_cache_dir(session_id)
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.iterdir()):
            if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"name": f.name, "path": str(f), "size": st.st_size, "mtime": st.st_mtime})
        return out

    # -- file history ------------------------------------------------------- #

    def get_file_history(self, session_id: str) -> dict:
        d = paths.file_history_dir(session_id)
        count = 0
        total = 0
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    count += 1
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return {"dir": str(d), "count": count, "bytes": total}

    # -- shells & environments (monitor) ------------------------------------ #

    def get_shells(self) -> dict:
        home = paths.claude_home()
        snaps = []
        sdir = paths.shell_snapshots_dir()
        if sdir.is_dir():
            for f in sdir.iterdir():
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                snaps.append({"name": f.name, "path": str(f), "size": st.st_size, "mtime": st.st_mtime})
            snaps.sort(key=lambda x: x["mtime"], reverse=True)
        envs = []
        edir = home / "session-env"
        if edir.is_dir():
            for d in edir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    mtime = d.stat().st_mtime
                except OSError:
                    mtime = 0.0
                envs.append({"session_id": d.name, "path": str(d), "mtime": mtime})
            envs.sort(key=lambda x: x["mtime"], reverse=True)
        return {"snapshots": snaps[:20], "envs": envs[:20]}

    # -- global stats (all sessions, all projects) --------------------------- #

    def global_stats(self) -> dict:
        """Aggregate analytics across every session on the machine.

        Reads only cached summaries (cheap after the first scan)."""
        root = paths.projects_dir()
        totals = pricing.Usage()
        by_model: dict[str, pricing.Usage] = {}
        tools: Counter[str] = Counter()
        days: Counter[str] = Counter()
        cost = 0.0
        sessions = active = prompts = turns = tool_calls = subagent_sessions = 0
        now = time.time()
        if root.is_dir():
            for pdir in root.iterdir():
                if not pdir.is_dir():
                    continue
                for f in pdir.glob("*.jsonl"):
                    s = self._summary(f)
                    sessions += 1
                    cost += s.cost
                    prompts += s.user_messages
                    turns += s.assistant_messages
                    tool_calls += s.tool_calls
                    if s.has_subagents:
                        subagent_sessions += 1
                    if now - s.mtime <= ACTIVE_WINDOW_SECONDS:
                        active += 1
                    u = s.usage or {}
                    totals.add(pricing.Usage(
                        input=u.get("input", 0), output=u.get("output", 0),
                        cache_read=u.get("cache_read", 0), cache_write=u.get("cache_write", 0)))
                    for m, ud in (s.usage_by_model or {}).items():
                        if m in ("unknown", "<synthetic>"):
                            continue
                        bm = by_model.setdefault(m, pricing.Usage())
                        bm.add(pricing.Usage(
                            input=ud.get("input", 0), output=ud.get("output", 0),
                            cache_read=ud.get("cache_read", 0), cache_write=ud.get("cache_write", 0)))
                    for name, c in (s.tool_counts or {}).items():
                        tools[name] += c
                    if s.mtime:
                        days[time.strftime("%Y-%m-%d", time.localtime(s.mtime))] += 1
        self._save_cache()
        # last 14 calendar days, zero-filled
        by_day = []
        for i in range(13, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            by_day.append([d, days.get(d, 0)])
        usage_total = asdict(totals)
        usage_total["total"] = totals.total
        return {
            "cost": round(cost, 4),
            "usage": usage_total,
            "sessions": sessions,
            "active": active,
            "prompts": prompts,
            "turns": turns,
            "tool_calls": tool_calls,
            "subagent_sessions": subagent_sessions,
            "by_model": {
                m: {**asdict(u), "total": u.total, "cost": round(pricing.cost_for(u, m), 4)}
                for m, u in by_model.items()
            },
            "tool_counts": dict(tools.most_common(14)),
            "sessions_by_day": by_day,
        }

    # -- global search ------------------------------------------------------- #

    def search_all(self, query: str) -> dict:
        q = query.lower().strip()
        if not q:
            return {"sessions": [], "prompts": []}
        sessions = []
        for key, s in self._cache.items():
            hay = " ".join((s.get("title", ""), s.get("first_prompt", ""), s.get("cwd", ""), s.get("session_id", ""))).lower()
            if q in hay:
                p = Path(key)
                sessions.append(
                    {
                        "project_id": p.parent.name,
                        "project_name": Path(s.get("cwd", "")).name or p.parent.name,
                        "session_id": s.get("session_id", ""),
                        "title": s.get("title") or s.get("first_prompt", ""),
                        "cost": s.get("cost", 0),
                        "mtime": s.get("mtime", 0),
                    }
                )
        sessions.sort(key=lambda x: x["mtime"], reverse=True)

        prompts = []
        hist = paths.claude_home() / "history.jsonl"
        if hist.is_file():
            try:
                for line in hist.read_text("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    display = rec.get("display", "")
                    if q in display.lower():
                        project = rec.get("project", "")
                        prompts.append(
                            {
                                "display": display[:220],
                                "project": project,
                                "project_name": Path(project).name if project else "",
                                "project_id": paths.encode_project_path(project) if project else "",
                                "session_id": rec.get("sessionId", ""),
                                "timestamp": rec.get("timestamp", 0),
                            }
                        )
            except OSError:
                pass
        prompts.reverse()  # newest first
        return {"sessions": sessions[:50], "prompts": prompts[:50]}

    # -- settings & statusline --------------------------------------------- #

    def get_settings(self) -> dict:
        merged: dict = {}
        raw: list[dict] = []
        for f in paths.settings_files():
            if f.is_file():
                try:
                    data = json.loads(f.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                merged.update(data)
                raw.append({"name": f.name, "path": str(f), "data": data})
        statusline = merged.get("statusLine", {})
        sl_script = None
        cmd = statusline.get("command", "") if isinstance(statusline, dict) else ""
        if isinstance(cmd, str) and cmd:
            token = cmd.replace("bash ", "").replace("~", str(Path.home())).split()[0] if cmd.split() else ""
            sp = Path(token).expanduser() if token else None
            if sp and sp.is_file():
                sl_script = {"path": str(sp), "content": sp.read_text("utf-8", errors="replace")}
        return {
            "merged": merged,
            "files": raw,
            "home": str(paths.claude_home()),
            "statusline_script": sl_script,
            "live": self.get_statusline_live(),
        }

    def get_statusline_live(self) -> dict | None:
        f = paths.statusline_capture_file()
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text("utf-8"))
            data["_captured_mtime"] = f.stat().st_mtime
            return data
        except (OSError, json.JSONDecodeError):
            return None


def _dir_size(d: Path) -> int:
    """Total size of files under ``d`` (bounded recursion), 0 if missing."""
    total = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                    elif e.is_dir(follow_symlinks=False):
                        total += _dir_size(Path(e.path))
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _parse_frontmatter(text: str) -> dict:
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key in ("name", "description", "type", "node_type") and val:
                meta[key.replace("node_type", "type")] = val
    return meta
