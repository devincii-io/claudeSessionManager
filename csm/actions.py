"""Mutating operations and OS integrations: delete sessions/memory, save memory,
open paths in an editor or file manager, and manage the optional statusline
capture hook. All destructive operations are confirmed by the UI before calling.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from urllib.parse import quote
from pathlib import Path

from . import paths

_STATUSLINE_MARKER = "# >>> claude-session-manager capture >>>"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text via same-directory replace to avoid partial configs."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, "utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _backup_existing(path: Path) -> str | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.csm-backup-{stamp}")
    shutil.copy2(path, backup)
    return str(backup)


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
    try:
        if jsonl.is_file() and time.time() - jsonl.stat().st_mtime <= 600:
            return {"ok": False, "error": "Session had activity in the last 10 minutes; deletion is temporarily blocked"}
    except OSError:
        pass
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
            home / "uploads" / session_id,
            home / "session-env" / session_id,
        ):
            if d.is_dir() and _under(d, home):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(str(d))

    return {"ok": bool(removed), "removed": removed}


def delete_sessions(items: list, purge: bool = False) -> dict:
    """Bulk-delete a list of ``{"project_id", "session_id"}`` records.

    Each is deleted with :func:`delete_session` (path-guarded). Returns an
    aggregate so the UI can report "deleted N of M" in one confirmation.
    """
    results: list[dict] = []
    freed = 0
    for it in items or []:
        pid = (it or {}).get("project_id")
        sid = (it or {}).get("session_id")
        if not pid or not sid:
            continue
        r = delete_session(pid, sid, purge)
        results.append({"project_id": pid, "session_id": sid, **r})
    deleted = sum(1 for r in results if r.get("ok"))
    return {"ok": deleted > 0, "deleted": deleted, "count": len(results), "results": results}


def delete_inventory_path(path: str, allowed_paths: set[str]) -> dict:
    """Delete one backend-inventoried asset group, never an arbitrary path."""
    target = Path(path)
    try:
        resolved = str(target.resolve())
        allowed = {str(Path(item).resolve()) for item in allowed_paths}
    except OSError:
        return {"ok": False, "error": "Invalid asset path"}
    if resolved not in allowed:
        return {"ok": False, "error": "Asset is no longer in the cleanup inventory"}
    if not target.is_dir():
        return {"ok": False, "error": "Asset group no longer exists"}
    try:
        newest = max(
            (item.stat().st_mtime for item in target.rglob("*") if item.is_file()),
            default=target.stat().st_mtime,
        )
        if time.time() - newest <= 600:
            return {"ok": False, "error": "Asset group changed in the last 10 minutes"}
        shutil.rmtree(target)
        return {"ok": True, "removed": [str(target)]}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


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
    if not p.is_file():
        return {"ok": False, "error": "refused"}
    try:
        resolved = p.resolve()
        for root in paths.temp_roots():
            try:
                rel = resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            if len(rel.parts) >= 5 and rel.parts[0].startswith("claude-") and rel.parts[3] == "scratchpad":
                p.unlink()
                return {"ok": True, "removed": str(p)}
    except OSError:
        pass
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


def _cli_binary(provider: str) -> str | None:
    """Resolve a CLI, honoring an explicit Codex path before PATH.

    Microsoft Store aliases can be discoverable yet inaccessible to ordinary
    desktop processes.  Codex candidates are therefore probed once per action;
    the documented ``CODEX_CLI_PATH`` escape hatch supports custom installs.
    """
    command = "claude" if provider == "claude" else "codex"
    candidates = []
    if provider == "codex" and os.environ.get("CODEX_CLI_PATH"):
        candidates.append(os.environ["CODEX_CLI_PATH"])
    found = shutil.which(command)
    if found:
        candidates.append(found)
    for candidate in candidates:
        if provider == "claude":
            return candidate
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True, timeout=5, check=False)
            if probe.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def launch_session(provider: str, project_path: str, session_id: str = "", mode: str = "resume") -> dict:
    """Open Claude Code or Codex in a real terminal.

    ``mode`` is currently ``resume`` or ``fork``.  A blank session id always
    starts a new session.  Keeping terminal launch here (instead of in the web
    UI) gives every platform the same validation and quoting rules.
    """
    if provider not in {"claude", "codex"}:
        return {"ok": False, "error": "Unknown agent"}
    cwd = Path(project_path).expanduser()
    if not cwd.is_dir():
        return {"ok": False, "error": "Project folder no longer exists"}
    if session_id and not all(ch.isalnum() or ch in "-_" for ch in session_id):
        return {"ok": False, "error": "Invalid session id"}

    command = "claude" if provider == "claude" else "codex"
    binary = _cli_binary(provider)
    if not binary:
        # The Windows Codex desktop app documents deep links for new and
        # existing local chats.  They keep quick launch useful even when the
        # Store CLI alias is inaccessible to an unpackaged process.
        if provider == "codex" and platform.system() == "Windows" and mode != "fork":
            uri = f"codex://threads/{session_id}" if session_id else f"codex://new?path={quote(str(cwd))}"
            try:
                os.startfile(uri)  # type: ignore[attr-defined]
                return {"ok": True, "provider": "codex", "mode": "resume" if session_id else "new", "target": "desktop"}
            except OSError as exc:
                return {"ok": False, "error": f"Codex CLI unavailable and desktop deep link failed: {exc}"}
        suffix = "; set CODEX_CLI_PATH to a runnable binary" if provider == "codex" else ""
        return {"ok": False, "error": f"The '{command}' CLI was not found or runnable{suffix}"}
    if provider == "claude":
        if mode == "fork":
            return {"ok": False, "error": "Fork is only available for Codex sessions"}
        args = ["--resume", session_id] if session_id else []
    else:
        subcommand = "fork" if mode == "fork" else "resume"
        args = [subcommand, session_id] if session_id else []

    try:
        system = platform.system()
        if system == "Windows":
            # Claude is commonly a .cmd shim. cmd.exe supports that while the
            # new console keeps the interactive process independent of the app.
            subprocess.Popen(
                ["cmd.exe", "/k", binary, *args],
                cwd=str(cwd),
                creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
            )
            return {"ok": True, "provider": provider, "mode": mode if session_id else "new"}

        if system == "Darwin":
            command = f"cd {shlex.quote(str(cwd))} && exec {shlex.join([binary, *args])}"
            script = f'tell application "Terminal" to do script {json.dumps(command)}'
            subprocess.Popen(["osascript", "-e", script])
            return {"ok": True, "provider": provider, "mode": mode if session_id else "new"}

        terminal_specs = (
            ("gnome-terminal", ["--working-directory", str(cwd), "--", binary, *args]),
            ("konsole", ["--workdir", str(cwd), "-e", binary, *args]),
            ("xfce4-terminal", ["--working-directory", str(cwd), "-x", binary, *args]),
            ("x-terminal-emulator", ["-e", binary, *args]),
        )
        for terminal, terminal_args in terminal_specs:
            found = shutil.which(terminal)
            if found:
                subprocess.Popen([found, *terminal_args], cwd=str(cwd))
                return {"ok": True, "provider": provider, "mode": mode if session_id else "new"}
        return {"ok": False, "error": "No supported terminal emulator was found"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def launch_claude(project_path: str, session_id: str = "") -> dict:
    """Backward-compatible Claude launcher used by older callers/tests."""
    return launch_session("claude", project_path, session_id)


def launch_wsl_session(
    distro: str,
    provider: str,
    linux_cwd: str,
    session_id: str = "",
    mode: str = "resume",
) -> dict:
    """Launch an agent inside an already-validated WSL distribution.

    Arguments are passed as an argv array; neither distro, path nor session id
    is interpolated through a shell.
    """
    if platform.system() != "Windows" or provider not in {"claude", "codex"}:
        return {"ok": False, "error": "WSL launch is only available on Windows"}
    if not distro or not linux_cwd.startswith("/"):
        return {"ok": False, "error": "Invalid WSL source or project path"}
    if session_id and not all(ch.isalnum() or ch in "-_" for ch in session_id):
        return {"ok": False, "error": "Invalid session id"}
    if provider == "claude":
        if mode == "fork":
            return {"ok": False, "error": "Fork is only available for Codex sessions"}
        agent_args = ["claude", "--resume", session_id] if session_id else ["claude"]
    else:
        subcommand = "fork" if mode == "fork" else "resume"
        agent_args = ["codex", subcommand, session_id] if session_id else ["codex"]
    wsl_args = ["wsl.exe", "-d", distro, "--cd", linux_cwd, "--", *agent_args]
    try:
        terminal = shutil.which("wt.exe") or shutil.which("wt")
        if terminal:
            subprocess.Popen([terminal, "-w", "0", "new-tab", "--", *wsl_args])
            target = "windows-terminal"
        else:
            subprocess.Popen(wsl_args, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
            target = "console"
        return {"ok": True, "provider": provider, "mode": mode if session_id else "new", "target": target}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def archive_wsl_codex_session(distro: str, session_id: str) -> dict:
    if platform.system() != "Windows" or not distro:
        return {"ok": False, "error": "Invalid WSL source"}
    if not session_id or not all(ch.isalnum() or ch in "-_" for ch in session_id):
        return {"ok": False, "error": "Invalid session id"}
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "codex", "archive", session_id],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode:
        return {"ok": False, "error": (result.stderr or result.stdout or "Codex archive failed").strip()[-1000:]}
    return {"ok": True, "session_id": session_id, "archived": True}


def archive_codex_session(session_id: str) -> dict:
    """Ask the Codex CLI to archive a session without mutating its indexes.

    The CLI owns the archive format and any database bookkeeping.  This is the
    normal Codex cleanup action; raw JSONL deletion is deliberately not used.
    """
    if not session_id or not all(ch.isalnum() or ch in "-_" for ch in session_id):
        return {"ok": False, "error": "Invalid session id"}
    binary = _cli_binary("codex")
    if not binary:
        return {"ok": False, "error": "The 'codex' CLI was not found or runnable; set CODEX_CLI_PATH"}
    try:
        result = subprocess.run(
            [binary, "archive", session_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode:
        message = (result.stderr or result.stdout or "Codex could not archive the session").strip()
        return {"ok": False, "error": message[-1000:]}
    return {"ok": True, "session_id": session_id, "archived": True}


def list_codex_config_files(project_path: str = "") -> list[dict]:
    """Return only safe, user-editable Codex instruction/config files."""
    candidates: list[tuple[Path, str]] = [
        (paths.codex_home() / "config.toml", "Codex home"),
        (paths.codex_home() / "AGENTS.md", "Codex home"),
    ]
    base = Path(project_path).expanduser() if project_path else None
    if base and base.is_dir():
        candidates.append((base / "AGENTS.md", "Project"))
    out: list[dict] = []
    for path, group in candidates:
        try:
            st = path.stat()
            out.append({"path": str(path), "name": path.name, "group": group, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            out.append({"path": str(path), "name": path.name, "group": group, "size": 0, "mtime": 0, "missing": True})
    return out


def write_codex_file(path: str, content: str, project_path: str = "") -> dict:
    """Atomically write config.toml or AGENTS.md at an allowed Codex scope."""
    target = Path(path).expanduser()
    allowed = {paths.codex_home() / "config.toml", paths.codex_home() / "AGENTS.md"}
    base = Path(project_path).expanduser() if project_path else None
    if base and base.is_dir():
        allowed.add(base / "AGENTS.md")
    try:
        resolved = target.resolve()
        allowed_resolved = {p.resolve() for p in allowed}
    except OSError:
        return {"ok": False, "error": "Invalid path"}
    if resolved not in allowed_resolved:
        return {"ok": False, "error": "Refused: not an editable Codex config file"}
    if resolved.name == "config.toml":
        try:
            import tomllib
            tomllib.loads(content)
        except ModuleNotFoundError:
            pass  # Python 3.10: keep compatibility without adding a dependency.
        except Exception as exc:
            return {"ok": False, "error": f"Invalid config.toml: {exc}"}
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup_existing(resolved)
        _atomic_write_text(resolved, content)
        return {"ok": True, "path": str(resolved), "backup": backup}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _settings_set(data: dict, key: str, value) -> None:
    """Apply one dotted ``key`` = ``value`` to ``data`` in place. ``value`` of
    ``None`` deletes the key and prunes any parent objects it leaves empty, so
    the file never accumulates dead ``"env": {}`` husks."""
    parts = key.split(".")
    chain: list[tuple[dict, str]] = []
    node = data
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            if value is None:
                return  # nothing to delete along a non-existent path
            nxt = {}
            node[p] = nxt
        chain.append((node, p))
        node = nxt
    if value is None:
        node.pop(parts[-1], None)
        # Walk back up, dropping now-empty parent objects.
        for parent, pkey in reversed(chain):
            if isinstance(parent.get(pkey), dict) and not parent[pkey]:
                parent.pop(pkey, None)
            else:
                break
    else:
        node[parts[-1]] = value


def _read_settings() -> dict:
    f = paths.claude_home() / "settings.json"
    try:
        return json.loads(f.read_text("utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        return {}


def _write_settings(data: dict) -> dict:
    f = paths.claude_home() / "settings.json"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(f, json.dumps(data, indent=2) + "\n")
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def update_setting(key: str, value) -> dict:
    """Set (or, when ``value`` is ``None``, delete) a dotted key in the base
    ``settings.json`` file."""
    data = _read_settings()
    _settings_set(data, key, value)
    r = _write_settings(data)
    return {**r, "key": key, "value": value} if r.get("ok") else r


def update_settings(items: list) -> dict:
    """Apply many ``{"key", "value"}`` edits in a single read-modify-write.
    A ``value`` of ``None`` (or absent) deletes that key. Powers one-click
    presets like the privacy defaults without rewriting the file per key."""
    data = _read_settings()
    applied = 0
    for it in items or []:
        key = (it or {}).get("key")
        if not key:
            continue
        _settings_set(data, key, it.get("value"))
        applied += 1
    r = _write_settings(data)
    return {**r, "applied": applied} if r.get("ok") else r


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


def _guidance_path(scope: str, project_path: str) -> Path | None:
    if scope == "global":
        return paths.claude_home() / "CLAUDE.md"
    base = Path(project_path).expanduser() if project_path else None
    return (base / "CLAUDE.md") if base else None


def read_guidance(scope: str, project_path: str = "") -> dict:
    """Read a CLAUDE.md — ``scope`` is ``global`` (~/.claude) or ``project``."""
    p = _guidance_path(scope, project_path)
    if p is None:
        return {"ok": False, "error": "no project path"}
    if p.is_file():
        return {"ok": True, "path": str(p), "exists": True,
                "content": p.read_text("utf-8", errors="replace")}
    return {"ok": True, "path": str(p), "exists": False, "content": ""}


def write_guidance(scope: str, content: str, project_path: str = "") -> dict:
    """Write a CLAUDE.md. Global goes to ~/.claude; project writes the file named
    exactly ``CLAUDE.md`` in the project's own directory (which must exist)."""
    if scope == "project":
        base = Path(project_path).expanduser() if project_path else None
        if not base or not base.is_dir():
            return {"ok": False, "error": "project directory not found"}
    p = _guidance_path(scope, project_path)
    if p is None:
        return {"ok": False, "error": "no target"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup_existing(p)
        _atomic_write_text(p, content)
        return {"ok": True, "path": str(p), "backup": backup}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def write_memory_notes(project_id: str, notes: list) -> dict:
    """Write consolidated memory notes into a project's memory store and append
    pointers to its MEMORY.md index. Guarded to the projects dir."""
    mem_dir = paths.projects_dir() / project_id / "memory"
    if not _under(mem_dir, paths.projects_dir()):
        return {"ok": False, "error": "refused"}
    mem_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    backups: list[str] = []
    index_lines: list[str] = []
    for n in notes or []:
        name = str(n.get("name") or "").strip()
        body = str(n.get("body") or "").strip()
        if not name or not body:
            continue
        fname = name if name.endswith(".md") else f"{name}.md"
        f = mem_dir / fname
        front = (
            "---\n"
            f"name: {name.removesuffix('.md')}\n"
            f"description: {n.get('description', '')}\n"
            "metadata:\n"
            f"  type: {n.get('type', 'project')}\n"
            "---\n\n"
        )
        try:
            backup = _backup_existing(f)
            if backup:
                backups.append(backup)
            _atomic_write_text(f, front + body + "\n")
            written.append(str(f))
            index_lines.append(f"- [{name.removesuffix('.md')}]({fname}) — {n.get('description', '')}")
        except OSError:
            continue
    # Append new pointers to MEMORY.md (create it if missing).
    if index_lines:
        idx = mem_dir / "MEMORY.md"
        try:
            existing = idx.read_text("utf-8", errors="replace") if idx.is_file() else "# Memory Index\n"
            if not existing.endswith("\n"):
                existing += "\n"
            additions = [line for line in index_lines if line.split(")", 1)[0] + ")" not in existing]
            if additions:
                backup = _backup_existing(idx)
                if backup:
                    backups.append(backup)
                _atomic_write_text(idx, existing + "\n".join(additions) + "\n")
        except OSError:
            pass
    return {"ok": bool(written), "written": written, "count": len(written), "backups": backups}


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
