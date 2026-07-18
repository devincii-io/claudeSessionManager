"""Streaming, *incremental* parser for Claude Code session ``.jsonl`` transcripts.

Performance model:

* ``orjson`` (Rust) is used when available — 5-10x faster than stdlib ``json``
  on the multi-megabyte transcripts Claude Code produces.
* Both the summary and the detail reconstruction are **builders** that can be
  fed records one at a time. The scanner keeps a builder + byte offset per
  file, so when an active session appends output only the *new* bytes are read
  and parsed — a live 100 MB session refreshes in microseconds instead of
  re-parsing the whole file.
* Appends are cut at the last complete newline so a partially-written trailing
  line is never consumed (it is picked up on the next refresh).

Assistant token usage is logged once per *content block* line but reflects the
whole API response, so usage is deduplicated by ``message.id`` — counting it per
line would multiply cost several-fold.
"""

from __future__ import annotations

import json as _stdjson
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:  # fast path
    import orjson as _orjson

    def _loads(data: bytes | str):
        return _orjson.loads(data)
except ImportError:  # pragma: no cover
    def _loads(data: bytes | str):
        return _stdjson.loads(data)

TEXT_TRUNCATE = 8000
PREVIEW_TRUNCATE = 600


# --------------------------------------------------------------------------- #
# Low-level reading                                                            #
# --------------------------------------------------------------------------- #


def read_new_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Read complete lines appended after ``offset``; return (lines, new_offset)."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    end = data.rfind(b"\n")
    if end == -1:
        return [], offset
    return data[:end].split(b"\n"), offset + end + 1


def iter_file_records(path: Path):
    """One full pass over a jsonl file (used where incremental state is absent)."""
    lines, _ = read_new_lines(path, 0)
    for line in lines:
        if not line:
            continue
        try:
            yield _loads(line)
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #

from . import pricing  # noqa: E402


def _usage_dict(u: pricing.Usage) -> dict:
    d = asdict(u)
    d["total"] = u.total
    return d


def _extract_usage(message: dict) -> pricing.Usage:
    u = message.get("usage") or {}
    return pricing.Usage(
        input=int(u.get("input_tokens") or 0),
        output=int(u.get("output_tokens") or 0),
        cache_read=int(u.get("cache_read_input_tokens") or 0),
        cache_write=int(u.get("cache_creation_input_tokens") or 0),
    )


def _context_tokens(message: dict) -> int:
    u = message.get("usage") or {}
    return (
        int(u.get("input_tokens") or 0)
        + int(u.get("cache_read_input_tokens") or 0)
        + int(u.get("cache_creation_input_tokens") or 0)
    )


