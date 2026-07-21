r"""Discover native and WSL agent-data sources without eagerly starting WSL.

Distribution names are cheap to enumerate. A distro's Linux home and UNC path
are resolved only when that source is explicitly enabled/selected, keeping the
default Windows-only refresh path fast.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path, PurePosixPath

from . import paths


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    kind: str
    home: str = ""
    linux_home: str = ""
    distro: str = ""
    available: bool = True
    resolved: bool = True
    writable: bool = True
    live_watch: bool = True

    @property
    def claude_home(self) -> Path:
        return (Path(self.home) / ".claude") if self.kind == "wsl" else paths.claude_home()

    @property
    def codex_home(self) -> Path:
        return (Path(self.home) / ".codex") if self.kind == "wsl" else paths.codex_home()

    @property
    def temp_roots(self) -> list[Path]:
        if self.kind != "wsl":
            return paths.temp_roots()
        return [Path(f"\\\\wsl.localhost\\{self.distro}\\tmp")]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["claude_home"] = str(self.claude_home) if self.resolved else ""
        data["codex_home"] = str(self.codex_home) if self.resolved else ""
        data["has_claude"] = self.resolved and self.claude_home.is_dir()
        data["has_codex"] = self.resolved and self.codex_home.is_dir()
        data["status"] = "live" if self.live_watch else ("manual" if self.available else "unavailable")
        return data


def _decode_output(data: bytes) -> str:
    if b"\x00" in data:
        return data.decode("utf-16-le", "replace")
    for encoding in ("utf-8", "mbcs"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _unc_home(distro: str, linux_home: str) -> str:
    parts = [part for part in PurePosixPath(linux_home).parts if part != "/"]
    return str(Path(f"\\\\wsl.localhost\\{distro}").joinpath(*parts))


@lru_cache(maxsize=1)
def discover_sources() -> tuple[Source, ...]:
    local_id = "windows" if platform.system() == "Windows" else "local"
    local_label = "Windows" if local_id == "windows" else platform.system() or "Local"
    result = [Source(local_id, local_label, "local", str(Path.home()))]
    if platform.system() != "Windows":
        return tuple(result)
    listed = _run(["wsl.exe", "-l", "-q"])
    if listed is None or listed.returncode:
        return tuple(result)
    names = []
    for line in _decode_output(listed.stdout).splitlines():
        name = line.strip().replace("\x00", "")
        if name and name not in names:
            names.append(name)
    result.extend(
        Source(
            id=f"wsl:{distro}", label=f"WSL - {distro}", kind="wsl",
            distro=distro, available=True, resolved=False,
            writable=False, live_watch=False,
        )
        for distro in names
    )
    return tuple(result)


@lru_cache(maxsize=16)
def resolve_source(source_id: str) -> Source | None:
    source = next((item for item in discover_sources() if item.id == source_id), None)
    if source is None or source.kind != "wsl" or source.resolved:
        return source
    got = _run(["wsl.exe", "-d", source.distro, "--", "sh", "-lc", 'printf "%s" "$HOME"'], timeout=12)
    if got is None or got.returncode:
        return replace(source, available=False)
    linux_home = _decode_output(got.stdout).strip()
    if not linux_home:
        return replace(source, available=False)
    unc = _unc_home(source.distro, linux_home)
    available = Path(unc).is_dir()
    return replace(source, home=unc, linux_home=linux_home, available=available, resolved=available)


def refresh_sources() -> tuple[Source, ...]:
    discover_sources.cache_clear()
    resolve_source.cache_clear()
    return discover_sources()


def source_by_id(source_id: str, *, resolve: bool = False) -> Source | None:
    return resolve_source(source_id) if resolve else next(
        (source for source in discover_sources() if source.id == source_id), None
    )
