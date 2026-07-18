# Claude Session Manager

A modern, fast desktop app for exploring everything **Claude Code** stores on your
machine — sessions, memory, subagents, scratchpads, tasks, settings and live
state — all grouped by project and richly visualized. Built with **PySide6** +
**QtWebEngine** (an HTML/CSS/JS frontend for a beautiful, extensible UI) and
managed with **uv**. Cross-platform: Linux and Windows.

![Claude Session Manager](docs/screenshot.png)

## What it shows

- **Projects** — every project Claude Code has touched, ranked by spend, with live
  activity indicators.
- **Sessions** — per-session transcript viewer (user / assistant / thinking / tool
  calls / results), reconstructed from the `.jsonl` transcripts.
- **Analytics** — cost, token composition (input / output / cache read / write),
  spend by model, context-window-over-time and cumulative-cost sparklines, and
  tool-usage breakdowns. Cost is computed from real usage with a built-in,
  editable model price table; assistant usage is de-duplicated by `message.id`.
- **Context meters** — the statusline-style 10-slot meter, reconstructed per
  session from token usage (`input + cache_read + cache_write` ÷ context window).
- **Subagents** — sidechain messages and `Agent`/`Task` invocations.
- **Memory** — the project `memory/` store (MEMORY.md index + individual memory
  files with frontmatter), with in-app **edit / save / delete**.
- **Scratchpads** — the per-session scratchpad tree, with previews.
- **Tasks** — the per-session task board.
- **Settings** — merged user settings, raw settings files, and the statusline
  script.
- **Live monitor** — active sessions and context pressure, updated live via a
  filesystem watcher.
- **Live statusline capture** (opt-in) — rate limits (5h / 7d) and live context %
  are only handed to your statusline command by Claude Code and aren't stored on
  disk. An optional, removable one-line hook lets the app read the latest values.

Buttons throughout open paths in **VS Code** or your file manager, and sessions /
memory can be **deleted** (with confirmation).

## Where the data lives

| What | Path |
|---|---|
| Config home | `~/.claude` (or `%USERPROFILE%\.claude`, or `$CLAUDE_CONFIG_DIR`) |
| Sessions | `~/.claude/projects/<encoded-path>/<session>.jsonl` |
| Memory | `~/.claude/projects/<encoded-path>/memory/` |
| Tasks | `~/.claude/tasks/<session>/*.json` |
| Scratchpads | `<tmp>/claude-<uid>/<encoded-path>/<session>/scratchpad/` |
| Settings | `~/.claude/settings.json`, `settings.local.json` |
| Statusline | `~/.claude/statusline-command.sh` |

## Run

```bash
uv sync
uv run csm
```

or `uv run python -m csm.app`.

## Build a standalone executable

```bash
uv sync --extra build
uv run pyinstaller --noconfirm --windowed --name "ClaudeSessionManager" \
  --add-data "web:web" csm/app.py     # use "web;web" on Windows
```

The result lands in `dist/`. Runs on Linux and Windows.

## Architecture

```
csm/
  paths.py            cross-platform Claude path resolution
  pricing.py          model price table + cost math
  session_parser.py   streaming .jsonl parser (summary + detail)
  scanner.py          enumerate projects/sessions/memory/tasks/scratchpad/settings (cached)
  watcher.py          watchdog → Qt signals (live updates)
  actions.py          delete / save / open-in-editor / statusline hook
  bridge.py           QWebChannel object exposed to JS
  app.py              QApplication + QWebEngineView shell
web/
  index.html styles.css app.js   the frontend
```

Nothing is sent anywhere — the app only reads and (on explicit action) writes your
local Claude directory.
