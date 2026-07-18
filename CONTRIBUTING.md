# Contributing

Thanks for your interest! This is a small, focused tool — contributions that
keep it fast and dependency-light are very welcome.

## Development setup

```bash
git clone https://github.com/devincii-io/claudeSessionManager
cd claudeSessionManager
uv sync
uv run csm
```

Python ≥ 3.10. The GUI needs a display; on WSL, WSLg works out of the box
(the app self-configures QtWebEngine, including the vendored `libxkbfile`).

## Code layout

| Path | Responsibility |
|---|---|
| `csm/paths.py` | Where Claude Code keeps things, cross-platform |
| `csm/pricing.py` | Model price table + cost math |
| `csm/session_parser.py` | Incremental `.jsonl` builders (summary, detail, analytics) |
| `csm/scanner.py` | Enumeration + caching + paged transcript state |
| `csm/watcher.py` / `csm/bridge.py` | Live updates and the QWebChannel API |
| `csm/actions.py` | Anything that writes (deletes, settings, statusline hook) |
| `web/` | The frontend (vanilla JS, no build step) |

## Ground rules

- **Performance is a feature.** Transcripts can be 100 MB+; never serialize a
  whole session to the frontend, and keep per-refresh work proportional to
  what changed (see the incremental builders before adding parsing).
- **Only write under `~/.claude` deliberately.** Every mutating action lives in
  `csm/actions.py` behind a path guard; keep it that way.
- **No network calls.** The app reads local files only.
- Conventional commits (`feat:`, `fix:`, `perf:`, `docs:`, `chore:`) — match
  the existing history.
- Check `uv run python -m compileall csm` passes and click through the views
  you touched (headless screenshot scripts in the repo history are a handy
  pattern).

## Releasing

Bump `version` in `pyproject.toml`, add a CHANGELOG entry, tag `vX.Y.Z`.
