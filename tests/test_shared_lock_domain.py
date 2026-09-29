"""Shared single-lock-domain tests for all top-level vault mutations.

Phase6a-2: every managed writer (item/card/moc/mineru/index rebuild/
migration apply/rollback/delete) uses the SAME
``<vault>/.paper-notes/write.lock``. The operation name in the lock
metadata is bookkeeping only, NOT a separate lock domain — holding the
lock for any operation conflicts with every other managed mutation.

Covered here:

- Direct hold of an arbitrary operation's lock conflicts with MOC
  create / index rebuild / migration apply / migration rollback, each
  with ZERO corresponding writes.
- Reverse direction: while a moc create / rebuild / migration holds the
  lock, a card create / item mutation conflicts.
- Parameterized: operation metadata never creates multiple lock
  domains (same lock file, cross-operation mutual exclusion).
- release failure while an active primary exception is in flight does
  not hide it (locking contract, exercised through mocs/csl).
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_notes import cards, csl, mocs
from paper_notes.locking import acquire_lock, lock_path, release_lock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PAPER_ID = "550e8400-e29b-41d4-a716-446655440000"
KEY = "smithExample2026"
SELECTION = "## Figure 1\n\n- 解读\n"


def write_paper(root: Path, key: str = KEY) -> Path:
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        "schema_version: 1\n"
        f"paper_id: {PAPER_ID}\n"
        f"citation_key: {key}\n"
        "item_type: article-journal\n"
        "title: An example paper\n"
        "authors:\n- family: Smith\n  given: John\n"
        "publication_date: 2026-05-01\n"
        "pdf_status: missing\n"
        "reading_status: unread\n"
    )
    (d / f"{key}.md").write_text(f"---\n{fm}---\n# body\n", encoding="utf-8")
    return d


def _vault_tree_snapshot(root: Path) -> set[str]:
    """Relative paths of everything except the .paper-notes lock dir."""
    out: set[str] = set()
    for p in root.rglob("*"):
        rel = str(p.relative_to(root))
        if rel.startswith(".paper-notes"):
            continue
        out.add(rel)
    return out


class SharedLockDomainTest(unittest.TestCase):
    """Hold one operation's lock; every other mutation conflicts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_paper(self.root)
        (self.root / "05 Literature" / "MOCs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # forward direction: foreign lock held → operation conflicts, zero writes
    # ------------------------------------------------------------------

    def test_moc_create_conflicts_on_foreign_lock_zero_writes(self):
        from paper_notes import locking

        holder = acquire_lock(self.root, "import")
        try:
            snapshot = _vault_tree_snapshot(self.root)
            with self.assertRaises(mocs.MocConflict):
                mocs.create_moc(self.root, title="新主题")
            self.assertEqual(
                _vault_tree_snapshot(self.root), snapshot,
                "moc create under a foreign lock must be zero-write "
                "(no MOCs mkdir, no temp, no note)",
            )
        finally:
            release_lock(holder)

    def test_moc_create_conflict_does_not_even_create_mocs_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            holder = acquire_lock(root, "import")
            try:
                with self.assertRaises(mocs.MocConflict):
                    mocs.create_moc(root, title="主题")
                self.assertFalse(
                    (root / "05 Literature").exists(),
                    "conflict must not create the literature root",
                )
            finally:
                release_lock(holder)

    def test_index_rebuild_conflicts_on_foreign_lock_zero_writes(self):
        from paper_notes.csl import IndexConflict

        holder = acquire_lock(self.root, "import")
        try:
            notes = self.root / ".paper-notes"
            before = sorted(p.name for p in notes.iterdir()) if notes.is_dir() else []
            with self.assertRaises(IndexConflict):
                csl.rebuild_indexes(self.root)
            after = sorted(p.name for p in notes.iterdir()) if notes.is_dir() else []
            self.assertEqual(before, after)
            self.assertFalse(
                (notes / "library.json").exists(),
                "conflict must not publish library.json",
            )
            self.assertFalse(
                (notes / "citation-aliases.json").exists()
            )
        finally:
            release_lock(holder)

    def test_rebuild_observes_index_inside_the_lock(self):
        """Observation ordering: build_index must run INSIDE the shared
        lock — while the rebuild's build_index is in flight, another
        operation's lock acquisition must conflict, proving the
        observation and the publications share one lock hold."""
        from paper_notes import locking
        from paper_notes.locking import LockConflict

        real_build_index = csl.build_index

        def build_index_under_lock(root):
            # running inside the rebuild's lock hold: any other
            # operation must be refused with LockConflict
            with self.assertRaises(LockConflict):
                locking.acquire_lock(root, "import")
            return real_build_index(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            with patch.object(
                csl, "build_index", side_effect=build_index_under_lock
            ):
                result = csl.rebuild_indexes(root)
            self.assertEqual(result.papers, 1)
            self.assertTrue(
                (root / ".paper-notes" / "library.json").is_file()
            )

    def test_rebuild_build_index_exception_releases_lock_and_publishes_nothing(self):
        """If build_index raises inside the lock, the lock is released
        in finally (another process can then acquire it) and zero index
        writes happened."""
        import os

        from paper_notes import locking

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            notes = root / ".paper-notes"
            before = sorted(p.name for p in notes.iterdir()) if notes.is_dir() else []

            def boom(root):
                raise RuntimeError("injected index failure")

            with patch.object(csl, "build_index", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    csl.rebuild_indexes(root)

            # zero index writes
            after = sorted(p.name for p in notes.iterdir()) if notes.is_dir() else []
            self.assertEqual(before, after)
            self.assertFalse((notes / "library.json").exists())
            self.assertFalse((notes / "citation-aliases.json").exists())

            # lock released: another operation can acquire it now
            holder = locking.acquire_lock(root, "import")
            release_lock(holder)
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_migration_apply_conflicts_on_foreign_lock_zero_writes(self):
        from paper_notes.migration import transaction

        fixture = REPO / "tests" / "fixtures" / "legacy_vault"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = tmp / "vault"
            shutil.copytree(fixture, vault)
            state = tmp / "state"
            plan = transaction.__dict__
            from paper_notes.migration import build_migration_plan

            plan = build_migration_plan(vault, state_root=state)
            holder = acquire_lock(vault, "import")
            try:
                snapshot = _vault_tree_snapshot(vault)
                with self.assertRaises(transaction.MigrationConflict):
                    transaction.apply_migration(
                        plan.run_id,
                        plan.confirmation_token,
                        vault_root=vault,
                        state_root=state,
                    )
                self.assertEqual(
                    _vault_tree_snapshot(vault), snapshot,
                    "migration apply under a foreign lock must be zero-vault-write",
                )
                # zero journal writes too: no journal file created
                journal_path = state / plan.run_id / "journal.json"
                self.assertFalse(
                    journal_path.exists(),
                    "migration conflict must not write the journal",
                )
            finally:
                release_lock(holder)

    def test_migration_rollback_conflicts_on_foreign_lock_zero_writes(self):
        from paper_notes.migration import build_migration_plan
        from paper_notes.migration import transaction

        fixture = REPO / "tests" / "fixtures" / "legacy_vault"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = tmp / "vault"
            shutil.copytree(fixture, vault)
            state = tmp / "state"
            plan = build_migration_plan(vault, state_root=state)
            # apply first (no lock contention)
            transaction.apply_migration(
                plan.run_id,
                plan.confirmation_token,
                vault_root=vault,
                state_root=state,
            )
            journal_before = (state / plan.run_id / "journal.json").read_bytes()
            holder = acquire_lock(vault, "import")
            try:
                snapshot = _vault_tree_snapshot(vault)
                with self.assertRaises(transaction.MigrationConflict):
                    transaction.rollback_migration(
                        plan.run_id, vault_root=vault, state_root=state
                    )
                self.assertEqual(
                    _vault_tree_snapshot(vault), snapshot,
                    "migration rollback under a foreign lock must be zero-vault-write",
                )
                self.assertEqual(
                    (state / plan.run_id / "journal.json").read_bytes(),
                    journal_before,
                    "migration rollback conflict must not rewrite the journal",
                )
            finally:
                release_lock(holder)

    def test_migration_verify_stays_lock_free(self):
        from paper_notes.migration import build_migration_plan
        from paper_notes.migration import transaction

        fixture = REPO / "tests" / "fixtures" / "legacy_vault"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = tmp / "vault"
            shutil.copytree(fixture, vault)
            state = tmp / "state"
            plan = build_migration_plan(vault, state_root=state)
            transaction.apply_migration(
                plan.run_id,
                plan.confirmation_token,
                vault_root=vault,
                state_root=state,
            )
            holder = acquire_lock(vault, "import")
            try:
                # read-only verify must NOT conflict on the write lock
                report = transaction.verify_migration(
                    plan.run_id, vault_root=vault, state_root=state
                )
                self.assertEqual(report.status, "ok")
            finally:
                release_lock(holder)

    # ------------------------------------------------------------------
    # reverse direction: these operations hold the lock → card/item conflicts
    # ------------------------------------------------------------------

    def test_card_create_conflicts_while_moc_lock_held(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            holder = acquire_lock(root, "create_moc")
            try:
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        root,
                        key=KEY,
                        title="T",
                        selection=SELECTION,
                    )
            finally:
                release_lock(holder)

    def test_card_create_conflicts_while_rebuild_lock_held(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            holder = acquire_lock(root, "rebuild_indexes")
            try:
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        root,
                        key=KEY,
                        title="T",
                        selection=SELECTION,
                    )
            finally:
                release_lock(holder)

    def test_card_create_conflicts_while_migrate_lock_held(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            holder = acquire_lock(root, "migrate")
            try:
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        root,
                        key=KEY,
                        title="T",
                        selection=SELECTION,
                    )
            finally:
                release_lock(holder)

    def test_moc_create_conflicts_while_rebuild_lock_held(self):
        holder = acquire_lock(self.root, "rebuild_indexes")
        try:
            with self.assertRaises(mocs.MocConflict):
                mocs.create_moc(self.root, title="互斥主题")
        finally:
            release_lock(holder)

    def test_index_rebuild_conflicts_while_migrate_lock_held(self):
        from paper_notes.csl import IndexConflict

        holder = acquire_lock(self.root, "migrate")
        try:
            with self.assertRaises(IndexConflict):
                csl.rebuild_indexes(self.root)
        finally:
            release_lock(holder)

    # ------------------------------------------------------------------
    # parameterized: operation metadata is NOT a lock domain
    # ------------------------------------------------------------------

    def test_operation_names_share_one_lock_domain(self):
        """Every managed operation token locks the same single file:
        acquiring op X blocks op Y — parameterized across the full set."""
        from paper_notes.locking import OPERATIONS

        # Operations that are pure write-path bookkeeping tokens.
        tokens = sorted(OPERATIONS)
        self.assertGreaterEqual(len(tokens), 10)
        for held_op in tokens:
            with self.subTest(held_op=held_op):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    holder = acquire_lock(root, held_op)
                    try:
                        for other in tokens:
                            if other == held_op:
                                continue
                            with self.assertRaises(Exception) as ctx:
                                acquire_lock(root, other)
                            # must be LockConflict, not stale/other
                            from paper_notes.locking import LockConflict

                            self.assertIsInstance(ctx.exception, LockConflict)
                    finally:
                        release_lock(holder)

    def test_same_lock_file_for_all_operations(self):
        """lock_path takes no operation argument: exactly one lock node,
        independent of the operation token."""
        import inspect

        from paper_notes.locking import OPERATIONS, lock_path

        self.assertEqual(
            list(inspect.signature(lock_path).parameters), ["vault_root"],
            "lock_path must be operation-independent",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for op in sorted(OPERATIONS):
                holder = acquire_lock(root, op)
                self.assertTrue((root / ".paper-notes" / "write.lock").is_file())
                release_lock(holder)
                self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_moc_release_failure_does_not_hide_active_primary(self):
        """Real mocs.create_moc wrapper: a primary exception raised by
        the business body (injected) while release also fails must
        surface the PRIMARY untouched (release_lock only adds a note
        over an active primary) — never an rc-4-style raw LockError."""
        import os

        # Inject the business primary via the atomic writer: the
        # operation path itself raises while the lock is held by
        # create_moc.
        with patch.object(
            mocs,
            "_atomic_write_new",
            side_effect=RuntimeError("primary failure"),
        ):
            orig_unlink = os.unlink

            def fail_unlink(p, *a, **k):
                # fail every release attempt (5 retries inside
                # release_lock + the outer except-branch attempt)
                raise OSError(16, "EBUSY")

            with patch("os.unlink", side_effect=fail_unlink):
                with self.assertRaises(RuntimeError) as ctx:
                    mocs.create_moc(self.root, title="primary主题")
            self.assertIn("primary failure", str(ctx.exception))
            self.assertNotIn("write.lock residue", str(ctx.exception))

        # outside the patched unlink the lock residue from the failed
        # release is cleaned up (handle capability preserved through the
        # wrapper) — verify the lock file state and reset it so other
        # tests in this class are unaffected.
        lock_file = lock_path(self.root)
        if lock_file.exists():
            lock_file.unlink()

    def test_moc_release_failure_after_commit_maps_to_moc_error(self):
        """P1-1: business body succeeds, then release_lock exhausts its
        retries with NO active primary -> MocError with an explicit
        may-be-committed / write.lock-residue message (never a raw
        LockError, never a zero-write claim)."""
        import os

        orig_unlink = os.unlink
        calls = {"n": 0}

        def fail_unlink_five(p, *a, **k):
            # release_lock retries 5x; fail all of them so the release
            # path raises LockError with no active primary. The
            # handle-based release unlinks via (name, dir_fd=...):
            # match that call shape. Temp-file cleanups (other names)
            # still succeed so staging itself is unaffected.
            if str(p) == "write.lock" and "dir_fd" in k:
                calls["n"] += 1
                raise OSError(16, "EBUSY")
            return orig_unlink(p, *a, **k)

        with patch("os.unlink", side_effect=fail_unlink_five):
            with self.assertRaises(mocs.MocError) as ctx:
                mocs.create_moc(self.root, title="residue主题")
        self.assertGreaterEqual(calls["n"], 5)
        self.assertIn("may already be committed", str(ctx.exception))
        self.assertIn("write.lock residue", str(ctx.exception))
        # cause preserved (raw LockError chained, not swallowed)
        self.assertIsInstance(ctx.exception.__cause__, Exception)
        # the MOC itself was actually written (business committed)
        self.assertTrue(
            (self.root / "05 Literature" / "MOCs" / "residue主题.md").exists()
        )
        # lock residue remains; clean it up for later tests
        lock_file = lock_path(self.root)
        if lock_file.exists():
            lock_file.unlink()

    def test_rebuild_release_failure_after_commit_maps_to_index_lock_error(self):
        """P1-1: both files published, then release fails with no active
        primary -> IndexLockError with may-be-committed / residue
        message."""
        import os

        orig_unlink = os.unlink
        calls = {"n": 0}

        def fail_unlock_lock_only(p, *a, **k):
            if str(p) == "write.lock" and "dir_fd" in k:
                calls["n"] += 1
                raise OSError(16, "EBUSY")
            return orig_unlink(p, *a, **k)

        with patch("os.unlink", side_effect=fail_unlock_lock_only):
            with self.assertRaises(csl.IndexLockError) as ctx:
                csl.rebuild_indexes(self.root)
        self.assertGreaterEqual(calls["n"], 5)
        self.assertIn("may already be committed", str(ctx.exception))
        self.assertIn("write.lock residue", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, Exception)
        # business committed: both index files exist
        self.assertTrue((self.root / ".paper-notes" / "library.json").is_file())
        self.assertTrue(
            (self.root / ".paper-notes" / "citation-aliases.json").is_file()
        )
        lock_file = lock_path(self.root)
        if lock_file.exists():
            lock_file.unlink()

    def test_migration_apply_release_failure_after_commit_maps_to_migration_error(self):
        """P1-1: migration apply succeeds, then release fails with no
        active primary -> MigrationError with may-be-committed /
        residue message."""
        import os

        from paper_notes.migration import build_migration_plan
        from paper_notes.migration import transaction

        fixture = REPO / "tests" / "fixtures" / "legacy_vault"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = tmp / "vault"
            shutil.copytree(fixture, vault)
            state = tmp / "state"
            plan = build_migration_plan(vault, state_root=state)

            orig_unlink = os.unlink
            calls = {"n": 0}
            lock_file = lock_path(vault)

            def fail_lock_unlink_only(p, *a, **k):
                if str(p) == "write.lock" and "dir_fd" in k:
                    calls["n"] += 1
                    raise OSError(16, "EBUSY")
                return orig_unlink(p, *a, **k)

            with patch("os.unlink", side_effect=fail_lock_unlink_only):
                with self.assertRaises(transaction.MigrationError) as ctx:
                    transaction.apply_migration(
                        plan.run_id,
                        plan.confirmation_token,
                        vault_root=vault,
                        state_root=state,
                    )
            self.assertGreaterEqual(calls["n"], 5)
            self.assertIn("may already be committed", str(ctx.exception))
            self.assertIn("write.lock residue", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, Exception)
            # business committed: journal status is applied
            journal = transaction._load_journal(state, plan.run_id)
            self.assertEqual(journal.get("status"), "applied")
            # cleanup residue
            if lock_file.exists():
                lock_file.unlink()

    def test_migration_rollback_release_failure_maps_to_migration_error(self):
        """P1-1 rollback variant: rollback succeeds, then release fails
        with no active primary -> MigrationError with residue message."""
        import os

        from paper_notes.migration import build_migration_plan
        from paper_notes.migration import transaction

        fixture = REPO / "tests" / "fixtures" / "legacy_vault"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = tmp / "vault"
            shutil.copytree(fixture, vault)
            state = tmp / "state"
            plan = build_migration_plan(vault, state_root=state)
            transaction.apply_migration(
                plan.run_id,
                plan.confirmation_token,
                vault_root=vault,
                state_root=state,
            )

            orig_unlink = os.unlink
            calls = {"n": 0}
            lock_file = lock_path(vault)

            def fail_lock_unlink_only(p, *a, **k):
                if str(p) == "write.lock" and "dir_fd" in k:
                    calls["n"] += 1
                    raise OSError(16, "EBUSY")
                return orig_unlink(p, *a, **k)

            with patch("os.unlink", side_effect=fail_lock_unlink_only):
                with self.assertRaises(transaction.MigrationError) as ctx:
                    transaction.rollback_migration(
                        plan.run_id, vault_root=vault, state_root=state
                    )
            self.assertGreaterEqual(calls["n"], 5)
            self.assertIn("may already be committed", str(ctx.exception))
            self.assertIn("write.lock residue", str(ctx.exception))
            journal = transaction._load_journal(state, plan.run_id)
            self.assertEqual(journal.get("status"), "rolled_back")
            if lock_file.exists():
                lock_file.unlink()

    # ------------------------------------------------------------------
    # P2: reverse direction with a REAL item mutation + zero-write snapshot
    # ------------------------------------------------------------------

    def test_item_create_conflicts_while_moc_lock_held_zero_writes(self):
        self._assert_item_mutation_conflicts_zero_writes("create_moc")

    def test_item_create_conflicts_while_rebuild_lock_held_zero_writes(self):
        self._assert_item_mutation_conflicts_zero_writes("rebuild_indexes")

    def test_item_create_conflicts_while_migrate_lock_held_zero_writes(self):
        self._assert_item_mutation_conflicts_zero_writes("migrate")

    def _assert_item_mutation_conflicts_zero_writes(self, held_op: str) -> None:
        """Real reverse conflict: while `held_op` holds the shared lock,
        a real items.create_item returns ItemConflict with the vault
        byte-snapshot unchanged (except the lock file itself, which is
        the baseline of the hold)."""
        from paper_notes import items

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            holder = acquire_lock(root, held_op)
            try:
                # baseline includes the held lock file (excluded below)
                lock_file = lock_path(root)
                baseline_files = {
                    p: (p.stat().st_mode, p.read_bytes())
                    for p in sorted(root.rglob("*"))
                    if p.is_file() and not p.is_symlink()
                }
                baseline_dirs = {p for p in root.rglob("*") if p.is_dir()}

                with self.assertRaises(items.ItemConflict):
                    items.create_item(root, confirmed={
                        "title": "Reverse conflict probe",
                        "item_type": "article-journal",
                        "publication_date": "2026",
                        "authors": [{"family": "Smith", "given": "John"}],
                    })

                after_files = {
                    p: (p.stat().st_mode, p.read_bytes())
                    for p in sorted(root.rglob("*"))
                    if p.is_file() and not p.is_symlink()
                }
                after_dirs = {p for p in root.rglob("*") if p.is_dir()}
                # ignore the lock file's own bytes (metadata is
                # re-serialized by holders; content under it is
                # part of the hold baseline)
                def without_lock(d):
                    return {p: v for p, v in d.items() if p != lock_file}

                self.assertEqual(
                    without_lock(baseline_files), without_lock(after_files),
                    f"item create under a held {held_op} lock must be "
                    "zero-write (no item dir, no note, no temp)",
                )
                self.assertEqual(baseline_dirs, after_dirs)
            finally:
                release_lock(holder)


if __name__ == "__main__":
    unittest.main()
