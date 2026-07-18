"""Streaming parser for Claude Code session ``.jsonl`` transcripts.

Two entry points:

* :func:`summarize` — one fast pass producing lightweight metadata for the
  session list (title, counts, token totals, cost, context usage). Results are
  meant to be cached by the scanner keyed on file mtime+size.
* :func:`detail` — a fuller reconstruction of the transcript for the viewer,
  merging the multiple per-content-block JSONL records that make up a single
  assistant turn back into one logical message.

Assistant token usage is logged once per *content block* line but reflects the
whole API response, so usage is deduplicated by ``message.id`` — counting it per
line would multiply cost several-fold.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import pricing

TEXT_TRUNCATE = 8000
PREVIEW_TRUNCATE = 600


def iter_records(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a ``.jsonl`` file, skipping bad lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _usage_dict(u: pricing.Usage) -> dict:
    """Serialise a Usage including its derived ``total`` for the UI."""
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
    """Tokens counting toward the context window on this request."""
    u = message.get("usage") or {}
    return (
        int(u.get("input_tokens") or 0)
        + int(u.get("cache_read_input_tokens") or 0)
        + int(u.get("cache_creation_input_tokens") or 0)
    )


def _first_text(content: Any) -> str:
    """Best-effort plain text from a message ``content`` (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _looks_like_command(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("<") or t.startswith("Caveat:") or t.startswith("[Request interrupted")


# --------------------------------------------------------------------------- #
# Summary                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class SessionSummary:
    session_id: str
    path: str
    cwd: str = ""
    git_branch: str = ""
    title: str = ""
    first_prompt: str = ""
    created: str = ""  # ISO timestamp of first event
    updated: str = ""  # ISO timestamp of last event
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    models: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # totals
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


def summarize(path: Path, *, size: int = 0, mtime: float = 0.0) -> SessionSummary:
    session_id = path.stem
    s = SessionSummary(session_id=session_id, path=str(path), size_bytes=size, mtime=mtime)

    seen_msg_ids: set[str] = set()
    total = pricing.Usage()
    by_model: dict[str, pricing.Usage] = {}
    models_order: list[str] = []
    tool_counts: Counter[str] = Counter()
    last_model: str | None = None

    for rec in iter_records(path):
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if ts:
            if not s.created:
                s.created = ts
            s.updated = ts
        if not s.cwd and rec.get("cwd"):
            s.cwd = rec["cwd"]
        if not s.git_branch and rec.get("gitBranch"):
            s.git_branch = rec["gitBranch"]

        if rtype == "ai-title" and rec.get("aiTitle"):
            s.title = rec["aiTitle"]
            continue

        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "user":
            content = message.get("content")
            # Tool results are logged as user messages; don't count them as prompts.
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result:
                s.user_messages += 1
                if not s.first_prompt:
                    text = _first_text(content).strip()
                    if text and not _looks_like_command(text):
                        s.first_prompt = text[:280]

        elif role == "assistant":
            model = message.get("model")
            if model and model != "<synthetic>" and model not in models_order:
                models_order.append(model)
            if model and model != "<synthetic>":
                last_model = model

            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_counts[name] += 1
                    s.tool_calls += 1
                    if name in ("Agent", "Task"):
                        s.subagent_calls += 1

            msg_id = message.get("id") or rec.get("requestId")
            ctx = _context_tokens(message)
            if ctx:
                s.last_context_tokens = ctx
            if msg_id and msg_id not in seen_msg_ids:
                seen_msg_ids.add(msg_id)
                s.assistant_messages += 1
                u = _extract_usage(message)
                total.add(u)
                bm = by_model.setdefault(model or "unknown", pricing.Usage())
                bm.add(u)

        if rec.get("isSidechain"):
            s.has_subagents = True

    s.models = models_order
    s.usage = _usage_dict(total)
    s.usage_by_model = {m: _usage_dict(u) for m, u in by_model.items()}
    s.cost = sum(pricing.cost_for(u, m) for m, u in by_model.items())
    s.context_window = pricing.context_window(last_model)
    if s.context_window:
        s.context_pct = round(100.0 * s.last_context_tokens / s.context_window, 1)
    s.tool_counts = dict(tool_counts.most_common(20))
    if s.has_subagents or s.subagent_calls:
        s.has_subagents = True
    return s


# --------------------------------------------------------------------------- #
# Detail                                                                       #
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
        raw = json.dumps(block.get("input", {}), ensure_ascii=False, default=str)
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
            text = json.dumps(content, ensure_ascii=False, default=str)
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


def detail(path: Path, *, max_events: int = 4000) -> dict:
    """Reconstruct a transcript for the viewer plus charts-ready aggregates."""
    events: list[dict] = []
    seen_msg_ids: set[str] = set()
    total = pricing.Usage()
    by_model: dict[str, pricing.Usage] = {}
    tool_counts: Counter[str] = Counter()
    timeline: list[dict] = []  # {t, ctx, cost} per assistant request
    cumulative_cost = 0.0

    for rec in iter_records(path):
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        ts = rec.get("timestamp")
        is_side = bool(rec.get("isSidechain"))

        if role == "assistant":
            model = message.get("model")
            blocks = [b for b in (_condense_block(b) for b in (message.get("content") or [])) if b]
            for b in blocks:
                if b["type"] == "tool_use":
                    tool_counts[b["name"]] += 1

            msg_id = message.get("id") or rec.get("requestId")
            # Merge consecutive block-lines of one assistant turn into one event.
            if events and events[-1].get("_merge_id") == msg_id and events[-1]["role"] == "assistant":
                events[-1]["blocks"].extend(blocks)
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
                events.append(event)

            if msg_id and msg_id not in seen_msg_ids:
                seen_msg_ids.add(msg_id)
                u = _extract_usage(message)
                total.add(u)
                by_model.setdefault(model or "unknown", pricing.Usage()).add(u)
                cumulative_cost = sum(pricing.cost_for(v, m) for m, v in by_model.items())
                ctx = _context_tokens(message)
                if ctx and ts:
                    timeline.append({"t": ts, "ctx": ctx, "cost": round(cumulative_cost, 4)})

        elif role == "user":
            content = message.get("content")
            blocks = []
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content[:TEXT_TRUNCATE], "truncated": len(content) > TEXT_TRUNCATE}]
            elif isinstance(content, list):
                blocks = [b for b in (_condense_block(b) for b in content) if b]
            events.append(
                {
                    "uuid": rec.get("uuid"),
                    "role": "user",
                    "ts": ts,
                    "sidechain": is_side,
                    "blocks": blocks,
                }
            )

    # Strip internal merge keys before returning.
    for e in events:
        e.pop("_merge_id", None)

    truncated = len(events) > max_events
    if truncated:
        events = events[-max_events:]

    return {
        "events": events,
        "truncated": truncated,
        "usage": _usage_dict(total),
        "usage_by_model": {m: _usage_dict(u) for m, u in by_model.items()},
        "cost": round(sum(pricing.cost_for(u, m) for m, u in by_model.items()), 4),
        "tool_counts": dict(tool_counts.most_common()),
        "timeline": timeline,
    }
