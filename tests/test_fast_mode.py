from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.fast_mode import resolve_request_fast


class ResolveRequestFastTests(unittest.TestCase):
    def test_supported_modes_pass_through_silently(self) -> None:
        warnings: list[str] = []
        for mode in ("auto", "off"):
            self.assertEqual(resolve_request_fast({"fast": mode}, warn=warnings.append), mode)
        self.assertEqual(resolve_request_fast({}, warn=warnings.append), "auto")
        self.assertEqual(resolve_request_fast(None, warn=warnings.append), "auto")
        self.assertEqual(warnings, [])

    def test_retired_on_runs_as_auto_and_says_so(self) -> None:
        # `on` only ever differed from `auto` by failing a run whose subtitles
        # did not fit one window -- after recognition had already been paid for.
        warnings: list[str] = []
        self.assertEqual(resolve_request_fast({"fast": "on"}, warn=warnings.append), "auto")
        self.assertEqual(len(warnings), 1)

    def test_unknown_mode_falls_back_to_auto(self) -> None:
        warnings: list[str] = []
        self.assertEqual(resolve_request_fast({"fast": "turbo"}, warn=warnings.append), "auto")
        self.assertEqual(len(warnings), 1)
        self.assertIn("turbo", warnings[0])


if __name__ == "__main__":
    unittest.main()
