"""Confirmed permanent item deletion tests (Task 14).

Frozen after the first red run; do not weaken or delete assertions.

Contract (spec §8.3 + plan Task 14 + manager approval):
- Preview is read-only (zero writes, no lock, no staging, hook 0) and
  returns needs_confirmation with the canonical citation key, paper_id,
  file_count, total_bytes, item-external Pandoc/wikilink backlink
  occurrences (path/kind/line/column; frontmatter/code/escaped
  literals never misreported), warnings, the complete plan (global
  markdown candidate manifest + whole item subtree manifest bound to
  the token), and a deterministic confirmation token.
- Confirm requires the exact canonical key char-for-char (alias, case
  and surrounding whitespace rejected; rc2, zero writes, zero hook)
  plus a matching token (wrong/expired token rc3, zero writes, zero
  hook). The plan is rebuilt under the write lock, so any subtree or
  global-markdown add/delete/edit/chmod/type/symlink change since the
  preview makes the token stale.
- Invalid YAML, unknown/ambiguous keys, duplicate keys/UUIDs/aliases,
  and canonical-directory symlinks all block.
- Deletion is all-or-nothing: every injectable failure (move, managed
  delete/backup, rebuild hook, post-verify) restores the fixture with
  exact bytes, modes, and topology. Success removes the canonical
  directory, leaves the fresh index free of the key/aliases/paper_id,
  fires the rebuild hook exactly once, leaves every other vault
  byte/mode unchanged, and leaves no lock/staging/work residue.
- Repair-R1 frozen regression: a commit conflict (an external racer
  reappearing at a staged hidden work target right before the real
  commit) is an explicit ItemConflict that fires the rebuild hook zero
  times, restores every pre-delete original file with exact
  bytes/mode/topology (the canonical item is complete, never a stub
  directory), releases the lock, and preserves the racer untouched at
  a named .paper-notes/recovery/ location (never overwritten, deleted
  or silently adopted as the deletion baseline).
- Repair-R2 frozen regressions (each verified red before the fix):
  (a) post-verdict TOCTOU: a racer reappearing at a staged hidden work
  target after the clean commit verdict but before the rebuild hook is
  an explicit ItemConflict that fires the hook zero times, restores the
  item byte-for-byte, and preserves the racer at a named recovery
  location (never reported 'deleted' with the racer left behind);
  (b) recovery no-replace: recovery material that already exists at
  the destination is never overwritten, deleted or rewritten — the new
  racer is preserved at a fresh in-vault location instead;
  (c) vault escape: a symlink planted at
  .paper-notes/recovery/<operation>/ pointing at an outside directory
  can never redirect the racer out of the vault — the outside
  directory receives zero writes, the racer stays inside the vault, and
  the planted symlink is left untouched.
- Error messages and CLI JSON never leak the confirmation original
  text or underlying exceptions.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes import cli, deletion, fsops, items
from paper_notes.repository import build_index

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "vault" / "rename_cases"

OLD = "shiauSpatiallyResolvedAnalysis2024"
ALIAS = "shiauSpatiallyResolved2023"
OTHER = "jonesOther2026"
PAPER_ID = "550e8400-e29b-41d4-a716-446655440001"

LIT = "05 Literature"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_vault(root=None):
    """Copy the fixture vault into a fresh temp dir (never mutated in
    place); accepts an existing destination directory."""
    if root is None:
        root = Path(tempfile.mkdtemp())
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    return root


def vault_manifest(root):
    """Sorted {relpath: (type, sha_or_link, mode)} over the whole vault,
    skipping only the .paper-notes transaction area."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".paper-notes")
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries[str(p.relative_to(root))] = ("symlink", os.readlink(p), None)
            else:
                entries[str(p.relative_to(root))] = (
                    "dir",
                    None,
                    stat.S_IMODE(st.st_mode),
                )
        for f in sorted(filenames):
            p = Path(dirpath) / f
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries[str(p.relative_to(root))] = ("symlink", os.readlink(p), None)
            else:
                entries[str(p.relative_to(root))] = (
                    "file",
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                    stat.S_IMODE(st.st_mode),
                )
    return dict(sorted(entries.items()))


