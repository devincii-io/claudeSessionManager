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

    def test_three_pane_detail_uses_container_responsiveness(self) -> None:
        css = (Path(__file__).resolve().parents[1] / "web" / "styles.css").read_text("utf-8")
        detail_rules = re.findall(r"\.detail-pane\s*\{([^}]+)\}", css)
        self.assertTrue(
            any(re.search(r"\bcontainer\s*:\s*detail\s*/\s*inline-size\s*;", rule) for rule in detail_rules),
            "The detail view must respond to its pane width, not only the outer window width",
        )
        self.assertRegex(css, r"@container\s+detail\s*\(max-width:\s*520px\)")
        self.assertRegex(css, r"@media\s*\(max-width:\s*860px\)")

    def test_desktop_window_launch_size_is_screen_bounded(self) -> None:
        app = (Path(__file__).resolve().parents[1] / "asm" / "app.py").read_text("utf-8")
        self.assertIn("availableGeometry()", app)
        self.assertNotIn("self.setMinimumSize(1040, 680)", app)

    def test_fullscreen_content_and_panes_are_resizable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "web" / "styles.css").read_text("utf-8")
        html = (root / "web" / "index.html").read_text("utf-8")
        js = (root / "web" / "app.js").read_text("utf-8")
        self.assertRegex(css, r"\.detail-inner\s*\{[^}]*max-width\s*:\s*none\s*;")
        self.assertIn("detail-inner overview-layout", js)
        self.assertEqual(html.count('role="separator"'), 2)
        self.assertIn("--rail-width", css)
        self.assertIn("--list-width", css)
        self.assertIn("function initPaneResizers()", js)
        self.assertIn('storage: "asm.railWidth"', js)
        self.assertIn('storage: "asm.listWidth"', js)
        self.assertIn("MIN_DETAIL_WIDTH = 420", js)


if __name__ == "__main__":
    unittest.main()
