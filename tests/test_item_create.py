"""Canonical item create + show tests (Task 11).

Frozen after the first red run; do not weaken or delete assertions.
Synthetic PDFs are built with PyMuPDF in isolated temporary directories —
no network, no real vault, no real PDFs. Remote metadata sources are
always mocked adapters. The rebuild hook contract: exactly one call on
every successful mutation (create / attached_pdf), zero calls on no-op
duplicates (duplicate_exists / already_attached), show,
needs_confirmation, and failure.
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
from paper_notes import fsops
from paper_notes.identifiers import ParsedIdentifier

REPO = Path(__file__).resolve().parents[1]

DOI = "10.1038/s41591-024-00000-0"

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


def ident(kind, value):
    return ParsedIdentifier(kind=kind, value=value, original=value)


def make_vault():
    td = tempfile.TemporaryDirectory()
    return Path(td.name)


def make_pdf(path, *, pages=1, texts=(), xmp=None):
    """Build a synthetic PDF (same approach as test_pdf_metadata)."""
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
    d = root / "05 Literature" / key
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


def count_paper_dirs(root):
    lit = root / "05 Literature"
    if not lit.is_dir():
        return 0
    return sum(1 for p in lit.iterdir() if p.is_dir())


class CreateTest(unittest.TestCase):
    def test_metadata_only_create_writes_canonical_item(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            confirmed = {
                "title": "A new example paper",
                "authors": [{"family": "Chen", "given": "Wei"}],
                "publication_date": "2026-06-01",
                "year": 2026,
            }
            result = items.create_item(vault, confirmed=confirmed)
            self.assertEqual(result.status, "created")
            self.assertEqual(result.action, "created")
            self.assertEqual(result.citation_key, "chenNewExamplePaper2026")
            note = vault / "05 Literature" / result.citation_key / f"{result.citation_key}.md"
            self.assertTrue(note.is_file())
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.citation_key, "chenNewExamplePaper2026")
            self.assertEqual(paper.title, "A new example paper")
            self.assertEqual(paper.paper_id, __import__("uuid").UUID(result.paper_id))
            self.assertEqual(paper.pdf_status, "missing")
            self.assertEqual(paper.year, 2026)
            self.assertTrue(paper.model_extra["created_at"])
            self.assertTrue(paper.model_extra["updated_at"])
            self.assertIsNotNone(result.path)

    def test_metadata_only_create_allocates_suffixed_key_on_collision(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            # A DIFFERENT work that already owns the base key: collision must
            # suffix, never overwrite, and must not be mistaken for a fuzzy
            # duplicate of the incoming paper.
            write_paper(
                vault,
                "chenNewExamplePaper2026",
                title="Quantum dots in retinal imaging",
                authors=[{"family": "Li", "given": "Na"}],
                year=2025,
                publication_date="2025-01-01",
            )
            confirmed = {
                "title": "A new example paper",
                "authors": [{"family": "Chen", "given": "Wei"}],
                "year": 2026,
            }
            result = items.create_item(vault, confirmed=confirmed)
            self.assertEqual(result.status, "created")
            self.assertEqual(result.citation_key, "chenNewExamplePaper2026a")
            self.assertEqual(count_paper_dirs(vault), 2)

    def test_doi_backed_create_with_mocked_adapter(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            adapter = FakeAdapter(CROSSREF_VALUES)
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": adapter},
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.citation_key, "shiauSpatiallyResolvedAnalysis2024")
            note = vault / "05 Literature" / result.citation_key / f"{result.citation_key}.md"
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.doi, DOI)
            self.assertEqual(paper.title, CROSSREF_VALUES["title"])
            self.assertEqual(paper.metadata_sources, ["crossref"])
            self.assertEqual(paper.field_provenance["title"], "crossref")
            self.assertEqual(paper.journal, "Nature Medicine")

    def test_doi_backed_create_missing_critical_field_returns_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            adapter = FakeAdapter({"title": "Untitled work", "doi": "10.1000/nope"})
            result = items.create_item(
                vault,
                identifiers=[ident("doi", "10.1000/nope")],
                adapters={"doi": adapter},
            )
            self.assertEqual(result.status, "needs_confirmation")
            self.assertTrue(result.confirmation_token)
            self.assertIn("action", result.plan)
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_pdf_backed_create_copies_and_verifies_sha(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            adapter = FakeAdapter(CROSSREF_VALUES)
            result = items.create_item(
                vault,
                pdf=pdf,
                adapters={"doi": adapter},
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.pdf_sha256, source_sha)
            key = result.citation_key
            note = vault / "05 Literature" / key / f"{key}.md"
            copied = vault / "05 Literature" / key / f"{key}.pdf"
            self.assertTrue(copied.is_file())
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), source_sha)
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(note)
            self.assertEqual(paper.pdf_status, "available")
            self.assertEqual(paper.pdf_sha256, source_sha)

    def test_pdf_backed_create_leaves_source_intact(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            before = pdf.read_bytes()
            stat_before = pdf.stat()
            adapter = FakeAdapter(CROSSREF_VALUES)
            items.create_item(vault, pdf=pdf, adapters={"doi": adapter})
            self.assertEqual(pdf.read_bytes(), before)
            self.assertEqual(pdf.stat().st_mtime_ns, stat_before.st_mtime_ns)
            self.assertEqual(pdf.stat().st_ino, stat_before.st_ino)

    def test_pdf_without_identifiers_returns_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "blank.pdf", texts=["no identifiers here"])
            hook = mock.Mock()
            result = items.create_item(vault, pdf=pdf, rebuild_hook=hook)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertTrue(result.confirmation_token)
            self.assertEqual(count_paper_dirs(vault), 0)
            hook.assert_not_called()

    def test_create_with_nothing_raises_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(items.ItemError):
                items.create_item(Path(td))

    def test_strong_doi_duplicate_is_update_attach_not_second_item(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI)
            adapter = FakeAdapter(CROSSREF_VALUES)
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": adapter},
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "smithExample2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            # J: a no-op duplicate is not a bibliographic mutation.
            hook.assert_not_called()

    def test_strong_doi_duplicate_with_pdf_attaches_file_to_existing_item(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI, pdf_status="missing")
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            adapter = FakeAdapter(CROSSREF_VALUES)
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                pdf=pdf,
                adapters={"doi": adapter},
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "attached_pdf")
            self.assertEqual(result.citation_key, "smithExample2026")
            copied = vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf"
            self.assertTrue(copied.is_file())
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), source_sha)
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(
                vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            )
            self.assertEqual(paper.pdf_status, "available")
            self.assertEqual(paper.pdf_sha256, source_sha)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_called_once()

    def test_pdf_hash_duplicate_attaches_to_owner_not_new_item(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            write_paper(
                vault,
                "smithExample2026",
                doi=DOI,
                pdf_status="available",
                pdf_sha256=source_sha,
            )
            (vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf").write_bytes(
                pdf.read_bytes()
            )
            hook = mock.Mock()
            result = items.create_item(vault, pdf=pdf, rebuild_hook=hook)
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "already_attached")
            self.assertEqual(result.citation_key, "smithExample2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            # J: an already-present attachment is not a mutation.
            hook.assert_not_called()

    def test_attach_differing_existing_primary_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI, pdf_status="available")
            old = make_pdf(Path(td) / "old.pdf", texts=["old version"])
            (vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf").write_bytes(
                old.read_bytes()
            )
            new = make_pdf(Path(td) / "new.pdf", texts=["new version"])
            before = (vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf").read_bytes()
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                pdf=new,
                adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "replace_primary")
            self.assertTrue(result.confirmation_token)
            # existing primary untouched, no new item
            self.assertEqual(
                (vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf").read_bytes(),
                before,
            )
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_fuzzy_duplicate_returns_confirmation_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = {
                "title": "An example PAPER!",
                "authors": [{"family": "Smith", "given": "John"}],
                "year": 2026,
            }
            hook = mock.Mock()
            result = items.create_item(vault, confirmed=confirmed, rebuild_hook=hook)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertTrue(result.confirmation_token)
            self.assertEqual(result.plan["action"], "confirm_candidates")
            keys = [c["citation_key"] for c in result.candidates]
            self.assertIn("smithExample2026", keys)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_fuzzy_duplicate_never_auto_merges_even_when_high_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = {
                "title": "An example paper",
                "authors": [{"family": "Smith", "given": "John"}],
                "year": 2026,
                "doi": DOI,
            }
            result = items.create_item(vault, confirmed=confirmed)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_fuzzy_non_duplicate_creates_normally(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = {
                "title": "Quantum dots in retinal imaging",
                "authors": [{"family": "Li", "given": "Na"}],
                "year": 2025,
            }
            result = items.create_item(vault, confirmed=confirmed)
            self.assertEqual(result.status, "created")
            self.assertEqual(count_paper_dirs(vault), 2)

    def test_existing_different_target_blocks_create(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = {
                "citation_key": "smithExample2026",
                "title": "A completely different paper",
                "authors": [{"family": "Jones", "given": "Beth"}],
                "year": 2025,
            }
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(vault, confirmed=confirmed, rebuild_hook=hook)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_create_rejects_invalid_citation_key_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            for bad in ("../evil", "a/b", "a b", ".."):
                confirmed = {
                    "citation_key": bad,
                    "title": "A valid paper",
                    "authors": [{"family": "Jones", "given": "Beth"}],
                    "year": 2025,
                }
                with self.assertRaises(items.ItemError):
                    items.create_item(vault, confirmed=confirmed)
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_failed_create_rolls_back_leaving_no_partial_item(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            adapter = FakeAdapter(CROSSREF_VALUES)
            real_write = fsops.write_target
            calls = {"n": 0}

            def flaky(op, target, content):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("simulated disk failure")
                return real_write(op, target, content)

            hook = mock.Mock()
            with mock.patch("paper_notes.fsops.write_target", side_effect=flaky):
                with self.assertRaises(items.ItemError):
                    items.create_item(vault, pdf=pdf, adapters={"doi": adapter}, rebuild_hook=hook)
            self.assertEqual(count_paper_dirs(vault), 0)
            # fsops removes the per-operation staging subdirectory; the
            # empty .staging container may remain but must hold nothing.
            staging = vault / ".paper-notes" / ".staging"
            self.assertEqual(list(staging.iterdir()), [])
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_not_called()

    def test_rebuild_hook_called_exactly_once_on_create_success(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            items.create_item(
                vault,
                confirmed={
                    "title": "A hook test paper",
                    "authors": [{"family": "Hook", "given": "Ann"}],
                    "year": 2026,
                },
                rebuild_hook=hook,
            )
            hook.assert_called_once()


class ShowTest(unittest.TestCase):
    def test_show_returns_paper_for_current_key(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            items.create_item(
                vault,
                confirmed={
                    "title": "A shown paper",
                    "authors": [{"family": "Shown", "given": "Ada"}],
                    "year": 2026,
                },
            )
            result = items.show_item(vault, key="shownShownPaper2026")
            self.assertEqual(result.resolved_as, "key")
            self.assertEqual(result.citation_key, "shownShownPaper2026")
            self.assertEqual(result.requested_key, "shownShownPaper2026")
            self.assertEqual(result.frontmatter["title"], "A shown paper")
            self.assertTrue(str(result.path).endswith("shownShownPaper2026.md"))

    def test_show_resolves_alias(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", aliases=("smithExample2026old",))
            result = items.show_item(vault, key="smithExample2026old")
            self.assertEqual(result.resolved_as, "alias")
            self.assertEqual(result.citation_key, "smithExample2026")
            self.assertEqual(result.frontmatter["title"], "An example paper")

    def test_show_unknown_key_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            with self.assertRaises(items.ItemError):
                items.show_item(vault, key="ghost2026")

    def test_show_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            items.show_item(vault, key="smithExample2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            self.assertFalse((vault / ".paper-notes").exists())


class CreateCliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
        )

    def write_confirmed(self, vault, data):
        path = Path(vault) / "confirmed.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_cli_create_metadata_only_single_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            confirmed = self.write_confirmed(
                vault,
                {
                    "title": "A CLI created paper",
                    "authors": [{"family": "Claire", "given": "Lin"}],
                    "year": 2026,
                },
            )
            result = self.run_cli("item", "create", "--vault", str(vault), "--confirmed", str(confirmed))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["protocol_version"], 1)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "created")
            key = payload["data"]["citation_key"]
            self.assertTrue(
                (vault / "05 Literature" / key / f"{key}.md").is_file()
            )

    def test_cli_create_needs_confirmation_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = self.write_confirmed(
                vault,
                {
                    "title": "An example paper",
                    "authors": [{"family": "Smith", "given": "John"}],
                    "year": 2026,
                },
            )
            result = self.run_cli("item", "create", "--vault", str(vault), "--confirmed", str(confirmed))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertTrue(payload["data"]["confirmation_token"])
            self.assertEqual(payload["data"]["plan"]["action"], "confirm_candidates")
            self.assertEqual(len(payload["data"]["candidates"]), 1)

    def test_cli_create_no_inputs_usage_error(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.run_cli("item", "create", "--vault", str(Path(td)))
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_create_conflict_exit_code_three(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            confirmed = self.write_confirmed(
                vault,
                {
                    "citation_key": "smithExample2026",
                    "title": "A totally different paper",
                    "authors": [{"family": "Zhao", "given": "Qi"}],
                    "year": 2025,
                },
            )
            result = self.run_cli("item", "create", "--vault", str(vault), "--confirmed", str(confirmed))
            self.assertEqual(result.returncode, 3, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "conflict")

    def test_cli_show_resolves_key_and_alias(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", aliases=("smithExample2026old",))
            result = self.run_cli("item", "show", "--vault", str(vault), "--key", "smithExample2026old")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["resolved_as"], "alias")
            self.assertEqual(payload["data"]["citation_key"], "smithExample2026")

    def test_cli_show_unknown_key_exit_two(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.run_cli("item", "show", "--vault", str(Path(td)), "--key", "ghost2026")
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")


class AcceptanceRegressionCreateTest(unittest.TestCase):
    """Round-1 management acceptance regressions (A/B/C/E/F/G/H/I/J/K)."""

    CONFIRMED = {
        "title": "A new example paper",
        "authors": [{"family": "Chen", "given": "Wei"}],
        "year": 2026,
    }

    def write_lock_file(self, vault, pid):
        lockdir = vault / ".paper-notes"
        lockdir.mkdir(parents=True, exist_ok=True)
        (lockdir / "write.lock").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "operation": "create_item",
                    "operation_id": "abc123",
                    "host": "test",
                }
            ),
            encoding="utf-8",
        )

    # --- A: rebuild hook failure rolls back create/attach, zero partial ---
    def test_hook_failure_rolls_back_create_leaving_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock(side_effect=RuntimeError("simulated rebuild failure"))
            with self.assertRaises(items.ItemError):
                items.create_item(vault, confirmed=dict(self.CONFIRMED), rebuild_hook=hook)
            self.assertEqual(count_paper_dirs(vault), 0)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_called_once()

    def test_hook_failure_rolls_back_pdf_attach(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI, pdf_status="missing")
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock(side_effect=RuntimeError("simulated rebuild failure"))
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    pdf=pdf,
                    adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                    rebuild_hook=hook,
                )
            self.assertFalse(
                (vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf").exists()
            )
            self.assertEqual(note.read_bytes(), before)
            self.assertEqual(count_paper_dirs(vault), 1)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_called_once()

    # --- B: orphan / symlink targets are read-only conflicts ---
    def test_orphan_target_dir_is_read_only_conflict_sentinel_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            d = vault / "05 Literature" / "chenNewExamplePaper2026"
            d.mkdir(parents=True)
            sentinel = d / "chenNewExamplePaper2026.md"
            sentinel.write_text("SENTINEL", encoding="utf-8")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(vault, confirmed=dict(self.CONFIRMED), rebuild_hook=hook)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "SENTINEL")
            self.assertEqual(count_paper_dirs(vault), 1)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            hook.assert_not_called()

    def test_symlink_at_item_directory_is_read_only_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            outside = Path(td) / "outside"
            outside.mkdir()
            (vault / "05 Literature").mkdir(parents=True)
            (vault / "05 Literature" / "chenNewExamplePaper2026").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(items.ItemConflict):
                items.create_item(vault, confirmed=dict(self.CONFIRMED))
            self.assertTrue(
                (vault / "05 Literature" / "chenNewExamplePaper2026").is_symlink()
            )

    # --- C: stale create — never rely on the pre-lock index ---
    def test_stale_create_rebuilds_index_under_lock(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            real_acquire = items.acquire_lock

            def concurrent_acquire(root, operation):
                # another managed writer claims the key before we lock
                write_paper(
                    vault,
                    "chenNewExamplePaper2026",
                    title="Concurrent owner paper",
                    authors=[{"family": "Zhang", "given": "Min"}],
                    year=2025,
                    publication_date="2025-01-01",
                )
                return real_acquire(root, operation)

            hook = mock.Mock()
            with mock.patch.object(
                items, "acquire_lock", side_effect=concurrent_acquire
            ):
                result = items.create_item(
                    vault, confirmed=dict(self.CONFIRMED), rebuild_hook=hook
                )
            self.assertEqual(result.status, "created")
            # the key got suffixed instead of overwriting the owner
            self.assertEqual(result.citation_key, "chenNewExamplePaper2026a")
            owner_note = (
                vault / "05 Literature" / "chenNewExamplePaper2026"
                / "chenNewExamplePaper2026.md"
            )
            self.assertIn("Concurrent owner paper", owner_note.read_text(encoding="utf-8"))
            self.assertEqual(count_paper_dirs(vault), 2)
            hook.assert_called_once()

    # --- E: commit that reports conflicts must not leak/rollback ---
    def test_commit_conflict_returns_item_conflict_not_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            real_commit = fsops.commit

            def conflicted_commit(op):
                conflicts = real_commit(op)  # real commit: op is finished
                return conflicts + [str(vault / "05 Literature" / "x" / "x.md")]

            hook = mock.Mock()
            with mock.patch("paper_notes.fsops.commit", side_effect=conflicted_commit):
                with self.assertRaises(items.ItemConflict):
                    items.create_item(
                        vault, confirmed=dict(self.CONFIRMED), rebuild_hook=hook
                    )
            hook.assert_called_once()
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())
            # the finished operation is not rolled back
            self.assertEqual(count_paper_dirs(vault), 1)

    # --- F: repository identity conflicts force read-only errors ---
    def test_create_against_duplicate_uuid_repository_is_read_only_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            uid = "550e8400-e29b-41d4-a716-446655440000"
            write_paper(vault, "smithExample2026", paper_id=uid)
            write_paper(vault, "jonesOther2026", paper_id=uid)
            note = vault / "05 Literature" / "smithExample2026" / "smithExample2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed={"title": "Brand new", "authors": [{"family": "Zhao", "given": "Qi"}], "year": 2026},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            self.assertEqual(count_paper_dirs(vault), 2)
            hook.assert_not_called()

    def test_create_against_alias_collision_repository_is_read_only_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", aliases=("jonesOther2026",))
            write_paper(vault, "jonesOther2026")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed={"title": "Brand new", "authors": [{"family": "Zhao", "given": "Qi"}], "year": 2026},
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 2)

    # --- G: strong-identity split brain is a conflict, never a pick ---
    def test_split_identity_owners_return_conflict_not_silent_pick(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            write_paper(
                vault, "bBeta2026", pmid="30000001",
                paper_id="550e8400-e29b-41d4-a716-446655440099",
            )
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", "10.1000/a"), ident("pmid", "30000001")],
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 2)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())

    def test_split_owner_pdf_hash_and_identifier_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi=DOI)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            write_paper(
                vault, "bBeta2026", pdf_status="available", pdf_sha256=source_sha,
                paper_id="550e8400-e29b-41d4-a716-446655440099",
            )
            (vault / "05 Literature" / "bBeta2026" / "bBeta2026.pdf").write_bytes(
                pdf.read_bytes()
            )
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    pdf=pdf,
                    adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                )

    def test_attach_refuses_symlink_primary_as_already_attached(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            external = Path(td) / "external.pdf"
            make_pdf(external, texts=["external copy"])
            external_sha = hashlib.sha256(external.read_bytes()).hexdigest()
            write_paper(
                vault,
                "smithExample2026",
                doi=DOI,
                pdf_status="available",
                pdf_sha256=external_sha,
            )
            target = vault / "05 Literature" / "smithExample2026" / "smithExample2026.pdf"
            target.symlink_to(external)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    pdf=external,
                    adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            self.assertTrue(target.is_symlink())
            self.assertEqual(count_paper_dirs(vault), 1)

    # --- H: legal item_type is preserved, missing defaults ---
    def test_confirmed_preprint_item_type_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            result = items.create_item(
                vault,
                confirmed={
                    "title": "A preprint paper",
                    "authors": [{"family": "Pre", "given": "Ann"}],
                    "year": 2026,
                    "item_type": "preprint",
                },
            )
            self.assertEqual(result.status, "created")
            assert result.citation_key is not None
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(
                vault / "05 Literature" / result.citation_key / f"{result.citation_key}.md"
            )
            self.assertEqual(paper.item_type, "preprint")

    def test_default_item_type_is_article_journal(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            result = items.create_item(vault, confirmed=dict(self.CONFIRMED))
            self.assertEqual(result.status, "created")
            assert result.citation_key is not None
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(
                vault / "05 Literature" / result.citation_key / f"{result.citation_key}.md"
            )
            self.assertEqual(paper.item_type, "article-journal")

    # --- I: Unicode-safe fuzzy similarity (CJK) ---
    def test_cjk_identical_title_same_author_year_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "wang2026",
                title="肺癌的精准治疗研究",
                authors=[{"family": "王", "given": "明"}],
                year=2026,
                publication_date="2026-01-01",
            )
            hook = mock.Mock()
            result = items.create_item(
                vault,
                confirmed={
                    "title": "肺癌的精准治疗研究",
                    "authors": [{"family": "王", "given": "明"}],
                    "year": 2026,
                },
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "needs_confirmation")
            keys = [c["citation_key"] for c in result.candidates]
            self.assertIn("wang2026", keys)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_cjk_different_title_creates_normally(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "wang2026",
                title="胰腺外分泌功能",
                authors=[{"family": "王", "given": "明"}],
                year=2026,
                publication_date="2026-01-01",
            )
            result = items.create_item(
                vault,
                confirmed={
                    "title": "临床应用方法",
                    "authors": [{"family": "杨", "given": "华"}],
                    "year": 2025,
                },
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(count_paper_dirs(vault), 2)

    # --- K: lock failures map to structured errors ---
    def test_create_against_held_lock_is_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            self.write_lock_file(vault, os.getpid())
            with self.assertRaises(items.ItemConflict):
                items.create_item(vault, confirmed=dict(self.CONFIRMED))

    def test_create_against_stale_lock_is_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            probe = subprocess.Popen([sys.executable, "-c", "pass"])
            probe.wait()  # pid is dead now
            self.write_lock_file(vault, probe.pid)
            with self.assertRaises(items.ItemError):
                items.create_item(vault, confirmed=dict(self.CONFIRMED))


class AcceptanceRound2CreateTest(unittest.TestCase):
    """Round-2 management acceptance (L + strong-ID canonicalization).

    Every strong identity source (explicit ParsedIdentifier, PDF
    extraction, confirmed metadata, adapter candidate values) must land
    in the same canonical vocabulary as the indexed YAML (resolver URLs,
    ``DOI:``/``PMID:`` prefixes and case are all equivalent to the bare
    canonical value). Exact strong-identifier duplicates always become
    update/attach on the owner — never a second item. Invalid non-empty
    strong values and pdf-managed fields are rejected with zero writes.
    """

    def test_confirmed_doi_url_form_dedupes_later_canonical_create(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            first = items.create_item(
                vault,
                confirmed={
                    "title": "A confirmed URL-doi paper",
                    "authors": [{"family": "Url", "given": "Dan"}],
                    "year": 2026,
                    "doi": "https://doi.org/10.1000/XYZ",
                },
                rebuild_hook=hook,
            )
            self.assertEqual(first.status, "created")
            assert first.citation_key is not None
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(
                vault / "05 Literature" / first.citation_key
                / f"{first.citation_key}.md"
            )
            # the authoritative YAML stores the canonical DOI, not the URL
            self.assertEqual(paper.model_extra["doi"], "10.1000/xyz")
            hook.reset_mock()
            second = items.create_item(
                vault,
                identifiers=[ident("doi", "10.1000/xyz")],
                adapters={
                    "doi": FakeAdapter(
                        {
                            "title": "A totally different title from the resolver",
                            "authors": [{"family": "Other", "given": "Ann"}],
                            "year": 2025,
                        }
                    )
                },
                rebuild_hook=hook,
            )
            self.assertEqual(second.status, "attached")
            self.assertEqual(second.action, "duplicate_exists")
            self.assertEqual(second.citation_key, first.citation_key)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_metadata_only_confirmed_canonical_doi_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            first = items.create_item(
                vault,
                confirmed={
                    "title": "A url-doi paper",
                    "authors": [{"family": "Url", "given": "Dan"}],
                    "year": 2026,
                    "doi": "https://doi.org/10.1000/XYZ",
                },
                rebuild_hook=hook,
            )
            self.assertEqual(first.status, "created")
            hook.reset_mock()
            second = items.create_item(
                vault,
                confirmed={
                    "title": "The same paper in canonical doi form",
                    "authors": [{"family": "Url", "given": "Dan"}],
                    "year": 2026,
                    "doi": "10.1000/xyz",
                },
                rebuild_hook=hook,
            )
            self.assertEqual(second.status, "attached")
            self.assertEqual(second.action, "duplicate_exists")
            self.assertEqual(second.citation_key, first.citation_key)
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_existing_yaml_url_form_doi_dedupes_canonical_input(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi="https://doi.org/10.1000/XYZ")
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("doi", "10.1000/xyz")],
                adapters={
                    "doi": FakeAdapter(
                        {
                            "title": "A different resolved title",
                            "authors": [{"family": "Other", "given": "Ann"}],
                            "year": 2025,
                        }
                    )
                },
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "smithExample2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_candidate_cross_id_doi_attaches_to_existing_owner(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("pmid", "30000002")],
                adapters={
                    "pmid": FakeAdapter(
                        {
                            "title": "A paper resolving to an owned DOI",
                            "authors": [{"family": "Cross", "given": "Ida"}],
                            "year": 2026,
                            "doi": "10.1000/a",
                        }
                    )
                },
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "aAlpha2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_candidate_cross_id_split_owners_is_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            write_paper(
                vault,
                "pPmid2026",
                pmid="30000001",
                paper_id="550e8400-e29b-41d4-a716-446655440077",
            )
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("arxiv", "2401.00001v2")],
                    adapters={
                        "arxiv": FakeAdapter(
                            {
                                "title": "Split brain via candidate ids",
                                "authors": [{"family": "Split", "given": "Sam"}],
                                "year": 2026,
                                "pmid": "30000001",
                                "doi": "10.1000/a",
                            }
                        )
                    },
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 2)

    def test_confirmed_invalid_doi_is_item_error_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    confirmed={
                        "title": "Bad doi paper",
                        "authors": [{"family": "Bad", "given": "Bo"}],
                        "year": 2026,
                        "doi": "not-a-doi",
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(count_paper_dirs(vault), 0)
            hook.assert_not_called()

    def test_adapter_invalid_doi_is_item_error_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    identifiers=[ident("pmid", "30000002")],
                    adapters={
                        "pmid": FakeAdapter(
                            {
                                "title": "Bad adapter doi",
                                "authors": [{"family": "Bad", "given": "Bo"}],
                                "year": 2026,
                                "doi": "http://example.com/not-a-doi",
                            }
                        )
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(count_paper_dirs(vault), 0)
            hook.assert_not_called()

    def test_metadata_only_confirmed_cannot_fabricate_pdf_fields(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    confirmed={
                        "title": "Fake pdf paper",
                        "authors": [{"family": "Fake", "given": "Fi"}],
                        "year": 2026,
                        "pdf_status": "available",
                        "pdf_sha256": "f" * 64,
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(count_paper_dirs(vault), 0)
            hook.assert_not_called()

    def test_pdf_backed_create_overrides_confirmed_pdf_fields_with_real_sha(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(Path(td) / "source.pdf", texts=[f"DOI {DOI}"])
            source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            hook = mock.Mock()
            result = items.create_item(
                vault,
                pdf=pdf,
                adapters={"doi": FakeAdapter(CROSSREF_VALUES)},
                confirmed={
                    "title": "Real pdf paper",
                    "authors": [{"family": "Real", "given": "Re"}],
                    "year": 2024,
                    "publication_date": "2024-05-01",
                    "pdf_status": "missing",
                    "pdf_sha256": "f" * 64,
                },
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.pdf_sha256, source_sha)
            assert result.citation_key is not None
            from paper_notes.frontmatter import load_paper_note

            paper, _ = load_paper_note(
                vault / "05 Literature" / result.citation_key
                / f"{result.citation_key}.md"
            )
            self.assertEqual(paper.pdf_status, "available")
            self.assertEqual(paper.pdf_sha256, source_sha)
            hook.assert_called_once()


class AcceptanceRound3CreateTest(unittest.TestCase):
    """Round-3 management acceptance (N1/N2 + confirmed fast paths).

    Confirmed metadata is validated BEFORE any owner fast path: legal
    confirmed strong identifiers join the explicit/PDF identifiers in
    the pre-lock owner set, so a confirmed cross-ID owned by a different
    item is an ItemConflict (never a silent duplicate_exists), an
    invalid confirmed strong value is an ItemError (never swallowed by a
    fast-path duplicate), and a metadata-only confirmed strong
    identifier hits its owner without any resolution/adapter call.
    pdf_status/pdf_sha256 cannot be fabricated without a real PDF, and
    a non-object confirmed is a structured ItemError. All failures are
    zero-write with the rebuild hook never called.
    """

    def test_confirmed_pmid_cross_owner_is_conflict_zero_writes(self):
        # N1: A owns DOI, B owns PMID; explicit DOI + confirmed PMID
        # must surface the split owner before the fast path returns.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            write_paper(
                vault,
                "bBeta2026",
                pmid="28845751",
                paper_id="550e8400-e29b-41d4-a716-446655440099",
            )
            note = vault / "05 Literature" / "aAlpha2026" / "aAlpha2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", "10.1000/a")],
                    confirmed={"pmid": "PMID: 28845751"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 2)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())

    def test_confirmed_invalid_doi_blocks_fastpath_item_error(self):
        # N2: an invalid confirmed strong value must fail validation
        # before the pre-owner fast path — never duplicate_exists.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            note = vault / "05 Literature" / "aAlpha2026" / "aAlpha2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", "10.1000/a")],
                    confirmed={"doi": "definitely-not-a-doi"},
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 1)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())

    # --- confirmed identifier canonical fast paths (one per strong kind) ---
    # Resolution is patched to explode: a metadata-only confirmed strong
    # identifier that hits an existing owner must be served by the
    # pre-lock fast path without any adapter/network call.
    def test_confirmed_doi_fast_path_attaches_without_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            hook = mock.Mock()
            with mock.patch.object(
                items, "_resolve_candidate",
                side_effect=AssertionError("resolution must not run"),
            ):
                result = items.create_item(
                    vault,
                    confirmed={
                        "title": "Url doi paper",
                        "authors": [{"family": "Url", "given": "Dan"}],
                        "year": 2026,
                        "doi": "https://doi.org/10.1000/a",
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "aAlpha2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_confirmed_pmid_fast_path_attaches_without_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "bBeta2026",
                pmid="28845751",
                paper_id="550e8400-e29b-41d4-a716-446655440099",
            )
            hook = mock.Mock()
            with mock.patch.object(
                items, "_resolve_candidate",
                side_effect=AssertionError("resolution must not run"),
            ):
                result = items.create_item(
                    vault,
                    confirmed={
                        "title": "Prefixed pmid paper",
                        "authors": [{"family": "Pre", "given": "Ann"}],
                        "year": 2026,
                        "pmid": "PMID: 28845751",
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "bBeta2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_confirmed_pmcid_fast_path_attaches_without_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "cPmcid2026",
                pmcid="PMC1234567",
                paper_id="550e8400-e29b-41d4-a716-446655440077",
            )
            hook = mock.Mock()
            with mock.patch.object(
                items, "_resolve_candidate",
                side_effect=AssertionError("resolution must not run"),
            ):
                result = items.create_item(
                    vault,
                    confirmed={
                        "title": "Lowercase pmcid paper",
                        "authors": [{"family": "Case", "given": "Cy"}],
                        "year": 2026,
                        "pmcid": "pmc1234567",
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "cPmcid2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_confirmed_arxiv_fast_path_attaches_without_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "aArxiv2026",
                arxiv="2401.00001v2",
                paper_id="550e8400-e29b-41d4-a716-446655440066",
            )
            hook = mock.Mock()
            with mock.patch.object(
                items, "_resolve_candidate",
                side_effect=AssertionError("resolution must not run"),
            ):
                result = items.create_item(
                    vault,
                    confirmed={
                        "title": "Arxiv url paper",
                        "authors": [{"family": "Abs", "given": "Al"}],
                        "year": 2026,
                        "arxiv": "https://arxiv.org/abs/2401.00001v2",
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(result.citation_key, "aArxiv2026")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

    def test_confirmed_not_a_mapping_is_structured_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            hook = mock.Mock()
            for bad in (["not", "a", "mapping"], "just a string", 42):
                with self.assertRaises(items.ItemError):
                    items.create_item(vault, confirmed=bad, rebuild_hook=hook)
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_confirmed_pdf_fields_rejected_before_fastpath(self):
        # fabricated confirmed pdf fields must be rejected even when the
        # explicit DOI would otherwise hit the owner fast path
        # (duplicate_exists never validates the paper).
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "aAlpha2026", doi="10.1000/a")
            note = vault / "05 Literature" / "aAlpha2026" / "aAlpha2026.md"
            before = note.read_bytes()
            hook = mock.Mock()
            with self.assertRaises(items.ItemError):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", "10.1000/a")],
                    confirmed={
                        "title": "Fake pdf fields",
                        "authors": [{"family": "Fake", "given": "Fi"}],
                        "year": 2026,
                        "pdf_status": "available",
                        "pdf_sha256": "f" * 64,
                    },
                    rebuild_hook=hook,
                )
            self.assertEqual(note.read_bytes(), before)
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 1)
            self.assertFalse((vault / ".paper-notes" / "write.lock").exists())


def _vault_manifest(root):
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


def _manifest_diff(before, after):
    added = sorted(p for p in after if p not in before)
    removed = sorted(p for p in before if p not in after)
    changed = sorted(p for p in before if p in after and before[p] != after[p])
    return added, removed, changed


class TestItemCreateConfirmation(unittest.TestCase):
    """Phase C2 confirmation write, token validation, and fuzzy escape hatch tests."""

    def test_fuzzy_escape_hatch_solves_deadlock(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "smith2024",
                title="Quantum Computing in Biology",
                authors=[{"family": "Smith", "given": "John"}],
                year=2024,
                publication_date="2024-01-01",
            )
            self.assertEqual(count_paper_dirs(vault), 1)

            incoming = {
                "title": "Machine Learning in Astronomy",
                "authors": [{"family": "Smith", "given": "John"}],
                "year": 2024,
            }

            # Preview returns needs_confirmation with fuzzy candidates and confirmation token
            preview = items.preview_create(vault, confirmed=incoming)
            self.assertEqual(preview.status, "needs_confirmation")
            self.assertEqual(preview.action, "confirm_candidates")
            self.assertTrue(preview.confirmation_token)
            self.assertEqual(len(preview.candidates), 1)
            self.assertEqual(preview.candidates[0]["citation_key"], "smith2024")
            self.assertEqual(count_paper_dirs(vault), 1)

            # Unconfirmed retry: calling create_item with confirmed metadata but WITHOUT token
            # still returns needs_confirmation (anti-accidental-merge guard maintained)
            hook = mock.Mock()
            unconfirmed = items.create_item(vault, confirmed=incoming, rebuild_hook=hook)
            self.assertEqual(unconfirmed.status, "needs_confirmation")
            self.assertEqual(unconfirmed.action, "confirm_candidates")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_not_called()

            # Confirmed execution with the preview token: creates the second paper (escape hatch!)
            hook = mock.Mock()
            result = items.create_item(
                vault,
                confirmed=incoming,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.action, "created")
            self.assertTrue(result.citation_key)
            self.assertNotEqual(result.citation_key, "smith2024")
            self.assertEqual(count_paper_dirs(vault), 2)
            self.assertEqual(len(result.candidates), 1)
            self.assertEqual(result.candidates[0]["citation_key"], "smith2024")
            hook.assert_called_once()

            # The new note actually exists on disk with correct frontmatter
            note_path = Path(result.path)
            self.assertTrue(note_path.is_file())
            from paper_notes.frontmatter import load_paper_note
            paper, _ = load_paper_note(note_path)
            self.assertEqual(paper.title, "Machine Learning in Astronomy")

    def test_fuzzy_token_mismatch_and_stale_detection(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(
                vault,
                "smith2024",
                title="Quantum Computing in Biology",
                authors=[{"family": "Smith", "given": "John"}],
                year=2024,
                publication_date="2024-01-01",
            )
            incoming = {
                "title": "Machine Learning in Astronomy",
                "authors": [{"family": "Smith", "given": "John"}],
                "year": 2024,
            }
            preview = items.preview_create(vault, confirmed=incoming)
            token = preview.confirmation_token

            # 1. Bogus token raises ItemConflict and makes ZERO writes
            manifest_before = _vault_manifest(vault)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed=incoming,
                    confirm_token="bogus_token_12345",
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            manifest_after = _vault_manifest(vault)
            self.assertEqual(_manifest_diff(manifest_before, manifest_after), ([], [], []))

            # 2. Existing candidate note modified before confirm makes token stale -> ItemConflict
            note_file = vault / "05 Literature" / "smith2024" / "smith2024.md"
            note_file.write_text(note_file.read_text(encoding="utf-8") + "\n<!-- edit -->", encoding="utf-8")
            manifest_before = _vault_manifest(vault)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed=incoming,
                    confirm_token=token,
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            manifest_after = _vault_manifest(vault)
            self.assertEqual(_manifest_diff(manifest_before, manifest_after), ([], [], []))

    def test_token_validation_on_normal_create(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            confirmed = {
                "title": "A Brand New Unique Paper",
                "authors": [{"family": "UniqueAuthor", "given": "Alice"}],
                "year": 2025,
            }
            preview = items.preview_create(vault, confirmed=confirmed)
            self.assertEqual(preview.status, "needs_confirmation")
            self.assertEqual(preview.action, "create")
            token = preview.confirmation_token
            self.assertTrue(token)

            # 1. Bogus token raises ItemConflict and zero writes
            manifest_before = _vault_manifest(vault)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed=confirmed,
                    confirm_token="bogus-token-value",
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            manifest_after = _vault_manifest(vault)
            self.assertEqual(_manifest_diff(manifest_before, manifest_after), ([], [], []))

            # 2. Target occupied before confirmation -> ItemConflict and zero writes
            key = preview.citation_key
            target_note = vault / "05 Literature" / key / f"{key}.md"
            target_note.parent.mkdir(parents=True, exist_ok=True)
            target_note.write_text("occupied", encoding="utf-8")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    confirmed=confirmed,
                    confirm_token=token,
                    rebuild_hook=hook,
                )
            hook.assert_not_called()

            # Clean up occupied file for next step
            target_note.unlink()
            target_note.parent.rmdir()

            # 3. Valid token succeeds
            hook = mock.Mock()
            result = items.create_item(
                vault,
                confirmed=confirmed,
                confirm_token=token,
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.action, "created")
            self.assertEqual(result.citation_key, key)
            hook.assert_called_once()
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_duplicate_exists_confirmation_token_validation(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "existingPaper", doi=DOI)
            self.assertEqual(count_paper_dirs(vault), 1)

            preview = items.preview_create(
                vault,
                identifiers=[ident("doi", DOI)],
            )
            self.assertEqual(preview.status, "needs_confirmation")
            self.assertEqual(preview.action, "duplicate_exists")
            token = preview.confirmation_token

            # Bogus token raises ItemConflict and makes zero writes
            manifest_before = _vault_manifest(vault)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                items.create_item(
                    vault,
                    identifiers=[ident("doi", DOI)],
                    confirm_token="bogus-duplicate-token",
                    rebuild_hook=hook,
                )
            hook.assert_not_called()
            manifest_after = _vault_manifest(vault)
            self.assertEqual(_manifest_diff(manifest_before, manifest_after), ([], [], []))

            # Valid token returns duplicate_exists with status="attached", zero writes, hook 0
            hook = mock.Mock()
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                confirm_token=token,
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            hook.assert_not_called()
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_backward_compatibility_without_token(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            adapter = FakeAdapter(CROSSREF_VALUES)
            hook = mock.Mock()
            # Explicitly omitting confirm_token (default None) creates item immediately
            result = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": adapter},
                rebuild_hook=hook,
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(count_paper_dirs(vault), 1)
            hook.assert_called_once()

            # Second call without token detects duplicate immediately
            hook.reset_mock()
            second = items.create_item(
                vault,
                identifiers=[ident("doi", DOI)],
                adapters={"doi": adapter},
                rebuild_hook=hook,
            )
            self.assertEqual(second.status, "attached")
            self.assertEqual(second.action, "duplicate_exists")
            hook.assert_not_called()

    def test_cli_fuzzy_escape_and_confirm_token_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            (vault / "05 Literature").mkdir(parents=True)
            # Create paper 1 via CLI
            conf1 = vault / "conf1.json"
            conf1.write_text(
                json.dumps({
                    "title": "Quantum Computing in Biology",
                    "authors": [{"family": "Smith", "given": "John"}],
                    "year": 2024,
                }),
                encoding="utf-8",
            )
            res1 = subprocess.run(
                [sys.executable, "-m", "paper_notes", "--json", "item", "create", "--vault", str(vault), "--confirmed", str(conf1)],
                capture_output=True,
                text=True,
                check=True,
            )
            data1 = json.loads(res1.stdout)
            self.assertEqual(data1["status"], "success")
            self.assertEqual(data1["data"]["action"], "created")

            # Paper 2: same author, same year, different title -> fuzzy candidate
            conf2 = vault / "conf2.json"
            conf2.write_text(
                json.dumps({
                    "title": "Machine Learning in Astronomy",
                    "authors": [{"family": "Smith", "given": "John"}],
                    "year": 2024,
                }),
                encoding="utf-8",
            )

            # Step 1: preview / initial create without confirm token returns needs_confirmation
            res2 = subprocess.run(
                [sys.executable, "-m", "paper_notes", "--json", "item", "create", "--vault", str(vault), "--confirmed", str(conf2)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res2.returncode, 0)
            data2 = json.loads(res2.stdout)
            self.assertEqual(data2["status"], "needs_confirmation")
            self.assertEqual(data2["data"]["action"], "confirm_candidates")
            token = data2["data"]["confirmation_token"]
            self.assertTrue(token)
            self.assertEqual(len(data2["data"]["candidates"]), 1)

            # Step 2: bogus token fails with conflict (rc 3)
            res_bogus = subprocess.run(
                [sys.executable, "-m", "paper_notes", "--json", "item", "create", "--vault", str(vault), "--confirmed", str(conf2), "--confirm-token", "bogus-cli-token"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_bogus.returncode, 3)
            data_bogus = json.loads(res_bogus.stdout)
            self.assertEqual(data_bogus["status"], "conflict")

            # Step 3: dry-run combined with confirm-token fails with user error (rc 2)
            res_dry_conflict = subprocess.run(
                [sys.executable, "-m", "paper_notes", "--json", "item", "create", "--vault", str(vault), "--confirmed", str(conf2), "--dry-run", "--confirm-token", token],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_dry_conflict.returncode, 2)

            # Step 4: confirmed execution with valid token succeeds (rc 0), creating 2nd paper
            res_confirmed = subprocess.run(
                [sys.executable, "-m", "paper_notes", "--json", "item", "create", "--vault", str(vault), "--confirmed", str(conf2), "--confirm-token", token],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_confirmed.returncode, 0)
            data_confirmed = json.loads(res_confirmed.stdout)
            self.assertEqual(data_confirmed["status"], "success")
            self.assertEqual(data_confirmed["data"]["action"], "created")
            self.assertIn("candidates", data_confirmed["data"])
            self.assertEqual(count_paper_dirs(vault), 2)


if __name__ == "__main__":
    unittest.main()
