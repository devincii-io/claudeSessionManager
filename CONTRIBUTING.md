# Contributing

Thanks for your interest! This is a small, focused tool — contributions that
keep it fast and dependency-light are very welcome.

## Development setup

```bash
git clone https://github.com/devincii-io/agent-session-manager
cd agent-session-manager
uv sync
uv run asm
```

Python ≥ 3.10. The GUI needs a display; on WSL, WSLg works out of the box
(the app self-configures QtWebEngine, including the vendored `libxkbfile`).

## Code layout

| Path | Responsibility |
|---|---|
| `asm/paths.py` | Where Claude Code keeps things, cross-platform |
| `asm/pricing.py` | Model price table + cost math |
| `asm/session_parser.py` | Incremental `.jsonl` builders (summary, detail, analytics) |
| `asm/scanner.py` | Enumeration + caching + paged transcript state |
| `asm/watcher.py` / `asm/bridge.py` | Live updates and the QWebChannel API |
| `asm/actions.py` | Anything that writes (deletes, settings, statusline hook) |
| `web/` | The frontend (vanilla JS, no build step) |

## Ground rules

- **Performance is a feature.** Transcripts can be 100 MB+; never serialize a
  whole session to the frontend, and keep per-refresh work proportional to
  what changed (see the incremental builders before adding parsing).
- **Only write under `~/.claude` deliberately.** Every mutating action lives in
  `asm/actions.py` behind a path guard; keep it that way.
- **No network calls.** The app reads local files only.
- Conventional commits (`feat:`, `fix:`, `perf:`, `docs:`, `chore:`) — match
  the existing history.
- Check `uv run python -m compileall asm` passes and click through the views
  you touched (headless screenshot scripts in the repo history are a handy
  pattern).

## Releasing

Bump `version` in `pyproject.toml`, add a CHANGELOG entry, tag `vX.Y.Z`.
