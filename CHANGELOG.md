# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-07-21

### Added
- **First-class Codex support** alongside Claude Code, with a persistent
  `All | Claude | Codex` switcher, provider badges, unified project/session
  browsing, global search, transcripts, analytics, and provider-aware commands.
- A version-tolerant Codex rollout adapter that uses canonical metadata IDs,
  groups sessions by working directory, excludes subagent rollouts from root
  counts, de-duplicates message channels, and treats token snapshots as
  cumulative rather than additive.
- Codex quick launch/resume (including documented Windows desktop deep-link
  fallback), fork, supported CLI archive cleanup, and safe `config.toml` /
  `AGENTS.md` editing. Credentials, SQLite state, encrypted reasoning, and
  sandbox secrets are intentionally never exposed.
- **Universal command launcher** (`Ctrl+Shift+P`, `Ctrl+K`, or `F1`) for views,
  projects, current-session navigation, refresh, new sessions, resume, and help.
  `Ctrl+P` opens the project/session quick-open scope.
- **Quick launch** actions on Overview and project/session pages: start Claude in
  a project or resume a selected session directly in a new terminal.
- A complete, discoverable keyboard model with contextual shortcuts, pane
  cycling, tab cycling, filter/global search, rail toggle, and a searchable
  shortcut reference.
- **Context status** guidance using context pressure, compactions, and tool error
  rate, with focused `/compact` and start-fresh actions. Wall-clock duration alone
  is deliberately not treated as unhealthy.
- Fourteen regression tests covering both providers, deletion safety, atomic
  backups, path guards, launcher routing, identity, cumulative usage,
  root/subagent grouping, and incremental JSONL reading.
- A static browser preview with generated data for fast frontend development and
  visual review without access to a real Claude home.

### Changed
- Rebuilt the interface as a dense graphite developer workbench: one restrained
  accent, flatter surfaces, smaller radii, higher-contrast metadata, visible
  keyboard focus, a status bar, responsive breakpoints, and reduced-motion
  support.
- Renamed **Tune** to **Instructions** and made long Claude optimization work cancellable.
  Prompts now stream over stdin (avoiding Windows command-line limits), jobs have
  concurrency and timeout guards, and CLAUDE.md/memory writes create backups.
- Live refreshes are now path-aware and view-targeted. Expensive global aggregates
  are deferred outside Overview, refreshes cannot overlap, and continuous writes
  can no longer starve the trailing debounce.
- Prompt-history search is incrementally indexed in memory instead of rereading
  the full history file on every query. JSONL input is streamed line-by-line to
  avoid a duplicate whole-file buffer.
- Browser transcript growth is bounded during live tail-following; session-only
  scratchpad watches and large reconstructed detail state are released on exit.
- Cleanup renders large libraries in bounded chunks of 300 rows.
- Codex ChatGPT-plan usage is never combined into a dollar-spend claim. Claude
  pricing is labelled as an API-price estimate rather than a billing statement;
  capabilities that only exist in one agent are explicitly gated.

### Fixed
- Deletion is revalidated in the backend. Sessions with transcript activity in
  the last 10 minutes are conservatively protected, closing the gap where a quiet
  but still-running task could previously become deletable after two minutes.
- Settings, guidance, memory notes, and memory indexes use safer atomic writes;
  guidance and overwritten memory content are backed up first.
- Custom window controls are now shown only for the WSL workaround instead of
  duplicating native Windows controls.
- Clickable project/session/file rows are keyboard focusable, dialogs expose
  dialog semantics and initial focus, toasts are announced, and muted text now
  meets a substantially higher contrast target.

## [1.0.0] — 2026-07-18

First stable release. The app now covers the full lifecycle — explore, analyze,
tune and clean up — and ships as a single self-contained executable for Windows
and Linux.

### Added
- **Cleanup** — a disk-space helper that lists every session on the machine with
  its full on-disk footprint (transcript + tasks / file-history / image-cache /
  session-env). Multi-select by hand or with one-tap presets (empty, small talk,
  under 1¢, older than 30 days, largest 10), sort by size / age / cost, and
  delete in bulk (optionally purging ancillary data). Live sessions are
  protected from deletion. Select-to-delete is also available inside a project's
  session list.
