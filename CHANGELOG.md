# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres to
[Semantic Versioning](https://semver.org/).

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
