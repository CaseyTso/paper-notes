"""Primary / supplementary PDF attachment and reconcile tests (Task 12).

Frozen after the first red run; do not weaken or delete assertions.
Synthetic PDFs are built with PyMuPDF in isolated temporary directories —
no network, no real vault, no real PDFs. The rebuild-hook contract is
shared with Task 11: exactly one call on every successful mutation
(attached_primary / reconciled_metadata / attached_supplement /
reconciled), zero calls on no-op outcomes (already_attached /
already_present / needs_confirmation / no_changes / conflicts /
failures). Confirmation tokens bind paper_id, canonical key, source
hash, current target hash+type, and note bytes; every stale-token
confirm is a read-only ItemConflict.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import fitz

from paper_notes import attachments, cli, fsops, items
from paper_notes import pdf as pdf_mod
from paper_notes.frontmatter import load_paper_note
from paper_notes.pdf import PdfError

REPO = Path(__file__).resolve().parents[1]

DOI = "10.1038/s41591-024-00000-0"


def make_vault():
    return Path(tempfile.mkdtemp())


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


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def staging_residue(vault):
    """Files left in the staging area.

    An absent staging directory is legitimate zero residue — the failure
    happened before any transaction began — so the assertion is
    "staging is empty or does not exist", not "staging exists and is
    empty" (which crashes with FileNotFoundError on the legal absent
    case). Equal-strength helper for the zero-residue checks.
    """
    staging = Path(vault) / ".paper-notes" / ".staging"
    if not staging.is_dir():
        return []
    return list(staging.iterdir())


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


def primary_target(vault, key):
    return vault / "05 Literature" / key / f"{key}.pdf"


def note_path(vault, key):
    return vault / "05 Literature" / key / f"{key}.md"


def write_lock_file(vault, pid):
    lockdir = vault / ".paper-notes"
    lockdir.mkdir(parents=True, exist_ok=True)
    (lockdir / "write.lock").write_text(
        json.dumps(
            {
                "pid": pid,
                "started_at": "2026-01-01T00:00:00+00:00",
                "operation": "attach_pdf",
                "operation_id": "abc123",
                "host": "test",
            }
        ),
        encoding="utf-8",
    )


def lying_extraction(new_sha):
    """Adapter: run the real extraction, then lie about the source hash."""

    def fake(path):
        result = pdf_mod.extract_pdf_identifiers(path)
        return replace(result, sha256=new_sha)

    return fake


class PrimaryAttachTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.vault_path = self.vault

    def _cleanup(self):
        shutil.rmtree(self.vault_path, ignore_errors=True)

    def test_primary_attach_copies_pdf_updates_note_and_verifies(self):
        write_paper(self.vault, "smithExample2026", doi=DOI, pdf_status="missing")
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "attached_primary")
        self.assertEqual(result.citation_key, "smithExample2026")
        self.assertEqual(result.sha256, file_sha(pdf))
        target = primary_target(self.vault, "smithExample2026")
        self.assertTrue(target.is_file())
        self.assertEqual(file_sha(target), file_sha(pdf))
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, file_sha(pdf))
        hook.assert_called_once()

    def test_primary_attach_leaves_source_intact(self):
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        before = pdf.read_bytes()
        stat_before = pdf.stat()
        attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertEqual(pdf.read_bytes(), before)
        self.assertEqual(pdf.stat().st_mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual(pdf.stat().st_ino, stat_before.st_ino)

    def test_primary_attach_resolves_alias(self):
        write_paper(self.vault, "smithExample2026", aliases=("smithOld2026",))
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        result = attachments.attach_pdf(self.vault, key="smithOld2026", file=pdf)
        self.assertEqual(result.citation_key, "smithExample2026")
        self.assertTrue(primary_target(self.vault, "smithExample2026").is_file())
        self.assertFalse(primary_target(self.vault, "smithOld2026").exists())

    def test_primary_same_hash_and_metadata_idempotent(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(
            self.vault, "smithExample2026", pdf_status="available", pdf_sha256=sha
        )
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        target_before = target.stat()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "already_attached")
        self.assertEqual(note.read_bytes(), note_before)
        self.assertEqual(target.stat().st_mtime_ns, target_before.st_mtime_ns)
        self.assertEqual(target.stat().st_ino, target_before.st_ino)
        hook.assert_not_called()

    def test_primary_same_file_metadata_mismatch_needs_confirmation(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        # YAML says missing, but the file already matches the source
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
        )
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.action, "reconcile_metadata")
        self.assertTrue(result.confirmation_token)
        self.assertEqual(result.plan["action"], "reconcile_metadata")
        # zero writes: file and note untouched
        self.assertEqual(file_sha(target), sha)
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()

    def test_primary_metadata_mismatch_confirm_fixes_metadata_only(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertEqual(preview.action, "reconcile_metadata")
        target_before = target.stat()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=pdf,
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "reconciled_metadata")
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, sha)
        # the file itself was not rewritten
        self.assertEqual(target.stat().st_mtime_ns, target_before.st_mtime_ns)
        self.assertEqual(target.stat().st_ino, target_before.st_ino)
        hook.assert_called_once()

    def test_primary_different_existing_requires_confirmation(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        old_bytes = target.read_bytes()
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault, key="smithExample2026", file=new, rebuild_hook=hook
        )
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.action, "replace_primary")
        self.assertTrue(result.confirmation_token)
        self.assertEqual(result.plan["action"], "attach_pdf")
        self.assertEqual(result.plan["existing_sha256"], file_sha(old))
        self.assertEqual(result.plan["incoming_sha256"], file_sha(new))
        # zero writes: existing primary and note untouched
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()

    def test_primary_confirm_replacement_copies_and_verifies(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        self.assertEqual(preview.action, "replace_primary")
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=new,
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "attached_primary")
        self.assertEqual(file_sha(target), file_sha(new))
        self.assertNotEqual(file_sha(target), file_sha(old))
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, file_sha(new))
        hook.assert_called_once()

    def test_primary_confirm_stale_token_source_changed(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        # the source file changes between preview and confirm
        make_pdf(self.vault / "new.pdf", texts=["changed source"])
        target_before = target.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertEqual(target.read_bytes(), target_before)
        hook.assert_not_called()

    def test_primary_confirm_stale_token_target_changed(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        # the target primary changes between preview and confirm
        other = make_pdf(self.vault / "other.pdf", texts=["someone else replaced it"])
        target.write_bytes(other.read_bytes())
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertEqual(target.read_bytes(), other.read_bytes())
        hook.assert_not_called()

    def test_primary_confirm_stale_token_note_changed(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        note = note_path(self.vault, "smithExample2026")
        note.write_text(note.read_text(encoding="utf-8") + "manual edit\n", encoding="utf-8")
        edited = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        # the manual edit survives byte-for-byte; nothing else written
        self.assertEqual(note.read_bytes(), edited)
        hook.assert_not_called()

    def test_primary_confirm_stale_token_target_type_changed(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        # the target becomes a symlink between preview and confirm
        external = make_pdf(self.vault / "external.pdf", texts=["external"])
        target.unlink()
        target.symlink_to(external)
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertTrue(target.is_symlink())
        hook.assert_not_called()

    def test_primary_confirm_replacement_failure_restores_old(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        # inject a copy-verification failure: the staged copy will not hash
        # to the (real) source hash, so the transaction must roll back and
        # restore the previous primary byte-identically.
        with mock.patch(
            "paper_notes.items.sha256_stream", return_value="0" * 64
        ):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=new,
                    confirm_token=preview.confirmation_token,
                    rebuild_hook=hook,
                )
        self.assertEqual(file_sha(target), file_sha(old))
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()

    def test_primary_rejects_symlink_source(self):
        write_paper(self.vault, "smithExample2026")
        real = make_pdf(self.vault / "real.pdf", texts=[f"DOI {DOI}"])
        link = self.vault / "link.pdf"
        link.symlink_to(real)
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=link)
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())

    def test_primary_rejects_directory_source(self):
        write_paper(self.vault, "smithExample2026")
        d = self.vault / "sourcedir"
        d.mkdir()
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=d)

    def test_primary_rejects_missing_source(self):
        write_paper(self.vault, "smithExample2026")
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=self.vault / "ghost.pdf"
            )

    def test_primary_rejects_symlink_target(self):
        external = make_pdf(self.vault / "external.pdf", texts=["external"])
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        target = primary_target(self.vault, "smithExample2026")
        target.symlink_to(external)
        incoming = make_pdf(self.vault / "incoming.pdf", texts=["incoming"])
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=incoming, rebuild_hook=hook
            )
        self.assertTrue(target.is_symlink())
        hook.assert_not_called()

    def test_primary_rejects_non_pdf_source(self):
        write_paper(self.vault, "smithExample2026")
        fake = self.vault / "notapdf.pdf"
        fake.write_text("this is definitely not a pdf", encoding="utf-8")
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=fake)

    def test_primary_unknown_key_item_error(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(self.vault, key="ghost2026", file=pdf)

    def test_primary_repository_identity_conflict_read_only(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        write_paper(self.vault, "smithExample2026", paper_id=uid)
        write_paper(
            self.vault,
            "jonesOther2026",
            paper_id=uid,
            title="Another paper",
            authors=[{"family": "Jones", "given": "Beth"}],
        )
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        note = note_path(self.vault, "smithExample2026")
        before = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
            )
        self.assertEqual(note.read_bytes(), before)
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())
        hook.assert_not_called()

    def test_primary_copy_hash_mismatch_rolls_back_zero_residue(self):
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        note = note_path(self.vault, "smithExample2026")
        before = note.read_bytes()
        hook = mock.Mock()
        with mock.patch(
            "paper_notes.attachments.extract_pdf_identifiers",
            side_effect=lying_extraction("0" * 64),
        ):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
                )
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())
        self.assertEqual(note.read_bytes(), before)
        staging = self.vault / ".paper-notes" / ".staging"
        self.assertEqual(list(staging.iterdir()), [])
        self.assertFalse((self.vault / ".paper-notes" / "write.lock").exists())
        hook.assert_not_called()

    def test_primary_hook_failure_rolls_back_byte_identical(self):
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        note = note_path(self.vault, "smithExample2026")
        before = note.read_bytes()
        hook = mock.Mock(side_effect=RuntimeError("simulated rebuild failure"))
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
            )
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())
        self.assertEqual(note.read_bytes(), before)
        self.assertFalse((self.vault / ".paper-notes" / "write.lock").exists())
        hook.assert_called_once()

    def test_primary_commit_conflict_not_rolled_back(self):
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        real_commit = fsops.commit

        def conflicted_commit(op):
            conflicts = real_commit(op)
            return conflicts + [str(self.vault / "05 Literature" / "x" / "x.md")]

        hook = mock.Mock()
        with mock.patch("paper_notes.fsops.commit", side_effect=conflicted_commit):
            with self.assertRaises(items.ItemConflict):
                attachments.attach_pdf(
                    self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
                )
        hook.assert_called_once()
        self.assertFalse((self.vault / ".paper-notes" / "write.lock").exists())
        # the finished operation is not rolled back
        self.assertTrue(primary_target(self.vault, "smithExample2026").is_file())
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")

    def test_primary_held_lock_is_conflict(self):
        write_paper(self.vault, "smithExample2026")
        write_lock_file(self.vault, os.getpid())
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)

    def test_primary_stale_lock_is_item_error(self):
        write_paper(self.vault, "smithExample2026")
        write_lock_file(self.vault, 999999999)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)

    def test_primary_concurrent_target_change_before_lock_needs_confirmation(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        planted = make_pdf(self.vault / "planted.pdf", texts=["planted version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(planted.read_bytes())
        incoming = make_pdf(self.vault / "incoming.pdf", texts=["incoming version"])
        real_acquire = items.acquire_lock

        def concurrent_acquire(root, operation):
            # another writer replaces the primary before we lock
            target.write_bytes(make_pdf(self.vault / "racer.pdf", texts=["racer"]).read_bytes())
            return real_acquire(root, operation)

        hook = mock.Mock()
        with mock.patch.object(items, "acquire_lock", side_effect=concurrent_acquire):
            result = attachments.attach_pdf(
                self.vault, key="smithExample2026", file=incoming, rebuild_hook=hook
            )
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.action, "replace_primary")
        self.assertTrue(result.confirmation_token)
        hook.assert_not_called()

    def test_primary_item_directory_symlink_is_conflict(self):
        d = write_paper(self.vault, "smithExample2026", doi=DOI)
        outside = self.vault / "outside"
        outside.mkdir()
        (outside / "smithExample2026.md").write_text(
            (d / "smithExample2026.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        shutil.rmtree(d)
        (d.parent / "smithExample2026").symlink_to(outside, target_is_directory=True)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertTrue((d.parent / "smithExample2026").is_symlink())

    def test_primary_fresh_attach_when_yaml_available_but_no_sha(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        result = attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertEqual(result.action, "attached_primary")
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, file_sha(pdf))

    # ---- round-2 root-cause regression: token-first validation (A/B/D) ----

    def test_primary_confirm_stale_token_target_deleted_between_preview_and_confirm(self):
        # (A) preview saw a differing primary; the target is deleted before
        # confirm. The old token does NOT authorize a fresh attach: it must
        # be a stale ItemConflict with zero writes and hook 0.
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        self.assertEqual(preview.action, "replace_primary")
        target.unlink()
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        # zero writes: the target is NOT re-created, the note is untouched
        self.assertFalse(target.exists())
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()
        self.assertEqual(
            staging_residue(self.vault), []
        )

    def test_primary_fresh_attach_with_arbitrary_token_is_stale_conflict(self):
        # (B) a fresh target (absent) has NO confirmation flow: any token
        # arriving here is stale by construction — conflict, zero writes.
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=pdf,
                confirm_token="deadbeef",
                rebuild_hook=hook,
            )
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()

    def test_primary_replay_token_after_successful_replace_is_stale_conflict(self):
        # (D) after a successful replacement the current state needs no
        # confirmation; replaying the old token is stale — conflict, zero
        # writes, hook 0 (already-attached state preserved byte-identical).
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=new,
            confirm_token=preview.confirmation_token,
        )
        after_bytes = target.read_bytes()
        note = note_path(self.vault, "smithExample2026")
        note_after = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=new,
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertEqual(target.read_bytes(), after_bytes)
        self.assertEqual(note.read_bytes(), note_after)
        hook.assert_not_called()

    # ---- round-2 root-cause regression: check-to-use (E/F) ----

    def test_primary_fresh_attach_racer_target_before_transaction_conflict(self):
        # (E) the absent decision was made; an external writer plants a
        # racer target before the transaction runs. The transaction must
        # verify the expected (absent) state at entry: ItemConflict, racer
        # preserved byte-identical, note unchanged, hook 0.
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        target = primary_target(self.vault, "smithExample2026")
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        racer = make_pdf(self.vault / "racer.pdf", texts=["racer wrote it first"])
        racer_bytes = racer.read_bytes()
        hook = mock.Mock()
        real_tx = attachments._primary_pdf_transaction

        def plant_then_transact(*args, **kwargs):
            target.write_bytes(racer_bytes)
            return real_tx(*args, **kwargs)

        with mock.patch(
            "paper_notes.attachments._primary_pdf_transaction",
            side_effect=plant_then_transact,
        ):
            with self.assertRaises(items.ItemConflict):
                attachments.attach_pdf(
                    self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
                )
        self.assertEqual(target.read_bytes(), racer_bytes)
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()
        self.assertEqual(
            staging_residue(self.vault), []
        )

    def test_primary_replace_racer_target_after_token_check_conflict(self):
        # (F) the token validated against the old target hash; an external
        # writer replaces the target before the transaction runs. The
        # transaction must verify the expected state at entry: stale
        # ItemConflict, racer preserved, note unchanged, hook 0.
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old version"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new version"])
        preview = attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        self.assertEqual(preview.action, "replace_primary")
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        racer = make_pdf(self.vault / "racer.pdf", texts=["racer"])
        racer_bytes = racer.read_bytes()
        hook = mock.Mock()
        real_tx = attachments._primary_pdf_transaction

        def plant_then_transact(*args, **kwargs):
            target.write_bytes(racer_bytes)
            return real_tx(*args, **kwargs)

        with mock.patch(
            "paper_notes.attachments._primary_pdf_transaction",
            side_effect=plant_then_transact,
        ):
            with self.assertRaises(items.ItemConflict):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=new,
                    confirm_token=preview.confirmation_token,
                    rebuild_hook=hook,
                )
        self.assertEqual(target.read_bytes(), racer_bytes)
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()
        self.assertEqual(
            staging_residue(self.vault), []
        )

    # ---- round-2 root-cause regression: source topology (fail closed) ----

    def test_primary_source_becomes_symlink_before_copy_fails_closed(self):
        # the source was verified as a real PDF; it becomes a symlink to
        # byte-identical content before the copy — still an ItemError, zero
        # writes (the copy must never follow the link).
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        hook = mock.Mock()
        real_copy = items._stage_file_copy

        def symlink_then_copy(op, target, source, source_sha):
            real = self.vault / "real.pdf"
            real.write_bytes(pdf.read_bytes())
            pdf.unlink()
            pdf.symlink_to(real)
            return real_copy(op, target, source, source_sha)

        with mock.patch(
            "paper_notes.items._stage_file_copy", side_effect=symlink_then_copy
        ):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault, key="smithExample2026", file=pdf, rebuild_hook=hook
                )
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())
        hook.assert_not_called()


class SupplementAttachTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.vault_path = self.vault

    def _cleanup(self):
        shutil.rmtree(self.vault_path, ignore_errors=True)

    def attach_dir(self, key="smithExample2026"):
        return self.vault / "05 Literature" / key / "attachments"

    def test_supplement_copies_any_regular_file(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "supplement.xlsx"
        blob.write_bytes(b"PK\x03\x04 not really xlsx but a regular file")
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=blob,
            supplementary=True,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "attached_supplement")
        target = self.attach_dir() / "supplement.xlsx"
        self.assertTrue(target.is_file())
        self.assertEqual(file_sha(target), file_sha(blob))
        # the note is never touched by supplementary attach
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_called_once()

    def test_supplement_same_content_same_name_idempotent(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "supplement.txt"
        blob.write_bytes(b"same bytes")
        attachments.attach_pdf(
            self.vault, key="smithExample2026", file=blob, supplementary=True
        )
        target = self.attach_dir() / "supplement.txt"
        target_before = target.stat()
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=blob,
            supplementary=True,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.action, "already_present")
        self.assertEqual(result.target, str(target))
        self.assertEqual(target.stat().st_mtime_ns, target_before.st_mtime_ns)
        self.assertEqual(target.stat().st_ino, target_before.st_ino)
        hook.assert_not_called()

    def test_supplement_same_content_different_name_idempotent(self):
        write_paper(self.vault, "smithExample2026")
        first = self.vault / "first.txt"
        first.write_bytes(b"identical payload")
        attachments.attach_pdf(
            self.vault, key="smithExample2026", file=first, supplementary=True
        )
        second = self.vault / "second.txt"
        second.write_bytes(b"identical payload")
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=second,
            supplementary=True,
            rebuild_hook=hook,
        )
        self.assertEqual(result.action, "already_present")
        self.assertEqual(result.target, str(self.attach_dir() / "first.txt"))
        # no duplicate file was created under the second name
        self.assertFalse((self.attach_dir() / "second.txt").exists())
        hook.assert_not_called()

    def test_supplement_same_name_different_content_hash_suffix(self):
        write_paper(self.vault, "smithExample2026")
        first = self.vault / "fig.png"
        first.write_bytes(b"version one")
        attachments.attach_pdf(
            self.vault, key="smithExample2026", file=first, supplementary=True
        )
        original = self.attach_dir() / "fig.png"
        original_bytes = original.read_bytes()
        # a DIFFERENT source file with the SAME basename: the existing
        # attachment must never be overwritten — a deterministic hash
        # suffix is allocated instead
        incoming_dir = self.vault / "incoming"
        incoming_dir.mkdir()
        second = incoming_dir / "fig.png"
        second.write_bytes(b"version two different")
        hook = mock.Mock()
        result = attachments.attach_pdf(
            self.vault,
            key="smithExample2026",
            file=second,
            supplementary=True,
            rebuild_hook=hook,
        )
        self.assertEqual(result.action, "attached_supplement")
        sha = file_sha(second)
        suffixed = self.attach_dir() / f"fig.{sha[:16]}.png"
        self.assertTrue(suffixed.is_file())
        self.assertEqual(file_sha(suffixed), sha)
        # the existing file's bytes are never changed
        self.assertEqual(original.read_bytes(), original_bytes)
        hook.assert_called_once()

    def test_supplement_rejects_directory_source(self):
        write_paper(self.vault, "smithExample2026")
        d = self.vault / "somedir"
        d.mkdir()
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=d, supplementary=True
            )

    def test_supplement_rejects_symlink_source(self):
        write_paper(self.vault, "smithExample2026")
        real = self.vault / "real.txt"
        real.write_bytes(b"data")
        link = self.vault / "link.txt"
        link.symlink_to(real)
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=link, supplementary=True
            )

    def test_supplement_rejects_attachments_dir_symlink(self):
        d = write_paper(self.vault, "smithExample2026")
        outside = self.vault / "outside-att"
        outside.mkdir()
        (d / "attachments").symlink_to(outside, target_is_directory=True)
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=blob, supplementary=True
            )
        self.assertTrue((d / "attachments").is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_supplement_rejects_unsafe_basename(self):
        write_paper(self.vault, "smithExample2026")
        evil = self.vault / "evil\\name.txt"
        evil.write_bytes(b"data")
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=evil, supplementary=True
            )
        self.assertFalse(self.attach_dir().exists())

    def test_supplement_copy_failure_zero_residue(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        hook = mock.Mock()
        with mock.patch("paper_notes.items.sha256_stream", return_value="0" * 64):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertFalse(self.attach_dir().exists())
        hook.assert_not_called()

    def test_supplement_target_symlink_is_conflict(self):
        d = write_paper(self.vault, "smithExample2026")
        att = d / "attachments"
        att.mkdir()
        outside = self.vault / "outside-blob.txt"
        outside.write_bytes(b"outside")
        (att / "blob.txt").symlink_to(outside)
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"new bytes")
        with self.assertRaises(items.ItemConflict):
            attachments.attach_pdf(
                self.vault, key="smithExample2026", file=blob, supplementary=True
            )
        self.assertTrue((att / "blob.txt").is_symlink())

    # ---- round-2 root-cause regression: token rejection + races (C) ----

    def test_supplement_confirm_token_rejected_zero_writes(self):
        # (C) supplementary attach has NO confirmation flow: any token is an
        # explicit user error (ItemError, rc 2), zero writes, hook 0.
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemError):
            attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=blob,
                supplementary=True,
                confirm_token="deadbeef",
                rebuild_hook=hook,
            )
        self.assertFalse(self.attach_dir().exists())
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()

    def test_supplement_racer_target_before_transaction_conflict(self):
        # a different-content file lands at the decided target between the
        # decision and the transaction: never overwritten — ItemConflict,
        # racer preserved byte-identical, hook 0.
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"incoming bytes")
        att_dir = self.attach_dir()
        target = att_dir / "blob.txt"
        hook = mock.Mock()
        real_begin = fsops.begin_operation

        def plant_then_begin(root, operation_id):
            att_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"racer bytes")
            return real_begin(root, operation_id)

        with mock.patch(
            "paper_notes.fsops.begin_operation", side_effect=plant_then_begin
        ):
            with self.assertRaises(items.ItemConflict):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertEqual(target.read_bytes(), b"racer bytes")
        hook.assert_not_called()
        self.assertEqual(
            staging_residue(self.vault), []
        )

    def test_supplement_same_content_racer_under_other_name_no_duplicate(self):
        # identical content appears under ANOTHER name between the decision
        # and the transaction: no duplicate copy is created — the outcome is
        # already_present and the pending write is rolled back.
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "incoming.txt"
        blob.write_bytes(b"same payload")
        att_dir = self.attach_dir()
        hook = mock.Mock()
        real_begin = fsops.begin_operation

        def plant_then_begin(root, operation_id):
            att_dir.mkdir(parents=True, exist_ok=True)
            (att_dir / "racer.txt").write_bytes(b"same payload")
            return real_begin(root, operation_id)

        with mock.patch(
            "paper_notes.fsops.begin_operation", side_effect=plant_then_begin
        ):
            result = attachments.attach_pdf(
                self.vault,
                key="smithExample2026",
                file=blob,
                supplementary=True,
                rebuild_hook=hook,
            )
        self.assertEqual(result.action, "already_present")
        self.assertEqual(result.target, str(att_dir / "racer.txt"))
        # no duplicate copy was created under the incoming name
        self.assertFalse((att_dir / "incoming.txt").exists())
        hook.assert_not_called()

    # ---- round-2 root-cause regression: source topology (fail closed) ----

    def test_supplement_source_becomes_symlink_before_copy_fails_closed(self):
        # the source becomes a symlink to byte-identical content after the
        # initial check: the copy must not follow the link — ItemError,
        # zero writes.
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"original bytes")
        hook = mock.Mock()
        real_copy = attachments._stage_file_copy

        def symlink_then_copy(op, target, source, source_sha):
            real = self.vault / "real.txt"
            real.write_bytes(b"original bytes")
            blob.unlink()
            blob.symlink_to(real)
            return real_copy(op, target, source, source_sha)

        with mock.patch(
            "paper_notes.attachments._stage_file_copy", side_effect=symlink_then_copy
        ):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertFalse(self.attach_dir().exists())
        hook.assert_not_called()

    def test_supplement_source_becomes_directory_before_copy_fails_closed(self):
        # the source becomes a directory after the initial check: fail
        # closed with zero writes (defense in depth — also pinned here).
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        hook = mock.Mock()
        real_copy = attachments._stage_file_copy

        def dir_then_copy(op, target, source, source_sha):
            blob.unlink()
            blob.mkdir()
            return real_copy(op, target, source, source_sha)

        with mock.patch(
            "paper_notes.attachments._stage_file_copy", side_effect=dir_then_copy
        ):
            with self.assertRaises(items.ItemError):
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertFalse(self.attach_dir().exists())
        hook.assert_not_called()


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.vault_path = self.vault

    def _cleanup(self):
        shutil.rmtree(self.vault_path, ignore_errors=True)

    def test_reconcile_consistent_available_no_changes(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(self.vault, "smithExample2026", pdf_status="available", pdf_sha256=sha)
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        note = note_path(self.vault, "smithExample2026")
        target_before = target.stat()
        note_before = note.stat()
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault, key="smithExample2026", rebuild_hook=hook
        )
        self.assertEqual(result.status, "no_changes")
        self.assertEqual(result.action, "consistent")
        self.assertIsNone(result.confirmation_token)
        self.assertEqual(result.before["pdf_status"], "available")
        self.assertEqual(result.before["pdf_sha256"], sha)
        self.assertEqual(target.stat().st_mtime_ns, target_before.st_mtime_ns)
        self.assertEqual(note.stat().st_mtime_ns, note_before.st_mtime_ns)
        hook.assert_not_called()

    def test_reconcile_consistent_missing_no_changes(self):
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault, key="smithExample2026", rebuild_hook=hook
        )
        self.assertEqual(result.status, "no_changes")
        self.assertEqual(result.action, "consistent")
        hook.assert_not_called()

    def test_reconcile_preview_file_present_yaml_missing(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault, key="smithExample2026", rebuild_hook=hook
        )
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.action, "reconcile_pdf_status")
        self.assertTrue(result.confirmation_token)
        self.assertEqual(result.before, {"pdf_status": "missing", "pdf_sha256": None})
        self.assertEqual(
            result.after, {"pdf_status": "available", "pdf_sha256": sha}
        )
        self.assertEqual(result.plan["action"], "reconcile_pdf_status")
        self.assertEqual(result.plan["actual"]["type"], "file")
        # preview is read-only: zero writes, zero placeholder
        self.assertEqual(note.read_bytes(), note_before)
        self.assertEqual(file_sha(target), sha)
        hook.assert_not_called()

    def test_reconcile_preview_sha_mismatch(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(
            self.vault,
            "smithExample2026",
            pdf_status="available",
            pdf_sha256="0" * 64,
        )
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        result = attachments.reconcile(self.vault, key="smithExample2026")
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(
            result.after, {"pdf_status": "available", "pdf_sha256": sha}
        )

    def test_reconcile_preview_available_but_missing_file(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available", pdf_sha256="0" * 64)
        result = attachments.reconcile(self.vault, key="smithExample2026")
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.before["pdf_status"], "available")
        self.assertEqual(
            result.after, {"pdf_status": "missing", "pdf_sha256": None}
        )

    def test_reconcile_confirm_updates_yaml_only_file_untouched(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        target_before = target.stat()
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault,
            key="smithExample2026",
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "reconciled")
        self.assertEqual(result.action, "reconciled")
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, sha)
        # the actual PDF is never rewritten, moved, or deleted
        self.assertEqual(file_sha(target), sha)
        self.assertEqual(target.stat().st_mtime_ns, target_before.st_mtime_ns)
        self.assertEqual(target.stat().st_ino, target_before.st_ino)
        hook.assert_called_once()

    def test_reconcile_confirm_missing_clears_hash(self):
        write_paper(self.vault, "smithExample2026", pdf_status="available", pdf_sha256="0" * 64)
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        self.assertEqual(preview.after, {"pdf_status": "missing", "pdf_sha256": None})
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault,
            key="smithExample2026",
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "reconciled")
        raw = note_path(self.vault, "smithExample2026").read_text(encoding="utf-8")
        self.assertIn("pdf_status: missing", raw)
        self.assertNotIn("pdf_sha256", raw)
        paper, _ = load_paper_note(note_path(self.vault, "smithExample2026"))
        # pdf_sha256 is an extra (non-reserved) field: absent means None
        self.assertNotIn("pdf_sha256", paper.model_extra or {})
        hook.assert_called_once()

    def test_reconcile_stale_note_changed_conflict(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        note = note_path(self.vault, "smithExample2026")
        note.write_text(note.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        edited = note.read_bytes()
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.reconcile(
                self.vault,
                key="smithExample2026",
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertEqual(note.read_bytes(), edited)
        hook.assert_not_called()

    def test_reconcile_stale_target_changed_conflict(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        other = make_pdf(self.vault / "other.pdf", texts=["other"])
        target.write_bytes(other.read_bytes())
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.reconcile(
                self.vault,
                key="smithExample2026",
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertEqual(file_sha(target), file_sha(other))
        hook.assert_not_called()

    def test_reconcile_stale_type_changed_conflict(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        external = make_pdf(self.vault / "external.pdf", texts=["external"])
        target.unlink()
        target.symlink_to(external)
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.reconcile(
                self.vault,
                key="smithExample2026",
                confirm_token=preview.confirmation_token,
                rebuild_hook=hook,
            )
        self.assertTrue(target.is_symlink())
        hook.assert_not_called()

    def test_reconcile_preview_symlink_target_proposes_missing(self):
        external = make_pdf(self.vault / "external.pdf", texts=["external"])
        write_paper(self.vault, "smithExample2026", pdf_status="available", pdf_sha256="0" * 64)
        target = primary_target(self.vault, "smithExample2026")
        target.symlink_to(external)
        hook = mock.Mock()
        result = attachments.reconcile(
            self.vault, key="smithExample2026", rebuild_hook=hook
        )
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.after, {"pdf_status": "missing", "pdf_sha256": None})
        self.assertIn("warning", result.plan)
        # the symlink is observed but never touched
        self.assertTrue(target.is_symlink())
        hook.assert_not_called()

    def test_reconcile_unknown_key_item_error(self):
        with self.assertRaises(items.ItemError):
            attachments.reconcile(self.vault, key="ghost2026")

    def test_reconcile_identity_conflict_read_only(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        write_paper(self.vault, "smithExample2026", paper_id=uid)
        write_paper(
            self.vault,
            "jonesOther2026",
            paper_id=uid,
            title="Another paper",
            authors=[{"family": "Jones", "given": "Beth"}],
        )
        hook = mock.Mock()
        with self.assertRaises(items.ItemConflict):
            attachments.reconcile(
                self.vault, key="smithExample2026", rebuild_hook=hook
            )
        hook.assert_not_called()

    def test_reconcile_resolves_alias(self):
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        write_paper(self.vault, "smithExample2026", pdf_status="missing", aliases=("smithOld2026",))
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        result = attachments.reconcile(self.vault, key="smithOld2026")
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.citation_key, "smithExample2026")

    def test_reconcile_confirm_on_consistent_state_is_stale_conflict(self):
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        # preview requires confirmation (file appears)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        self.assertEqual(preview.status, "needs_confirmation")
        # the file disappears again: state is consistent now
        target.unlink()
        with self.assertRaises(items.ItemConflict):
            attachments.reconcile(
                self.vault,
                key="smithExample2026",
                confirm_token=preview.confirmation_token,
            )

    # ---- round-2 root-cause regression: reconcile check-to-use (G) ----

    def test_reconcile_note_changed_after_token_check_conflict(self):
        # (G) the token validated; the note is edited externally before the
        # transaction runs. The transaction must verify the expected note
        # bytes at entry: stale ItemConflict, external edit preserved, the
        # actual PDF untouched, hook 0.
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        sha = file_sha(pdf)
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        note = note_path(self.vault, "smithExample2026")
        hook = mock.Mock()
        real_tx = attachments._note_only_transaction

        def edit_then_transact(*args, **kwargs):
            note.write_text(
                note.read_text(encoding="utf-8") + "external edit\n",
                encoding="utf-8",
            )
            return real_tx(*args, **kwargs)

        with mock.patch(
            "paper_notes.attachments._note_only_transaction",
            side_effect=edit_then_transact,
        ):
            with self.assertRaises(items.ItemConflict):
                attachments.reconcile(
                    self.vault,
                    key="smithExample2026",
                    confirm_token=preview.confirmation_token,
                    rebuild_hook=hook,
                )
        self.assertIn(b"external edit", note.read_bytes())
        self.assertEqual(file_sha(target), sha)
        hook.assert_not_called()

    def test_reconcile_target_changed_after_token_check_conflict(self):
        # (G) the token validated; the actual primary PDF is replaced before
        # the transaction runs. Reconcile never writes the target, but the
        # authorized transition no longer matches: stale ItemConflict,
        # external target preserved, note unchanged, hook 0.
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        write_paper(self.vault, "smithExample2026", pdf_status="missing")
        target = primary_target(self.vault, "smithExample2026")
        target.write_bytes(pdf.read_bytes())
        preview = attachments.reconcile(self.vault, key="smithExample2026")
        note = note_path(self.vault, "smithExample2026")
        note_before = note.read_bytes()
        other = make_pdf(self.vault / "other.pdf", texts=["external replacement"])
        other_bytes = other.read_bytes()
        hook = mock.Mock()
        real_tx = attachments._note_only_transaction

        def swap_then_transact(*args, **kwargs):
            target.write_bytes(other_bytes)
            return real_tx(*args, **kwargs)

        with mock.patch(
            "paper_notes.attachments._note_only_transaction",
            side_effect=swap_then_transact,
        ):
            with self.assertRaises(items.ItemConflict):
                attachments.reconcile(
                    self.vault,
                    key="smithExample2026",
                    confirm_token=preview.confirmation_token,
                    rebuild_hook=hook,
                )
        self.assertEqual(file_sha(target), file_sha(other))
        self.assertEqual(note.read_bytes(), note_before)
        hook.assert_not_called()


class SecretSanitizationTest(unittest.TestCase):
    """Round-2 regression (H): low-level exception text must never enter
    ItemError / ItemConflict / the CLI JSON envelope."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.vault_path = self.vault

    def _cleanup(self):
        shutil.rmtree(self.vault_path, ignore_errors=True)

    def test_oserror_text_not_in_item_error_supplement_source_hash(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        with mock.patch(
            "paper_notes.attachments.sha256_stream",
            side_effect=OSError("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemError) as cm:
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                )
        self.assertNotIn("SECRET_MARK", str(cm.exception))

    def test_oserror_text_not_in_item_error_target_or_note_read(self):
        # primary attach with an existing differing target: the decision
        # reads the target (and note) through the attachment layer
        write_paper(self.vault, "smithExample2026", pdf_status="available")
        old = make_pdf(self.vault / "old.pdf", texts=["old"])
        primary_target(self.vault, "smithExample2026").write_bytes(old.read_bytes())
        new = make_pdf(self.vault / "new.pdf", texts=["new"])
        with mock.patch(
            "paper_notes.attachments.sha256_stream",
            side_effect=OSError("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemError) as cm:
                attachments.attach_pdf(self.vault, key="smithExample2026", file=new)
        self.assertNotIn("SECRET_MARK", str(cm.exception))

    def test_pdf_error_text_not_in_item_error(self):
        write_paper(self.vault, "smithExample2026")
        pdf = make_pdf(self.vault / "source.pdf", texts=["x"])
        with mock.patch(
            "paper_notes.attachments.extract_pdf_identifiers",
            side_effect=PdfError(Path(pdf), "SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemError) as cm:
                attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertNotIn("SECRET_MARK", str(cm.exception))

    def test_operation_conflict_text_not_in_item_conflict(self):
        write_paper(self.vault, "smithExample2026", doi=DOI)
        pdf = make_pdf(self.vault / "source.pdf", texts=[f"DOI {DOI}"])
        with mock.patch(
            "paper_notes.fsops.commit",
            side_effect=fsops.OperationConflict("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemConflict) as cm:
                attachments.attach_pdf(self.vault, key="smithExample2026", file=pdf)
        self.assertNotIn("SECRET_MARK", str(cm.exception))
        # the failed transaction rolled back with zero residue
        self.assertFalse(primary_target(self.vault, "smithExample2026").exists())

    # ---- final single-point (Task 12): the supplementary and item-update
    # OperationConflict handlers must never echo low-level exception text
    # (hidden probe: begin_operation raises OperationConflict("SECRET_MARK")
    # during supplement attach; the old code embedded `{exc}` in the
    # ItemConflict message). begin_operation / commit injections ----

    def test_supplement_begin_operation_conflict_hides_secret(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        hook = mock.Mock()
        with mock.patch(
            "paper_notes.attachments.fsops.begin_operation",
            side_effect=fsops.OperationConflict("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemConflict) as cm:
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertNotIn("SECRET_MARK", str(cm.exception))
        # the transaction never began: zero writes, hook 0, no staging
        self.assertFalse(
            (self.vault / "05 Literature" / "smithExample2026" / "attachments").exists()
        )
        hook.assert_not_called()
        self.assertEqual(staging_residue(self.vault), [])

    def test_supplement_commit_conflict_hides_secret(self):
        write_paper(self.vault, "smithExample2026")
        blob = self.vault / "blob.txt"
        blob.write_bytes(b"data")
        hook = mock.Mock()
        with mock.patch(
            "paper_notes.attachments.fsops.commit",
            side_effect=fsops.OperationConflict("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemConflict) as cm:
                attachments.attach_pdf(
                    self.vault,
                    key="smithExample2026",
                    file=blob,
                    supplementary=True,
                    rebuild_hook=hook,
                )
        self.assertNotIn("SECRET_MARK", str(cm.exception))
        # the failing commit was rolled back: no file landed, staging empty
        self.assertFalse(
            (self.vault / "05 Literature" / "smithExample2026" / "attachments").exists()
        )
        # hook fires immediately before commit (unchanged boundary); the
        # commit raising OperationConflict happens after that call
        hook.assert_called_once()
        self.assertEqual(staging_residue(self.vault), [])

    def test_item_update_begin_operation_conflict_hides_secret(self):
        write_paper(self.vault, "smithExample2026")
        note = note_path(self.vault, "smithExample2026")
        before = note.read_bytes()
        hook = mock.Mock()
        with mock.patch(
            "paper_notes.fsops.begin_operation",
            side_effect=fsops.OperationConflict("SECRET_MARK"),
        ):
            with self.assertRaises(items.ItemConflict) as cm:
                items.update_item(
                    self.vault,
                    key="smithExample2026",
                    patch={"reading_status": "read"},
                    rebuild_hook=hook,
                )
        self.assertNotIn("SECRET_MARK", str(cm.exception))
        # zero writes: note byte-identical, hook 0, no staging, lock released
        self.assertEqual(note.read_bytes(), before)
        hook.assert_not_called()
        self.assertEqual(staging_residue(self.vault), [])
        self.assertFalse((self.vault / ".paper-notes" / "write.lock").exists())


class AttachCliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
        )

    def test_cli_attach_primary_single_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI)
            pdf = make_pdf(vault / "source.pdf", texts=[f"DOI {DOI}"])
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026", "--file", str(pdf)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["protocol_version"], 1)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "attached_primary")
            self.assertEqual(payload["data"]["pdf_sha256"], file_sha(pdf))
            self.assertEqual(payload["data"]["citation_key"], "smithExample2026")
            self.assertTrue(
                primary_target(vault, "smithExample2026").is_file()
            )

    def test_cli_attach_different_primary_needs_confirmation_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", pdf_status="available")
            old = make_pdf(vault / "old.pdf", texts=["old"])
            primary_target(vault, "smithExample2026").write_bytes(old.read_bytes())
            new = make_pdf(vault / "new.pdf", texts=["new"])
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026", "--file", str(new)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "replace_primary")
            self.assertTrue(payload["data"]["confirmation_token"])
            self.assertEqual(payload["data"]["plan"]["action"], "attach_pdf")
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)

    def test_cli_attach_confirm_replacement_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", pdf_status="available")
            old = make_pdf(vault / "old.pdf", texts=["old"])
            primary_target(vault, "smithExample2026").write_bytes(old.read_bytes())
            new = make_pdf(vault / "new.pdf", texts=["new"])
            preview = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026", "--file", str(new)
            )
            token = json.loads(preview.stdout)["data"]["confirmation_token"]
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026",
                "--file", str(new), "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "attached_primary")
            self.assertEqual(file_sha(primary_target(vault, "smithExample2026")), file_sha(new))

    def test_cli_attach_stale_token_rc3(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", pdf_status="available")
            old = make_pdf(vault / "old.pdf", texts=["old"])
            primary_target(vault, "smithExample2026").write_bytes(old.read_bytes())
            new = make_pdf(vault / "new.pdf", texts=["new"])
            preview = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026", "--file", str(new)
            )
            token = json.loads(preview.stdout)["data"]["confirmation_token"]
            # target changes before confirm
            other = make_pdf(vault / "other.pdf", texts=["other"])
            primary_target(vault, "smithExample2026").write_bytes(other.read_bytes())
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026",
                "--file", str(new), "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "conflict")
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)

    def test_cli_attach_unknown_key_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "ghost2026", "--file", str(pdf)
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_attach_supplementary_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            blob = vault / "notes.csv"
            blob.write_text("a,b,c\n", encoding="utf-8")
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026",
                "--file", str(blob), "--supplementary",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "attached_supplement")
            self.assertTrue(
                (vault / "05 Literature" / "smithExample2026" / "attachments" / "notes.csv").is_file()
            )

    def test_cli_attach_missing_file_usage_error_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.run_cli("item", "attach-pdf", "--vault", str(Path(td)), "--key", "k2026")
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["errors"][0]["code"], "usage_error")
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)

    def test_cli_attach_metadata_fix_confirm_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            write_paper(vault, "smithExample2026", pdf_status="missing")
            primary_target(vault, "smithExample2026").write_bytes(pdf.read_bytes())
            preview = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026", "--file", str(pdf)
            )
            payload = json.loads(preview.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "reconcile_metadata")
            token = payload["data"]["confirmation_token"]
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026",
                "--file", str(pdf), "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "reconciled_metadata")

    # ---- round-2 regression: supplementary token rejection via CLI (C) ----

    def test_cli_supplement_confirm_token_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            blob = vault / "blob.txt"
            blob.write_bytes(b"data")
            result = self.run_cli(
                "item", "attach-pdf", "--vault", str(vault), "--key", "smithExample2026",
                "--file", str(blob), "--supplementary", "--confirm-token", "deadbeef",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            # zero writes
            self.assertFalse(
                (vault / "05 Literature" / "smithExample2026" / "attachments").exists()
            )

    # ---- round-2 regression (H): the CLI JSON envelope hides low-level
    # exception text (in-process run so mocks apply) ----

    def run_cli_inproc(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--json", *argv])
        return rc, buf.getvalue()

    def test_cli_envelope_hides_oserror_text(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            blob = vault / "blob.txt"
            blob.write_bytes(b"data")
            with mock.patch(
                "paper_notes.attachments.sha256_stream",
                side_effect=OSError("SECRET_MARK"),
            ):
                rc, out = self.run_cli_inproc(
                    "item", "attach-pdf", "--vault", str(vault), "--key",
                    "smithExample2026", "--file", str(blob), "--supplementary",
                )
            self.assertEqual(rc, 2)
            self.assertNotIn("SECRET_MARK", out)
            json.loads(out)  # still one parseable envelope

    def test_cli_envelope_hides_pdf_error_text(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            with mock.patch(
                "paper_notes.attachments.extract_pdf_identifiers",
                side_effect=PdfError(Path(pdf), "SECRET_MARK"),
            ):
                rc, out = self.run_cli_inproc(
                    "item", "attach-pdf", "--vault", str(vault), "--key",
                    "smithExample2026", "--file", str(pdf),
                )
            self.assertEqual(rc, 2)
            self.assertNotIn("SECRET_MARK", out)
            json.loads(out)

    def test_cli_envelope_hides_operation_conflict_text(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", doi=DOI)
            pdf = make_pdf(vault / "source.pdf", texts=[f"DOI {DOI}"])
            with mock.patch(
                "paper_notes.fsops.commit",
                side_effect=fsops.OperationConflict("SECRET_MARK"),
            ):
                rc, out = self.run_cli_inproc(
                    "item", "attach-pdf", "--vault", str(vault), "--key",
                    "smithExample2026", "--file", str(pdf),
                )
            self.assertEqual(rc, 3)
            self.assertNotIn("SECRET_MARK", out)
            json.loads(out)

    def test_cli_supplement_operation_conflict_rc3_hides_secret(self):
        # supplement attach: begin_operation raises OperationConflict with a
        # SECRET_MARK payload -> rc 3, one parseable envelope, no marker
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            blob = vault / "blob.txt"
            blob.write_bytes(b"data")
            with mock.patch(
                "paper_notes.attachments.fsops.begin_operation",
                side_effect=fsops.OperationConflict("SECRET_MARK"),
            ):
                rc, out = self.run_cli_inproc(
                    "item", "attach-pdf", "--vault", str(vault), "--key",
                    "smithExample2026", "--file", str(blob), "--supplementary",
                )
            self.assertEqual(rc, 3)
            self.assertNotIn("SECRET_MARK", out)
            json.loads(out)  # still one parseable envelope
            self.assertEqual(len(out.strip().splitlines()), 1)

    def test_cli_item_update_operation_conflict_rc3_hides_secret(self):
        # item update: begin_operation raises OperationConflict with a
        # SECRET_MARK payload -> rc 3, one parseable envelope, no marker,
        # note untouched
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026")
            note = note_path(vault, "smithExample2026")
            before = note.read_bytes()
            patch_file = vault / "patch.json"
            patch_file.write_text(
                json.dumps({"reading_status": "read"}), encoding="utf-8"
            )
            with mock.patch(
                "paper_notes.fsops.begin_operation",
                side_effect=fsops.OperationConflict("SECRET_MARK"),
            ):
                rc, out = self.run_cli_inproc(
                    "item", "update", "--vault", str(vault), "--key",
                    "smithExample2026", "--patch", str(patch_file),
                )
            self.assertEqual(rc, 3)
            self.assertNotIn("SECRET_MARK", out)
            json.loads(out)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertEqual(note.read_bytes(), before)


class ReconcileCliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
        )

    def test_cli_reconcile_preview_needs_confirmation_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            write_paper(vault, "smithExample2026", pdf_status="missing")
            primary_target(vault, "smithExample2026").write_bytes(pdf.read_bytes())
            result = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "reconcile_pdf_status")
            self.assertTrue(payload["data"]["confirmation_token"])
            self.assertEqual(payload["data"]["before"]["pdf_status"], "missing")
            self.assertEqual(payload["data"]["after"]["pdf_status"], "available")
            self.assertEqual(payload["data"]["after"]["pdf_sha256"], file_sha(pdf))

    def test_cli_reconcile_confirm_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            write_paper(vault, "smithExample2026", pdf_status="missing")
            primary_target(vault, "smithExample2026").write_bytes(pdf.read_bytes())
            preview = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026"
            )
            token = json.loads(preview.stdout)["data"]["confirmation_token"]
            result = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026",
                "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "reconciled")
            paper, _ = load_paper_note(note_path(vault, "smithExample2026"))
            self.assertEqual(paper.pdf_status, "available")

    def test_cli_reconcile_consistent_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_paper(vault, "smithExample2026", pdf_status="missing")
            result = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "consistent")

    def test_cli_reconcile_stale_token_rc3(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            pdf = make_pdf(vault / "source.pdf", texts=["x"])
            write_paper(vault, "smithExample2026", pdf_status="missing")
            target = primary_target(vault, "smithExample2026")
            target.write_bytes(pdf.read_bytes())
            preview = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026"
            )
            token = json.loads(preview.stdout)["data"]["confirmation_token"]
            target.unlink()
            result = self.run_cli(
                "item", "reconcile", "--vault", str(vault), "--key", "smithExample2026",
                "--confirm-token", token,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
