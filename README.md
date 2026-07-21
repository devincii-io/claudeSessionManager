<p align="center"><img src="web/icons/app-128.png" alt="Agent Session Manager" width="96"></p>

# Agent Session Manager

> **Unofficial community tool** — not affiliated with or endorsed by Anthropic
> or OpenAI. Claude is a trademark of Anthropic, PBC. Codex and OpenAI are
> trademarks of OpenAI. The app indexes local agent session data.

A fast desktop workbench for local **Claude Code and Codex** sessions. Switch
independently between agents (`All`, `Claude`, `Codex`) and environments
(`Windows`, an enabled WSL distro, or `All enabled`); browse projects and transcripts; inspect
tokens, context pressure, tools, compactions, and subagents; launch or resume
work; search history; manage storage; and edit each agent's configuration without
pretending their formats or capabilities are identical. Built with **PySide6** +
**QtWebEngine** and managed with **uv**. Cross-platform: Windows and Linux.

![Agent Session Manager workbench](docs/screenshot.png)

*(Screenshots show generated demo data.)*

**Fast by design:** streaming `orjson` parsing (a cold 10 MB Claude transcript summarizes in
~40 ms), disk-cached summaries keyed by mtime+size, and *incremental* parsing —
while a session is live, only the newly appended bytes are read (~0.1 ms per
refresh), never the whole file. Transcripts are **paged**: the backend serves a
small window of messages and loads earlier pages on demand, so even a 100 MB
session opens with a small tail window and earlier messages load on demand. Live
tail-following keeps a bounded browser window, and path-aware filesystem events
refresh only the affected view instead of repeatedly rebuilding global analytics.
Codex rollouts are grouped by canonical working directory, parsed lazily, and
use cumulative token snapshots correctly; subagent rollouts do not inflate the
top-level session count. WSL distro names are discovered cheaply and remain off
by default; a distro is resolved and scanned only after you enable and select it.

**Fast to drive:** `Ctrl+Shift+P` opens every command, `Ctrl+P` quick-opens a
project/session, `Ctrl+F` filters, `Ctrl+Shift+F` searches all prompt history,
`Ctrl+N` starts the selected agent in the current project, and `Ctrl+Enter` resumes the
selected session. Press `?` inside the app for the complete shortcut reference.

Docs: [CHANGELOG](CHANGELOG.md) · [CONTRIBUTING](CONTRIBUTING.md) ·
[LICENSE](LICENSE) · [vendor/NOTICE](vendor/NOTICE)

## What it shows

- **Agent switcher** — `All | Claude | Codex` is a visible session-source filter,
  not a claim that an existing conversation can be converted between agents.
- **Environment switcher** — Windows and each WSL distribution are independent
  sources. Enable only the distros you want in Settings; `All enabled` aggregates
  Claude and Codex metrics without slowing the default Windows-only refresh path.
- **Projects** — every local project either agent has touched, ranked by recent
  activity with explicit agent badges.
- **Sessions** — per-session transcript viewer (user / assistant / thinking / tool
  calls / results), reconstructed from the `.jsonl` transcripts.
- **Analytics** — token composition, context pressure, compactions,
  reasoning/output, tool errors, files, commands, and model share. Claude can
  show an explicitly labelled API-price estimate. Codex ChatGPT-plan usage is
  never misrepresented as dollar spend.
- **Context meters** — the statusline-style 10-slot meter, reconstructed per
  session from token usage (`input + cache_read + cache_write` ÷ context window).
- **Subagents** — sidechain messages and `Agent`/`Task` invocations.
- **Memory** — the project `memory/` store (MEMORY.md index + individual memory
  files with frontmatter), with in-app **edit / save / delete**.
- **Scratchpads** — the per-session scratchpad tree, with previews.
- **Tasks** — the per-session task board.
- **Cleanup** — narrow sessions with real title/project/source, age, size, state,
  turn, and asset filters, then explicitly select matching safe items. A separate
  Assets & images view can remove uploads, legacy images, file history, tasks,
  environments, and scratchpads without deleting transcripts. Live
  sessions active in the last 10 minutes are conservatively protected and the
  backend rechecks immediately before deletion. Claude transcripts can be
  deleted; Codex sessions are archived through the supported Codex CLI command
  and correctly report `0 B` reclaimed. WSL Claude cleanup is inspection-only.
