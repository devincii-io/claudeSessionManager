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

    def test_bar_fill_accepts_calculated_width(self) -> None:
        css = (Path(__file__).resolve().parents[1] / "web" / "styles.css").read_text("utf-8")
        rules = re.findall(r"\.bar-fill\s*\{([^}]+)\}", css)
        self.assertTrue(rules, "Missing shared .bar-fill style")
        self.assertTrue(
            any(re.search(r"\bdisplay\s*:\s*(?:block|inline-block|flex)\s*;", rule) for rule in rules),
            "The bar fill is a span; without non-inline display its percentage width is ignored",
        )


if __name__ == "__main__":
    unittest.main()
