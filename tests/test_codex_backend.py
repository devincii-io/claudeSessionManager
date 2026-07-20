from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csm.codex_scanner import CodexScanner
from csm.codex_session_parser import detail, summarize


def _record(timestamp: str, record_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _write_jsonl(path: Path, records: list[dict], trailing: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in records)
    path.write_bytes(data + trailing)


class CodexParserTests(unittest.TestCase):
    def test_normalizes_identity_messages_tools_usage_and_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-filename-is-not-the-id.jsonl"
            records = [
                _record("2026-07-20T10:00:00Z", "session_meta", {
                    "id": "root-thread-id",
                    "session_id": "legacy-or-root-id",
                    "cwd": str(Path(tmp) / "demo"),
                    "source": "vscode",
                    "context_window": 200,
                }),
                _record("2026-07-20T10:00:01Z", "turn_context", {"model": "gpt-test", "cwd": str(Path(tmp) / "demo")}),
                _record("2026-07-20T10:00:02Z", "event_msg", {"type": "user_message", "message": "Fix the parser"}),
                # Duplicate representation of the same user prompt.
                _record("2026-07-20T10:00:02Z", "response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the parser"}],
                }),
                _record("2026-07-20T10:00:03Z", "response_item", {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Working on it."}],
                }),
                _record("2026-07-20T10:00:04Z", "response_item", {
                    "type": "function_call", "name": "shell_command", "call_id": "call-1",
                    "arguments": json.dumps({"command": "pytest -q"}),
                }),
                _record("2026-07-20T10:00:05Z", "response_item", {
                    "type": "function_call_output", "call_id": "call-1", "output": "Error: test failed",
                }),
                _record("2026-07-20T10:00:06Z", "event_msg", {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 8, "cached_input_tokens": 2,
                            "cache_write_input_tokens": 0, "output_tokens": 10,
                            "reasoning_output_tokens": 4, "total_tokens": 20,
                        },
                        "last_token_usage": {"total_tokens": 12},
                        "model_context_window": 200,
                    },
                }),
                # A later cumulative snapshot must replace, not add to, the first.
                _record("2026-07-20T10:00:07Z", "event_msg", {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 18, "cached_input_tokens": 2,
                            "cache_write_input_tokens": 0, "output_tokens": 20,
                            "reasoning_output_tokens": 7, "total_tokens": 40,
                        },
                        "last_token_usage": {"total_tokens": 30},
                        "model_context_window": 200,
                    },
                }),
                _record("2026-07-20T10:00:08Z", "event_msg", {"type": "context_compacted"}),
                # A later metadata-like record must not replace the first identity.
                _record("2026-07-20T10:00:09Z", "session_meta", {"id": "wrong-later-id", "cwd": "wrong"}),
            ]
            _write_jsonl(rollout, records, trailing=b'{"type":"partially-written"')

            summary = summarize(rollout, size=rollout.stat().st_size, mtime=1.0)
            self.assertEqual(summary.session_id, "root-thread-id")
            self.assertEqual(summary.user_messages, 1)
            self.assertEqual(summary.assistant_messages, 1)
            self.assertEqual(summary.first_prompt, "Fix the parser")
            self.assertEqual(summary.tool_counts, {"shell_command": 1})
            self.assertEqual(summary.usage["total"], 40)
            self.assertEqual(summary.last_context_tokens, 30)
            self.assertEqual(summary.context_pct, 15.0)
            self.assertEqual(summary.compactions, 1)
            self.assertEqual(summary.cost, 0.0)
            self.assertFalse(summary.cost_available)

            parsed = detail(rollout)
            self.assertEqual(parsed["session_id"], "root-thread-id")
            self.assertEqual(parsed["analytics"]["user_prompts"], 1)
            self.assertEqual(parsed["analytics"]["assistant_turns"], 1)
            self.assertEqual(parsed["analytics"]["tool_calls"], 1)
            self.assertEqual(parsed["analytics"]["tool_error_total"], 1)
            self.assertEqual(parsed["analytics"]["compactions"], 1)
            self.assertEqual(parsed["usage"]["total"], 40)
            visible_text = [
                block.get("text")
                for event in parsed["events"]
                for block in event.get("blocks", [])
                if block.get("type") == "text"
            ]
            self.assertEqual(visible_text, ["Fix the parser", "Working on it."])

    def test_response_item_user_is_used_when_event_message_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "fallback.jsonl"
            _write_jsonl(rollout, [
                _record("2026-07-20T10:00:00Z", "session_meta", {"id": "fallback-id", "cwd": tmp, "source": "vscode"}),
                _record("2026-07-20T10:00:01Z", "response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Fallback prompt"}],
                }),
            ])
            summary = summarize(rollout)
            self.assertEqual(summary.user_messages, 1)
            self.assertEqual(summary.first_prompt, "Fallback prompt")


