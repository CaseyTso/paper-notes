"""Workspace write-lock tests (Task 6).

Frozen after the first red run; do not weaken or delete assertions.
"""

import copy
import gc
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_notes.locking import (
    LockConflict,
    LockError,
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


class LockSymlinkSecurityTests(unittest.TestCase):
    def test_lock_dir_symlink_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            lock_dir_symlink = vault / ".paper-notes"
            lock_dir_symlink.symlink_to(outside)

            with self.assertRaises(LockError):
                acquire_lock(vault, "import")

            # Outside directory must have zero writes
            self.assertEqual(list(outside.iterdir()), [])

    def test_lock_file_symlink_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            sentinel = outside / "sentinel.lock"
            sentinel.write_text("outside secret", encoding="utf-8")

            lock_dir = vault / ".paper-notes"
            lock_dir.mkdir(parents=True)
            lock_file_symlink = lock_dir / "write.lock"
            lock_file_symlink.symlink_to(sentinel)

            with self.assertRaises((LockConflict, LockError)):
                acquire_lock(vault, "import")

            # Outside file must remain untouched
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside secret")

    def test_release_handle_after_lock_dir_renamed_and_symlinked_preserves_victim(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)

            handle = acquire_lock(vault, "import")
            self.assertTrue(handle.is_file())

            # Outside victim directory has a write.lock file
            victim_lock = outside / "write.lock"
            victim_lock.write_text("VICTIM_LOCK_CONTENT", encoding="utf-8")

            # Rename original .paper-notes directory
            orig_lock_dir = vault / ".paper-notes"
            renamed_lock_dir = vault / ".paper-notes.renamed"
            orig_lock_dir.rename(renamed_lock_dir)

            # Replace original path with symlink to outside
            orig_lock_dir.symlink_to(outside)

            # Release lock via handle
            release_lock(handle)

            # Assert:
            # 1. Victim lock in outside directory was NOT deleted
            self.assertTrue(victim_lock.exists())
            self.assertEqual(victim_lock.read_text(encoding="utf-8"), "VICTIM_LOCK_CONTENT")
            # 2. Original lock in renamed directory WAS deleted
            self.assertFalse((renamed_lock_dir / "write.lock").exists())

    def test_legacy_release_symlink_does_not_delete_victim(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)

            victim_lock = outside / "write.lock"
            victim_lock.write_text("VICTIM_LEGACY_CONTENT", encoding="utf-8")

            # Symlink .paper-notes to outside
            (vault / ".paper-notes").symlink_to(outside)

            # Legacy release passing lock_path(vault)
            release_lock(lock_path(vault))

            # Outside victim must remain untouched
            self.assertTrue(victim_lock.exists())
            self.assertEqual(victim_lock.read_text(encoding="utf-8"), "VICTIM_LEGACY_CONTENT")

    def test_legacy_release_vault_root_symlink_swap_preserves_victim(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault) / "real_vault"
            vault.mkdir()
            outside = Path(td_out)

            # Outside has victim .paper-notes/write.lock
            outside_lock_dir = outside / ".paper-notes"
            outside_lock_dir.mkdir()
            victim_lock = outside_lock_dir / "write.lock"
            victim_lock.write_text("VICTIM_ROOT_SWAP_CONTENT", encoding="utf-8")

            # Vault has real lock
            real_lock = acquire_lock(vault, "import")
            self.assertTrue(real_lock.is_file())

            # Rename real vault and replace original path with symlink to outside
            vault_renamed = Path(td_vault) / "real_vault_renamed"
            vault.rename(vault_renamed)
            vault.symlink_to(outside)

            # Attempt legacy release passing lock_path(vault)
            release_lock(lock_path(vault))

            # Outside victim lock MUST be preserved and untouched!
            self.assertTrue(victim_lock.exists())
            self.assertEqual(victim_lock.read_text(encoding="utf-8"), "VICTIM_ROOT_SWAP_CONTENT")

    def test_lock_handle_copy_deepcopy_fspath_gc_pickle(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            handle = acquire_lock(vault, "import")

            # 1. os.fspath and Path conversion
            self.assertEqual(os.fspath(handle), str(lock_path(vault)))
            self.assertEqual(Path(handle), lock_path(vault))

            # 2. copy and deepcopy return same capability/lease
            self.assertIs(copy.copy(handle), handle)
            self.assertIs(copy.deepcopy(handle), handle)

            # 3. pickle is prohibited
            with self.assertRaises(TypeError):
                pickle.dumps(handle)

            # 4. Path-like query methods
            self.assertEqual(handle.name, "write.lock")
            self.assertEqual(handle.parent, vault / ".paper-notes")
            self.assertTrue(handle.exists())
            self.assertTrue(handle.is_file())
            self.assertGreater(handle.stat().st_size, 0)
            self.assertIn("import", handle.read_text(encoding="utf-8"))

            # 5. GC closes fd without raising and without deleting lock file
            handle2 = acquire_lock(vault, "import", root_fd=None) if False else None
            # Test GC on a dummy handle
            ld_fd = os.open(str(vault / ".paper-notes"), os.O_RDONLY | os.O_DIRECTORY)
            st = os.stat("write.lock", dir_fd=ld_fd)
            from paper_notes.locking import LockHandle
            h_gc = LockHandle(lock_path(vault), lock_dir_fd=ld_fd, dev=st.st_dev, ino=st.st_ino)
            del h_gc
            gc.collect()
            # Lock file must still exist
            self.assertTrue(handle.exists())

            # 6. Normal release
            release_lock(handle)
            self.assertFalse(handle.exists())

    def test_release_retry_on_temporary_unlink_failure_and_inode_protection(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            handle = acquire_lock(vault, "import")

            # 1. Injected temporary unlink failure
            orig_unlink = os.unlink
            failed_once = False

            def flaky_unlink(path, *args, **kwargs):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise OSError(16, "Device or resource busy")
                return orig_unlink(path, *args, **kwargs)

            with patch("os.unlink", side_effect=flaky_unlink):
                # First attempt raises LockError
                with self.assertRaises(LockError):
                    handle.release()

                # Handle retains capability and is not marked released
                self.assertFalse(handle._released)
                self.assertIsNotNone(handle.lock_dir_fd)

            # Second attempt succeeds without mock
            handle.release()
            self.assertTrue(handle._released)
            self.assertIsNone(handle.lock_dir_fd)
            self.assertFalse(handle.exists())

            # Third attempt is idempotent no-op
            handle.release()
            self.assertTrue(handle._released)

            # 2. Inode replaced protection: foreign inode is not deleted
            handle_new = acquire_lock(vault, "import")
            # Replace lock file with a new file (different inode)
            foreign_file = vault / ".paper-notes" / "write.lock.new"
            foreign_file.write_text("FOREIGN_LOCK", encoding="utf-8")
            os.replace(foreign_file, vault / ".paper-notes" / "write.lock")

            # Releasing old handle must NOT delete the new lock file!
            handle_new.release()
            self.assertTrue((vault / ".paper-notes" / "write.lock").exists())
            self.assertEqual((vault / ".paper-notes" / "write.lock").read_text(encoding="utf-8"), "FOREIGN_LOCK")
            # Cleanup foreign lock
            (vault / ".paper-notes" / "write.lock").unlink()

    def test_public_release_lock_retries_transient_failures_single_and_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)

            # Case A: 1 transient unlink failure -> release_lock succeeds without error to caller
            handle1 = acquire_lock(vault, "import")
            orig_unlink = os.unlink
            failed_once = False

            def unlink_fail_once(p, *args, **kwargs):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise OSError(16, "EBUSY")
                return orig_unlink(p, *args, **kwargs)

            with patch("os.unlink", side_effect=unlink_fail_once):
                release_lock(handle1)  # must not raise
            self.assertTrue(handle1._released)
            self.assertFalse(handle1.exists())

            # Case B: 3 transient unlink failures -> release_lock succeeds on 4th attempt
            handle2 = acquire_lock(vault, "import")
            fail_count = 0

            def unlink_fail_3_times(p, *args, **kwargs):
                nonlocal fail_count
                if fail_count < 3:
                    fail_count += 1
                    raise OSError(16, "EBUSY")
                return orig_unlink(p, *args, **kwargs)

            with patch("os.unlink", side_effect=unlink_fail_3_times):
                release_lock(handle2)  # must not raise
            self.assertTrue(handle2._released)
            self.assertFalse(handle2.exists())

            # Case C: Retries exhausted (5 failures) -> raises LockError, capability preserved for retry
            handle3 = acquire_lock(vault, "import")

            with patch("os.unlink", side_effect=OSError(16, "EBUSY")):
                with self.assertRaises(LockError):
                    release_lock(handle3)

            # Handle capability is preserved
            self.assertFalse(handle3._released)
            self.assertIsNotNone(handle3.lock_dir_fd)
            self.assertTrue(handle3.exists())

            # Subsequent call without error succeeds cleanly
            release_lock(handle3)
            self.assertTrue(handle3._released)
            self.assertFalse(handle3.exists())

    def test_lock_handle_equality_and_hash_contract(self):
        from paper_notes.locking import LockHandle

        p1 = Path("/tmp/vault/.paper-notes/write.lock")
        p2 = Path("/tmp/vault/.paper-notes/write.lock")
        p_diff = Path("/tmp/other/.paper-notes/write.lock")

        h1 = LockHandle(p1, dev=100, ino=200)
        h2 = LockHandle(p2, dev=100, ino=200)
        h3 = LockHandle(p1, dev=100, ino=201)  # different ino
        h4 = LockHandle(p1, dev=101, ino=200)  # different dev
        h5 = LockHandle(p_diff, dev=100, ino=200)  # different path

        # 1. Reflexive & symmetric & transitive equality with same type
        self.assertEqual(h1, h1)
        self.assertEqual(h1, h2)
        self.assertEqual(h2, h1)
        self.assertNotEqual(h1, h3)
        self.assertNotEqual(h1, h4)
        self.assertNotEqual(h1, h5)

        # 2. Never equal to Path or str (returns False / NotImplemented)
        self.assertFalse(h1 == p1)
        self.assertFalse(p1 == h1)
        self.assertFalse(h1 == str(p1))
        self.assertFalse(str(p1) == h1)
        self.assertFalse(h1 == 42)

        # 3. Hash consistency with equality
        self.assertEqual(hash(h1), hash(h2))
        self.assertNotEqual(hash(h1), hash(h3))
        self.assertNotEqual(hash(h1), hash(h4))
        self.assertNotEqual(hash(h1), hash(h5))

        # 4. Set membership contract
        s = {h1}
        self.assertIn(h2, s)
        self.assertNotIn(h3, s)
        self.assertNotIn(h4, s)
        self.assertNotIn(h5, s)
        self.assertNotIn(p1, s)
        self.assertNotIn(str(p1), s)

        # 5. Dict key lookup contract
        d = {h1: "lease_value"}
        self.assertEqual(d[h2], "lease_value")
        self.assertNotIn(h3, d)
        self.assertNotIn(p1, d)
        self.assertNotIn(str(p1), d)


if __name__ == "__main__":
    unittest.main()
