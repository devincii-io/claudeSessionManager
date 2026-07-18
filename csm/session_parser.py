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


class DetailBuilder:
    """Reconstructs the transcript + chart aggregates; feed() incrementally."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seen_ids: set[str] = set()
        self.total = pricing.Usage()
        self.by_model: dict[str, pricing.Usage] = {}
        self.tool_counts: Counter[str] = Counter()
        self.timeline: list[dict] = []

    def feed(self, rec: dict) -> None:
        message = rec.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        ts = rec.get("timestamp")
        is_side = bool(rec.get("isSidechain"))

        if role == "assistant":
            model = message.get("model")
            blocks = [b for b in (_condense_block(b) for b in (message.get("content") or [])) if b]
            for b in blocks:
                if b["type"] == "tool_use":
                    self.tool_counts[b["name"]] += 1

            msg_id = message.get("id") or rec.get("requestId")
            last = self.events[-1] if self.events else None
            if last is not None and last.get("_merge_id") == msg_id and last["role"] == "assistant":
                last["blocks"].extend(blocks)
            else:
                self.events.append({
                    "uuid": rec.get("uuid"),
                    "role": "assistant",
                    "ts": ts,
                    "model": model,
                    "sidechain": is_side,
                    "blocks": blocks,
                    "_merge_id": msg_id,
                })

            if msg_id and msg_id not in self.seen_ids:
                self.seen_ids.add(msg_id)
                u = _extract_usage(message)
                self.total.add(u)
                self.by_model.setdefault(model or "unknown", pricing.Usage()).add(u)
                ctx = _context_tokens(message)
                if ctx and ts:
                    cost = sum(pricing.cost_for(v, m) for m, v in self.by_model.items())
                    self.timeline.append({"t": ts, "ctx": ctx, "cost": round(cost, 4)})

        elif role == "user":
            content = message.get("content")
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content[:TEXT_TRUNCATE], "truncated": len(content) > TEXT_TRUNCATE}]
            elif isinstance(content, list):
                blocks = [b for b in (_condense_block(b) for b in content) if b]
            else:
                blocks = []
            self.events.append({
                "uuid": rec.get("uuid"),
                "role": "user",
                "ts": ts,
                "sidechain": is_side,
                "blocks": blocks,
            })

    def result(self, *, max_events: int = 4000) -> dict:
        truncated = len(self.events) > max_events
        tail = self.events[-max_events:] if truncated else self.events
        events = [{k: v for k, v in e.items() if k != "_merge_id"} for e in tail]
        return {
            "events": events,
            "truncated": truncated,
            "usage": _usage_dict(self.total),
            "usage_by_model": {m: _usage_dict(u) for m, u in self.by_model.items()},
            "cost": round(sum(pricing.cost_for(u, m) for m, u in self.by_model.items()), 4),
            "tool_counts": dict(self.tool_counts.most_common()),
            "timeline": list(self.timeline),
        }


def detail(path: Path, *, max_events: int = 4000) -> dict:
    """Convenience one-shot full parse (non-incremental callers/tests)."""
    b = DetailBuilder()
    for rec in iter_file_records(path):
        b.feed(rec)
    return b.result(max_events=max_events)
