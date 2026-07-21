from __future__ import annotations

import re
import unittest
from pathlib import Path


class FrontendConventionTests(unittest.TestCase):
    def test_native_browser_confirm_is_never_used(self) -> None:
        web = Path(__file__).resolve().parents[1] / "web"
        native_confirm = re.compile(r"\b(?:window\s*\.\s*)?confirm\s*\(")
        offenders = []
        for path in sorted(web.rglob("*")):
            if path.suffix.lower() not in {".js", ".html"}:
                continue
            for line_number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if native_confirm.search(line):
                    offenders.append(f"{path.relative_to(web.parent)}:{line_number}")
        self.assertEqual(
            offenders,
            [],
            "Use the app's custom modal(...) confirmation flow instead of window.confirm: "
            + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