- **Instructions** — drive your own signed-in `claude` CLI over your history to **refine
  a CLAUDE.md** (global or per-project) or **consolidate sessions into memory
  notes**. Runs headless, async, cancellable, and backed up before writing; only
  session summaries are sent, never full transcripts. Codex exposes safe
  `config.toml` and global/project `AGENTS.md` editing with backups; the app does
  not auto-synchronize AGENTS.md and CLAUDE.md.
- **Claude settings** — a comprehensive, catalog-driven editor: a **Privacy & data**
  section with one-tap privacy-first defaults (keep sessions off claude.ai, kill
  non-essential traffic, disable telemetry / error reporting), a dedicated
  **environment-variable** editor, and arbitrary custom keys — all writing
  straight to `settings.json`. Only settings you actually set are written;
  removing one prunes it, so the file never accumulates dead keys.
- **Live monitor** — recently active sessions and context pressure, updated via a
  filesystem watcher.
- **Live statusline capture** (opt-in) — rate limits (5h / 7d) and live context %
  are only handed to your statusline command by Claude Code and aren't stored on
  disk. An optional, removable one-line hook lets the app read the latest values.
- **Global search** — press Enter in the search box to search every session
  (titles, first prompts) *and* your full prompt history, with jump-to-session.
- **Image gallery** — current Claude uploads and legacy image-cache files, as thumbnails.
- **Workspace** — the per-session scratchpad *and* background-task outputs.
- **Shells & environments** — shell snapshots and session-env dirs in Monitor.
- **Settings as controls** — toggles and dropdowns writing straight to
  `settings.json`, plus an in-app editor for small config files
  (`statusline-command.sh`, `settings.json`, commands, agents…).
- **Quick launch** — start/resume Claude or Codex with the exact provider-aware
  command. On Windows, documented `codex://` desktop deep links are used when the
  Store CLI alias is unavailable; WSL launches execute inside the owning distro
  with its raw Linux working directory. Set `CODEX_CLI_PATH` for native terminal launch.

Buttons throughout open paths in **VS Code** or your file manager, and sessions /
memory can be **deleted** (with confirmation).

## Where the data lives

| Agent | What | Path |
|---|---|---|
| Claude | Config home | `~/.claude` or `$CLAUDE_CONFIG_DIR` |
| Claude | Sessions | `~/.claude/projects/<encoded-path>/<session>.jsonl` |
| Claude | Memory / tasks | `~/.claude/projects/.../memory/`, `~/.claude/tasks/...` |
| Codex | Data home | `~/.codex` or `$CODEX_HOME` |
| Codex | Sessions | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| Codex | Settings / instructions | `$CODEX_HOME/config.toml`, `AGENTS.md` |
| WSL | Per-distro agent homes | `\\wsl.localhost\<distro>\...\.claude`, `.codex` |

## Run

```bash
uv sync
uv run csm
```

or `uv run python -m csm.app`.

## Build a standalone executable

```bash
uv sync --extra build
uv run pyinstaller --noconfirm ClaudeSessionManager.spec
```

This produces a **single-file** `dist/ClaudeSessionManager` executable — no
`_internal` folder beside it. The build keeps only English/German Qt locales,
omits Tk/splash payloads, and bundles Linux compatibility files only on Linux.
PyInstaller cannot
cross-compile, so run the build on the OS you're targeting (on Windows via
`powershell.exe` + a Windows `uv` when working from WSL).

## Architecture

```
csm/
  paths.py            cross-platform Claude and Codex path resolution
  pricing.py          model price table + cost math
  session_parser.py   streaming .jsonl parser (summary + detail)
  codex_session_parser.py  tolerant Codex rollout adapter
  codex_scanner.py    Codex project/session index and locator map
  sources.py          lazy native/WSL source discovery and path context
  scanner.py          enumerate projects/sessions/memory/tasks/scratchpad/settings (cached)
  watcher.py          watchdog → Qt signals (live updates)
  actions.py          delete / bulk-delete / save / settings / statusline hook
  assistant.py        headless `claude` CLI prompts + output parsing (Tune)
  bridge.py           QWebChannel object exposed to JS
  app.py              QApplication + QWebEngineView shell
web/
  index.html styles.css app.js   the frontend
```

Browsing and analytics stay local. Writes occur only after an explicit action and
are path-guarded; instruction/config overwrites create backups. Claude instruction
optimization intentionally invokes your signed-in local CLI with selected summaries,
which may contact that agent's service. Full transcripts are not sent by the app.
