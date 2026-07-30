"""Regression checks that the 1.5.12 release history remains documented."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestVersion1512History(unittest.TestCase):
    def test_previous_release_history_is_preserved(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release_notes = (ROOT / "docs" / "リリースノート.md").read_text(encoding="utf-8")
        self.assertIn("## 1.5.12 (42)", changelog)
        self.assertIn("## バージョン 1.5.12", release_notes)


if __name__ == "__main__":
    unittest.main()
