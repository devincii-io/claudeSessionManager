"""Cross-platform resolution of Claude Code's on-disk locations.

Claude Code stores everything under a home directory (``~/.claude`` by default,
overridable with ``CLAUDE_CONFIG_DIR``) plus a per-session scratchpad tree under
the OS temp directory. This module locates those paths on both Linux and Windows
and maps between real project paths and Claude's encoded directory names.
"""

from __future__ import annotations

import glob
import os
import tempfile
from pathlib import Path


def claude_home() -> Path:
    """Return the Claude config directory (``~/.claude`` unless overridden)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def projects_dir() -> Path:
    return claude_home() / "projects"


def encode_project_path(project_path: str | Path) -> str:
    """Encode a filesystem path the way Claude names its ``projects/`` subdirs.

    Claude replaces every path separator (and other non-alphanumeric characters)
    with ``-`` — e.g. ``/home/alice/MyProject`` -> ``-home-alice-MyProject``.
    The transform is lossy, so prefer :func:`decode_from_session` when a real path
    is needed; this is only used to *locate* the scratchpad for a known project.
    """
    text = str(project_path)
    return "".join(c if c.isalnum() else "-" for c in text)


def temp_roots() -> list[Path]:
    """Candidate temp roots that may hold ``claude-*`` scratchpad trees."""
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in (
        os.environ.get("TMPDIR"),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        tempfile.gettempdir(),
        "/tmp",
    ):
        if not candidate:
            continue
        p = Path(candidate)
        key = str(p)
        if key not in seen and p.exists():
            seen.add(key)
            roots.append(p)
    return roots


def scratchpad_dir(project_path: str, session_id: str) -> Path | None:
    """Locate the scratchpad directory for a given project + session, if present.

    Searches ``{temp}/claude-*/{encoded-project}/{session}/scratchpad`` across all
    plausible temp roots (the ``claude-<uid>`` component varies by user/platform).
    """
    encoded = encode_project_path(project_path)
    for root in temp_roots():
        pattern = str(root / "claude-*" / encoded / session_id / "scratchpad")
        for match in glob.glob(pattern):
            path = Path(match)
            if path.is_dir():
                return path
    return None


def session_tmp_dir(project_path: str, session_id: str) -> Path | None:
    """Locate the per-session temp dir (holds scratchpad/ and tasks/ output)."""
    encoded = encode_project_path(project_path)
    for root in temp_roots():
        pattern = str(root / "claude-*" / encoded / session_id)
        for match in glob.glob(pattern):
            path = Path(match)
            if path.is_dir():
                return path
    return None


def tasks_dir(session_id: str) -> Path:
    return claude_home() / "tasks" / session_id


def file_history_dir(session_id: str) -> Path:
    return claude_home() / "file-history" / session_id


def image_cache_dir(session_id: str) -> Path:
    return claude_home() / "image-cache" / session_id


def shell_snapshots_dir() -> Path:
    return claude_home() / "shell-snapshots"


def settings_files() -> list[Path]:
    """User-level settings files, in precedence order (base first)."""
    home = claude_home()
    return [home / "settings.json", home / "settings.local.json"]


def statusline_capture_file() -> Path:
    """Where the optional statusline hook writes the latest live payload."""
    return claude_home() / ".csm-statusline.json"


def cache_dir() -> Path:
    """Per-user cache directory for parsed-session summaries."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        d = base / "ClaudeSessionManager" / "cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        d = base / "claude-session-manager"
    d.mkdir(parents=True, exist_ok=True)
    return d
