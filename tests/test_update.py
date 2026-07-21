from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from asm import update


class UpdateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        update._cache = None
        update._cache_at = 0.0

    def test_version_comparison_handles_prefixes_and_padding(self) -> None:
        self.assertTrue(update.is_newer("v1.3", "1.2.9"))
        self.assertFalse(update.is_newer("1.2", "1.2.0"))
        self.assertFalse(update.is_newer("1.1.9", "1.2.0"))

    def test_download_url_must_be_https_on_github_owned_host(self) -> None:
        update._validate_download_url("https://github.com/devincii-io/file.exe")
        for url in ("http://github.com/file.exe", "https://example.com/file.exe", "file:///tmp/file.exe"):
            with self.assertRaises(ValueError):
                update._validate_download_url(url)

    def test_checksum_parser_requires_exact_filename(self) -> None:
        digest = "a" * 64
        text = f"{digest}  AgentSessionManager-v1.3.0-Setup.exe\n"
        self.assertEqual(update._expected_hash(text, "AgentSessionManager-v1.3.0-Setup.exe"), digest)
        self.assertIsNone(update._expected_hash(text, "AgentSessionManager-v1.3.0-Setup.exe.old"))

    def test_check_only_marks_exact_setup_plus_checksums_installable(self) -> None:
        payload = {
            "tag_name": "v9.0.0",
            "html_url": "https://github.com/devincii-io/agent-session-manager/releases/tag/v9.0.0",
            "assets": [
                {"name": "AgentSessionManager-v9.0.0-Setup.exe"},
                {"name": "SHA256SUMS.txt"},
            ],
        }
        with patch.object(update, "_release", return_value=payload), patch.object(update.os, "name", "nt"):
            result = update.check(force=True)
        self.assertTrue(result["update_available"])
        self.assertTrue(result["installable"])

        payload["assets"][0]["name"] = "AgentSessionManager-Setup.exe"
        with patch.object(update, "_release", return_value=payload), patch.object(update.os, "name", "nt"):
            result = update.check(force=True)
        self.assertFalse(result["installable"])

    def test_installer_size_must_match_release_metadata(self) -> None:
        blob = b"installer"
        payload = self._payload(blob, size=len(blob) + 1)
        digest = hashlib.sha256(blob).hexdigest()
        responses = [f"{digest}  AgentSessionManager-v9.0.0-Setup.exe\n".encode(), blob]
        with patch.object(update.os, "name", "nt"), patch.object(update, "_release", return_value=payload), patch.object(update, "_get", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "size does not match"):
                update.download_and_run()

    def test_checksum_mismatch_refuses_to_launch(self) -> None:
        blob = b"installer"
        payload = self._payload(blob)
        responses = [("0" * 64 + "  AgentSessionManager-v9.0.0-Setup.exe\n").encode(), blob]
        with patch.object(update.os, "name", "nt"), patch.object(update, "_release", return_value=payload), patch.object(update, "_get", side_effect=responses), patch.object(update.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "checksum"):
                update.download_and_run()
        popen.assert_not_called()

    def test_verified_installer_is_written_and_launched(self) -> None:
        blob = b"installer"
        payload = self._payload(blob)
        digest = hashlib.sha256(blob).hexdigest()
        responses = [f"{digest}  AgentSessionManager-v9.0.0-Setup.exe\n".encode(), blob]
        with patch.object(update.os, "name", "nt"), patch.object(update, "_release", return_value=payload), patch.object(update, "_get", side_effect=responses), patch.object(update.subprocess, "Popen") as popen:
            result = update.download_and_run()
        self.assertTrue(result["launched"])
        self.assertEqual(result["version"], "9.0.0")
        popen.assert_called_once()

    @staticmethod
    def _payload(blob: bytes, *, size: int | None = None) -> dict:
        return {
            "tag_name": "v9.0.0",
            "assets": [
                {
                    "name": "AgentSessionManager-v9.0.0-Setup.exe",
                    "size": len(blob) if size is None else size,
                    "browser_download_url": "https://github.com/devincii-io/setup.exe",
                },
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": "https://github.com/devincii-io/SHA256SUMS.txt",
                },
            ],
        }


if __name__ == "__main__":
    unittest.main()