def manifest_diff(before, after):
    added = sorted(p for p in after if p not in before)
    removed = sorted(p for p in before if p not in after)
    changed = sorted(p for p in before if p in after and before[p] != after[p])
    return added, removed, changed


def staging_residue(root):
    staging = Path(root) / ".paper-notes" / ".staging"
    if not staging.is_dir():
        return []
    return sorted(str(p.relative_to(staging)) for p in staging.rglob("*"))


def item(root, key):
    return Path(root) / LIT / key


def note(root, key):
    return item(root, key) / f"{key}.md"


def workdir(root, key=OLD):
    """Hidden same-filesystem work directory the deletion transaction
    moves the item into before removing it."""
    return item(root, key).parent / f".{key}.delete-work"


def preview(root, key=OLD):
    return deletion.preview_delete(root, key=key)


def get_token(root, key=OLD):
    return preview(root, key=key).confirmation_token


def confirm(root, token, key=OLD, confirm_key=OLD, hook=None):
    return deletion.confirm_delete(
        root, key=key, confirm_key=confirm_key, confirm_token=token,
        rebuild_hook=hook,
    )


def write_raw_paper(root, key, paper_id, aliases=()):
    """Minimal dynamic main note (only the mandatory file)."""
    d = item(root, key)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "schema_version: 1",
        f"paper_id: {paper_id}",
        f"citation_key: {key}",
        "item_type: article-journal",
        "title: A minimal paper",
        "authors:",
        "- family: Writer",
        "  given: Ann",
        "publication_date: 2025-01-01",
        "year: 2025",
        "pdf_status: missing",
        "reading_status: unread",
    ]
    if aliases:
        lines.append("citation_key_aliases:")
        lines.extend(f"  - {a}" for a in aliases)
    lines.append("---")
    lines.append("# Minimal")
    lines.append("")
    lines.append(f"citing [@{key}]")
    (d / f"{key}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def assert_zero_side_effects(testcase, root, hook=None):
    testcase.assertFalse((root / ".paper-notes" / "write.lock").exists())
    testcase.assertEqual(staging_residue(root), [])
    if hook is not None:
        hook.assert_not_called()


# ---------------------------------------------------------------------------
# 1. preview: read-only plan
# ---------------------------------------------------------------------------


class PreviewTest(unittest.TestCase):
    def test_preview_zero_writes_zero_lock_zero_staging(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            result = preview(root)
            self.assertEqual(vault_manifest(root), before)
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "delete")

    def test_preview_returns_plan_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview(root)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "delete")
            self.assertEqual(result.citation_key, OLD)
            self.assertEqual(result.requested_key, OLD)
            self.assertEqual(result.resolved_as, "key")
            self.assertEqual(result.paper_id, PAPER_ID)
            self.assertEqual(len(result.confirmation_token), 64)
            for key_name in (
                "action",
                "paper_id",
                "citation_key",
                "requested_key",
                "resolved_as",
                "file_count",
                "total_bytes",
                "occurrences",
                "warnings",
                "files",
                "subtree",
                "confirmation_token",
            ):
                self.assertIn(key_name, result.plan)

    def test_preview_file_count_and_total_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview(root)
            files = []
            total = 0
            for dirpath, dirnames, filenames in os.walk(item(root, OLD)):
                for f in filenames:
                    p = Path(dirpath) / f
                    if p.is_symlink():
                        continue
                    files.append(p)
                    total += p.stat().st_size
            self.assertEqual(result.file_count, len(files))
            self.assertEqual(result.total_bytes, total)
            self.assertGreater(result.file_count, 5)
            self.assertGreater(result.total_bytes, 0)
            # deterministic: a second preview is identical
            second = preview(root)
            self.assertEqual(second.confirmation_token, result.confirmation_token)
            self.assertEqual(second.plan, result.plan)

    def test_preview_alias_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview(root, key=ALIAS)
            self.assertEqual(result.resolved_as, "alias")
            self.assertEqual(result.citation_key, OLD)
            self.assertEqual(result.paper_id, PAPER_ID)

    def test_preview_backlink_occurrences_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview(root)
            occ = result.occurrences
            self.assertEqual(len(occ), 13)
            self.assertEqual(
                sorted(o.kind for o in occ), ["pandoc"] * 6 + ["wikilink"] * 7
            )
            by_path: dict[str, list] = {}
            for o in occ:
                by_path.setdefault(str(o.path.relative_to(root)), []).append(o)
            self.assertEqual(len(by_path["notes/reading-notes.md"]), 8)
            self.assertEqual(len(by_path["notes/crlf-note.md"]), 2)
            self.assertEqual(len(by_path["notes/中文笔记.md"]), 2)
            self.assertEqual(
                len(by_path["05 Literature/smithExample2026/smithExample2026.md"]), 1
            )
            # every occurrence is outside the deleted item subtree
            self.assertTrue(
                all(not o.path.is_relative_to(item(root, OLD)) for o in occ)
            )
            # coordinates: kind/line per file
            crlf = sorted(by_path["notes/crlf-note.md"], key=lambda o: o.line)
            self.assertEqual([(o.kind, o.line) for o in crlf], [("pandoc", 3), ("wikilink", 5)])
            zh = sorted(by_path["notes/中文笔记.md"], key=lambda o: o.line)
            self.assertEqual([(o.kind, o.line) for o in zh], [("pandoc", 3), ("wikilink", 4)])
            # occurrence entries carry context
            self.assertTrue(all(o.context for o in occ))

    def test_preview_frontmatter_never_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (root / "notes" / "frontmatter.md").write_text(
                "---\n"
                f"citation_key: {OLD}\n"
                "title: mentions the key in YAML only\n"
                "---\n"
                "# body has no real citation\n",
                encoding="utf-8",
            )
            result = preview(root)
            self.assertFalse(
                any(o.path.name == "frontmatter.md" for o in result.occurrences)
            )

    def test_preview_global_md_manifest_excludes_item_subtree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview(root)
            self.assertTrue(result.plan["files"])
            self.assertTrue(
                all(
                    not Path(f["path"]).is_relative_to(item(root, OLD))
                    for f in result.plan["files"]
                )
            )

    def test_preview_subtree_binds_hidden_and_empty_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / ".hidden").mkdir()
            (item(root, OLD) / ".hidden" / "secret.txt").write_text("s", encoding="utf-8")
            (item(root, OLD) / "empty").mkdir()
            result = preview(root)
            subtree = {f["path"]: f for f in result.plan["subtree"]}
            hidden_dir = str(item(root, OLD) / ".hidden")
            empty_dir = str(item(root, OLD) / "empty")
            self.assertEqual(subtree[hidden_dir]["type"], "dir")
            self.assertEqual(subtree[empty_dir]["type"], "dir")
            self.assertEqual(
                subtree[str(item(root, OLD) / ".hidden" / "secret.txt")]["type"], "file"
            )

    def test_preview_symlink_in_subtree_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / "attachments" / "evil").symlink_to(
                Path(outside) / "outside-target"
            )
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview(root)
            self.assertEqual(vault_manifest(root), before)

    def test_preview_canonical_dir_symlink_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            shutil.rmtree(item(root, OLD))
            real = item(root, OLD).parent / "real-old"
            real.mkdir()
            (real / f"{OLD}.md").write_text(
                (FIXTURE / LIT / OLD / f"{OLD}.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            item(root, OLD).symlink_to(real)
            with self.assertRaises(items.ItemConflict):
                preview(root)

    def test_preview_unknown_key_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            with self.assertRaises(items.ItemError):
                preview(root, key="doesNotExist2026")
            self.assertEqual(vault_manifest(root), before)

    def test_preview_duplicate_key_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            # a second top-level note in the same directory declaring the
            # same citation key is a duplicate_key identity conflict
            (item(root, OLD) / "zzz-extra.md").write_text(
                note(root, OLD).read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaises(items.ItemConflict):
                preview(root)
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_preview_duplicate_uuid_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            # a second item directory reusing the same paper_id is a
            # duplicate_uuid identity conflict
            write_raw_paper(root, "dupUuid2026", PAPER_ID)
            with self.assertRaises(items.ItemConflict):
                preview(root)

    def test_preview_alias_collision_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            write_raw_paper(root, OTHER, "550e8400-e29b-41d4-a716-446655440099",
                            aliases=(ALIAS,))
            with self.assertRaises(items.ItemConflict):
                preview(root)

    def test_preview_invalid_yaml_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            note(root, OLD).write_text(
                "---\ntitle: [unclosed\n---\n# broken\n", encoding="utf-8"
            )
            with self.assertRaises(items.ItemError):
                preview(root)


# ---------------------------------------------------------------------------
# 2. confirmation: exact key + token
# ---------------------------------------------------------------------------


class ConfirmKeyTest(unittest.TestCase):
    def test_confirm_requires_exact_canonical_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            token = get_token(root)
            hook = mock.Mock()
            for bad in (ALIAS, OLD.upper(), " " + OLD, OLD + " ", OLD[:-1]):
                with self.assertRaises(items.ItemError):
                    confirm(root, token, confirm_key=bad, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_zero_side_effects(self, root, hook)

    def test_confirm_wrong_token_conflict_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, "0" * 64, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_zero_side_effects(self, root, hook)

    def test_confirm_alias_lookup_with_canonical_confirm_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root, key=ALIAS)
            result = confirm(root, token, key=ALIAS, confirm_key=OLD)
            self.assertEqual(result.status, "deleted")
            self.assertEqual(result.citation_key, OLD)
            self.assertFalse(item(root, OLD).exists())


# ---------------------------------------------------------------------------
# 3. stale token: any change since the preview
# ---------------------------------------------------------------------------


class StaleTest(unittest.TestCase):
    def assert_stale_zero_writes(self, root, hook):
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])
        hook.assert_not_called()
        self.assertTrue(item(root, OLD).is_dir())

    def test_stale_subtree_file_added(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            (item(root, OLD) / "new-file.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_file_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            (item(root, OLD) / "stray-notes.txt").unlink()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_file_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            p = item(root, OLD) / "stray-notes.txt"
            p.write_text(p.read_text(encoding="utf-8") + "edited", encoding="utf-8")
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_file_chmod(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            os.chmod(item(root, OLD) / "stray-notes.txt", 0o600)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_file_type_change_to_symlink(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            p = item(root, OLD) / "stray-notes.txt"
            p.unlink()
            p.symlink_to(Path(outside) / "target")
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_empty_dir_added(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            (item(root, OLD) / "empty-dir").mkdir()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_empty_dir_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / "empty-dir").mkdir()
            token = get_token(root)
            hook = mock.Mock()
            (item(root, OLD) / "empty-dir").rmdir()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_subtree_empty_dir_chmod(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / "empty-dir").mkdir()
            token = get_token(root)
            hook = mock.Mock()
            os.chmod(item(root, OLD) / "empty-dir", 0o700)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_global_md_added(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            (root / "notes" / "new-note.md").write_text("# new\n", encoding="utf-8")
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_global_md_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            (root / "notes" / "clean.md").unlink()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)

    def test_stale_global_md_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            hook = mock.Mock()
            p = root / "notes" / "clean.md"
            p.write_text(p.read_text(encoding="utf-8") + "edited", encoding="utf-8")
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)


# ---------------------------------------------------------------------------
# 4. success: all-or-nothing deletion
# ---------------------------------------------------------------------------


class ConfirmSuccessTest(unittest.TestCase):
    def test_success_removes_item_and_rebuilds_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            removed_rel = sorted(
                [str(p.relative_to(root)) for p in item(root, OLD).rglob("*")]
                + [str(item(root, OLD).relative_to(root))]
            )
            hook = mock.Mock()
            token = get_token(root)
            result = confirm(root, token, hook=hook)
            self.assertEqual(result.status, "deleted")
            self.assertEqual(result.action, "delete")
            self.assertEqual(result.citation_key, OLD)
            self.assertEqual(result.paper_id, PAPER_ID)
            self.assertGreater(result.file_count, 0)
            hook.assert_called_once_with()
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse(item(root, OLD).is_symlink())
            index = build_index(root)
            self.assertNotIn(OLD, index.by_key)
            self.assertNotIn(ALIAS, index.aliases)
            self.assertNotIn(OLD, index.aliases)
            self.assertTrue(
                all(str(r.paper.paper_id) != PAPER_ID for r in index.by_key.values())
            )
            # other vault content byte/mode unchanged: only the item subtree gone
            after = vault_manifest(root)
            added, removed, changed = manifest_diff(before, after)
            self.assertEqual(added, [])
            self.assertEqual(changed, [])
            self.assertEqual(removed, removed_rel)
            # no lock / staging / work residue
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self.assertFalse(workdir(root).exists())

    def test_success_with_alias_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root, key=ALIAS)
            result = confirm(root, token, key=ALIAS, confirm_key=OLD)
            self.assertEqual(result.status, "deleted")
            self.assertFalse(item(root, OLD).exists())
            index = build_index(root)
            self.assertNotIn(OLD, index.by_key)
            self.assertNotIn(ALIAS, index.aliases)


# ---------------------------------------------------------------------------
# 5. injected failure: all-or-nothing rollback
# ---------------------------------------------------------------------------


class RollbackTest(unittest.TestCase):
    def assert_restored(self, root, before, hook):
        self.assertEqual(vault_manifest(root), before)
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])
        self.assertFalse(workdir(root).exists())
        hook.assert_not_called()

    def test_failure_at_directory_move_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with mock.patch(
                "paper_notes.deletion._rename_dir_noreplace",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)

    def test_failure_at_managed_delete_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            real_delete = fsops.delete_target

            def failing_delete(op, target):
                real_delete(op, target)
                raise OSError("boom")

            with mock.patch(
                "paper_notes.deletion.fsops.delete_target", side_effect=failing_delete
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)

    def test_failure_at_rebuild_hook_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock(side_effect=RuntimeError("rebuild boom"))
            with self.assertRaises(items.ItemError):
                confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertFalse(workdir(root).exists())
            hook.assert_called_once()  # the hook itself ran and failed

    def test_failure_at_post_verify_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with mock.patch(
                "paper_notes.deletion._post_verify",
                side_effect=RuntimeError("verify boom"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)

    def test_failure_at_stage_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with mock.patch(
                "paper_notes.deletion.fsops.stage_target",
                side_effect=fsops.OperationConflict("simulated stage failure"),
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)


class CommitConflictTest(unittest.TestCase):
    """Frozen repair-R1 regression: a commit conflict must never lose
    the original files, must fire the rebuild hook zero times, must
    restore the canonical item completely (no stub directory), and must
    preserve the external racer untouched at a named recovery location.

    A distinct external racer appears at a staged hidden work target
    (the canonical main note) immediately before the real commit; the
    staged deletion must then roll back completely while the racer
    survives with its exact bytes at ``.paper-notes/recovery/<op>/``.
    """

    def test_commit_conflict_restores_item_and_preserves_racer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            real_commit = fsops.commit
            work = workdir(root)
            racer_content = b"external racer: reappeared at a deleted target"

            def racer_commit(op):
                # create the racer at a staged hidden work target right
                # before the real commit runs
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_commit(op)

            with mock.patch(
                "paper_notes.deletion.fsops.commit", side_effect=racer_commit
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            # explicit conflict, not silent: the racer path is named
            self.assertIn(f"{OLD}.md", str(ctx.exception))
            # 1) the rebuild hook never fired on the failed commit
            hook.assert_not_called()
            # 2) every pre-delete original file is restored with exact
            #    bytes, modes and topology; the canonical item is
            #    complete, not a stub directory
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # 3) the external racer survives untouched at the named
            #    recovery location and was never adopted as the baseline
            op_dirs = sorted((root / ".paper-notes" / "recovery").glob("*"))
            self.assertEqual(len(op_dirs), 1)
            racer_backup = op_dirs[0] / f"{OLD}.md"
            self.assertEqual(racer_backup.read_bytes(), racer_content)
            self.assertNotEqual(racer_backup.read_bytes(), original_note)


# ---------------------------------------------------------------------------
# Repair-R2 frozen regressions: post-verdict race, recovery no-replace,
# recovery vault escape
# ---------------------------------------------------------------------------


class PostVerdictRaceTest(unittest.TestCase):
    """Frozen repair-R2 regression: a racer that reappears at a staged
    hidden work target AFTER the clean commit verdict but BEFORE the
    rebuild hook must be an explicit conflict — the hook never fires,
    the canonical item is restored byte-for-byte, the racer survives at
    a named recovery location, and the result is never reported as
    'deleted' with the racer left behind in the work directory."""

    def test_racer_after_clean_verdict_conflicts_and_restores(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            work = workdir(root)
            racer_content = b"post-verdict racer at a staged hidden work target"
            real_verdict = deletion._commit_verdict

            def post_verdict_racer(op):
                verdict = real_verdict(op)  # clean
                self.assertEqual(verdict, [])
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return verdict

            with mock.patch(
                "paper_notes.deletion._commit_verdict", side_effect=post_verdict_racer
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            # explicit conflict, never a silent 'deleted'
            self.assertIn(f"{OLD}.md", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the racer survives untouched at the named recovery location
            op_dirs = sorted((root / ".paper-notes" / "recovery").glob("*"))
            self.assertEqual(len(op_dirs), 1)
            racer_backup = op_dirs[0] / f"{OLD}.md"
            self.assertEqual(racer_backup.read_bytes(), racer_content)
            self.assertNotEqual(racer_backup.read_bytes(), original_note)


class RecoveryNoReplaceTest(unittest.TestCase):
    """Frozen repair-R2 regression: recovery material that already
    exists at the destination is never overwritten, deleted or
    rewritten — the new racer is preserved at a fresh in-vault
    location instead, the pre-seeded bytes survive exactly, and the
    item is restored."""

    def test_preseeded_recovery_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            work = workdir(root)
            racer_content = b"external racer: reappeared at a deleted target"
            seeded_content = b"pre-seeded recovery bytes: must survive untouched"
            seeded_paths = []
            real_commit = fsops.commit

            def seed_then_racer(op):
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                seeded = (
                    root / ".paper-notes" / "recovery" / op.operation_id / f"{OLD}.md"
                )
                seeded.parent.mkdir(parents=True, exist_ok=True)
                seeded.write_bytes(seeded_content)
                seeded_paths.append(seeded)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_commit(op)

            with mock.patch(
                "paper_notes.deletion.fsops.commit", side_effect=seed_then_racer
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            self.assertIn(f"{OLD}.md", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the pre-seeded recovery material is byte-identical
            seeded = seeded_paths[0]
            self.assertEqual(seeded.read_bytes(), seeded_content)
            # the new racer was preserved at a DIFFERENT in-vault path
            racer_paths = [
                p
                for p in (root / ".paper-notes" / "recovery").rglob("*")
                if p.is_file() and p.read_bytes() == racer_content
            ]
            self.assertEqual(len(racer_paths), 1)
            self.assertNotEqual(racer_paths[0], seeded)
            self.assertTrue(racer_paths[0].resolve().is_relative_to(root.resolve()))


class RecoveryEscapeTest(unittest.TestCase):
    """Frozen repair-R2 regression: a symlink planted at
    .paper-notes/recovery/<operation>/ pointing at an outside
    directory must never redirect the racer out of the vault — the
    racer is preserved inside the vault, the outside directory
    receives zero writes, and the item is restored."""

    def test_recovery_symlink_never_escapes_the_vault(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            work = workdir(root)
            racer_content = b"external racer: must never leave the vault"
            real_commit = fsops.commit

            def symlink_then_racer(op):
                recovery_root = root / ".paper-notes" / "recovery" / op.operation_id
                recovery_root.parent.mkdir(parents=True, exist_ok=True)
                recovery_root.symlink_to(Path(outside), target_is_directory=True)
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_commit(op)

            with mock.patch(
                "paper_notes.deletion.fsops.commit", side_effect=symlink_then_racer
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            self.assertIn(f"{OLD}.md", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the outside directory received zero writes
            self.assertEqual(sorted(os.listdir(Path(outside))), [])
            # the planted symlink is untouched
            recovery_root = root / ".paper-notes" / "recovery"
            symlinks = [p for p in recovery_root.iterdir() if p.is_symlink()]
            self.assertEqual(len(symlinks), 1)
            self.assertEqual(os.readlink(symlinks[0]), str(Path(outside)))
            # the racer was preserved INSIDE the vault
            racer_paths = []
            for dirpath, dirnames, filenames in os.walk(
                recovery_root, followlinks=False
            ):
                for f in filenames:
                    p = Path(dirpath) / f
                    if p.is_file() and p.read_bytes() == racer_content:
                        racer_paths.append(p)
            self.assertEqual(len(racer_paths), 1)
            self.assertTrue(racer_paths[0].resolve().is_relative_to(root.resolve()))


# ---------------------------------------------------------------------------
# 6. CLI envelopes and exit codes
# ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=90,
        )

    def test_cli_dry_run_needs_confirmation_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli("item", "delete", "--vault", str(root), "--key", OLD, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            data = payload["data"]
            self.assertEqual(data["action"], "delete")
            self.assertEqual(data["citation_key"], OLD)
            self.assertEqual(data["paper_id"], PAPER_ID)
            self.assertEqual(data["plan"]["citation_key"], OLD)
            self.assertIn("confirmation_token", data)
            self.assertGreater(data["file_count"], 0)
            self.assertGreater(data["total_bytes"], 0)
            self.assertTrue(data["occurrences"])
            self.assertTrue(any(o["kind"] == "pandoc" for o in data["occurrences"]))

    def test_cli_confirm_success_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-key", OLD, "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "deleted")
            self.assertEqual(payload["data"]["citation_key"], OLD)
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_cli_wrong_confirm_key_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-key", ALIAS, "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertTrue(item(root, OLD).is_dir())

    def test_cli_wrong_token_rc3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-key", OLD, "--confirm-token", "f" * 64,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "conflict")
            self.assertTrue(item(root, OLD).is_dir())

    def test_cli_missing_confirm_key_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertTrue(item(root, OLD).is_dir())

    def test_cli_missing_confirm_token_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-key", OLD,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertTrue(item(root, OLD).is_dir())

    def test_cli_dry_run_with_confirm_token_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--dry-run", "--confirm-token", "x",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_unknown_key_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", "missing2026", "--dry-run"
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_confirm_key_never_leaks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            secret = "CONFIRM-SECRET-MARKER-«sk-…»"
            result = self.run_cli(
                "item", "delete", "--vault", str(root), "--key", OLD,
                "--confirm-key", secret, "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)

    def test_cli_secret_marker_never_leaks(self):
        # In-process CLI harness: an in-process mock cannot cross a
        # subprocess boundary, so the real CLI main() is driven directly
        # with stdout/stderr captured (argparse usage errors surface as
        # SystemExit and are normalized to their exit code).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            secret = "«redacted:sk-…»"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch(
                "paper_notes.deletion.preview_delete",
                side_effect=RuntimeError(secret),
            ):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        rc = cli.main(
                            ["--json", "item", "delete", "--vault", str(root), "--key", OLD, "--dry-run"]
                        )
                    except SystemExit as exc:
                        rc = exc.code if isinstance(exc.code, int) else 2
            self.assertEqual(rc, 4)
            self.assertEqual(len(out.getvalue().strip().splitlines()), 1)
            self.assertNotIn(secret, out.getvalue())
            self.assertNotIn(secret, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["errors"][0]["message"], "Internal error")


if __name__ == "__main__":
    unittest.main()
