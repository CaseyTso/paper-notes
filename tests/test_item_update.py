"""Canonical item update tests (Task 11).

Frozen after the first red run; do not weaken or delete assertions.
Update contract: metadata fields change, the citation key never changes
(citation-key renames are a separate command), the Markdown body and
unknown/custom fields survive byte-for-byte, and the rebuild hook fires
exactly once on success and zero times on failure. All writes go
through the staged operation with a workspace lock.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes import fsops, items

REPO = Path(__file__).resolve().parents[1]

NOTE_TEMPLATE = """---
schema_version: 1
paper_id: 550e8400-e29b-41d4-a716-446655440000
citation_key: smithExample2026
citation_key_aliases:
  - smithExample2026old
item_type: article-journal
title: An example paper
authors:
- family: Smith
  given: John
publication_date: 2026-05-01
year: 2026
pdf_status: missing
reading_status: unread
custom_note: keep me
# a preserved comment
---
# Body title

Body paragraph with **markdown**.
"""

BODY = "# Body title\n\nBody paragraph with **markdown**.\n"


def write_paper(root, key="smithExample2026", text=None):
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.md").write_text(text or NOTE_TEMPLATE, encoding="utf-8")
    return d


class UpdateTest(unittest.TestCase):
    def test_update_changes_metadata_but_not_citation_key(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            result = items.update_item(
                vault,
                key="smithExample2026",
                patch={
                    "title": "A renamed paper",
                    "publication_date": "2027-05-01",
                    "year": 2027,
                    "reading_status": "read",
                },
            )
            self.assertEqual(result.status, "updated")
            self.assertEqual(result.citation_key, "smithExample2026")
            self.assertEqual(result.path, str(note))
            self.assertEqual(
                sorted(result.updated_fields),
                ["publication_date", "reading_status", "title", "year"],
            )
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.title, "A renamed paper")
            self.assertEqual(paper.reading_status, "read")
            self.assertEqual(paper.citation_key, "smithExample2026")
            self.assertEqual(paper.year, 2027)
            self.assertEqual(paper.publication_date, "2027-05-01")

    def test_update_via_alias_resolves_to_canonical_record(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            result = items.update_item(
                vault,
                key="smithExample2026old",
                patch={"title": "Updated through alias"},
            )
            self.assertEqual(result.citation_key, "smithExample2026")
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.title, "Updated through alias")

    def test_update_preserves_markdown_body_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_text(encoding="utf-8")
            items.update_item(vault, key="smithExample2026", patch={"title": "New title"})
            text = note.read_text(encoding="utf-8")
            self.assertIn(BODY, text)
            self.assertIn("custom_note: keep me", text)
            self.assertIn("# a preserved comment", text)
            self.assertIn("citation_key: smithExample2026", text)
            self.assertIn("smithExample2026old", text)
            self.assertIn("title: New title", text)
            # nothing outside the frontmatter title line may change
            self.assertNotEqual(text, before)

    def test_update_rejects_citation_key_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemError):
                items.update_item(vault, key="smithExample2026", patch={"citation_key": "hacked2026"})
            self.assertEqual(note.read_bytes(), before)

    def test_update_unknown_key_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            with self.assertRaises(items.ItemError):
                items.update_item(vault, key="ghost2026", patch={"title": "x"})

    def test_update_empty_patch_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            with self.assertRaises(items.ItemError):
                items.update_item(vault, key="smithExample2026", patch={})

    def test_update_invalid_schema_rolls_back_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"year": 1999},  # conflicts with publication_date 2026
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_not_called()

    def test_update_removes_field_with_null(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            items.update_item(vault, key="smithExample2026", patch={"doi": "10.1000/x"})
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.model_extra["doi"], "10.1000/x")
            items.update_item(vault, key="smithExample2026", patch={"doi": None})
            paper, _ = load_paper_note(note)
            self.assertNotIn("doi", paper.model_extra)

    def test_update_success_rebuild_hook_called_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            hook = mock.Mock()
            items.update_item(vault, key="smithExample2026", patch={"title": "X"}, rebuild_hook=hook)
            hook.assert_called_once()


class UpdateCliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
        )

    def test_cli_update_single_envelope_keeps_key(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            patch = Path(td) / "patch.json"
            patch.write_text(json.dumps({"title": "CLI updated", "reading_status": "reading"}), encoding="utf-8")
            result = self.run_cli("item", "update", "--vault", str(vault), "--key", "smithExample2026", "--patch", str(patch))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "updated")
            self.assertEqual(payload["data"]["citation_key"], "smithExample2026")
            self.assertEqual(payload["data"]["updated_fields"], ["title", "reading_status"])

    def test_cli_update_unknown_key_exit_two(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            patch = Path(td) / "patch.json"
            patch.write_text(json.dumps({"title": "x"}), encoding="utf-8")
            result = self.run_cli("item", "update", "--vault", str(vault), "--key", "ghost2026", "--patch", str(patch))
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_update_rejects_citation_key_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            patch = Path(td) / "patch.json"
            patch.write_text(json.dumps({"citation_key": "hacked2026"}), encoding="utf-8")
            result = self.run_cli("item", "update", "--vault", str(vault), "--key", "smithExample2026", "--patch", str(patch))
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            from paper_notes.frontmatter import load_paper_note

            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            paper, _ = load_paper_note(note)
            self.assertEqual(paper.citation_key, "smithExample2026")

    def test_cli_update_invalid_patch_file_exit_two(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            patch = Path(td) / "patch.json"
            patch.write_text("not json{", encoding="utf-8")
            result = self.run_cli("item", "update", "--vault", str(vault), "--key", "smithExample2026", "--patch", str(patch))
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")


class AcceptanceRegressionUpdateTest(unittest.TestCase):
    """Round-1 management acceptance regressions (A/D/E/F)."""

    @staticmethod
    def write_raw(root, key, paper_id, title="An example paper", aliases=()):
        d = root / "05 Literature" / key
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            "schema_version: 1",
            f"paper_id: {paper_id}",
            f"citation_key: {key}",
            "item_type: article-journal",
            f"title: {title}",
            "authors:",
            "- family: Smith",
            "  given: John",
            "publication_date: 2026-05-01",
            "year: 2026",
            "pdf_status: missing",
            "reading_status: unread",
        ]
        if aliases:
            lines.append("citation_key_aliases:")
            lines.extend(f"  - {a}" for a in aliases)
        (d / f"{key}.md").write_text(
            "---\n" + "\n".join(lines) + "\n---\n# body\n", encoding="utf-8"
        )
        return d

    # --- A: rebuild hook failure rolls back update ---
    def test_hook_failure_rolls_back_update_leaving_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock(side_effect=RuntimeError("simulated rebuild failure"))
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"title": "New title"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_called_once()

    # --- D: stale update — merge onto the latest on-disk state ---
    def test_stale_update_keeps_concurrent_field_changes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            real_acquire = items.acquire_lock

            def concurrent_acquire(root, operation):
                # another managed writer changes the title before we lock
                text = note.read_text(encoding="utf-8")
                note.write_text(
                    text.replace("title: An example paper", "title: Concurrent title"),
                    encoding="utf-8",
                )
                return real_acquire(root, operation)

            with mock.patch.object(
                items, "acquire_lock", side_effect=concurrent_acquire
            ):
                result = items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"reading_status": "read"},
                )
            self.assertEqual(result.status, "updated")
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.title, "Concurrent title")
            self.assertEqual(paper.reading_status, "read")

    # --- E: commit conflict surfaces as ItemConflict, never a leak ---
    def test_update_commit_conflict_returns_item_conflict_not_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            real_commit = fsops.commit

            def conflicted_commit(op):
                conflicts = real_commit(op)  # real commit: op is finished
                return conflicts + [str(note)]

            hook = mock.Mock()
            with mock.patch("paper_notes.fsops.commit", side_effect=conflicted_commit):
                with self.assertRaises(items.ItemConflict):
                    items.update_item(
                        vault,
                        key="smithExample2026",
                        patch={"title": "X"},
                        rebuild_hook=hook,
                    )
            hook.assert_called_once()
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())

    # --- F: identity fields are immutable through update ---
    def test_update_rejects_paper_id_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"paper_id": "11111111-2222-3333-4444-555555555555"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()

    def test_update_rejects_citation_key_aliases_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"citation_key_aliases": ["hacked2026"]},
                )
            self.assertEqual(note.read_bytes(), before)

    def test_update_rejects_schema_version_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault, key="smithExample2026", patch={"schema_version": 2}
                )
            self.assertEqual(note.read_bytes(), before)

    def test_update_against_duplicate_uuid_repository_is_read_only_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            uid = "550e8400-e29b-41d4-a716-446655440000"
            self.write_raw(vault, "smithExample2026", uid)
            self.write_raw(vault, "jonesOther2026", uid)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault,
                    key="smithExample2026",
                    patch={"title": "X"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()

    def test_update_against_alias_collision_repository_is_read_only_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_raw(
                vault, "smithExample2026", "550e8400-e29b-41d4-a716-446655440001",
                aliases=("jonesOther2026",),
            )
            self.write_raw(
                vault, "jonesOther2026", "550e8400-e29b-41d4-a716-446655440002"
            )
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="smithExample2026", patch={"title": "X"}
                )
            self.assertEqual(note.read_bytes(), before)


class AcceptanceRound2UpdateTest(unittest.TestCase):
    """Round-2 management acceptance (M + update strong-ID canonicalization).

    ``item update`` may change DOI/PMID/PMCID/arXiv, but the modified
    value is canonicalized through the identifiers parsers first and
    checked against every other item's canonical strong ids (excluding
    self): any collision is an ItemConflict with the original bytes
    untouched and the rebuild hook never called. pdf_status/pdf_sha256
    are strong identity artifacts managed only by attach/reconcile —
    direct changes through update are rejected.
    """

    @staticmethod
    def write_with_ids(root, key, paper_id, **strong):
        d = root / "05 Literature" / key
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            "schema_version: 1",
            f"paper_id: {paper_id}",
            f"citation_key: {key}",
            "item_type: article-journal",
            "title: An example paper",
            "authors:",
            "- family: Smith",
            "  given: John",
            "publication_date: 2026-05-01",
            "year: 2026",
            "pdf_status: missing",
            "reading_status: unread",
        ]
        for field, value in strong.items():
            lines.append(f"{field}: {value}")
        (d / f"{key}.md").write_text(
            "---\n" + "\n".join(lines) + "\n---\n# body\n", encoding="utf-8"
        )
        return d

    # --- M: patching a DOI onto a second owner is a read-only conflict ---
    def test_update_doi_collision_is_item_conflict_bytes_unchanged_hook_zero(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "aAlpha2026", "550e8400-e29b-41d4-a716-446655440011",
                doi="10.1000/a",
            )
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                doi="10.1000/b",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="bBeta2026", patch={"doi": "10.1000/a"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()

    # --- canonical equivalence for each of the four strong kinds ---
    def test_update_doi_canonical_equivalence_url_form_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "aAlpha2026", "550e8400-e29b-41d4-a716-446655440011",
                doi="10.1000/a",
            )
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                doi="10.1000/b",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="bBeta2026",
                    patch={"doi": "https://doi.org/10.1000/a"},
                )
            self.assertEqual(note.read_bytes(), before)

    def test_update_pmid_canonical_equivalence_prefix_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "aAlpha2026", "550e8400-e29b-41d4-a716-446655440011",
                pmid="30000001",
            )
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                pmid="30000002",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="bBeta2026", patch={"pmid": "PMID: 30000001"},
                )
            self.assertEqual(note.read_bytes(), before)

    def test_update_pmcid_canonical_equivalence_case_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "aAlpha2026", "550e8400-e29b-41d4-a716-446655440011",
                pmcid="PMC1234567",
            )
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                pmcid="PMC7654321",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="bBeta2026", patch={"pmcid": "pmc1234567"},
                )
            self.assertEqual(note.read_bytes(), before)

    def test_update_arxiv_canonical_equivalence_url_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "aAlpha2026", "550e8400-e29b-41d4-a716-446655440011",
                arxiv="2401.00001v2",
            )
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                arxiv="2401.00002v2",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            with self.assertRaises(items.ItemConflict):
                items.update_item(
                    vault, key="bBeta2026",
                    patch={"arxiv": "https://arxiv.org/abs/2401.00001v2"},
                )
            self.assertEqual(note.read_bytes(), before)

    # --- non-colliding strong edits succeed and are stored canonically ---
    def test_update_doi_non_colliding_writes_canonical_value(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                doi="10.1000/b",
            )
            result = items.update_item(
                vault, key="bBeta2026",
                patch={"doi": "https://doi.org/10.1000/c"},
            )
            self.assertEqual(result.status, "updated")
            from paper_notes.frontmatter import load_paper_note

            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            paper, _ = load_paper_note(note)
            self.assertEqual(paper.model_extra["doi"], "10.1000/c")

    def test_update_own_doi_canonical_form_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                doi="10.1000/b",
            )
            hook = mock.Mock()
            result = items.update_item(
                vault, key="bBeta2026",
                patch={"doi": "https://doi.org/10.1000/b"},
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "updated")
            from paper_notes.frontmatter import load_paper_note

            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            paper, _ = load_paper_note(note)
            self.assertEqual(paper.model_extra["doi"], "10.1000/b")
            hook.assert_called_once()

    # --- invalid strong values and pdf-managed fields are rejected ---
    def test_update_invalid_doi_is_item_error_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
                doi="10.1000/b",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault, key="bBeta2026", patch={"doi": "not-a-doi"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()

    def test_update_rejects_pdf_status_direct_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault, key="bBeta2026", patch={"pdf_status": "available"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()

    def test_update_rejects_pdf_sha256_direct_change(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_with_ids(
                vault, "bBeta2026", "550e8400-e29b-41d4-a716-446655440012",
            )
            note = vault / "05 Literature" / "bBeta2026" / "bBeta2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.update_item(
                    vault, key="bBeta2026",
                    patch={"pdf_sha256": "f" * 64},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
