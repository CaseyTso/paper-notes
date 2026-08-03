"""Workspace write-lock tests (Task 6).

Frozen after the first red run; do not weaken or delete assertions.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper_notes.locking import (
    LockConflict,
    StaleLockError,
    acquire_lock,
    lock_path,
    release_lock,
)

REPO = Path(__file__).resolve().parents[1]


class LockRoundTripTest(unittest.TestCase):
    def test_acquire_release_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = acquire_lock(root, "import")
            self.assertTrue(p.is_file())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            release_lock(p)
            self.assertFalse(p.exists())
            # re-acquire after release works
            acquire_lock(root, "import")
            release_lock(lock_path(root))

    def test_second_acquire_same_process_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = acquire_lock(root, "import")
            with self.assertRaises(LockConflict):
                acquire_lock(root, "show")
            release_lock(p)

    def test_metadata_has_pid_time_operation_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = acquire_lock(root, "import_items")
            meta = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(meta["pid"], os.getpid())
            self.assertEqual(meta["operation"], "import_items")
            self.assertTrue(meta["started_at"])
            self.assertTrue(
                set(meta) <= {"pid", "started_at", "operation", "operation_id", "host"}
            )
            # operation_id is a generated, non-sensitive unique id
            self.assertTrue(
                re.fullmatch(r"[0-9a-f]{32}", meta["operation_id"]),
                meta["operation_id"],
            )
            self.assertNotIn("secret", json.dumps(meta).lower())
            release_lock(p)

    def test_operation_must_be_whitelisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for bad in (
                "",
                "import-sk-DO-NOT-PERSIST-123",
                "sk-abc123def456ghi",
                "UPPER",
                "a/b",
                "..",
                "a..b",
                "arbitrary text",
            ):
                with self.assertRaises(ValueError) as ctx:
                    acquire_lock(root, bad)
                # error message must not echo the caller's text back
                if bad:
                    self.assertNotIn(str(bad), str(ctx.exception))
            for good in ("import", "import_items", "create_item", "reconcile1", "show"):
                p = acquire_lock(root, good)
                release_lock(p)

    def test_lock_removed_when_metadata_write_fails(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch(
                "paper_notes.locking.json.dump",
                side_effect=RuntimeError("injected dump failure"),
            ):
                with self.assertRaises(RuntimeError):
                    acquire_lock(root, "import")
            # no poisoned lock left behind; later writes work
            self.assertFalse(lock_path(root).exists())
            p = acquire_lock(root, "import")
            release_lock(p)


class StaleLockTest(unittest.TestCase):
    def test_stale_lock_detected_never_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = lock_path(root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "pid": 99999999,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "operation": "ghost",
                        "host": "ghost-host",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(StaleLockError):
                acquire_lock(root, "import")
            self.assertTrue(p.exists())  # never silently removed


class CrossProcessLockTest(unittest.TestCase):
    def test_second_process_receives_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = (
                "import sys, time\n"
                f"sys.path.insert(0, {str(REPO)!r})\n"
                "from pathlib import Path\n"
                "from paper_notes.locking import acquire_lock, release_lock\n"
                f"p = acquire_lock(Path({str(root)!r}), 'import')\n"
                "print('LOCKED', flush=True)\n"
                "time.sleep(3)\n"
                "release_lock(p)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                line = proc.stdout.readline()
                self.assertEqual(line.strip(), "LOCKED")
                with self.assertRaises(LockConflict):
                    acquire_lock(root, "show")
            finally:
                proc.wait(timeout=15)
                if proc.stdout is not None:
                    proc.stdout.close()
            # after the child releases, the parent can acquire
            p = acquire_lock(root, "show")
            release_lock(p)


if __name__ == "__main__":
    unittest.main()