- **Tune** — put your own signed-in `claude` CLI to work on your history,
  headless: **Refine CLAUDE.md** (global or per-project) folds durable
  conventions from recent sessions into a guidance file you review and save, and
  **Consolidate → memory** distills sessions into memory notes written to a
  project's memory store. Runs asynchronously so the UI never blocks; only
  session summaries are ever sent, never full transcripts.
- **Privacy-first settings** — a Privacy & data section with one-tap protections
  (keep sessions off claude.ai, master non-essential-traffic switch, disable
  telemetry / error reporting, drop the commit co-author trailer) and an **Apply
  privacy-first defaults** button.

### Changed
- **Settings redesigned** to be comprehensive but clean: a catalog of known
  settings, a dedicated environment-variable editor, and arbitrary custom
  key/value + env entries — so *any* setting is reachable. Only settings you
  actually set are written to `settings.json`; removing one prunes it (and any
  now-empty parent like `env`) so the file never accumulates dead keys.
- **Packaging is now single-file (onefile)**: one self-contained executable with
  no `_internal` folder beside it, a native splash screen during cold start, an
  app icon, and trimmed Qt modules for a smaller, faster-to-extract build.

## [0.4.0] — 2026-07-18

### Added
- **All-sessions Overview dashboard**: global spend (+avg/session), tokens,
  cache hit rate, prompts/turns/tool calls, subagent sessions, per-model cost,
  14-day activity, machine-wide tool usage and token composition.
- Application logo and icons — window/taskbar icon, Windows exe icon, favicon,
  in-app brand and README, all rendered from one SVG master.
- In-app minimize/close controls (WSLg title bars are nearly invisible).
- Per-model cost in the session model legend.
- PyInstaller entry point (`launcher.py`) and prebuilt release artifacts for
  Windows and Linux.

### Changed
- Monitor shows live state only (duplicated spend section removed) and every
  section/stat carries a plain-language explanation of what it is.
- The middle pane appears only inside a project — no duplicated project list.

### Fixed
- The filesystem watcher no longer crashes startup on unwatchable homes
  (e.g. UNC paths on Windows).

## [0.3.0] — 2026-07-18

### Changed
- **Transcripts are paged.** The backend now serves a small window of messages
  (the newest first) instead of serializing the whole reconstructed transcript;
  earlier pages load on demand and live sessions append only newly written
  events. Session payloads shrink from megabytes to kilobytes.
- **Analytics-first.** Opening a session lands on the Analytics tab.
- The live indicator is self-explanatory: steady green *watching*, amber
  *activity* while Claude Code writes to disk.

### Added
- Greatly expanded per-session analytics, all pre-aggregated server-side:
  cache hit rate, cost/output per turn, session duration, tool error counts
  and error rate, errors by tool, context compaction count, thinking share,
  hottest files (Read/Write/Edit targets), top shell commands, output tokens
  per turn, and activity-by-hour histogram — alongside the existing token
  composition, tokens by model, context-over-time and cumulative-cost charts.
- Subagent view is server-aggregated (Agent/Task invocations + sidechain
  messages) and independent of the loaded transcript window.
- CHANGELOG, CONTRIBUTING, and dummy-data documentation screenshots.

## [0.2.0] — 2026-07-18

### Added
- Global search across all session summaries and the prompt history, with
  jump-to-session.
- Image gallery (session image cache), workspace tab (scratchpad + background
  task outputs), shell snapshots and session environments in Monitor,
  file-history stats, copy-resume command, in-app file viewer.
- Settings as toggles/dropdowns writing to `settings.json`; in-app editor for
  small config files.

### Changed
- `orjson` decoding and incremental parsing: live sessions parse only appended
  bytes (~0.1 ms per refresh; cold 10 MB summary ≈ 40 ms). Statusline capture
  ticks refresh live meters without rescanning.

## [0.1.0] — 2026-07-18

### Added
- Initial release: project/session browser with cost and context meters,
  transcript viewer, analytics charts, memory manager with editing, task
  board, scratchpad browser, settings view, live filesystem watching, and the
  opt-in statusline capture hook. PySide6 + QtWebEngine, packaged with uv.
