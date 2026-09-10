"""Read-only metadata preview tests for manual import (Phase C1).

Verifies that previewing an item import:
- Acquires no lock and creates no .paper-notes directory.
- Makes zero disk writes (exact byte-for-byte vault manifest equality).
- Invokes rebuild_hook zero times.
- Returns CreateResult and JSON Envelope with status="needs_confirmation".
- Correctly distinguishes: ready to create, needs confirmation (conflicts),
  fuzzy duplicate match, existing item hit (strong identifier or PDF hash),
  resolution failure (error), and empty input (error).
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from paper_notes import items
from paper_notes.identifiers import ParsedIdentifier
from paper_notes.metadata import FieldConflict, MetadataCandidate, ResolutionError

REPO = Path(__file__).resolve().parents[1]

DOI = "10.1038/s41591-024-00000-0"
PMID = "38702444"

CROSSREF_VALUES = {
    "title": "Spatially resolved analysis of lung adenocarcinoma",
    "authors": [{"family": "Shiau", "given": "Chia-Yu"}],
    "publication_date": "2024-05-01",
    "year": 2024,
    "journal": "Nature Medicine",
    "journal_abbreviation": "Nat Med",
    "doi": DOI,
}


class FakeAdapter:
    """Duck-typed metadata adapter: fetch(ParsedIdentifier) -> canonical dict."""

    def __init__(self, values):
        self._values = dict(values)

    def fetch(self, identifier):
        return dict(self._values)


class FailingAdapter:
    """Duck-typed adapter that raises ResolutionError."""

    def fetch(self, identifier):
        raise ResolutionError("upstream service unavailable")


def ident(kind, value):
    return ParsedIdentifier(kind=kind, value=value, original=value)


def make_vault():
    td = tempfile.TemporaryDirectory()
    return Path(td.name), td


def make_pdf(path, *, pages=1, texts=(), xmp=None):
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=612, height=792)
        if index < len(texts) and texts[index]:
            page.insert_text((72, 72), texts[index])
    if xmp is not None:
        doc.set_xml_metadata(xmp)
    doc.save(str(path))
    doc.close()
    return path


def write_paper(
    root,
    key,
    paper_id="550e8400-e29b-41d4-a716-446655440000",
    citation_key=None,
    title="An example paper",
    authors=None,
    year=2026,
    publication_date="2026-05-01",
    doi=None,
    pmid=None,
    pmcid=None,
    arxiv=None,
    pdf_status="missing",
    pdf_sha256=None,
    aliases=(),
):
    d = Path(root) / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    if authors is None:
        authors = [{"family": "Smith", "given": "John"}]
    lines = [
        "schema_version: 1",
        f"paper_id: {paper_id}",
        f"citation_key: {citation_key or key}",
        "item_type: article-journal",
        f"title: {title}",
        "authors:",
    ]
    for author in authors:
        if "literal" in author:
            lines.append(f"- literal: {author['literal']}")
        else:
            lines.append(f"- family: {author['family']}")
            if author.get("given"):
                lines.append(f"  given: {author['given']}")
    lines.append(f"publication_date: {publication_date}")
    lines.append(f"year: {year}")
    if doi:
        lines.append(f"doi: {doi}")
    if pmid:
        lines.append(f"pmid: {pmid}")
    if pmcid:
        lines.append(f"pmcid: {pmcid}")
    if arxiv:
        lines.append(f"arxiv: {arxiv}")
    lines.append(f"pdf_status: {pdf_status}")
    if pdf_sha256:
        lines.append(f"pdf_sha256: {pdf_sha256}")
    lines.append("reading_status: unread")
    if aliases:
        lines.append("citation_key_aliases:")
        lines.extend(f"  - {a}" for a in aliases)
    fm = "\n".join(lines)
    (d / f"{key}.md").write_text(f"---\n{fm}\n---\n# body\n", encoding="utf-8")
    return d


def vault_manifest(root):
    """Sorted {relpath: (type, sha_or_link, mode)} over the entire vault."""
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


def assert_vault_untouched(testcase, root, before_manifest):
    after_manifest = vault_manifest(root)
    testcase.assertEqual(after_manifest, before_manifest)
    testcase.assertFalse((Path(root) / ".paper-notes").exists())


class ItemPreviewCoreTest(unittest.TestCase):
    """Test items.preview_create core API behavior."""

    def test_preview_ready_to_create_high_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "existingPaper2025", title="Completely different topic")
            before = vault_manifest(vault)
            hook = mock.Mock()

            result = items.preview_create(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                rebuild_hook=hook,
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "create")
            self.assertEqual(result.citation_key, "shiauSpatiallyResolvedAnalysis2024")
            self.assertIsNotNone(result.confirmation_token)
            self.assertIsNotNone(result.plan)
            self.assertEqual(result.plan["action"], "create")
            self.assertEqual(result.plan["citation_key"], "shiauSpatiallyResolvedAnalysis2024")
            self.assertEqual(result.plan["values"]["title"], CROSSREF_VALUES["title"])
            self.assertEqual(result.plan["conflicts"], [])
            self.assertEqual(result.candidates, [])

            hook.assert_not_called()
            assert_vault_untouched(self, vault, before)

    def test_preview_with_confirmed_metadata_and_custom_key(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)
            hook = mock.Mock()

            result = items.preview_create(
                vault,
                confirmed={
                    "citation_key": "customKey2026",
                    "title": "Custom Key Paper",
                    "authors": [{"family": "Tesla", "given": "Nikola"}],
                    "year": 2026,
                },
                rebuild_hook=hook,
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "create")
            self.assertEqual(result.citation_key, "customKey2026")
            self.assertEqual(result.plan["citation_key"], "customKey2026")
            hook.assert_not_called()
            assert_vault_untouched(self, vault, before)

    def test_preview_invalid_doi_format_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemError):
                items.preview_create(
                    vault,
                    identifiers=[ParsedIdentifier(kind="doi", value="not-a-valid-doi", original="not-a-valid-doi")],
                )

            assert_vault_untouched(self, vault, before)

    def test_preview_confirmed_not_mapping_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemError):
                items.preview_create(
                    vault,
                    confirmed="not-a-mapping",  # type: ignore
                )

            assert_vault_untouched(self, vault, before)

    def test_preview_confirmed_pdf_sha_without_pdf_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemError):
                items.preview_create(
                    vault,
                    confirmed={
                        "title": "Fabricated PDF",
                        "authors": [{"family": "Smith"}],
                        "year": 2026,
                        "pdf_sha256": "abcdef123456",
                    },
                )

            assert_vault_untouched(self, vault, before)

    def test_preview_needs_confirmation_with_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            # Metadata candidate with conflict
            conflict_candidate = MetadataCandidate(
                values={
                    "title": "A Conflicted Paper",
                    "authors": [{"family": "Doe", "given": "Jane"}],
                    "year": 2024,
                    "doi": DOI,
                },
                field_provenance={"title": "crossref", "authors": "crossref", "year": "crossref", "doi": "crossref"},
                confidence="needs_confirmation",
                conflicts=[
                    FieldConflict(
                        field="year",
                        values=(("crossref", 2024), ("pubmed", 2023)),
                    )
                ],
            )

            with mock.patch("paper_notes.items._resolve_candidate", return_value=conflict_candidate):
                result = items.preview_create(
                    vault,
                    identifiers=[ident("doi", DOI)],
                )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "create_with_confirmation")
            self.assertIsNotNone(result.plan)
            self.assertEqual(result.plan["action"], "create_with_confirmation")
            self.assertEqual(len(result.plan["conflicts"]), 1)
            self.assertEqual(result.plan["conflicts"][0]["field"], "year")
            self.assertEqual(result.candidates, [])

            assert_vault_untouched(self, vault, before)

    def test_preview_fuzzy_candidate_match(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "smithExample2026",
                title="An example paper on lung disease",
                authors=[{"family": "Smith", "given": "John"}],
                year=2026,
            )
            before = vault_manifest(vault)

            fuzzy_values = {
                "title": "An example paper on lung disease",
                "authors": [{"family": "Smith", "given": "John"}],
                "year": 2026,
                "doi": "10.1234/new-doi",
            }

            result = items.preview_create(
                vault,
                identifiers=[ident("doi", "10.1234/new-doi")],
                adapters={"doi": FakeAdapter(fuzzy_values)},
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "confirm_candidates")
            self.assertEqual(result.plan["action"], "confirm_candidates")
            self.assertTrue(len(result.candidates) > 0)
            self.assertEqual(result.candidates[0]["citation_key"], "smithExample2026")

            assert_vault_untouched(self, vault, before)

    def test_preview_existing_doi_hit_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "shiauExisting2024", doi=DOI)
            before = vault_manifest(vault)
            hook = mock.Mock()

            # Preview with same DOI must hit existing owner without writing or attaching
            result = items.preview_create(
                vault,
                identifiers=[ident("doi", DOI)],
                rebuild_hook=hook,
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "shiauExisting2024")
            self.assertEqual(result.plan["action"], "duplicate_exists")
            self.assertEqual(result.plan["citation_key"], "shiauExisting2024")
            self.assertIn("already exists", result.plan["message"])

            hook.assert_not_called()
            assert_vault_untouched(self, vault, before)

    def test_preview_existing_pdf_hash_hit_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "sample.pdf", texts=["Some document content"])
            pdf_sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()

            write_paper(vault, "pdfExisting2026", pdf_sha256=pdf_sha)
            before = vault_manifest(vault)
            hook = mock.Mock()

            # Preview with same PDF must hit existing owner
            result = items.preview_create(
                vault,
                pdf=pdf,
                rebuild_hook=hook,
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "pdfExisting2026")
            self.assertEqual(result.plan["action"], "duplicate_exists")
            self.assertEqual(result.plan["citation_key"], "pdfExisting2026")

            hook.assert_not_called()
            assert_vault_untouched(self, vault, before)

    def test_preview_candidate_cross_id_hit_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            # Vault has an item with PMID
            write_paper(vault, "pmidOwner2024", pmid=PMID)
            before = vault_manifest(vault)

            # Input DOI has no pre-lock hit, but adapter returns cross-ID PMID
            values_with_pmid = dict(CROSSREF_VALUES)
            values_with_pmid["pmid"] = PMID

            result = items.preview_create(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": FakeAdapter(values_with_pmid)},
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "pmidOwner2024")
            self.assertEqual(result.plan["citation_key"], "pmidOwner2024")

            assert_vault_untouched(self, vault, before)

    def test_preview_split_brain_owners_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "sample.pdf", texts=["Doc"])
            pdf_sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()

            write_paper(vault, "paperA", doi=DOI)
            write_paper(vault, "paperB", pdf_sha256=pdf_sha)
            before = vault_manifest(vault)

            # Input has DOI matching paperA and PDF matching paperB -> ItemConflict
            with self.assertRaises(items.ItemConflict):
                items.preview_create(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    pdf=pdf,
                )

            assert_vault_untouched(self, vault, before)

    def test_preview_metadata_resolution_error_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemError):
                items.preview_create(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    adapters={"doi": FailingAdapter()},
                )

            assert_vault_untouched(self, vault, before)

    def test_preview_empty_input_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemError) as ctx:
                items.preview_create(vault)

            self.assertIn("no identifiers, PDF, or confirmed metadata", str(ctx.exception))
            assert_vault_untouched(self, vault, before)

    def test_preview_with_local_pdf_extracts_embedded_doi(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "paper.pdf", texts=[f"Article with DOI {DOI} embedded"])
            before = vault_manifest(vault)

            result = items.preview_create(
                vault,
                pdf=pdf,
                adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
            )

            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "create")
            self.assertEqual(result.citation_key, "shiauSpatiallyResolvedAnalysis2024")
            self.assertIsNotNone(result.pdf_sha256)
            self.assertEqual(result.pdf_sha256, hashlib.sha256(Path(pdf).read_bytes()).hexdigest())

            # Verify the PDF was NOT copied into the vault
            assert_vault_untouched(self, vault, before)
            self.assertFalse((vault / "05 Literature" / "shiauSpatiallyResolvedAnalysis2024").exists())

    def test_preview_target_preflight_collision_raises_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            # Pre-create orphan directory matching target citation key
            orphan = vault / "05 Literature" / "shiauSpatiallyResolvedAnalysis2024"
            orphan.mkdir(parents=True, exist_ok=True)
            before = vault_manifest(vault)

            with self.assertRaises(items.ItemConflict):
                items.preview_create(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                )

            assert_vault_untouched(self, vault, before)


class ItemPreviewCliTest(unittest.TestCase):
    """Test CLI layer --dry-run envelope contract and exit codes."""

    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
        )

    def write_json(self, path, data):
        p = Path(path)
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_cli_dry_run_success_rc0_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            confirmed = self.write_json(
                vault / "confirmed.json",
                {
                    "title": "A Valid Preview Paper",
                    "authors": [{"family": "Watson", "given": "James"}],
                    "year": 2026,
                },
            )
            before = vault_manifest(vault)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--confirmed", str(confirmed),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1)

            payload = json.loads(lines[0])
            self.assertEqual(payload["protocol_version"], 1)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertIn("confirmation_token", payload["data"])
            self.assertIn("plan", payload["data"])
            self.assertEqual(payload["data"]["plan"]["action"], "create")
            self.assertEqual(payload["data"]["citation_key"], "watsonValidPreviewPaper2026")
            self.assertEqual(payload["warnings"], [])
            self.assertEqual(payload["errors"], [])

            assert_vault_untouched(self, vault, before)

    def test_cli_dry_run_existing_item_duplicate_exists_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "existingKey2026", doi=DOI)
            before = vault_manifest(vault)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--doi", DOI,
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "duplicate_exists")
            self.assertEqual(payload["data"]["citation_key"], "existingKey2026")
            self.assertEqual(payload["data"]["plan"]["action"], "duplicate_exists")
            self.assertEqual(payload["data"]["plan"]["citation_key"], "existingKey2026")

            assert_vault_untouched(self, vault, before)

    def test_cli_dry_run_empty_inputs_exit_code_two(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            before = vault_manifest(vault)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertTrue(len(payload["errors"]) > 0)

            assert_vault_untouched(self, vault, before)

    def test_cli_dry_run_with_confirm_token_exit_code_two(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--doi", DOI,
                "--dry-run",
                "--confirm-token", "some-token",
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertIn("--dry-run cannot be combined with --confirm-token", payload["errors"][0]["message"])

    def test_cli_dry_run_with_web_capture_exit_code_two(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            capture_file = self.write_json(
                vault / "capture.json",
                {"capture_id": "test", "page_url": "https://example.com"},
            )

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--web-capture", str(capture_file),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertIn("--dry-run cannot be combined with --web-capture", payload["errors"][0]["message"])

    def test_cli_dry_run_with_pdf_input_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "input.pdf", texts=["Sample content"])
            pdf_sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
            confirmed = self.write_json(
                vault / "confirmed.json",
                {
                    "title": "A PDF Paper",
                    "authors": [{"family": "Curie", "given": "Marie"}],
                    "year": 2026,
                },
            )
            before = vault_manifest(vault)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--pdf", str(pdf),
                "--confirmed", str(confirmed),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["citation_key"], "curiePDFPaper2026")
            self.assertEqual(payload["data"]["pdf_sha256"], pdf_sha)

            # Vault manifest must not change (no items created in 05 Literature)
            assert_vault_untouched(self, vault, before)
            self.assertFalse((vault / "05 Literature" / "curiePDFPaper2026").exists())

    def test_cli_dry_run_pdf_hash_duplicate_exists_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "existing.pdf", texts=["Document text"])
            pdf_sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()

            write_paper(vault, "existingByPdf2026", pdf_sha256=pdf_sha)
            before = vault_manifest(vault)

            result = self.run_cli(
                "item", "create",
                "--vault", str(vault),
                "--pdf", str(pdf),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "duplicate_exists")
            self.assertEqual(payload["data"]["citation_key"], "existingByPdf2026")
            self.assertEqual(payload["data"]["plan"]["citation_key"], "existingByPdf2026")

            assert_vault_untouched(self, vault, before)
