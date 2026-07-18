"""Mutating operations and OS integrations: delete sessions/memory, save memory,
open paths in an editor or file manager, and manage the optional statusline
capture hook. All destructive operations are confirmed by the UI before calling.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import paths

_STATUSLINE_MARKER = "# >>> claude-session-manager capture >>>"


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Delete / write                                                              #
# --------------------------------------------------------------------------- #


def delete_session(project_id: str, session_id: str, purge: bool = False) -> dict:
    """Delete a session transcript (and its ancillary data when ``purge``).

    Only paths under the Claude home are touched.
    """
    home = paths.claude_home()
    pdir = paths.projects_dir() / project_id
    removed: list[str] = []

    jsonl = pdir / f"{session_id}.jsonl"
    if jsonl.is_file() and _under(jsonl, home):
        jsonl.unlink()
        removed.append(str(jsonl))

    subdir = pdir / session_id
    if subdir.is_dir() and _under(subdir, home):
        shutil.rmtree(subdir, ignore_errors=True)
        removed.append(str(subdir))

    if purge:
        for d in (
            paths.tasks_dir(session_id),
            paths.file_history_dir(session_id),
            paths.image_cache_dir(session_id),
            home / "session-env" / session_id,
        ):
            if d.is_dir() and _under(d, home):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(str(d))

    return {"ok": bool(removed), "removed": removed}


def delete_memory(path: str) -> dict:
    p = Path(path)
    if p.is_file() and _under(p, paths.projects_dir()) and p.suffix == ".md":
        p.unlink()
        return {"ok": True, "removed": str(p)}
    return {"ok": False, "error": "refused: not a memory file"}


def save_memory(path: str, content: str) -> dict:
    p = Path(path)
    if not _under(p, paths.projects_dir()) or p.suffix != ".md":
        return {"ok": False, "error": "refused: not a memory file"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    return {"ok": True, "path": str(p)}


def delete_scratchpad_file(path: str) -> dict:
    p = Path(path)
    # Scratchpads live under a temp dir; only allow deletion inside a scratchpad tree.
    if p.is_file() and "scratchpad" in p.parts:
        p.unlink()
        return {"ok": True, "removed": str(p)}
    return {"ok": False, "error": "refused"}


# --------------------------------------------------------------------------- #
# OS integrations                                                             #
# --------------------------------------------------------------------------- #


def open_in_editor(path: str) -> dict:
    p = Path(path)
    for exe in ("code", "code-insiders", "codium"):
        found = shutil.which(exe)
        if found:
            try:
                subprocess.Popen([found, str(p)])
                return {"ok": True, "editor": exe}
            except OSError:
                continue
    return open_path(str(p if p.is_dir() else p.parent))


def open_path(path: str) -> dict:
    p = Path(path)
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            # Prefer explorer.exe under WSL so paths open on the Windows host.
            if shutil.which("explorer.exe") and str(p).startswith("/mnt/"):
                subprocess.Popen(["explorer.exe", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def update_setting(key: str, value) -> dict:
    """Set a (possibly dotted) key in the base ``settings.json`` file."""
    import json

    f = paths.claude_home() / "settings.json"
    try:
        data = json.loads(f.read_text("utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        data = {}
    parts = key.split(".")
    node = data
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value
    try:
        f.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return {"ok": True, "key": key, "value": value}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


CONFIG_EXTS = {".md", ".json", ".sh", ".js", ".ts", ".py", ".toml", ".txt"}
CONFIG_TOP = ["settings.json", "settings.local.json", "statusline-command.sh", "CLAUDE.md", ".mcp.json", "keybindings.json"]


def list_config_files() -> list[dict]:
    """Small, hand-editable config files under the Claude home."""
    home = paths.claude_home()
    out: list[dict] = []

    def add(p: Path, group: str) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        out.append({
            "name": str(p.relative_to(home)),
            "path": str(p),
            "group": group,
            "ext": p.suffix.lstrip("."),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })

    for rel in CONFIG_TOP:
        p = home / rel
        if p.is_file():
            add(p, "root")
    for sub in ("commands", "agents"):
        d = home / sub
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix in CONFIG_EXTS:
                    add(p, sub)
    return out


def write_claude_file(path: str, content: str) -> dict:
    """Write any small config file that lives under the Claude home."""
    p = Path(path)
    if not _under(p, paths.claude_home()):
        return {"ok": False, "error": "refused: outside Claude home"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return {"ok": True, "path": str(p)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def read_text_file(path: str, limit: int = 400_000) -> dict:
    p = Path(path)
    try:
        data = p.read_text("utf-8", errors="replace")
        return {"ok": True, "content": data[:limit], "truncated": len(data) > limit, "ext": p.suffix.lstrip(".")}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Statusline capture hook                                                     #
# --------------------------------------------------------------------------- #


def statusline_capture_status() -> dict:
    settings = _load_settings()
    script = _statusline_script_path(settings)
    installed = False
    if script and script.is_file():
        installed = _STATUSLINE_MARKER in script.read_text("utf-8", errors="replace")
    return {
        "script": str(script) if script else None,
        "installed": installed,
        "capture_file": str(paths.statusline_capture_file()),
    }


def install_statusline_capture() -> dict:
    settings = _load_settings()
    script = _statusline_script_path(settings)
    if not script or not script.is_file():
        return {"ok": False, "error": "no statusline command script found"}
    text = script.read_text("utf-8", errors="replace")
    if _STATUSLINE_MARKER in text:
        return {"ok": True, "already": True}
    capture = paths.statusline_capture_file()
    snippet = (
        f'\n{_STATUSLINE_MARKER}\n'
        f'printf \'%s\' "$input" > "{capture}" 2>/dev/null\n'
        f"# <<< claude-session-manager capture <<<\n"
    )
    # Insert right after the line that reads stdin into $input.
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and "input=$(cat)" in line:
            out.append(snippet)
            inserted = True
    if not inserted:
        out.append(snippet)
    script.write_text("".join(out), "utf-8")
    return {"ok": True, "installed": True}


def uninstall_statusline_capture() -> dict:
    settings = _load_settings()
    script = _statusline_script_path(settings)
    if not script or not script.is_file():
        return {"ok": False, "error": "no script"}
    text = script.read_text("utf-8", errors="replace")
    if _STATUSLINE_MARKER not in text:
        return {"ok": True, "already": True}
    lines = text.splitlines(keepends=True)
    out, skip = [], False
    for line in lines:
        if _STATUSLINE_MARKER in line:
            skip = True
            continue
        if "<<< claude-session-manager capture <<<" in line:
            skip = False
            continue
        if not skip:
            out.append(line)
    script.write_text("".join(out), "utf-8")
    return {"ok": True, "removed": True}


def _load_settings() -> dict:
    import json

    merged: dict = {}
    for f in paths.settings_files():
        if f.is_file():
            try:
                merged.update(json.loads(f.read_text("utf-8")))
            except (OSError, ValueError):
                pass
    return merged


def _statusline_script_path(settings: dict) -> Path | None:
    sl = settings.get("statusLine", {})
    cmd = sl.get("command", "") if isinstance(sl, dict) else ""
    if not isinstance(cmd, str) or not cmd:
        return None
    parts = cmd.split()
    token = next((p for p in parts if p not in ("bash", "sh", "zsh")), "")
    if not token:
        return None
    return Path(token.replace("~", str(Path.home()))).expanduser()
