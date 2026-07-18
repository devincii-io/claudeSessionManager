"""Enumerate Claude Code's on-disk state: projects, sessions, memory, tasks,
scratchpads, settings and the optional live statusline capture.

Session summaries are the expensive part (a full pass over each ``.jsonl``), so
they are cached on disk keyed by file mtime+size and only recomputed when a file
changes. Everything else is cheap directory reads.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths, pricing
from .session_parser import SessionSummary, summarize

ACTIVE_WINDOW_SECONDS = 120  # a session whose jsonl changed this recently is "live"
CACHE_VERSION = 2  # bump when SessionSummary's schema or computation changes


class Scanner:
    def __init__(self) -> None:
        self._cache_path = paths.cache_dir() / "summaries.json"
        self._cache: dict[str, dict] = {}
        self._load_cache()

    # -- summary cache ------------------------------------------------------ #

    def _load_cache(self) -> None:
        try:
            blob = json.loads(self._cache_path.read_text("utf-8"))
            if blob.get("_v") == CACHE_VERSION:
                self._cache = blob.get("entries", {})
            else:
                self._cache = {}
        except (OSError, json.JSONDecodeError, AttributeError):
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.write_text(json.dumps({"_v": CACHE_VERSION, "entries": self._cache}), "utf-8")
        except OSError:
            pass

    def _summary(self, jsonl: Path) -> SessionSummary:
        try:
            st = jsonl.stat()
        except OSError:
            return SessionSummary(session_id=jsonl.stem, path=str(jsonl))
        key = str(jsonl)
        cached = self._cache.get(key)
        if cached and cached.get("size_bytes") == st.st_size and cached.get("mtime") == st.st_mtime:
            return SessionSummary(**cached)
        summary = summarize(jsonl, size=st.st_size, mtime=st.st_mtime)
        self._cache[key] = summary.to_dict()
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

    def project_path(self, project_id: str) -> str:
        pdir = paths.projects_dir() / project_id
        for f in pdir.glob("*.jsonl"):
            s = self._summary(f)
            if s.cwd:
                return s.cwd
        return ""

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

    # -- scratchpad --------------------------------------------------------- #

    def get_scratchpad(self, project_path: str, session_id: str) -> dict:
        sp = paths.scratchpad_dir(project_path, session_id)
        result = {"dir": str(sp) if sp else "", "exists": bool(sp), "files": []}
        if not sp:
            return result
        files = []
        for f in sorted(sp.rglob("*")):
            if f.is_dir() or "__pycache__" in f.parts:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": str(f.relative_to(sp)),
                    "path": str(f),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ext": f.suffix.lstrip("."),
                }
            )
        files.sort(key=lambda x: x["mtime"], reverse=True)
        result["files"] = files
        return result

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
        # Statusline command script contents, if referenced.
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


def _parse_frontmatter(text: str) -> dict:
    """Light parse of the YAML-ish frontmatter used by memory files."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    lines = text.splitlines()
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key in ("name", "description", "type", "node_type") and val:
                meta[key.replace("node_type", "type")] = val
    return meta
