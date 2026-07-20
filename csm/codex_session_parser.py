"""Incremental parser for Codex rollout JSONL transcripts.

Codex and Claude expose similar concepts but their on-disk formats are not
interchangeable.  This module emits the same summary/detail shape consumed by
the existing UI while preserving Codex-specific identity rules:

* the first ``session_meta.payload.id`` is the rollout/thread identity;
* a subagent's ``payload.session_id`` may refer to its root, so it is not used
  ahead of ``id``;
* user prompts prefer ``event_msg.user_message`` while assistant messages and
  tool traffic come from ``response_item`` records, avoiding duplicate text;
* token-count records are cumulative snapshots, so only the latest is kept.

Builders can be fed appended records and are tolerant of unknown/new record
types.  Low-level reading reuses the partial-line-safe reader used by Claude.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .session_parser import iter_file_records, read_new_lines

TEXT_TRUNCATE = 8_000
PREVIEW_TRUNCATE = 600


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("input_text", "output_text", "text"):
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("message") or "")
    return ""


def _looks_internal(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("<environment_context>", "<INSTRUCTIONS>", "# AGENTS.md instructions"))


def _usage_snapshot(info: Any) -> tuple[dict, int]:
    """Normalize the latest cumulative Codex token snapshot.

    ``total_tokens`` is retained verbatim because future schemas may include a
    token class not represented by the four legacy UI buckets.
    """
    if not isinstance(info, dict):
        return _empty_usage(), 0
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    if not isinstance(total, dict):
        total = {}
    if not isinstance(last, dict):
        last = {}
    usage = {
        "input": int(total.get("input_tokens") or 0),
        "output": int(total.get("output_tokens") or 0),
        "cache_read": int(total.get("cached_input_tokens") or 0),
        "cache_write": int(total.get("cache_write_input_tokens") or 0),
        "reasoning_output": int(total.get("reasoning_output_tokens") or 0),
        "total": int(total.get("total_tokens") or 0),
    }
    if not usage["total"]:
        usage["total"] = usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_write"]
    last_context = int(last.get("total_tokens") or 0)
    return usage, last_context


def _empty_usage() -> dict:
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning_output": 0,
        "total": 0,
    }


def _decode_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"input": value}
        except (TypeError, ValueError):
            return {"input": raw}
    return {}


def _preview(value: Any) -> tuple[str, bool]:
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = str(value)
    return raw[:PREVIEW_TRUNCATE], len(raw) > PREVIEW_TRUNCATE


def _source_meta(source: Any) -> tuple[bool, str, str]:
    if not isinstance(source, dict):
        return False, "", ""
    subagent = source.get("subagent") or {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else {}
    if not isinstance(spawn, dict):
        return False, "", ""
    return True, str(spawn.get("parent_thread_id") or ""), str(spawn.get("agent_path") or "")


def _tool_name(payload: dict) -> str:
    return str(payload.get("name") or payload.get("tool_name") or "?")


def _is_error_output(payload: dict, text: str) -> bool:
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    status = str(payload.get("status") or "").lower()
    if status in ("error", "failed", "failure"):
        return True
    lowered = text.lstrip().lower()
    return lowered.startswith(("error:", "tool error", "failed:"))


@dataclass
class SessionSummary:
    session_id: str
    path: str
    provider: str = "codex"
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
    usage: dict = field(default_factory=_empty_usage)
    usage_by_model: dict = field(default_factory=dict)
    cost: float = 0.0
    cost_available: bool = False
    last_context_tokens: int = 0
    context_window: int = 0
    context_pct: float = 0.0
    has_subagents: bool = False
    subagent_calls: int = 0
    tool_counts: dict = field(default_factory=dict)
    compactions: int = 0
    size_bytes: int = 0
    mtime: float = 0.0
    is_subagent: bool = False
    parent_session_id: str = ""
    agent_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SummaryBuilder:
    """Incrementally aggregate one Codex rollout."""

    def __init__(self) -> None:
        self.session_id = ""
        self.cwd = ""
        self.created = ""
        self.updated = ""
        self.first_prompt = ""
        self.user_messages = 0
        self.assistant_messages = 0
        self.models: list[str] = []
        self.last_model = ""
        self.usage = _empty_usage()
        self.last_context_tokens = 0
        self.context_window = 0
        self.tool_counts: Counter[str] = Counter()
        self.has_subagents = False
        self.subagent_calls = 0
        self.compactions = 0
        self.is_subagent = False
        self.parent_session_id = ""
        self.agent_path = ""
        self._saw_meta = False
        self._last_user_text = ""
        self._last_was_user = False

    def _record_user(self, text: str) -> None:
        clean = text.strip()
        if not clean or _looks_internal(clean):
            return
        # Codex commonly writes event_msg.user_message immediately followed by
        # the same response_item user message.  Only collapse that adjacent pair;
        # the user is allowed to submit identical prompts in separate turns.
        if self._last_was_user and clean == self._last_user_text:
            return
        self.user_messages += 1
        if not self.first_prompt:
            self.first_prompt = clean[:280]
        self._last_user_text = clean
        self._last_was_user = True

    def feed(self, rec: dict) -> None:
        if not isinstance(rec, dict):
            return
        ts = str(rec.get("timestamp") or "")
        if ts:
            if not self.created:
                self.created = ts
            self.updated = ts
        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            return
        ptype = payload.get("type")

        if rtype == "session_meta" and not self._saw_meta:
            self._saw_meta = True
            self.session_id = str(payload.get("id") or payload.get("session_id") or "")
            self.cwd = str(payload.get("cwd") or "")
            self.context_window = int(payload.get("context_window") or 0)
            self.is_subagent, self.parent_session_id, self.agent_path = _source_meta(payload.get("source"))
            return

        if rtype == "turn_context":
            if not self.cwd:
                self.cwd = str(payload.get("cwd") or "")
            model = str(payload.get("model") or "")
            if model:
                self.last_model = model
                if model not in self.models:
                    self.models.append(model)
            return

        if rtype == "compacted" or ptype == "context_compacted":
            self.compactions += 1
            return

        if rtype == "event_msg":
            if ptype == "user_message":
                self._record_user(_text(payload.get("message")))
            elif ptype == "token_count":
                self.usage, self.last_context_tokens = _usage_snapshot(payload.get("info"))
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    self.context_window = int(info.get("model_context_window") or self.context_window)
            elif ptype == "task_started":
                self.context_window = int(payload.get("model_context_window") or self.context_window)
            elif ptype == "sub_agent_activity":
                self.has_subagents = True
            return

        if rtype != "response_item":
            return
        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                self._record_user(_text(payload.get("content")))
            elif role == "assistant":
                self.assistant_messages += 1
                self._last_was_user = False
            return
        if ptype in ("function_call", "custom_tool_call"):
            name = _tool_name(payload)
            self.tool_counts[name] += 1
            if name in ("spawn_agent", "send_message", "followup_task", "Agent", "Task"):
                self.has_subagents = True
                if name in ("spawn_agent", "Agent", "Task"):
                    self.subagent_calls += 1
            self._last_was_user = False

    def result(self, session_id: str, path: str, size: int, mtime: float) -> SessionSummary:
        sid = self.session_id or session_id
        summary = SessionSummary(session_id=sid, path=path, size_bytes=size, mtime=mtime)
        summary.cwd = self.cwd
        summary.first_prompt = self.first_prompt
        summary.created = self.created
        summary.updated = self.updated
        summary.user_messages = self.user_messages
        summary.assistant_messages = self.assistant_messages
        summary.tool_calls = sum(self.tool_counts.values())
        summary.models = list(self.models)
        summary.usage = dict(self.usage)
        if self.last_model and summary.usage.get("total"):
            summary.usage_by_model = {self.last_model: dict(summary.usage)}
        summary.last_context_tokens = self.last_context_tokens
        summary.context_window = self.context_window
        if self.context_window:
            summary.context_pct = round(100.0 * self.last_context_tokens / self.context_window, 1)
        summary.has_subagents = self.has_subagents
        summary.subagent_calls = self.subagent_calls
        summary.tool_counts = dict(self.tool_counts.most_common(20))
        summary.compactions = self.compactions
        summary.is_subagent = self.is_subagent
        summary.parent_session_id = self.parent_session_id
        summary.agent_path = self.agent_path
        return summary


def summarize(path: Path, *, size: int = 0, mtime: float = 0.0) -> SessionSummary:
    builder = SummaryBuilder()
    for record in iter_file_records(path):
        builder.feed(record)
    return builder.result(path.stem, str(path), size, mtime)


class DetailBuilder:
    """Reconstruct a paged transcript and chart-ready aggregates."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.session_id = ""
        self.cwd = ""
        self.models: list[str] = []
        self.last_model = ""
        self.usage = _empty_usage()
        self.context_window = 0
        self.last_context_tokens = 0
        self.tool_counts: Counter[str] = Counter()
        self.tool_errors: Counter[str] = Counter()
        self._tool_names: dict[str, str] = {}
        self.files_touched: Counter[str] = Counter()
        self.bash_commands: Counter[str] = Counter()
        self.user_prompts = 0
        self.assistant_turns = 0
        self.thinking_chars = 0
        self.text_chars = 0
        self.compactions = 0
        self.hourly = [0] * 24
        self.daily: Counter[str] = Counter()
        self.first_ts = ""
        self.last_ts = ""
        self.timeline: list[dict] = []
        self.output_per_turn: list[int] = []
        self.agent_calls: list[dict] = []
        self.sidechain_events: list[dict] = []
        self.is_subagent = False
        self.parent_session_id = ""
        self.agent_path = ""
        self._saw_meta = False
        self._last_user_text = ""
        self._last_was_user = False

    def _clock(self, ts: str) -> None:
        if not ts:
            return
        if not self.first_ts:
            self.first_ts = ts
        self.last_ts = ts
        try:
            self.hourly[int(ts[11:13])] += 1
            self.daily[ts[:10]] += 1
        except (IndexError, ValueError):
            pass

    def _append(self, role: str, ts: str, blocks: list[dict], **extra) -> None:
        event = {"role": role, "ts": ts, "blocks": blocks, **extra}
        self.events.append(event)
        self._clock(ts)
        if self.is_subagent:
            self.sidechain_events.append(event)
            if len(self.sidechain_events) > 100:
                self.sidechain_events.pop(0)

    def _record_user(self, text: str, ts: str) -> None:
        clean = text.strip()
        if not clean or _looks_internal(clean):
            return
        if self._last_was_user and clean == self._last_user_text:
            return
        clipped = clean[:TEXT_TRUNCATE]
        self._append("user", ts, [{"type": "text", "text": clipped, "truncated": len(clean) > TEXT_TRUNCATE}])
        self.user_prompts += 1
        self._last_user_text = clean
        self._last_was_user = True

    def _track_tool_input(self, name: str, args: dict) -> None:
        for key in ("file_path", "path", "notebook_path"):
            value = args.get(key)
            if isinstance(value, str) and value:
                self.files_touched[value] += 1
        command = args.get("command")
        if not command and name in ("exec", "shell_command"):
            command = args.get("input")
        if isinstance(command, str) and command.strip():
            self.bash_commands[command.strip().split()[0][:40]] += 1

    def feed(self, rec: dict) -> None:
        if not isinstance(rec, dict):
            return
        ts = str(rec.get("timestamp") or "")
        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            return
        ptype = payload.get("type")

        if rtype == "session_meta" and not self._saw_meta:
            self._saw_meta = True
            self.session_id = str(payload.get("id") or payload.get("session_id") or "")
            self.cwd = str(payload.get("cwd") or "")
            self.context_window = int(payload.get("context_window") or 0)
            self.is_subagent, self.parent_session_id, self.agent_path = _source_meta(payload.get("source"))
            return
        if rtype == "turn_context":
            model = str(payload.get("model") or "")
            if model:
                self.last_model = model
                if model not in self.models:
                    self.models.append(model)
            if not self.cwd:
                self.cwd = str(payload.get("cwd") or "")
            return
        if rtype == "compacted" or ptype == "context_compacted":
            self.compactions += 1
            return
        if rtype == "event_msg":
            if ptype == "user_message":
                self._record_user(_text(payload.get("message")), ts)
            elif ptype == "token_count":
                self.usage, self.last_context_tokens = _usage_snapshot(payload.get("info"))
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    self.context_window = int(info.get("model_context_window") or self.context_window)
                self.timeline.append({"t": ts, "ctx": self.last_context_tokens})
                if len(self.timeline) > 300:
                    self.timeline.pop(0)
            elif ptype == "task_started":
                self.context_window = int(payload.get("model_context_window") or self.context_window)
            elif ptype == "sub_agent_activity":
                self.agent_calls.append({
                    "name": str(payload.get("kind") or "subagent"),
                    "desc": str(payload.get("agent_path") or payload.get("agent_thread_id") or "")[:160],
                    "ts": ts,
                })
            return
        if rtype != "response_item":
            return

        if ptype == "message":
            role = payload.get("role")
            text = _text(payload.get("content"))
            if role == "user":
                self._record_user(text, ts)
            elif role == "assistant" and text:
                clipped = text[:TEXT_TRUNCATE]
                self._append("assistant", ts, [{"type": "text", "text": clipped, "truncated": len(text) > TEXT_TRUNCATE}], model=self.last_model)
                self.assistant_turns += 1
                self.text_chars += len(text)
                self._last_was_user = False
            return

        if ptype in ("reasoning", "agent_reasoning"):
            raw = payload.get("summary") if ptype == "reasoning" else payload.get("text")
            text = _text(raw)
            if not text and isinstance(raw, list):
                text = "\n".join(_text(item) for item in raw)
            if text:
                clipped = text[:TEXT_TRUNCATE]
                self._append("assistant", ts, [{"type": "thinking", "text": clipped, "truncated": len(text) > TEXT_TRUNCATE}], model=self.last_model)
                self.thinking_chars += len(text)
            self._last_was_user = False
            return

        if ptype in ("function_call", "custom_tool_call"):
            name = _tool_name(payload)
            args = _decode_args(payload.get("arguments") if ptype == "function_call" else payload.get("input"))
            preview, truncated = _preview(args)
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            self.tool_counts[name] += 1
            if call_id:
                self._tool_names[call_id] = name
            self._track_tool_input(name, args)
            self._append("assistant", ts, [{
                "type": "tool_use", "id": call_id, "name": name,
                "input_preview": preview, "input_truncated": truncated,
            }], model=self.last_model)
            if name in ("spawn_agent", "send_message", "followup_task", "Agent", "Task"):
                self.agent_calls.append({"name": name, "desc": str(args.get("message") or args.get("prompt") or "")[:160], "ts": ts})
            self._last_was_user = False
            return

        if ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            raw = payload.get("output")
            preview, truncated = _preview(raw)
            name = self._tool_names.get(call_id, "?")
            is_error = _is_error_output(payload, preview)
            if is_error:
                self.tool_errors[name] += 1
            self._append("user", ts, [{
                "type": "tool_result", "tool_use_id": call_id,
                "content_preview": preview, "content_truncated": truncated,
                "is_error": is_error,
            }])
            self._last_was_user = False

    def meta(self) -> dict:
        usage_by_model = {}
        if self.last_model and self.usage.get("total"):
            usage_by_model[self.last_model] = {**self.usage, "cost": 0.0, "cost_available": False}
        return {
            "provider": "codex",
            "session_id": self.session_id,
            "cwd": self.cwd,
            "total_events": len(self.events),
            "usage": dict(self.usage),
            "usage_by_model": usage_by_model,
            "cost": 0.0,
            "cost_available": False,
            "tool_counts": dict(self.tool_counts.most_common()),
            "timeline": list(self.timeline),
            "context_window": self.context_window,
            "last_context_tokens": self.last_context_tokens,
            "context_pct": round(100.0 * self.last_context_tokens / self.context_window, 1) if self.context_window else 0.0,
            "analytics": {
                "user_prompts": self.user_prompts,
                "assistant_turns": self.assistant_turns,
                "tool_calls": sum(self.tool_counts.values()),
                "tool_error_total": sum(self.tool_errors.values()),
                "tool_errors": dict(self.tool_errors.most_common(10)),
                "files_touched": dict(self.files_touched.most_common(15)),
                "bash_commands": dict(self.bash_commands.most_common(12)),
                "thinking_chars": self.thinking_chars,
                "text_chars": self.text_chars,
                "output_per_turn": list(self.output_per_turn),
                "hourly_utc": list(self.hourly),
                "daily": sorted(self.daily.items())[-30:],
                "compactions": self.compactions,
                "first_ts": self.first_ts,
                "last_ts": self.last_ts,
            },
            "subagents": {
                "count": len(self.agent_calls),
                "agent_calls": list(self.agent_calls[-100:]),
                "events": list(self.sidechain_events),
            },
            "is_subagent": self.is_subagent,
            "parent_session_id": self.parent_session_id,
            "agent_path": self.agent_path,
        }

    def tail(self, count: int) -> tuple[list[dict], int]:
        events = self.events[-count:] if count else []
        return list(events), len(self.events) - len(events)

    def page_before(self, before: int, count: int) -> tuple[list[dict], int]:
        lo = max(0, before - count)
        return list(self.events[lo:before]), lo

    def page_after(self, after: int) -> tuple[list[dict], int]:
        start = after + 1
        return list(self.events[start:]), start


def detail(path: Path, *, tail: int = 4_000) -> dict:
    builder = DetailBuilder()
    for record in iter_file_records(path):
        builder.feed(record)
    result = builder.meta()
    result["events"], result["events_start"] = builder.tail(tail)
    return result
