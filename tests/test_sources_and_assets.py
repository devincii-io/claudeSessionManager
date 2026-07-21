from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asm import sources
from asm.scanner import Scanner


class SourceDiscoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        sources.discover_sources.cache_clear()
        sources.resolve_source.cache_clear()

    def test_wsl_names_are_discovered_without_starting_distributions(self) -> None:
        sources.discover_sources.cache_clear()
        sources.resolve_source.cache_clear()
        listed = "Ubuntu-24.04\r\nDebian\r\n".encode("utf-16-le")
        with patch.object(sources.platform, "system", return_value="Windows"), \
                patch.object(sources, "_run", return_value=SimpleNamespace(returncode=0, stdout=listed)) as run:
            found = sources.discover_sources()
        self.assertEqual([item.id for item in found], ["windows", "wsl:Ubuntu-24.04", "wsl:Debian"])
        self.assertFalse(found[1].resolved)
        run.assert_called_once_with(["wsl.exe", "-l", "-q"])


class StorageAssetTests(unittest.TestCase):
    def test_current_uploads_are_visible_and_recent_orphans_are_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".claude"
            upload = home / "uploads" / "orphan-session"
            upload.mkdir(parents=True)
            image = upload / "screen shot.png"
            image.write_bytes(b"png")
            scanner = Scanner(home, cache_namespace="test", temp_roots=[])
            inventory = scanner.storage_assets()
            images = scanner.get_images("orphan-session")
        self.assertEqual(inventory["items"][0]["kind"], "uploads")
        self.assertTrue(inventory["items"][0]["orphaned"])
        self.assertTrue(inventory["items"][0]["protected"])
        self.assertEqual(images[0]["name"], "screen shot.png")

    def test_old_orphan_upload_is_cleanable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".claude"
            upload = home / "uploads" / "old-orphan"
            upload.mkdir(parents=True)
            image = upload / "image.webp"
            image.write_bytes(b"data")
            old = time.time() - 601
            os.utime(image, (old, old)); os.utime(upload, (old, old))
            scanner = Scanner(home, cache_namespace="test-old", temp_roots=[])
            item = scanner.storage_assets()["items"][0]
        self.assertFalse(item["protected"])


if __name__ == "__main__":
    unittest.main()
