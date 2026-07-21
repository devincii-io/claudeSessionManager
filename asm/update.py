"""Checksum-verified GitHub release updates for installed Windows builds.

The updater accepts only the pinned repository's exact Setup filename and a
matching SHA256SUMS.txt entry. Downloads stay on GitHub-owned HTTPS hosts and
run on a bridge worker thread so the desktop UI never blocks on network I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__

REPO = "devincii-io/agent-session-manager"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
SUMS_NAME = "SHA256SUMS.txt"
CACHE_TTL_SECONDS = 6 * 3600
MAX_INSTALLER_BYTES = 350 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

_cache: dict | None = None
_cache_at = 0.0


def _validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"refusing non-GitHub https URL: {url}")


def _get(url: str, timeout: int = 15, max_bytes: int = MAX_METADATA_BYTES) -> bytes:
    _validate_download_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"agent-session-manager/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        _validate_download_url(response.geturl())
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"download exceeds {max_bytes} bytes")
        return data


def _version_tuple(value: str) -> tuple[int, ...]:
    result = []
    for piece in value.strip().lstrip("v").split("."):
        match = re.match(r"\d+", piece)
        result.append(int(match.group()) if match else 0)
    return tuple(result) or (0,)


def is_newer(latest: str, current: str) -> bool:
    left, right = _version_tuple(latest), _version_tuple(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _release() -> dict:
    payload = json.loads(_get(API_LATEST))
    if not isinstance(payload, dict):
        raise ValueError("invalid GitHub release response")
    return payload


def _assets_for(payload: dict) -> tuple[str, dict | None, dict | None]:
    latest = str(payload.get("tag_name") or "").lstrip("v")
    assets = payload.get("assets") or []
    expected_setup = f"AgentSessionManager-v{latest}-Setup.exe" if latest else ""
    setup = next((item for item in assets if str(item.get("name") or "") == expected_setup), None)
    sums = next((item for item in assets if str(item.get("name") or "") == SUMS_NAME), None)
    return latest, setup, sums


def check(force: bool = False) -> dict:
    global _cache, _cache_at
    if not force and _cache is not None and time.monotonic() - _cache_at < CACHE_TTL_SECONDS:
        return _cache
    payload = _release()
    latest, setup, sums = _assets_for(payload)
    result = {
        "current": __version__,
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest, __version__),
        "installable": os.name == "nt" and setup is not None and sums is not None,
        "url": payload.get("html_url") or f"https://github.com/{REPO}/releases/latest",
        "notes": str(payload.get("body") or "")[:3000],
    }
    _cache, _cache_at = result, time.monotonic()
    return result


def _expected_hash(sums_text: str, filename: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].strip("*") == filename:
            return parts[0]
    return None


def download_and_run() -> dict:
    if os.name != "nt":
        raise ValueError("in-app installation is only available on Windows")
    payload = _release()
    latest, setup, sums = _assets_for(payload)
    if not latest or not is_newer(latest, __version__):
        raise ValueError("latest release is not newer than this installation")
    expected_name = f"AgentSessionManager-v{latest}-Setup.exe"
    if setup is None:
        raise ValueError(f"latest release has no matching {expected_name} asset")
    if sums is None:
        raise ValueError(f"latest release has no {SUMS_NAME}; refusing unverified install")
    try:
        declared_size = int(setup.get("size", 0))
    except (TypeError, ValueError):
        raise ValueError("installer asset has an invalid size") from None
    if declared_size < 1 or declared_size > MAX_INSTALLER_BYTES:
        raise ValueError("installer asset is implausibly large")

    sums_text = _get(str(sums.get("browser_download_url") or "")).decode("utf-8", "replace")
    wanted = _expected_hash(sums_text, expected_name)
    if wanted is None or not re.fullmatch(r"[0-9a-fA-F]{64}", wanted):
        raise ValueError("release checksums have no valid installer entry")
    blob = _get(
        str(setup.get("browser_download_url") or ""),
        timeout=180,
        max_bytes=MAX_INSTALLER_BYTES,
    )
    if len(blob) != declared_size:
        raise ValueError("installer size does not match GitHub metadata")
    digest = hashlib.sha256(blob).hexdigest()
    if wanted.lower() != digest:
        raise ValueError("installer failed checksum verification")

    update_dir = Path(tempfile.mkdtemp(prefix="agent-session-manager-update-"))
    installer = update_dir / expected_name
    installer.write_bytes(blob)
    subprocess.Popen([str(installer)], close_fds=True)
    return {"launched": True, "version": latest, "path": str(installer)}
