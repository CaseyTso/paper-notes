"""Smoke test: the repo is an installable package exposing a JSON CLI.

Red-light discipline: frozen after first red run; see Task 1 of the
implementation plan (2026-08-02_132942-obsidian-native-literature-system.md).
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CliSmokeTest(unittest.TestCase):
    def test_version_is_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", "version"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertEqual(payload["status"], "success")


if __name__ == "__main__":
    unittest.main()
