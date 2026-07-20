from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from csm import actions


class DeleteSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.project = self.projects / "demo"
        self.project.mkdir(parents=True)
        self.session = self.project / "abc-123.jsonl"
        self.session.write_text("{}\n", "utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_paths(self):
        return (
            patch.object(actions.paths, "claude_home", return_value=self.root),
            patch.object(actions.paths, "projects_dir", return_value=self.projects),
        )

    def test_recent_session_cannot_be_deleted(self) -> None:
        home_patch, projects_patch = self._patch_paths()
        with home_patch, projects_patch:
            result = actions.delete_session("demo", "abc-123")
        self.assertFalse(result["ok"])
        self.assertIn("last 10 minutes", result["error"])
        self.assertTrue(self.session.exists())

    def test_old_session_can_be_deleted(self) -> None:
        old = time.time() - 601
        os.utime(self.session, (old, old))
        home_patch, projects_patch = self._patch_paths()
        with home_patch, projects_patch:
            result = actions.delete_session("demo", "abc-123")
        self.assertTrue(result["ok"])
        self.assertFalse(self.session.exists())


class SafeWriteTests(unittest.TestCase):
    def test_guidance_write_backs_up_previous_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "CLAUDE.md"
            target.write_text("old guidance\n", "utf-8")
            with patch.object(actions.paths, "claude_home", return_value=root):
                result = actions.write_guidance("global", "new guidance\n")
            self.assertTrue(result["ok"])
            self.assertEqual(target.read_text("utf-8"), "new guidance\n")
            backup = Path(result["backup"])
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text("utf-8"), "old guidance\n")

    def test_codex_writer_allows_only_config_and_agents_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            config = home / "config.toml"
            config.write_text("model = 'old'\n", "utf-8")
            with patch.object(actions.paths, "codex_home", return_value=home):
                saved = actions.write_codex_file(str(config), "model = 'new'\n")
                refused = actions.write_codex_file(str(home / "auth.json"), "{}")
            self.assertTrue(saved["ok"])
            self.assertTrue(Path(saved["backup"]).is_file())
            self.assertFalse(refused["ok"])
            self.assertEqual(config.read_text("utf-8"), "model = 'new'\n")


class LaunchValidationTests(unittest.TestCase):
    def test_missing_project_is_rejected_before_process_launch(self) -> None:
        result = actions.launch_claude("Z:/path/that/does/not/exist")
        self.assertFalse(result["ok"])

    def test_malformed_session_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = actions.launch_claude(tmp, "id & unsafe")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Invalid session id")

    def test_codex_resume_uses_provider_specific_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(actions.platform, "system", return_value="Linux"), \
                patch.object(actions.shutil, "which", side_effect=lambda name: f"/tools/{name}"), \
                patch.object(actions.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                patch.object(actions.subprocess, "Popen") as popen:
            result = actions.launch_session("codex", tmp, "abc-123", "resume")
        self.assertTrue(result["ok"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["/tools/codex", "resume", "abc-123"])

    def test_codex_windows_falls_back_to_documented_desktop_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(actions.platform, "system", return_value="Windows"), \
                patch.object(actions, "_cli_binary", return_value=None), \
                patch.object(actions.os, "startfile", create=True) as startfile:
            result = actions.launch_session("codex", tmp, "abc-123", "resume")
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], "desktop")
        startfile.assert_called_once_with("codex://threads/abc-123")


if __name__ == "__main__":
    unittest.main()
