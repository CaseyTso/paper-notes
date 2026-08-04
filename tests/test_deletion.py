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
- Repair-R3 frozen regressions (Decision A, each verified red before
  the fix): the deletion and the citation-index publication are ONE
  transaction seam driven by a minimal transactional participant
  (paper_notes.deletion.IndexParticipant, the Task 15 real-writer
  seam) writing .paper-notes/library.json and
  .paper-notes/citation-aliases.json as staged managed writes:
  (a) every move (item canonical -> hidden work, racer -> recovery,
  rollback restore) uses the dirfd-anchored atomic no-replace
  primitive paper_notes.fsops.no_replace_move — a late destination
  racer is preserved byte-for-byte, a recovery parent swapped to an
  outside symlink after the last check receives zero outside writes,
  and a file appearing at the recovery destination after its last
  check leaves the pre-seeded bytes untouched while the racer lands at
  a fresh in-vault path; (b) a racer reappearing at a hidden work
  target after the clean verdict but before the hook, and an external
  edit/chmod/type swap between the two index outputs, and a
  prepare/write/commit/finalize failure all roll the item AND both
  index files back to their exact bytes+mode with the racer preserved
  (at a named recovery location) and the hook zero times; (c) success
  makes the item deletion and both index files' new state visible
  together with the participant running exactly once; the R2
  _reappeared_targets clean check is gone — the final authority (a
  full expected-state detection) is the plain rebuild_hook success
  window.
- Repair-R4 frozen regressions (Decision A continued, each verified red
  before the fix): index publication, the final authority and the
  success transition are merged into IndexParticipant.finalize(hook) —
  the legacy rebuild hook is only a managed participant adapter invoked
  INSIDE the transaction between two full expected-state authorities,
  never an arbitrary writable callback after the final check:
  (a) a hidden work-target racer injected at the IndexParticipant
  finalize() seam (after the real _final_conflicts returned clean) is
  an explicit ItemConflict: the hook never fires, the item and both
  index files roll back to their exact bytes+mode, and the racer is
  preserved untouched at a named in-vault recovery location with no
  staging/work/temp/lock residue; (b) an external edit / chmod /
  file<->dir/symlink swap of library.json or citation-aliases.json
  performed by the successful rebuild_hook() (or during it) is
  detected by the post-hook authority and rolls the item AND both
  index files back, with the external bytes preserved at a named
  recovery location — the deletion is never reported 'deleted' with
  the external bytes as the final index; (c) the deletion's
  linearization point is the success transition at the end of
  finalize: after the last expected-state authority no writable
  callback runs.
- Repair-R5 frozen regressions (Decision A, each verified red before
  the fix): the second final authority inside
  IndexParticipant.finalize is the deletion's linearization point —
  after it the item deletion and both index publications are formally
  effective and are NEVER rolled back because of cleanup trouble.
  Staging cleanup is best-effort; when it fails, silently no-ops or
  leaves residue, confirm_delete returns the structured
  deleted_with_cleanup_required status (never a silent 'deleted')
  carrying the desensitized operation id, the exact vault-relative
  residue paths and idempotent retry-cleanup guidance;
  retry_cleanup() is an idempotent, vault-bound, symlink-safe retry
  seam that never follows symlinks and never overwrites external
  files.
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


def index_paths(root):
    pn = Path(root) / ".paper-notes"
    return pn / "library.json", pn / "citation-aliases.json"