def _first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _looks_like_command(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("<") or t.startswith("Caveat:") or t.startswith("[Request interrupted")


# --------------------------------------------------------------------------- #
# Summary builder                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class SessionSummary:
    session_id: str
    path: str
    cwd: str = ""
    git_branch: str = ""
    title: str = ""
    first_prompt: str = ""
    created: str = ""
    updated: str = ""
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    models: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    usage_by_model: dict = field(default_factory=dict)
    cost: float = 0.0
    last_context_tokens: int = 0
    context_window: int = 0
    context_pct: float = 0.0
    has_subagents: bool = False
    subagent_calls: int = 0
    tool_counts: dict = field(default_factory=dict)
    size_bytes: int = 0
    mtime: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class SummaryBuilder:
    """Aggregates summary stats; feed() records incrementally."""

    __slots__ = (
        "cwd", "git_branch", "title", "first_prompt", "created", "updated",
        "user_messages", "assistant_messages", "tool_calls", "models",
        "seen_ids", "total", "by_model", "tool_counts", "last_model",
        "last_context_tokens", "has_subagents", "subagent_calls",
    )

    def __init__(self) -> None:
        self.cwd = ""
        self.git_branch = ""
        self.title = ""
        self.first_prompt = ""
        self.created = ""
        self.updated = ""
        self.user_messages = 0
        self.assistant_messages = 0
        self.tool_calls = 0
        self.models: list[str] = []
        self.seen_ids: set[str] = set()
        self.total = pricing.Usage()
        self.by_model: dict[str, pricing.Usage] = {}
        self.tool_counts: Counter[str] = Counter()
        self.last_model: str | None = None
        self.last_context_tokens = 0
        self.has_subagents = False
        self.subagent_calls = 0

    def feed(self, rec: dict) -> None:
        ts = rec.get("timestamp")
        if ts:
            if not self.created:
                self.created = ts
            self.updated = ts
        if not self.cwd and rec.get("cwd"):
            self.cwd = rec["cwd"]
        if not self.git_branch and rec.get("gitBranch"):
            self.git_branch = rec["gitBranch"]
        if rec.get("isSidechain"):
            self.has_subagents = True

        rtype = rec.get("type")
        if rtype == "ai-title" and rec.get("aiTitle"):
            self.title = rec["aiTitle"]
            return

        message = rec.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")

        if role == "user":
            content = message.get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result:
                self.user_messages += 1
                if not self.first_prompt:
                    text = _first_text(content).strip()
                    if text and not _looks_like_command(text):
                        self.first_prompt = text[:280]

        elif role == "assistant":
            model = message.get("model")
            if model and model != "<synthetic>":
                if model not in self.models:
                    self.models.append(model)
                self.last_model = model

            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    self.tool_counts[name] += 1
                    self.tool_calls += 1
                    if name in ("Agent", "Task"):
                        self.subagent_calls += 1

            ctx = _context_tokens(message)
            if ctx:
                self.last_context_tokens = ctx
            msg_id = message.get("id") or rec.get("requestId")
            if msg_id and msg_id not in self.seen_ids:
                self.seen_ids.add(msg_id)
                self.assistant_messages += 1
                u = _extract_usage(message)
                self.total.add(u)
                self.by_model.setdefault(model or "unknown", pricing.Usage()).add(u)

    def result(self, session_id: str, path: str, size: int, mtime: float) -> SessionSummary:
        s = SessionSummary(session_id=session_id, path=path, size_bytes=size, mtime=mtime)
        s.cwd = self.cwd
        s.git_branch = self.git_branch
        s.title = self.title
        s.first_prompt = self.first_prompt
        s.created = self.created
        s.updated = self.updated
        s.user_messages = self.user_messages
        s.assistant_messages = self.assistant_messages
        s.tool_calls = self.tool_calls
        s.models = list(self.models)
        s.usage = _usage_dict(self.total)
        s.usage_by_model = {m: _usage_dict(u) for m, u in self.by_model.items()}
        s.cost = sum(pricing.cost_for(u, m) for m, u in self.by_model.items())
        s.last_context_tokens = self.last_context_tokens
        s.context_window = pricing.context_window(self.last_model)
        if s.context_window:
            s.context_pct = round(100.0 * s.last_context_tokens / s.context_window, 1)
        s.has_subagents = self.has_subagents or self.subagent_calls > 0
        s.subagent_calls = self.subagent_calls
        s.tool_counts = dict(self.tool_counts.most_common(20))
        return s


def summarize(path: Path, *, size: int = 0, mtime: float = 0.0) -> SessionSummary:
    """Convenience one-shot full parse (non-incremental callers/tests)."""
    b = SummaryBuilder()
    for rec in iter_file_records(path):
        b.feed(rec)
    return b.result(path.stem, str(path), size, mtime)


# --------------------------------------------------------------------------- #
# Detail builder                                                               #
# --------------------------------------------------------------------------- #


def _condense_block(block: dict) -> dict | None:
    btype = block.get("type")
    if btype == "text":
        text = block.get("text", "")
        return {"type": "text", "text": text[:TEXT_TRUNCATE], "truncated": len(text) > TEXT_TRUNCATE}
    if btype == "thinking":
        text = block.get("thinking", "")
        return {"type": "thinking", "text": text[:TEXT_TRUNCATE], "truncated": len(text) > TEXT_TRUNCATE}
    if btype == "tool_use":
        raw = _stdjson.dumps(block.get("input", {}), ensure_ascii=False, default=str)
        return {
            "type": "tool_use",
            "id": block.get("id"),
            "name": block.get("name", "?"),
            "input_preview": raw[:PREVIEW_TRUNCATE],
            "input_truncated": len(raw) > PREVIEW_TRUNCATE,
        }
    if btype == "tool_result":
        content = block.get("content")
        text = content if isinstance(content, str) else _first_text(content)
        if not text and isinstance(content, list):
            text = _stdjson.dumps(content, ensure_ascii=False, default=str)
        return {
            "type": "tool_result",
            "tool_use_id": block.get("tool_use_id"),
            "content_preview": (text or "")[:PREVIEW_TRUNCATE],
            "content_truncated": len(text or "") > PREVIEW_TRUNCATE,
            "is_error": bool(block.get("is_error")),
        }
    if btype == "image":
        return {"type": "image"}
    return {"type": btype or "unknown"}


_FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")


def _downsample(seq: list, cap: int = 300) -> list:
    if len(seq) <= cap:
        return list(seq)
    step = len(seq) / cap
    return [seq[int(i * step)] for i in range(cap)]


def _clean(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "_merge_id"}


class DetailBuilder:
    """Reconstructs the transcript + rich analytics; feed() incrementally.

    The full event list stays in memory but is *never* serialized whole — the
    scanner serves paged windows over it, and everything chart-shaped is
    pre-aggregated here so every tab's payload stays small and O(window).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seen_ids: set[str] = set()
        self.total = pricing.Usage()
        self.by_model: dict[str, pricing.Usage] = {}
        self.tool_counts: Counter[str] = Counter()
        self.timeline: list[dict] = []
        # analytics aggregates
        self.user_prompts = 0
        self.tool_errors: Counter[str] = Counter()
        self.tool_error_total = 0
        self._tool_id_name: dict[str, str] = {}
        self.files_touched: Counter[str] = Counter()
        self.bash_commands: Counter[str] = Counter()
        self.thinking_chars = 0
        self.text_chars = 0
        self.output_per_turn: list[int] = []
        self.hourly = [0] * 24
        self.daily: Counter[str] = Counter()
        self.compactions = 0
        self._last_ctx = 0
        self.first_ts = ""
        self.last_ts = ""
        # subagents
        self.sidechain_count = 0
        self.sidechain_events: list[dict] = []  # refs into self.events, capped
        self.agent_calls: list[dict] = []

    def _clock(self, ts: str | None) -> None:
        if not ts:
            return
        if not self.first_ts:
            self.first_ts = ts
        self.last_ts = ts
        try:
            self.hourly[int(ts[11:13])] += 1
            self.daily[ts[:10]] += 1
        except (ValueError, IndexError):
            pass

    def _track_sidechain(self, event: dict) -> None:
        self.sidechain_count += 1
        self.sidechain_events.append(event)
        if len(self.sidechain_events) > 100:
            self.sidechain_events.pop(0)

    def feed(self, rec: dict) -> None:
        message = rec.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        ts = rec.get("timestamp")
        is_side = bool(rec.get("isSidechain"))

        if role == "assistant":
            model = message.get("model")
            raw_blocks = message.get("content") or []
            blocks = []
            for raw in raw_blocks:
                b = _condense_block(raw) if isinstance(raw, dict) else None
                if not b:
                    continue
                blocks.append(b)
                btype = b["type"]
                if btype == "tool_use":
                    name = b["name"]
                    self.tool_counts[name] += 1
                    if b.get("id"):
                        self._tool_id_name[b["id"]] = name
                    inp = raw.get("input") if isinstance(raw.get("input"), dict) else {}
                    if name in _FILE_TOOLS:
                        fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                        if fp:
                            self.files_touched[str(fp)] += 1
                    elif name == "Bash":
                        cmd = str(inp.get("command", "")).strip()
                        if cmd:
                            self.bash_commands[cmd.split()[0][:40]] += 1
                    elif name in ("Agent", "Task", "Workflow"):
                        desc = inp.get("description") or inp.get("prompt") or ""
                        self.agent_calls.append({"name": name, "desc": str(desc)[:160], "ts": ts})
                        if len(self.agent_calls) > 100:
                            self.agent_calls.pop(0)
                elif btype == "thinking":
                    self.thinking_chars += len(b.get("text", ""))
                elif btype == "text":
                    self.text_chars += len(b.get("text", ""))

            msg_id = message.get("id") or rec.get("requestId")
            last = self.events[-1] if self.events else None
            if last is not None and last.get("_merge_id") == msg_id and last["role"] == "assistant":
                last["blocks"].extend(blocks)
            else:
                event = {
                    "uuid": rec.get("uuid"),
                    "role": "assistant",
                    "ts": ts,
                    "model": model,
                    "sidechain": is_side,
                    "blocks": blocks,
                    "_merge_id": msg_id,
                }
                self.events.append(event)
                self._clock(ts)
                if is_side:
                    self._track_sidechain(event)

            if msg_id and msg_id not in self.seen_ids:
                self.seen_ids.add(msg_id)
                u = _extract_usage(message)
                self.total.add(u)
                self.by_model.setdefault(model or "unknown", pricing.Usage()).add(u)
                self.output_per_turn.append(u.output)
                ctx = _context_tokens(message)
                if ctx:
                    if self._last_ctx and ctx < self._last_ctx * 0.65:
                        self.compactions += 1
                    self._last_ctx = ctx
                    if ts:
                        cost = sum(pricing.cost_for(v, m) for m, v in self.by_model.items())
                        self.timeline.append({"t": ts, "ctx": ctx, "cost": round(cost, 4)})

        elif role == "user":
            content = message.get("content")
            has_tool_result = False
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content[:TEXT_TRUNCATE], "truncated": len(content) > TEXT_TRUNCATE}]
            elif isinstance(content, list):
                blocks = []
                for raw in content:
                    b = _condense_block(raw) if isinstance(raw, dict) else None
                    if not b:
                        continue
                    blocks.append(b)
                    if b["type"] == "tool_result":
                        has_tool_result = True
                        if b.get("is_error"):
                            self.tool_error_total += 1
                            name = self._tool_id_name.get(b.get("tool_use_id") or "", "?")
                            self.tool_errors[name] += 1
            else:
                blocks = []
            if not has_tool_result:
                self.user_prompts += 1
            event = {
                "uuid": rec.get("uuid"),
                "role": "user",
                "ts": ts,
                "sidechain": is_side,
                "blocks": blocks,
            }
            self.events.append(event)
            self._clock(ts)
            if is_side:
                self._track_sidechain(event)

    # -- output ------------------------------------------------------------- #

    def meta(self) -> dict:
        """Everything except transcript events — small, chart-ready payload."""
        daily = sorted(self.daily.items())
        return {
            "total_events": len(self.events),
            "usage": _usage_dict(self.total),
            "usage_by_model": {m: _usage_dict(u) for m, u in self.by_model.items()},
            "cost": round(sum(pricing.cost_for(u, m) for m, u in self.by_model.items()), 4),
            "tool_counts": dict(self.tool_counts.most_common()),
            "timeline": _downsample(self.timeline),
            "analytics": {
                "user_prompts": self.user_prompts,
                "assistant_turns": len(self.seen_ids),
                "tool_calls": sum(self.tool_counts.values()),
                "tool_error_total": self.tool_error_total,
                "tool_errors": dict(self.tool_errors.most_common(10)),
                "files_touched": dict(self.files_touched.most_common(15)),
                "bash_commands": dict(self.bash_commands.most_common(12)),
                "thinking_chars": self.thinking_chars,
                "text_chars": self.text_chars,
                "output_per_turn": _downsample(self.output_per_turn),
                "hourly_utc": list(self.hourly),
                "daily": daily[-30:],
                "compactions": self.compactions,
                "first_ts": self.first_ts,
                "last_ts": self.last_ts,
            },
            "subagents": {
                "count": self.sidechain_count,
                "agent_calls": list(self.agent_calls),
                "events": [_clean(e) for e in self.sidechain_events],
            },
        }

    def tail(self, count: int) -> tuple[list[dict], int]:
        """Last `count` events; returns (events, global index of the first one)."""
        tail = self.events[-count:] if count else []
        start = len(self.events) - len(tail)
        return [_clean(e) for e in tail], start

    def page_before(self, before: int, count: int) -> tuple[list[dict], int]:
        """Up to `count` events with global index < before."""
        lo = max(0, before - count)
        return [_clean(e) for e in self.events[lo:before]], lo

    def page_after(self, after: int) -> tuple[list[dict], int]:
        """All events with global index > after (used for live tail-follow)."""
        start = after + 1
        return [_clean(e) for e in self.events[start:]], start


def detail(path: Path, *, tail: int = 4000) -> dict:
    """Convenience one-shot full parse (non-incremental callers/tests)."""
    b = DetailBuilder()
    for rec in iter_file_records(path):
        b.feed(rec)
    out = b.meta()
    out["events"], out["events_start"] = b.tail(tail)
    return out