class CodexScannerTests(unittest.TestCase):
    def test_groups_roots_by_cwd_excludes_children_and_uses_index_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            project = Path(tmp) / "workspace" / "demo"
            project.mkdir(parents=True)
            root_rollout = home / "sessions" / "2026" / "07" / "20" / "rollout-root.jsonl"
            child_rollout = home / "sessions" / "2026" / "07" / "20" / "rollout-child.jsonl"
            _write_jsonl(root_rollout, [
                _record("2026-07-20T10:00:00Z", "session_meta", {
                    "id": "root-id", "session_id": "legacy-id", "cwd": str(project), "source": "vscode",
                }),
                _record("2026-07-20T10:00:01Z", "event_msg", {"type": "user_message", "message": "Original prompt"}),
                _record("2026-07-20T10:00:02Z", "response_item", {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Root reply"}],
                }),
            ])
            _write_jsonl(child_rollout, [
                _record("2026-07-20T10:01:00Z", "session_meta", {
                    "id": "child-id", "session_id": "root-id", "cwd": str(project),
                    "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": "root-id", "depth": 1, "agent_path": "/root/reviewer",
                    }}},
                }),
                _record("2026-07-20T10:01:01Z", "event_msg", {"type": "user_message", "message": "Review it"}),
            ])
            _write_jsonl(home / "session_index.jsonl", [
                {"id": "root-id", "thread_name": "Indexed title", "updated_at": "2026-07-20T10:02:00Z"},
            ])

            scanner = CodexScanner(home)
            projects = scanner.scan_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["provider"], "codex")
            self.assertEqual(projects[0]["session_count"], 1)
            self.assertFalse(projects[0]["cost_available"])

            project_id = projects[0]["id"]
            sessions = scanner.list_sessions(project_id)
            self.assertEqual([item["session_id"] for item in sessions], ["root-id"])
            self.assertEqual(sessions[0]["title"], "Indexed title")
            self.assertTrue(sessions[0]["has_subagents"])
            self.assertEqual(sessions[0]["child_session_count"], 1)
            self.assertEqual(scanner.session_path(project_id, "root-id"), root_rollout)
            self.assertEqual(scanner.session_path("child-id"), child_rollout)

            parsed = scanner.detail(project_id, "root-id")
            self.assertEqual(parsed["provider"], "codex")
            self.assertEqual(parsed["session_id"], "root-id")
            self.assertEqual(parsed["child_sessions"][0]["session_id"], "child-id")
            self.assertEqual(parsed["child_sessions"][0]["agent_path"], "/root/reviewer")

            all_sessions = scanner.all_sessions()
            self.assertEqual(len(all_sessions["sessions"]), 1)
            self.assertFalse(all_sessions["cost_available"])
            search = scanner.search_all("indexed")
            self.assertEqual(search["sessions"][0]["session_id"], "root-id")
            self.assertEqual(scanner.global_stats()["sessions"], 1)

    def test_incremental_scan_waits_for_a_complete_trailing_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            rollout = home / "sessions" / "2026" / "07" / "20" / "rollout-live.jsonl"
            meta = _record("2026-07-20T10:00:00Z", "session_meta", {"id": "live-id", "cwd": tmp, "source": "vscode"})
            prompt = _record("2026-07-20T10:00:01Z", "event_msg", {"type": "user_message", "message": "Arrived later"})
            partial = json.dumps(prompt).encode("utf-8")
            _write_jsonl(rollout, [meta], trailing=partial)

            scanner = CodexScanner(home)
            project_id = scanner.scan_projects()[0]["id"]
            self.assertEqual(scanner.list_sessions(project_id)[0]["user_messages"], 0)

            with rollout.open("ab") as handle:
                handle.write(b"\n")
            session = scanner.list_sessions(project_id)[0]
            self.assertEqual(session["user_messages"], 1)
            self.assertEqual(session["first_prompt"], "Arrived later")


if __name__ == "__main__":
    unittest.main()