def seed_index(root, library=None, aliases=None):
    """Seed the two mock citation-index files (the R3 transactional
    participant's outputs) and return (lib, aliases, before_bytes,
    before_modes)."""
    lib, al = index_paths(root)
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(
        json.dumps(
            library if library is not None else {"papers": {OTHER: "other-uuid"}},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    al.write_text(
        json.dumps(
            aliases if aliases is not None else {"aliases": {}},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return lib, al, lib.read_bytes(), al.read_bytes(), lib.lstat().st_mode, al.lstat().st_mode


def assert_index_restored(testcase, root, before):
    lib, al = index_paths(root)
    testcase.assertEqual(lib.read_bytes(), before[2])
    testcase.assertEqual(al.read_bytes(), before[3])
    testcase.assertEqual(stat.S_IMODE(lib.lstat().st_mode), stat.S_IMODE(before[4]))
    testcase.assertEqual(stat.S_IMODE(al.lstat().st_mode), stat.S_IMODE(before[5]))


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
# Repair-R3 frozen regressions: deletion + citation-index publication as
# one transaction seam (dirfd no-replace primitive, transactional
# participant, final-authority success window)
# ---------------------------------------------------------------------------


class TransactionSeamTest(unittest.TestCase):
    """Frozen repair-R3 regressions: the deletion and the citation-index
    publication are ONE transaction. The fake index writer
    (deletion.IndexParticipant) writes both mock index files as staged
    managed writes, so a failure in prepare/write/commit/finalize or an
    external racer rolls the item AND both index files back to their
    exact bytes+mode; on success the item deletion and both index
    files' new state are visible together and the participant runs
    exactly once."""

    def test_success_updates_both_indexes_and_runs_participant_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            removed_rel = sorted(
                [str(p.relative_to(root)) for p in item(root, OLD).rglob("*")]
                + [str(item(root, OLD).relative_to(root))]
            )
            lib, al, lib_b, al_b, lib_m, al_m = seed_index(
                root,
                library={"papers": {OLD: PAPER_ID, OTHER: "other-uuid"}},
                aliases={"aliases": {ALIAS: OLD, "smithAlias": "smithExample2026"}},
            )
            hook = mock.Mock()
            real_commit = deletion.IndexParticipant.commit
            real_finalize = deletion.IndexParticipant.finalize
            commits, finalizes = [], []

            def counting_commit(self):
                commits.append(1)
                return real_commit(self)

            def counting_finalize(self, hook):
                finalizes.append(1)
                return real_finalize(self, hook)

            with mock.patch.object(
                deletion.IndexParticipant, "commit", counting_commit
            ), mock.patch.object(
                deletion.IndexParticipant, "finalize", counting_finalize
            ):
                result = confirm(root, get_token(root), hook=hook)

            self.assertEqual(result.status, "deleted")
            hook.assert_called_once_with()
            # the item deletion and both index files' new state are
            # visible together; the deleted key is gone from both files
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse(item(root, OLD).is_symlink())
            self.assertEqual(
                json.loads(lib.read_text()), {"papers": {OTHER: "other-uuid"}}
            )
            self.assertEqual(
                json.loads(al.read_text()), {"aliases": {"smithAlias": "smithExample2026"}}
            )
            # participant ran exactly once
            self.assertEqual(commits, [1])
            self.assertEqual(finalizes, [1])
            # every other vault byte/mode unchanged, no residue
            after = vault_manifest(root)
            added, removed, changed = manifest_diff(before, after)
            self.assertEqual(added, [])
            self.assertEqual(changed, [])
            self.assertEqual(removed, removed_rel)
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self.assertFalse(workdir(root).exists())

    def test_racer_after_clean_verdict_conflicts_restores_item_and_indexes(self):
        """Clean verdict 后、旧 hook 前注入 hidden work-target racer:
        no success / no hook publication; item fully restored; racer
        preserved at a named recovery location; both index files rolled
        back to their exact pre-transaction bytes+mode (the R2
        _reappeared_targets clean check is replaced by the final
        authority, the plain rebuild_hook success window)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            index_before = seed_index(root)
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

            self.assertIn(f"{OLD}.md", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            # both index files rolled back to their exact bytes+mode
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the racer survives untouched at a named recovery location
            op_dirs = sorted((root / ".paper-notes" / "recovery").glob("*"))
            self.assertEqual(len(op_dirs), 1)
            racer_backup = op_dirs[0] / f"{OLD}.md"
            self.assertEqual(racer_backup.read_bytes(), racer_content)
            self.assertNotEqual(racer_backup.read_bytes(), original_note)


class ParticipantFailureTest(unittest.TestCase):
    """Frozen repair-R3 regressions: a failure in the fake citation
    participant's prepare/write/commit/finalize phases or an external
    edit/chmod/type swap at an index path rolls the item AND both index
    files back to their exact bytes+mode, the hook fires zero times,
    and no staging/work residue is left behind."""

    def _scenario(self, seed=True):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        make_vault(root)
        before = vault_manifest(root)
        index_before = seed_index(root) if seed else None
        hook = mock.Mock()
        return td, root, before, index_before, hook

    def test_mid_write_failure_rolls_back_item_and_indexes(self):
        """两输出中途失败: the second index write raises — the item and
        BOTH index files roll back to their exact pre-transaction
        bytes+mode with no residue."""
        td, root, before, index_before, hook = self._scenario()
        with td:
            real_write = fsops.write_target
            calls = {"n": 0}

            def failing_second(op, target, content):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated mid-write failure")
                return real_write(op, target, content)

            with mock.patch(
                "paper_notes.deletion.fsops.write_target", side_effect=failing_second
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_index_restored(self, root, index_before)
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_external_edit_after_first_output_blocks_second_write(self):
        """第一输出后 external edit: the external bytes land on the
        second index file after the first output — the second write's
        expected-state guard refuses, the first output is rolled back,
        and the racer bytes are preserved untouched (never adopted)."""
        td, root, before, index_before, hook = self._scenario()
        with td:
            racer_bytes = b"external edit after the first index output"
            real_write = fsops.write_target
            calls = {"n": 0}

            def guarded(op, target, content):
                calls["n"] += 1
                if calls["n"] == 2:
                    Path(target).write_bytes(racer_bytes)
                return real_write(op, target, content)

            with mock.patch(
                "paper_notes.deletion.fsops.write_target", side_effect=guarded
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            lib, al = index_paths(root)
            self.assertEqual(lib.read_bytes(), index_before[2])  # first output undone
            self.assertEqual(al.read_bytes(), racer_bytes)  # racer preserved
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_external_chmod_after_first_output_blocks_second_write(self):
        td, root, before, index_before, hook = self._scenario()
        with td:
            real_write = fsops.write_target
            calls = {"n": 0}

            def guarded(op, target, content):
                calls["n"] += 1
                if calls["n"] == 2:
                    os.chmod(Path(target), 0o600)
                return real_write(op, target, content)

            with mock.patch(
                "paper_notes.deletion.fsops.write_target", side_effect=guarded
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            lib, al = index_paths(root)
            self.assertEqual(lib.read_bytes(), index_before[2])  # first output undone
            self.assertEqual(
                stat.S_IMODE(al.lstat().st_mode), 0o600
            )  # external chmod preserved
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_external_type_swap_after_first_output_blocks_second_write(self):
        td, root, before, index_before, hook = self._scenario()
        with td:
            real_write = fsops.write_target
            calls = {"n": 0}

            def guarded(op, target, content):
                calls["n"] += 1
                if calls["n"] == 2:
                    Path(target).unlink()
                    Path(target).mkdir()  # file -> directory type swap
                return real_write(op, target, content)

            with mock.patch(
                "paper_notes.deletion.fsops.write_target", side_effect=guarded
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            lib, al = index_paths(root)
            self.assertEqual(lib.read_bytes(), index_before[2])  # first output undone
            self.assertTrue(al.is_dir())  # external type swap preserved
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_finalization_conflict_rolls_back_item_and_indexes(self):
        """Deletion finalization conflict: an external racer appears at
        an index path after the clean verdict (caught by the final
        authority) — the item AND both index files roll back to their
        exact bytes+mode, the racer survives at a named recovery
        location, and the hook never fires."""
        td, root, before, index_before, hook = self._scenario()
        with td:
            racer_bytes = b"external edit of library.json at finalization"
            lib, al = index_paths(root)
            real_verdict = deletion._commit_verdict

            def verdict_then_index_racer(op):
                verdict = real_verdict(op)  # clean
                self.assertEqual(verdict, [])
                lib.write_bytes(racer_bytes)  # racer lands on an index target
                return verdict

            with mock.patch(
                "paper_notes.deletion._commit_verdict",
                side_effect=verdict_then_index_racer,
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            self.assertIn("library.json", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the racer bytes survive untouched inside the vault
            racer_paths = [
                p
                for p in (root / ".paper-notes" / "recovery").rglob("*")
                if p.is_file() and p.read_bytes() == racer_bytes
            ]
            self.assertEqual(len(racer_paths), 1)
            self.assertTrue(racer_paths[0].resolve().is_relative_to(root.resolve()))

    def test_prepare_failure_rolls_back_item_and_indexes(self):
        td, root, before, index_before, hook = self._scenario()
        with td:
            with mock.patch.object(
                deletion.IndexParticipant,
                "prepare",
                side_effect=RuntimeError("simulated prepare failure"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_index_restored(self, root, index_before)
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_commit_failure_rolls_back_item_and_indexes(self):
        td, root, before, index_before, hook = self._scenario()
        with td:
            with mock.patch.object(
                deletion.IndexParticipant,
                "commit",
                side_effect=RuntimeError("simulated commit failure"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_index_restored(self, root, index_before)
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_finalize_failure_rolls_back_item_and_indexes(self):
        td, root, before, index_before, hook = self._scenario()
        with td:
            with mock.patch.object(
                deletion.IndexParticipant,
                "finalize",
                side_effect=RuntimeError("simulated finalize failure"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            assert_index_restored(self, root, index_before)
            hook.assert_not_called()
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])


class FinalAuthoritySeamTest(unittest.TestCase):
    """Frozen repair-R4 regressions: the legacy rebuild hook is a
    managed participant adapter invoked INSIDE
    IndexParticipant.finalize(hook) between two full expected-state
    authorities.

    A hidden work-target racer injected at the finalize() seam (after
    the real _final_conflicts returned clean) is an explicit
    ItemConflict with the hook zero times; an edit / chmod /
    file<->dir/symlink swap of library.json or citation-aliases.json
    performed by the successful rebuild_hook() is detected by the
    post-hook authority and rolls the item AND both index files back
    with the external bytes preserved at a named recovery location —
    the deletion is never reported 'deleted' with the external bytes
    as the final index. The deletion's linearization point is the
    success transition at the end of finalize: after the last
    expected-state authority no writable callback runs.
    """

    def _scenario(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        make_vault(root)
        before = vault_manifest(root)
        index_before = seed_index(root)
        original_note = note(root, OLD).read_bytes()
        hook = mock.Mock()
        return td, root, before, index_before, original_note, hook

    def _assert_racer_in_recovery(self, root, racer_bytes, count=1):
        racer_paths = [
            p
            for p in (root / ".paper-notes" / "recovery").rglob("*")
            if p.is_file() and p.read_bytes() == racer_bytes
        ]
        self.assertEqual(len(racer_paths), count)
        for p in racer_paths:
            self.assertTrue(p.resolve().is_relative_to(root.resolve()))

    def test_racer_injected_at_finalize_seam_conflicts(self):
        """Clean _final_conflicts 后、由 IndexParticipant.finalize() seam
        注入 hidden work-target racer: no success / no hook publication;
        item and both index files restored to their exact bytes+mode;
        racer preserved untouched at a named in-vault recovery location;
        no staging/work/temp/lock residue."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            work = workdir(root)
            racer_content = b"racer injected at the IndexParticipant.finalize seam"
            real_finalize = deletion.IndexParticipant.finalize

            def finalize_with_racer(self, hook_):
                # the real _final_conflicts already returned clean; the
                # hidden work-target racer appears only inside the
                # finalize seam, right before the real finalize runs
                target = work / f"{OLD}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_finalize(self, hook_)

            with mock.patch.object(
                deletion.IndexParticipant, "finalize", finalize_with_racer
            ):
                with self.assertRaises(items.ItemConflict) as ctx:
                    confirm(root, get_token(root), hook=hook)

            self.assertIn(f"{OLD}.md", str(ctx.exception))
            hook.assert_not_called()
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self._assert_racer_in_recovery(root, racer_content)

    def test_hook_rewriting_library_json_conflicts(self):
        """成功 rebuild_hook() 外部改写 library.json: the post-hook
        authority detects the external bytes, the item AND both index
        files roll back to their exact bytes+mode, the external bytes
        survive untouched at a named recovery location, the hook ran
        exactly once, and the deletion is never reported 'deleted' with
        the external bytes as the final index."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            external = b'{"papers": {"external": "ext-uuid"}}\n'
            lib, _al = index_paths(root)
            calls = {"n": 0}

            def rewriting_hook():
                calls["n"] += 1
                lib.write_bytes(external)

            with self.assertRaises(items.ItemConflict) as ctx:
                confirm(root, get_token(root), hook=rewriting_hook)

            self.assertIn("library.json", str(ctx.exception))
            self.assertEqual(calls["n"], 1)  # the hook itself ran exactly once
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self._assert_racer_in_recovery(root, external)

    def test_hook_rewriting_citation_aliases_conflicts(self):
        """成功 rebuild_hook() 外部改写 citation-aliases.json（对称场景）:
        detected by the post-hook authority; item and both index files
        restored; the external bytes preserved at a named recovery
        location; hook ran exactly once."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            external = b'{"aliases": {"external": "ext"}}\n'
            _lib, al = index_paths(root)
            calls = {"n": 0}

            def rewriting_hook():
                calls["n"] += 1
                al.write_bytes(external)

            with self.assertRaises(items.ItemConflict) as ctx:
                confirm(root, get_token(root), hook=rewriting_hook)

            self.assertIn("citation-aliases.json", str(ctx.exception))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self._assert_racer_in_recovery(root, external)

    def test_hook_chmod_index_conflicts(self):
        """hook 期间 chmod library.json: the post-hook authority detects
        the mode change; item and both index files roll back to their
        exact bytes+mode; the chmod'd racer survives at a named recovery
        location with its mode preserved; hook ran exactly once."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            lib, _al = index_paths(root)
            calls = {"n": 0}

            def chmod_hook():
                calls["n"] += 1
                os.chmod(lib, 0o600)

            with self.assertRaises(items.ItemConflict) as ctx:
                confirm(root, get_token(root), hook=chmod_hook)

            self.assertIn("library.json", str(ctx.exception))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the chmod'd racer survives with its 0600 mode preserved
            racer_paths = [
                p
                for p in (root / ".paper-notes" / "recovery").rglob("*")
                if p.is_file() and p.name == "library.json"
            ]
            self.assertEqual(len(racer_paths), 1)
            self.assertEqual(stat.S_IMODE(racer_paths[0].lstat().st_mode), 0o600)

    def test_hook_type_swap_index_conflicts(self):
        """hook 期间 file->directory type swap of library.json: detected
        by the post-hook authority; the item and both index files are
        restored (library.json is a regular file again with its exact
        bytes+mode) and the swapped directory is preserved at a named
        in-vault recovery location; hook ran exactly once."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            lib, _al = index_paths(root)
            calls = {"n": 0}

            def swap_hook():
                calls["n"] += 1
                lib.unlink()
                lib.mkdir()

            with self.assertRaises(items.ItemConflict) as ctx:
                confirm(root, get_token(root), hook=swap_hook)

            self.assertIn("library.json", str(ctx.exception))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertTrue(lib.is_dir() is False)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the swapped directory was preserved inside the vault
            dir_racers = [
                p
                for p in (root / ".paper-notes" / "recovery").rglob("*")
                if p.is_dir() and p.name == "library.json"
            ]
            self.assertEqual(len(dir_racers), 1)
            self.assertTrue(dir_racers[0].resolve().is_relative_to(root.resolve()))

    def test_hook_symlink_swap_index_conflicts(self):
        """hook 期间 file->symlink swap of library.json: detected by the
        post-hook authority; the item and both index files are restored
        and the swapped symlink is preserved untouched at a named
        in-vault recovery location (never followed, never written
        through); hook ran exactly once."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            lib, _al = index_paths(root)
            calls = {"n": 0}

            def swap_hook():
                calls["n"] += 1
                lib.unlink()
                lib.symlink_to("outside-target")

            with self.assertRaises(items.ItemConflict) as ctx:
                confirm(root, get_token(root), hook=swap_hook)

            self.assertIn("library.json", str(ctx.exception))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(lib.is_symlink())
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            # the swapped symlink survives untouched inside the vault
            link_racers = [
                p
                for p in (root / ".paper-notes" / "recovery").rglob("*")
                if p.is_symlink() and p.name == "library.json"
            ]
            self.assertEqual(len(link_racers), 1)
            self.assertEqual(os.readlink(link_racers[0]), "outside-target")

    def test_failure_at_finalize_hook_restores_item_and_indexes(self):
        """rebuild hook 在 finalize 内失败: an exception raised by the
        hook inside the participant's finalize is an ItemError that
        rolls the item AND both index files back to their exact
        bytes+mode with no residue (the hook itself ran once)."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            hook = mock.Mock(side_effect=RuntimeError("rebuild boom"))
            with self.assertRaises(items.ItemError):
                confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(note(root, OLD).read_bytes(), original_note)
            assert_index_restored(self, root, index_before)
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            hook.assert_called_once()

    def test_rollback_failure_surfaces_sanitized(self):
        """rollback 自身失败: a failing rollback surfaces as a sanitized
        ItemError and never leaks the underlying exception text."""
        td, root, before, index_before, original_note, hook = self._scenario()
        with td:
            secret = "secret: /Users/evil/path sk-1234567890abcdef"
            real_rollback = fsops.rollback

            def exploding_rollback(op):
                real_rollback(op)
                raise OSError(secret)

            hook = mock.Mock(side_effect=RuntimeError("hook boom"))
            with mock.patch(
                "paper_notes.deletion.fsops.rollback", side_effect=exploding_rollback
            ):
                with self.assertRaises(items.ItemError) as ctx:
                    confirm(root, get_token(root), hook=hook)
            self.assertNotIn(secret, str(ctx.exception))
            self.assertNotIn("sk-", str(ctx.exception))
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_success_hook_writes_outside_transaction_preserved(self):
        """success exactly-once: a legitimate hook side effect on a path
        OUTSIDE the transaction targets (the authority only guards the
        staged targets) is preserved, the item deletion and both index
        files' managed new state are visible together, the hook ran
        exactly once, and no staging/work/lock residue remains."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            removed_rel = sorted(
                [str(p.relative_to(root)) for p in item(root, OLD).rglob("*")]
                + [str(item(root, OLD).relative_to(root))]
            )
            lib, al, lib_b, al_b, lib_m, al_m = seed_index(
                root,
                library={"papers": {OLD: PAPER_ID, OTHER: "other-uuid"}},
                aliases={"aliases": {ALIAS: OLD, "smithAlias": "smithExample2026"}},
            )
            scratch = root / LIT / "hook-scratch.md"
            scratch_bytes = b"# hook side effect\n"
            calls = {"n": 0}
            real_finalize = deletion.IndexParticipant.finalize
            finalizes = []

            def side_writing_hook():
                calls["n"] += 1
                scratch.write_bytes(scratch_bytes)

            def counting_finalize(self, hook_):
                finalizes.append(1)
                return real_finalize(self, hook_)

            with mock.patch.object(
                deletion.IndexParticipant, "finalize", counting_finalize
            ):
                result = confirm(root, get_token(root), hook=side_writing_hook)

            self.assertEqual(result.status, "deleted")
            self.assertEqual(calls["n"], 1)
            self.assertEqual(finalizes, [1])
            self.assertFalse(item(root, OLD).exists())
            self.assertEqual(
                json.loads(lib.read_text()), {"papers": {OTHER: "other-uuid"}}
            )
            self.assertEqual(
                json.loads(al.read_text()), {"aliases": {"smithAlias": "smithExample2026"}}
            )
            # the hook's own side effect is preserved untouched
            self.assertEqual(scratch.read_bytes(), scratch_bytes)
            after = vault_manifest(root)
            added, removed, changed = manifest_diff(before, after)
            self.assertEqual(added, [str(scratch.relative_to(root))])
            self.assertEqual(changed, [])
            self.assertEqual(removed, removed_rel)
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self.assertFalse(workdir(root).exists())


class CleanupFailureTest(unittest.TestCase):
    """Frozen repair-R5 regressions (Decision A): the second final
    authority inside IndexParticipant.finalize is the deletion's
    linearization point — after it the item deletion and both index
    publications are formally effective and are NEVER rolled back
    because of cleanup trouble.

    Staging cleanup is best-effort; when it fails, silently no-ops or
    leaves residue, confirm_delete returns the structured
    deleted_with_cleanup_required status (never a silent 'deleted')
    with the desensitized operation id, the exact vault-relative
    residue paths and idempotent retry-cleanup guidance, the item
    stays absent and both index files keep their published new
    bytes+mode. retry_cleanup() is idempotent, vault-bound and
    symlink-safe: a symlink or non-directory planted at the staging
    path or its .staging parent is never followed, deleted or
    overwritten and is reported as residue with zero outside writes.
    """

    def _scenario(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        make_vault(root)
        index_before = seed_index(
            root,
            library={"papers": {OLD: PAPER_ID, OTHER: "other-uuid"}},
            aliases={"aliases": {ALIAS: OLD, "smithAlias": "smithExample2026"}},
        )
        hook = mock.Mock()
        return td, root, index_before, hook

    def _assert_published(self, root, index_before):
        lib, al = index_paths(root)
        self.assertEqual(
            json.loads(lib.read_text()), {"papers": {OTHER: "other-uuid"}}
        )
        self.assertEqual(
            json.loads(al.read_text()),
            {"aliases": {"smithAlias": "smithExample2026"}},
        )
        self.assertEqual(stat.S_IMODE(lib.lstat().st_mode), stat.S_IMODE(index_before[4]))
        self.assertEqual(stat.S_IMODE(al.lstat().st_mode), stat.S_IMODE(index_before[5]))

    def test_cleanup_noop_residue_returns_cleanup_required(self):
        """_remove_staging 被 mock 为 no-op（静默残留）: 不得再返回
        'deleted' — 返回结构化 deleted_with_cleanup_required；item 仍
        absent、两 index 保持已发布新 bytes/mode、残留路径准确且脱敏
        （vault 相对路径）、hook 恰好一次、无回滚."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch("paper_notes.deletion._remove_staging"):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            self.assertEqual(result.action, "delete")
            self.assertEqual(result.citation_key, OLD)
            self.assertEqual(result.paper_id, PAPER_ID)
            # operation id: generated, non-sensitive, matches the residue dir
            self.assertRegex(result.operation_id, r"^[a-z0-9]{32}$")
            staging = root / ".paper-notes" / ".staging" / result.operation_id
            self.assertTrue(staging.is_dir())  # the residue is real
            self.assertEqual(
                result.residue, (f".paper-notes/.staging/{result.operation_id}",)
            )
            for entry in result.residue:
                self.assertFalse(Path(entry).is_absolute())  # desensitized
                self.assertTrue(entry.startswith(".paper-notes/"))
            self.assertIn(result.operation_id, result.retry)
            self.assertIn("retry_cleanup", result.retry)
            # the deletion is effective and never rolled back
            hook.assert_called_once_with()
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse(item(root, OLD).is_symlink())
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self._assert_published(root, index_before)

    def test_cleanup_exception_returns_cleanup_required(self):
        """_remove_staging 抛异常: cleanup 不得让已生效事务失败 — 异常
        被吞掉、残留被检测，返回 deleted_with_cleanup_required；item
        仍 absent、两 index 保持已发布、hook 恰好一次、无回滚."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch(
                "paper_notes.deletion._remove_staging",
                side_effect=OSError("cleanup boom"),
            ):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            staging = root / ".paper-notes" / ".staging" / result.operation_id
            self.assertTrue(staging.is_dir())
            self.assertEqual(
                result.residue, (f".paper-notes/.staging/{result.operation_id}",)
            )
            hook.assert_called_once_with()
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self._assert_published(root, index_before)

    def test_partial_cleanup_residue_returns_cleanup_required(self):
        """部分清理（fd 锚定递归删除删掉内容但留下目录本身）: 残留检测
        必须发现目录仍在并返回 deleted_with_cleanup_required.

        (R6 seam change: the injection moved from the pathname
        ``shutil.rmtree`` to the fd-anchored ``_rmtree_fd`` — the
        assertions are unchanged.)"""
        td, root, index_before, hook = self._scenario()
        with td:
            def partial_cleanup(fd):
                # remove every entry except the last one: the staging
                # directory itself must survive as the residue
                with os.scandir(fd) as it:
                    names = [e.name for e in it]
                for name in names[:-1]:
                    try:
                        os.unlink(name, dir_fd=fd)
                    except OSError:
                        pass
                return False
            with mock.patch(
                "paper_notes.deletion._rmtree_fd", side_effect=partial_cleanup
            ):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            staging = root / ".paper-notes" / ".staging" / result.operation_id
            self.assertTrue(staging.is_dir())  # partial residue
            self.assertEqual(
                result.residue, (f".paper-notes/.staging/{result.operation_id}",)
            )
            hook.assert_called_once_with()
            self.assertFalse(item(root, OLD).exists())
            self._assert_published(root, index_before)

    def test_retry_cleanup_idempotent_clears_residue(self):
        """retry_cleanup: 首次清理残留 → cleaned；重复调用幂等；未知
        operation 幂等 clean；非法 operation id 拒绝（ValueError, 零
        文件系统访问）；已生效删除与已发布 index 不受影响."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch("paper_notes.deletion._remove_staging"):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            op_id = result.operation_id
            staging = root / ".paper-notes" / ".staging" / op_id
            self.assertTrue(staging.is_dir())
            outcome = deletion.retry_cleanup(root, operation_id=op_id)
            self.assertTrue(outcome.cleaned)
            self.assertEqual(outcome.residue, ())
            self.assertEqual(outcome.operation_id, op_id)
            self.assertFalse(staging.exists())
            # idempotent repeat
            again = deletion.retry_cleanup(root, operation_id=op_id)
            self.assertTrue(again.cleaned)
            self.assertEqual(again.residue, ())
            # unknown operation: clean no-op
            unknown = deletion.retry_cleanup(root, operation_id="f" * 32)
            self.assertTrue(unknown.cleaned)
            self.assertEqual(unknown.residue, ())
            # invalid operation ids are rejected before any filesystem access
            for bad in ("../evil", "/abs/path", "has space", "UPPER", "a" * 33):
                with self.assertRaises(ValueError):
                    deletion.retry_cleanup(root, operation_id=bad)
            # the effective deletion stays untouched
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self._assert_published(root, index_before)

    def test_retry_cleanup_refuses_staging_symlink_zero_outside_writes(self):
        """staging 路径被换成指向 vault 外目录的 symlink: retry_cleanup
        拒绝删除（lstat 边界检查, 绝不穿越 symlink, 绝不覆盖外部文件），
        报告该路径为残留, outside 目录零写入, symlink 原样保留; 重复
        调用幂等."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                staging = root / ".paper-notes" / ".staging" / result.operation_id
                self.assertTrue(staging.is_dir())
                shutil.rmtree(staging)  # remove the real dir, then plant a symlink
                staging.symlink_to(outside, target_is_directory=True)
                outcome = deletion.retry_cleanup(
                    root, operation_id=result.operation_id
                )
                self.assertFalse(outcome.cleaned)
                self.assertEqual(
                    outcome.residue,
                    (f".paper-notes/.staging/{result.operation_id}",),
                )
                self.assertTrue(staging.is_symlink())
                self.assertEqual(os.readlink(staging), str(outside))
                self.assertEqual(sorted(os.listdir(outside)), [])
                # idempotent refusal
                again = deletion.retry_cleanup(
                    root, operation_id=result.operation_id
                )
                self.assertFalse(again.cleaned)
                self.assertEqual(again.residue, outcome.residue)

    def test_retry_cleanup_refuses_symlinked_staging_parent(self):
        """.staging 父目录被换成指向 vault 外目录的 symlink:
        retry_cleanup 拒绝（父链 lstat 检查失败, 零外部写入）, 报告
        .staging 自身为残留, symlink 原样保留."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                staging_parent = root / ".paper-notes" / ".staging"
                os.replace(staging_parent, staging_parent.parent / ".staging.real")
                staging_parent.symlink_to(outside, target_is_directory=True)
                outcome = deletion.retry_cleanup(
                    root, operation_id=result.operation_id
                )
                self.assertFalse(outcome.cleaned)
                self.assertEqual(outcome.residue, (".paper-notes/.staging",))
                self.assertTrue(staging_parent.is_symlink())
                self.assertEqual(os.readlink(staging_parent), str(outside))
                self.assertEqual(sorted(os.listdir(outside)), [])

    def test_retry_cleanup_staging_swap_after_check_zero_outside_writes(self):
        """Frozen R6 race (manager repro): ``.staging`` is atomically
        renamed away and replaced with a symlink to an outside
        directory AFTER the real-dir-chain check returns true but
        BEFORE the pathname rmtree. Red on 55cf784 (the pathname
        rmtree follows the symlink and deletes outside/<opid>/ with
        its MUST_SURVIVE.txt sentinel); the dirfd-anchored cleanup
        keeps the outside directory byte-identical and zero-write and
        never follows the planted symlink."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                op_id = result.operation_id
                outside_op = outside / op_id
                outside_op.mkdir()
                sentinel = outside_op / "MUST_SURVIVE.txt"
                sentinel.write_bytes(b"sentinel-bytes")
                sentinel.chmod(0o640)
                staging_parent = root / ".paper-notes" / ".staging"
                real_chain = deletion._real_dir_chain
                state = {"swapped": False}

                def swap_on_true(p, vault):
                    ok = real_chain(p, vault)
                    if ok and not state["swapped"]:
                        state["swapped"] = True
                        os.replace(
                            staging_parent,
                            staging_parent.parent / ".staging.real",
                        )
                        staging_parent.symlink_to(outside, target_is_directory=True)
                    return ok

                with mock.patch(
                    "paper_notes.deletion._real_dir_chain",
                    side_effect=swap_on_true,
                ):
                    outcome = deletion.retry_cleanup(root, operation_id=op_id)
                # outside: zero writes, sentinel keeps exact bytes+mode
                self.assertEqual(sentinel.read_bytes(), b"sentinel-bytes")
                self.assertEqual(
                    stat.S_IMODE(sentinel.lstat().st_mode), 0o640
                )
                self.assertEqual(sorted(os.listdir(outside)), [op_id])
                self.assertEqual(
                    sorted(os.listdir(outside_op)), ["MUST_SURVIVE.txt"]
                )
                # the original vault staging residue was safely cleaned
                # (anchored) — never followed through the swapped parent
                self.assertTrue(outcome.cleaned)
                self.assertEqual(outcome.residue, ())
                self.assertFalse(
                    (root / ".paper-notes" / ".staging.real" / op_id).exists()
                )

    def test_retry_cleanup_staging_swap_after_anchor_zero_outside_writes(self):
        """Frozen R6 deep race: ``.staging`` is swapped to an outside
        symlink AFTER the dirfd chain is already anchored. The
        anchored recursive delete may only touch the original vault
        inode: outside stays byte-identical (MUST_SURVIVE.txt
        bytes+mode), zero outside writes, the symlink is never
        followed or deleted, and the residue is reported — never a
        silent clean while external state stays visible through the
        swapped parent. (Reverse validation: reverting to a pathname
        ``shutil.rmtree`` after the anchor makes this test red.)"""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                op_id = result.operation_id
                outside_op = outside / op_id
                outside_op.mkdir()
                sentinel = outside_op / "MUST_SURVIVE.txt"
                sentinel.write_bytes(b"sentinel-bytes")
                sentinel.chmod(0o640)
                staging_parent = root / ".paper-notes" / ".staging"
                real_open = deletion._open_dir_chain_fd
                state = {"swapped": False}

                def swap_after_anchor(vault, directory):
                    fd = real_open(vault, directory)
                    if fd is not None and not state["swapped"]:
                        state["swapped"] = True
                        os.replace(
                            staging_parent,
                            staging_parent.parent / ".staging.real",
                        )
                        staging_parent.symlink_to(outside, target_is_directory=True)
                    return fd

                with mock.patch(
                    "paper_notes.deletion._open_dir_chain_fd",
                    side_effect=swap_after_anchor,
                ):
                    outcome = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertEqual(sentinel.read_bytes(), b"sentinel-bytes")
                self.assertEqual(
                    stat.S_IMODE(sentinel.lstat().st_mode), 0o640
                )
                self.assertEqual(sorted(os.listdir(outside)), [op_id])
                self.assertEqual(
                    sorted(os.listdir(outside_op)), ["MUST_SURVIVE.txt"]
                )
                # the swapped parent symlink is never followed or deleted
                self.assertTrue(staging_parent.is_symlink())
                self.assertEqual(os.readlink(staging_parent), str(outside))
                # the original vault staging residue was safely cleaned
                self.assertFalse(
                    (root / ".paper-notes" / ".staging.real" / op_id).exists()
                )
                # residue is explicitly reported — external state visible
                # through the swapped parent is never a silent clean
                self.assertFalse(outcome.cleaned)
                self.assertEqual(
                    outcome.residue, (f".paper-notes/.staging/{op_id}",)
                )

    def test_retry_cleanup_midlevel_dir_to_symlink_swap_zero_outside_writes(self):
        """Frozen R6: a mid-level staging directory swapped to an
        outside symlink during the recursive delete is removed as the
        link itself (never followed): outside keeps its sentinel
        bytes+mode, zero outside writes, and the cleanup completes
        with the moved-aside original directory preserved."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                op_id = result.operation_id
                sentinel = outside / "keep.txt"
                sentinel.write_bytes(b"outside-bytes")
                sentinel.chmod(0o600)
                staging = root / ".paper-notes" / ".staging" / op_id
                sub = staging / "sub"
                sub.mkdir()
                (sub / "inner.txt").write_text("inner")
                real_rmtree = deletion._rmtree_fd
                state = {"swapped": False}

                def swap_sub_then_real(fd):
                    if not state["swapped"]:
                        state["swapped"] = True
                        os.replace(sub, root / ".paper-notes" / "sub.real")
                        sub.symlink_to(outside, target_is_directory=True)
                    return real_rmtree(fd)

                with mock.patch(
                    "paper_notes.deletion._rmtree_fd",
                    side_effect=swap_sub_then_real,
                ):
                    outcome = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertTrue(outcome.cleaned)
                self.assertEqual(outcome.residue, ())
                self.assertFalse(staging.exists())
                self.assertFalse(sub.is_symlink())
                self.assertEqual(
                    (root / ".paper-notes" / "sub.real" / "inner.txt").read_text(),
                    "inner",
                )
                self.assertEqual(sentinel.read_bytes(), b"outside-bytes")
                self.assertEqual(
                    stat.S_IMODE(sentinel.lstat().st_mode), 0o600
                )
                self.assertEqual(sorted(os.listdir(outside)), ["keep.txt"])

    def test_retry_cleanup_midlevel_swap_between_stat_and_open_fails_closed(self):
        """Frozen R6: a mid-level directory swapped to an outside
        symlink BETWEEN its lstat and its O_NOFOLLOW directory open
        fails closed: the symlink is left untouched and reported as
        residue, outside receives zero writes; the retry removes the
        link itself and completes."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                op_id = result.operation_id
                sentinel = outside / "keep.txt"
                sentinel.write_bytes(b"outside-bytes")
                sentinel.chmod(0o600)
                staging = root / ".paper-notes" / ".staging" / op_id
                sub = staging / "sub"
                sub.mkdir()
                (sub / "inner.txt").write_text("inner")
                real_open = os.open
                state = {"swapped": False}

                def swap_before_open(name, flags, mode=0o777, *, dir_fd=None):
                    if name == "sub" and not state["swapped"]:
                        state["swapped"] = True
                        os.replace(sub, root / ".paper-notes" / "sub.real")
                        sub.symlink_to(outside, target_is_directory=True)
                    return real_open(name, flags, mode, dir_fd=dir_fd)

                with mock.patch(
                    "paper_notes.deletion.os.open", side_effect=swap_before_open
                ):
                    outcome = deletion.retry_cleanup(root, operation_id=op_id)
                # fail closed: the swapped link is untouched, reported
                self.assertFalse(outcome.cleaned)
                self.assertEqual(
                    outcome.residue, (f".paper-notes/.staging/{op_id}",)
                )
                self.assertTrue(sub.is_symlink())
                self.assertEqual(os.readlink(sub), str(outside))
                self.assertEqual(sentinel.read_bytes(), b"outside-bytes")
                self.assertEqual(
                    stat.S_IMODE(sentinel.lstat().st_mode), 0o600
                )
                self.assertEqual(sorted(os.listdir(outside)), ["keep.txt"])
                # the retry removes the link itself and completes
                again = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertTrue(again.cleaned)
                self.assertEqual(again.residue, ())

    def test_retry_cleanup_midlevel_dir_to_file_swap(self):
        """Frozen R6: a mid-level staging directory swapped to a
        regular file during the recursive delete is removed as the
        entry itself (an anchored vault inode) — deterministic and
        zero outside involvement; the moved-aside original directory
        survives untouched."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch("paper_notes.deletion._remove_staging"):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            op_id = result.operation_id
            staging = root / ".paper-notes" / ".staging" / op_id
            sub = staging / "sub"
            sub.mkdir()
            (sub / "inner.txt").write_text("inner")
            real_rmtree = deletion._rmtree_fd
            state = {"swapped": False}

            def swap_sub_then_real(fd):
                if not state["swapped"]:
                    state["swapped"] = True
                    os.replace(sub, root / ".paper-notes" / "sub.real")
                    sub.write_text("racer-file")
                return real_rmtree(fd)

            with mock.patch(
                "paper_notes.deletion._rmtree_fd",
                side_effect=swap_sub_then_real,
            ):
                outcome = deletion.retry_cleanup(root, operation_id=op_id)
            self.assertTrue(outcome.cleaned)
            self.assertEqual(outcome.residue, ())
            self.assertFalse(sub.exists())
            self.assertEqual(
                (root / ".paper-notes" / "sub.real" / "inner.txt").read_text(),
                "inner",
            )

    def test_retry_cleanup_concurrent_late_entry_reported_as_residue(self):
        """Frozen R6: a file appearing inside the staging directory
        after its contents were deleted but before its rmdir (a
        concurrent late entry) fails the rmdir (ENOTEMPTY): the entry
        is preserved untouched, the residue is reported, and the retry
        completes. Red on 55cf784 (the pathname rmtree never goes
        through this seam — the plant never triggers and the cleanup
        is silently reported clean)."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch("paper_notes.deletion._remove_staging"):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            op_id = result.operation_id
            staging = root / ".paper-notes" / ".staging" / op_id
            real_rmdir = os.rmdir
            state = {"planted": False}

            def late_entry_rmdir(name, *, dir_fd=None):
                if name == op_id and not state["planted"]:
                    state["planted"] = True
                    (staging / "late.txt").write_text("racer")
                return real_rmdir(name, dir_fd=dir_fd)

            with mock.patch(
                "paper_notes.deletion.os.rmdir", side_effect=late_entry_rmdir
            ):
                outcome = deletion.retry_cleanup(root, operation_id=op_id)
            self.assertFalse(outcome.cleaned)
            self.assertEqual(
                outcome.residue, (f".paper-notes/.staging/{op_id}",)
            )
            self.assertEqual((staging / "late.txt").read_text(), "racer")
            # retry without the race completes
            again = deletion.retry_cleanup(root, operation_id=op_id)
            self.assertTrue(again.cleaned)
            self.assertEqual(again.residue, ())

    def test_retry_cleanup_partial_residue_retry_cleans(self):
        """Partial deletion (a non-writable subdirectory blocks the
        removal of its file): residue is reported with the blocked
        file preserved byte-identical; after restoring permissions the
        retry completes — real filesystem, no mocks."""
        td, root, index_before, hook = self._scenario()
        with td:
            with mock.patch("paper_notes.deletion._remove_staging"):
                result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted_with_cleanup_required")
            op_id = result.operation_id
            staging = root / ".paper-notes" / ".staging" / op_id
            blocked = staging / "blocked"
            blocked.mkdir()
            payload = blocked / "payload.txt"
            payload.write_bytes(b"blocked-bytes")
            payload.chmod(0o600)
            blocked.chmod(0o555)
            try:
                outcome = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertFalse(outcome.cleaned)
                self.assertEqual(
                    outcome.residue, (f".paper-notes/.staging/{op_id}",)
                )
                self.assertEqual(payload.read_bytes(), b"blocked-bytes")
                self.assertEqual(
                    stat.S_IMODE(payload.lstat().st_mode), 0o600
                )
                # restore permissions: the retry completes
                blocked.chmod(0o755)
                again = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertTrue(again.cleaned)
                self.assertEqual(again.residue, ())
            finally:
                # restore permissions for fixture cleanup; the retry
                # above may already have removed the directory
                try:
                    blocked.chmod(0o755)
                except FileNotFoundError:
                    pass

    def test_retry_cleanup_removes_deep_tree_and_entry_symlinks(self):
        """Deep nested directories and an entry symlink pointing at an
        outside directory: the recursive anchored delete removes the
        whole tree and the link itself (never followed) — outside
        keeps its sentinel bytes+mode, zero outside writes, cleaned;
        idempotent repeat."""
        td, root, index_before, hook = self._scenario()
        with tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            with td:
                with mock.patch("paper_notes.deletion._remove_staging"):
                    result = confirm(root, get_token(root), hook=hook)
                self.assertEqual(result.status, "deleted_with_cleanup_required")
                op_id = result.operation_id
                sentinel = outside / "keep.txt"
                sentinel.write_bytes(b"outside-bytes")
                sentinel.chmod(0o600)
                staging = root / ".paper-notes" / ".staging" / op_id
                deep = staging / "a" / "b" / "c"
                deep.mkdir(parents=True)
                (deep / "deep.txt").write_bytes(b"deep-bytes")
                (staging / "escape_link").symlink_to(
                    outside, target_is_directory=True
                )
                outcome = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertTrue(outcome.cleaned)
                self.assertEqual(outcome.residue, ())
                self.assertFalse(staging.exists())
                self.assertFalse((staging / "escape_link").exists())
                self.assertEqual(sentinel.read_bytes(), b"outside-bytes")
                self.assertEqual(
                    stat.S_IMODE(sentinel.lstat().st_mode), 0o600
                )
                self.assertEqual(sorted(os.listdir(outside)), ["keep.txt"])
                # idempotent repeat
                again = deletion.retry_cleanup(root, operation_id=op_id)
                self.assertTrue(again.cleaned)
                self.assertEqual(again.residue, ())

    def test_normal_success_with_clean_cleanup_stays_deleted(self):
        """真实（未 mock）cleanup 全部成功: 结果保持既有 'deleted'
        状态, 无残留, item absent, 两 index 已发布, 全残留清零."""
        td, root, index_before, hook = self._scenario()
        with td:
            result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "deleted")
            self.assertRegex(result.operation_id, r"^[a-z0-9]{32}$")
            self.assertEqual(result.residue, ())
            self.assertEqual(result.retry, "")
            hook.assert_called_once_with()
            self.assertFalse(item(root, OLD).exists())
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])
            self._assert_published(root, index_before)


class RecoverySeamTest(unittest.TestCase):
    """Frozen repair-R3 regressions at the racer->recovery seam: the
    dirfd-anchored no-replace move preserves a file that appears at the
    recovery destination after its last check (seed bytes untouched,
    racer lands at a fresh in-vault path), and a recovery parent
    swapped to an outside symlink after the last check receives zero
    outside writes."""

    def test_late_recovery_destination_file_lands_at_fresh_invault_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            seed_index(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            work = workdir(root)
            racer_content = b"external racer: reappeared at a deleted target"
            seed_content = b"seed bytes: pre-existing recovery material must survive"
            real_commit = fsops.commit
            real_move = fsops.no_replace_move
            seeded_at = []
            recovery_base = root / ".paper-notes" / "recovery"

            def seed_then_racer(op):
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_commit(op)

            def seed_dest_then_move(src, dst, **kw):
                dst = Path(dst)
                if dst.is_relative_to(recovery_base) and not seeded_at:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(seed_content)  # appears after the last check
                    seeded_at.append(dst)
                return real_move(src, dst, **kw)

            with mock.patch(
                "paper_notes.deletion.fsops.commit", side_effect=seed_then_racer
            ), mock.patch(
                "paper_notes.deletion.fsops.no_replace_move", side_effect=seed_dest_then_move
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
            # seed bytes unchanged at their exact path
            self.assertEqual(seeded_at[0].read_bytes(), seed_content)
            # the racer landed at a DIFFERENT in-vault recovery path
            racer_paths = [
                p
                for p in recovery_base.rglob("*")
                if p.is_file() and p.read_bytes() == racer_content
            ]
            self.assertEqual(len(racer_paths), 1)
            self.assertNotEqual(racer_paths[0], seeded_at[0])
            self.assertTrue(racer_paths[0].resolve().is_relative_to(root.resolve()))

    def test_recovery_parent_symlink_swap_zero_outside_writes(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as otd:
            outside = Path(otd)
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            seed_index(root)
            original_note = note(root, OLD).read_bytes()
            hook = mock.Mock()
            work = workdir(root)
            racer_content = b"external racer: must never leave the vault"
            real_commit = fsops.commit
            real_move = fsops.no_replace_move
            swapped = {"n": 0}
            recovery_base = root / ".paper-notes" / "recovery"

            def racer_at_commit(op):
                target = next(
                    t
                    for t in op.targets
                    if work in t.parents and t.name == f"{OLD}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racer_content)
                return real_commit(op)

            def swap_parent_then_move(src, dst, **kw):
                dst = Path(dst)
                if dst.is_relative_to(recovery_base) and not swapped["n"]:
                    swapped["n"] = 1
                    parent = dst.parent
                    os.replace(parent, parent.parent / f"{parent.name}.real")
                    parent.symlink_to(outside, target_is_directory=True)
                return real_move(src, dst, **kw)

            with mock.patch(
                "paper_notes.deletion.fsops.commit", side_effect=racer_at_commit
            ), mock.patch(
                "paper_notes.deletion.fsops.no_replace_move",
                side_effect=swap_parent_then_move,
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
            # zero outside writes; planted symlink untouched
            self.assertEqual(sorted(os.listdir(outside)), [])
            symlinks = [p for p in recovery_base.iterdir() if p.is_symlink()]
            self.assertEqual(len(symlinks), 1)
            self.assertEqual(os.readlink(symlinks[0]), str(outside))
            # the racer was preserved INSIDE the vault at a fresh path
            racer_paths = [
                p
                for p in recovery_base.rglob("*")
                if p.is_file() and p.read_bytes() == racer_content
            ]
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
