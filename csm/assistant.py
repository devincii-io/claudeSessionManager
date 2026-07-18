"""Drive the local, already-authenticated ``claude`` CLI (headless ``--print``
mode) to tune CLAUDE.md guidance and consolidate sessions into memory.

Prompts and output parsing live here; the async subprocess plumbing (QProcess)
lives in the bridge so results can be pushed to the UI without blocking it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def claude_bin() -> str | None:
    """Locate the ``claude`` executable — PATH first, then common install dirs."""
    found = shutil.which("claude")
    if found:
        return found
    for cand in ("~/.local/bin/claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude"):
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    return None


def _digest(summaries: list[dict], limit: int = 60) -> str:
    """Compact, high-signal summary of sessions for prompt context — titles,
    first asks and tool mix, never full transcripts."""
    lines: list[str] = []
    for s in summaries[:limit]:
        title = (s.get("title") or s.get("first_prompt") or "session").strip().replace("\n", " ")
        first = (s.get("first_prompt") or "").strip().replace("\n", " ")
        tools = ", ".join(f"{k}×{v}" for k, v in list((s.get("tool_counts") or {}).items())[:6])
        turns = s.get("assistant_messages", 0)
        lines.append(f"- {title[:110]} — {turns} turns; tools: {tools or 'none'}"
                     + (f"; first ask: {first[:180]}" if first and first[:180] != title[:110] else ""))
    extra = len(summaries) - limit
    if extra > 0:
        lines.append(f"- …and {extra} more sessions")
    return "\n".join(lines) or "(no sessions selected)"


DEFAULT_TUNE_INSTRUCTION = (
    "Fold in any durable conventions, architecture facts, gotchas and preferences "
    "that recur in the work above and that a fresh session would benefit from knowing. "
    "Remove anything stale or contradicted."
)


def build_tune_prompt(req: dict, summaries: list[dict]) -> str:
    scope = req.get("scope", "global")
    where = "every project on this machine" if scope == "global" else f"the project “{req.get('project_name', 'this project')}”"
    current = (req.get("current_md") or "").strip()
    instruction = (req.get("instruction") or "").strip() or DEFAULT_TUNE_INSTRUCTION
    return (
        f"You are refining a CLAUDE.md file — persistent instructions that Claude Code loads "
        f"at the start of every session in {where}.\n\n"
        f"CURRENT CLAUDE.md:\n<<<\n{current or '(empty — none exists yet)'}\n>>>\n\n"
        f"Recent work in {where} looked like this:\n{_digest(summaries)}\n\n"
        f"TASK: {instruction}\n\n"
        f"Rewrite the CLAUDE.md so it is concise, high-signal, and genuinely useful to a fresh "
        f"session — durable conventions and context, not a changelog of the sessions above. "
        f"Output ONLY the new CLAUDE.md content in Markdown: no preamble, no code fences, no commentary."
    )


def build_consolidate_prompt(req: dict, summaries: list[dict]) -> str:
    return (
        "Extract durable, reusable knowledge from these Claude Code sessions into a small set of "
        "memory notes. Each note holds ONE self-contained fact worth remembering across future "
        "sessions (a convention, a decision and its rationale, a gotcha, a key location). "
        "Skip anything ephemeral or specific to a single transcript.\n\n"
        f"Sessions:\n{_digest(summaries)}\n\n"
        "Return ONLY a JSON array (no code fences, no prose). Each element:\n"
        '{"name": "kebab-case-slug", "description": "one-line summary", '
        '"type": "project" | "reference", "body": "the fact, in Markdown"}\n'
        "Aim for 1–6 notes. If nothing is worth keeping, return []."
    )


def parse_result(out: str, err: str, code: int) -> dict:
    """Turn the CLI's stdout/stderr/exit-code into a normalized result."""
    out = (out or "").strip()
    if not out:
        return {"ok": False, "error": (err or f"claude exited with code {code}").strip()[:600]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # Non-JSON (older CLI or an error banner) — treat the raw text as the answer.
        return {"ok": code == 0, "text": out, "error": None if code == 0 else out[:600]}
    if isinstance(data, dict):
        if data.get("is_error"):
            return {"ok": False, "error": str(data.get("result") or data.get("error") or "assistant error")[:600]}
        return {
            "ok": True,
            "text": (data.get("result") or "").strip(),
            "cost": data.get("total_cost_usd") or 0.0,
        }
    return {"ok": False, "error": "unexpected assistant output"}


def parse_memory_notes(text: str) -> list[dict]:
    """Parse the consolidate output into memory-note dicts, tolerantly."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("\n") + 1:] if "\n" in t else t
    # Find the JSON array if the model wrapped it in prose.
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    try:
        arr = json.loads(t)
    except json.JSONDecodeError:
        return []
    notes = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip().lower().replace(" ", "-")
        body = str(it.get("body") or "").strip()
        if not name or not body:
            continue
        notes.append({
            "name": name,
            "description": str(it.get("description") or "").strip(),
            "type": it.get("type") if it.get("type") in ("project", "reference", "user", "feedback") else "project",
            "body": body,
        })
    return notes
