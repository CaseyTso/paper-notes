"""Filesystem transaction helpers tests (Task 6).

Frozen after the first red run; do not weaken or delete assertions.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from paper_notes import fsops


class AtomicReplaceTest(unittest.TestCase):
    def test_atomic_replace_preserves_mode_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            t = td / "a.txt"
            t.write_text("old", encoding="utf-8")
            t.chmod(0o644)
            fsops.atomic_replace(t, "new content")
            self.assertEqual(t.read_text(encoding="utf-8"), "new content")
            self.assertEqual(t.stat().st_mode & 0o777, 0o644)
            self.assertEqual([x.name for x in td.iterdir()], ["a.txt"])

    def test_atomic_replace_new_file_default_mode(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            t = td / "new.md"
            fsops.atomic_replace(t, "# hi")
            self.assertEqual(t.read_text(encoding="utf-8"), "# hi")
            self.assertEqual(t.stat().st_mode & 0o777, 0o644)

    def test_atomic_replace_explicit_mode(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            t = td / "sec.txt"
            fsops.atomic_replace(t, "x", mode=0o600)
            self.assertEqual(t.stat().st_mode & 0o777, 0o600)


class StagingTest(unittest.TestCase):
    def test_cleanup_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = None
            try:
                with fsops.staging(root, "op-1") as d:
                    (d / "x.txt").write_text("x", encoding="utf-8")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            self.assertIsNotNone(d)
            self.assertFalse(d.exists())

    def test_cleanup_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with fsops.staging(root, "op-2") as d:
                (d / "x.txt").write_text("x", encoding="utf-8")
                self.assertTrue(d.is_dir())
            self.assertFalse(d.exists())

    def test_existing_staging_directory_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with fsops.staging(root, "op-x") as d:
                with self.assertRaises(fsops.OperationConflict):
                    fsops.staging(root, "op-x").__enter__()
            # after cleanup the same id can be reused
            with fsops.staging(root, "op-x") as d2:
                self.assertTrue(d2.is_dir())


class OperationIdTest(unittest.TestCase):
    def test_invalid_operation_ids_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for bad in (
                "",
                ".",
                "..",
                "../evil",
                "/abs/path",
                "a/b",
                "a\\b",
                "a..b",
                "import-sk-DO-NOT-PERSIST",
                "UPPER-case",
                "x" * 33,
            ):
                with self.assertRaises(ValueError):
                    fsops.begin_operation(root, bad)
            for good in ("op-1", "import_items", "a.b", "x" * 32, "0" * 32):
                op = fsops.begin_operation(root, good)
                self.assertTrue(op.directory.is_dir())
                fsops.commit(op)
                self.assertFalse(op.directory.exists())


class VaultBoundaryTest(unittest.TestCase):
    def test_stage_outside_vault_rejected(self):
        with tempfile.TemporaryDirectory() as vault_td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(vault_td) / "vault"
            root.mkdir()
            outside = Path(outside_td) / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            op = fsops.begin_operation(root, "op-b")
            with self.assertRaises(ValueError):
                fsops.stage_target(op, outside)
            inside = root / "in.md"
            fsops.stage_target(op, inside)  # vault-internal target is fine
            fsops.commit(op)

    def test_stage_vault_root_itself_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vault"
            root.mkdir()
            op = fsops.begin_operation(root, "op-c")
            with self.assertRaises(ValueError):
                fsops.stage_target(op, root)
            fsops.commit(op)


class StagedOperationTest(unittest.TestCase):
    def test_commit_applies_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-3")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "v2")
            conflicts = fsops.commit(op)
            self.assertEqual(conflicts, [])
            self.assertEqual(a.read_text(encoding="utf-8"), "v2")
            self.assertFalse(op.directory.exists())

    def test_rollback_restores_all_staged_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = []
            for i, name in enumerate(["a.md", "b.md", "c.md"]):
                p = root / name
                p.write_text(f"original-{i}", encoding="utf-8")
                files.append(p)
            op = fsops.begin_operation(root, "op-4")
            for p in files:
                fsops.stage_target(op, p)
            for i, p in enumerate(files):
                fsops.write_target(op, p, f"modified-{i}")
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            for i, p in enumerate(files):
                self.assertEqual(p.read_text(encoding="utf-8"), f"original-{i}")
            self.assertFalse(op.directory.exists())

    def test_rollback_after_partial_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            b = root / "b.md"
            b.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-5")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "changed")
            # failure before staging b: rollback must restore a only
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertEqual(a.read_text(encoding="utf-8"), "v1")
            self.assertEqual(b.read_text(encoding="utf-8"), "v1")

    def test_commit_never_overwrites_external_new_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new.md"  # does not exist at stage time
            op = fsops.begin_operation(root, "op-6")
            fsops.stage_target(op, n)
            n.write_text("external", encoding="utf-8")  # created by someone else
            conflicts = fsops.commit(op)
            self.assertEqual(conflicts, [n])
            self.assertEqual(n.read_text(encoding="utf-8"), "external")

    def test_rollback_restores_deleted_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-8")
            fsops.stage_target(op, a)
            fsops.delete_target(op, a)
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertEqual(a.read_text(encoding="utf-8"), "v1")

    def test_rollback_removes_managed_new_file_and_parents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new" / "deep" / "note.md"  # parents do not exist
            op = fsops.begin_operation(root, "op-9")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "# new")
            self.assertTrue(n.is_file())
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertFalse(n.exists())
            self.assertFalse(n.parent.exists())  # newly created parents removed
            self.assertFalse(n.parent.parent.exists())

    def test_rollback_keeps_external_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new.md"
            op = fsops.begin_operation(root, "op-a")
            fsops.stage_target(op, n)
            n.write_text("external", encoding="utf-8")  # not a managed write
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [n])
            self.assertEqual(n.read_text(encoding="utf-8"), "external")

    def test_rollback_never_overwrites_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-b2")
            fsops.stage_target(op, a)
            a.write_text("manual-edit", encoding="utf-8")  # manual edit, not managed
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "manual-edit")

    def test_rollback_after_managed_then_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-c2")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "v2-managed")
            a.write_text("v3-manual", encoding="utf-8")  # edited after managed write
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "v3-manual")

    def test_rollback_unchanged_target_no_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-d")
            fsops.stage_target(op, a)
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertEqual(a.read_text(encoding="utf-8"), "v1")

    def test_commit_keeps_managed_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new.md"
            op = fsops.begin_operation(root, "op-e")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "# created")
            conflicts = fsops.commit(op)
            self.assertEqual(conflicts, [])
            self.assertEqual(n.read_text(encoding="utf-8"), "# created")
            self.assertFalse(op.directory.exists())

    def test_stage_does_not_create_parent_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new" / "deep" / "note.md"
            op = fsops.begin_operation(root, "op-g")
            fsops.stage_target(op, n)
            self.assertFalse(n.parent.exists())
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertFalse(n.parent.exists())  # directory tree untouched

    def test_write_target_conflict_on_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-h")
            fsops.stage_target(op, a)
            a.write_text("manual", encoding="utf-8")  # manual edit before write
            with self.assertRaises(fsops.OperationConflict):
                fsops.write_target(op, a, "managed")
            self.assertEqual(a.read_text(encoding="utf-8"), "manual")

    def test_delete_target_conflict_on_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-i")
            fsops.stage_target(op, a)
            a.write_text("manual", encoding="utf-8")  # manual edit before delete
            with self.assertRaises(fsops.OperationConflict):
                fsops.delete_target(op, a)
            self.assertEqual(a.read_text(encoding="utf-8"), "manual")

    def test_commit_reports_conflict_after_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-j")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "v2")
            a.write_text("manual", encoding="utf-8")  # edited after managed write
            conflicts = fsops.commit(op)
            self.assertEqual(conflicts, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "manual")

    def test_rollback_reports_conflict_when_target_deleted_externally(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-k")
            fsops.stage_target(op, a)
            a.unlink()  # deleted externally after staging
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [a])
            self.assertFalse(a.exists())

    def test_symlink_escape_blocked_on_write(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            outside = Path(outside_td) / "escaped.txt"
            n = root / "a" / "note.md"  # "a" does not exist yet
            op = fsops.begin_operation(root, "op-l")
            fsops.stage_target(op, n)
            # parent directory replaced by a symlink pointing outside the vault
            os.symlink(outside_td, root / "a")
            with self.assertRaises(fsops.OperationConflict):
                fsops.write_target(op, n, "managed")
            self.assertFalse(outside.exists())  # nothing written outside

    def test_rollback_skips_escaped_target(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            outside = Path(outside_td) / "escaped.txt"
            n = root / "a" / "note.md"
            op = fsops.begin_operation(root, "op-m")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "managed")
            # replace the created parent directory with an escaping symlink
            n.unlink()
            (root / "a").rmdir()
            os.symlink(outside_td, root / "a")
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [n])
            self.assertFalse(outside.exists())  # outside file untouched

    def test_rollback_after_write_then_delete_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new" / "deep" / "note.md"
            op = fsops.begin_operation(root, "op-n")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "# created")
            fsops.delete_target(op, n)  # managed delete after managed write
            conflicts = fsops.rollback(op)  # must not raise FileNotFoundError
            self.assertEqual(conflicts, [])
            self.assertFalse(n.exists())
            self.assertFalse(n.parent.exists())  # created parents cleaned

    def test_rollback_after_direct_delete_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "new" / "deep" / "note.md"
            op = fsops.begin_operation(root, "op-o")
            fsops.stage_target(op, n)
            fsops.delete_target(op, n)  # delete a file that never existed
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [])
            self.assertFalse(n.exists())
            self.assertFalse(n.parent.exists())

    def test_commit_conflict_when_target_replaced_by_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "note.md"
            op = fsops.begin_operation(root, "op-p")
            fsops.stage_target(op, n)
            n.mkdir()  # external: path replaced by a directory
            conflicts = fsops.commit(op)  # must not raise IsADirectoryError
            self.assertEqual(conflicts, [n])
            self.assertTrue(n.is_dir())

    def test_rollback_conflict_when_target_replaced_by_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = root / "note.md"
            op = fsops.begin_operation(root, "op-q")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "# managed")
            n.unlink()
            n.mkdir()  # external: path replaced by a directory
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [n])
            self.assertTrue(n.is_dir())  # directory preserved untouched

    def test_rollback_conflict_when_target_is_symlink(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            outside = Path(outside_td) / "real.txt"
            outside.write_text("x", encoding="utf-8")
            n = root / "note.md"
            op = fsops.begin_operation(root, "op-r")
            fsops.stage_target(op, n)
            fsops.write_target(op, n, "# managed")
            n.unlink()
            os.symlink(outside, n)  # external: replaced by a symlink
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [n])
            self.assertTrue(n.is_symlink())  # symlink preserved, not followed

    def test_rollback_noop_preserves_inode_mode_mtime_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            a.chmod(0o640)
            op = fsops.begin_operation(root, "op-s")
            fsops.stage_target(op, a)
            before = a.stat()
            conflicts = fsops.rollback(op)  # never written: must be a no-op
            self.assertEqual(conflicts, [])
            after = a.stat()
            self.assertEqual(a.read_text(encoding="utf-8"), "v1")
            self.assertEqual(after.st_ino, before.st_ino)
            self.assertEqual(after.st_mode, before.st_mode)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_write_target_conflict_after_chmod(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            a.chmod(0o644)
            op = fsops.begin_operation(root, "op-t")
            fsops.stage_target(op, a)
            a.chmod(0o600)  # external chmod after staging
            with self.assertRaises(fsops.OperationConflict):
                fsops.write_target(op, a, "v2")
            self.assertEqual(a.read_text(encoding="utf-8"), "v1")
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)

    def test_rollback_conflict_after_chmod_after_managed_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-u")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "v2")
            a.chmod(0o600)  # external chmod after managed write
            conflicts = fsops.rollback(op)
            self.assertEqual(conflicts, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "v2")  # managed content kept
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)  # new mode kept

    def test_commit_conflict_after_chmod_after_managed_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-v")
            fsops.stage_target(op, a)
            fsops.write_target(op, a, "v2")
            a.chmod(0o600)  # external chmod after managed write
            conflicts = fsops.commit(op)
            self.assertEqual(conflicts, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "v2")
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)

    def test_stage_conflict_when_source_modified_during_copy(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-w")
            real_copy2 = fsops.shutil.copy2

            def racing_copy2(src, dst):
                real_copy2(src, dst)
                # manual edit lands right after the backup copy completed
                Path(src).write_text("manual-during-copy", encoding="utf-8")

            with mock.patch("paper_notes.fsops.shutil.copy2", side_effect=racing_copy2):
                with self.assertRaises(fsops.OperationConflict):
                    fsops.stage_target(op, a)
            self.assertEqual(a.read_text(encoding="utf-8"), "manual-during-copy")
            self.assertEqual(list(op.directory.iterdir()), [])  # backup removed
            fsops.commit(op)

    def test_write_conflict_when_target_modified_during_temp_write(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            a.write_text("v1", encoding="utf-8")
            op = fsops.begin_operation(root, "op-x2")
            fsops.stage_target(op, a)
            real_fp = fsops._file_fingerprint
            counter = {"n": 0}

            def racing_fp(path):
                counter["n"] += 1
                if counter["n"] == 2 and path == a:
                    # manual edit lands after the pre-check, before os.replace
                    a.write_text("manual-during-write", encoding="utf-8")
                return real_fp(path)

            with mock.patch(
                "paper_notes.fsops._file_fingerprint", side_effect=racing_fp
            ):
                with self.assertRaises(fsops.OperationConflict):
                    fsops.write_target(op, a, "v2")
            self.assertEqual(a.read_text(encoding="utf-8"), "manual-during-write")
            # temp file removed, no residue
            self.assertFalse(
                any(x.name.endswith(".tmp") for x in root.iterdir())
            )
            fsops.commit(op)

    def test_write_target_requires_staged_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.md"
            op = fsops.begin_operation(root, "op-f")
            with self.assertRaises(RuntimeError):
                fsops.write_target(op, a, "x")
            fsops.commit(op)


if __name__ == "__main__":
    unittest.main()
