"""MinerU conversion core tests (fake API adapter; no network).

Covers preflight rules, the confirmation-token contract, the network
pipeline with an injectable fake ``MineruApi``, cleaning semantics, the
staged commit, and every conflict/zero-write path. The vault fixtures are
built the same way as the other core tests (minimal canonical items).
"""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import fitz

from paper_notes import config, mineru, paths

MINERU_SECRET = "sk-mineru-" + "test-token-" + "1234567890abcdef"
KEY = "smithExample2026"
PAPER_ID = "550e8400-e29b-41d4-a716-446655440000"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_BYTES_OTHER = b"\x89PNG\r\n\x1a\n" + b"\xff" * 64


def make_pdf(path, *, pages=2):
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"page {index + 1}")
    doc.save(str(path))
    doc.close()
    return path


def write_paper(root, *, with_pdf=True, existing_md=None, attachments=()):
    d = root / "05 Literature" / KEY
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(
        [
            "schema_version: 1",
            f"paper_id: {PAPER_ID}",
            f"citation_key: {KEY}",
            "item_type: article-journal",
            "title: An example paper",
            "authors:",
            "- family: Smith",
            "  given: John",
            "publication_date: 2026-05-01",
            "year: 2026",
            f"pdf_status: {'available' if with_pdf else 'missing'}",
            "reading_status: unread",
        ]
    )
    (d / f"{KEY}.md").write_text(f"---\n{fm}\n---\n# body\n", encoding="utf-8")
    if with_pdf:
        make_pdf(d / f"{KEY}.pdf")
    if existing_md is not None:
        (d / f"minerUmd_{KEY}.md").write_text(existing_md, encoding="utf-8")
    for name, data in attachments:
        att = d / "attachments"
        att.mkdir(parents=True, exist_ok=True)
        (att / name).write_bytes(data)
    return d


def make_result_zip(dest_dir, *, md_text, images):
    """Build a MinerU-style result zip: full.md + images/<name>."""
    zip_path = dest_dir / "mineru_result.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("full.md", md_text)
        for name, data in images.items():
            zf.writestr(f"images/{name}", data)
    return zip_path


class FakeMineruApi:
    """Injectable stand-in for MineruApi; no network."""

    def __init__(self, *, md_text=None, images=None, mutate_on_download=None):
        self.calls = []
        self.md_text = (
            md_text
            if md_text is not None
            else (
                "# MinerU transcript\n\n"
                "## Figure 1\n\n![fig1 caption](images/fig1.png)\n\n"
                "<details>\n<summary>hidden</summary>\nfoo\n</details>\n\n"
                "A\n\nB\n\n\n\nC\n\n(legend continued on next page)\n"
            )
        )
        self.images = images if images is not None else {"fig1.png": PNG_BYTES}
        self.mutate_on_download = mutate_on_download

    def create_batch(self, filename):
        self.calls.append(("create_batch", filename))
        return "batch-1", "https://oss.example/upload"

    def upload(self, pdf_path, upload_url):
        self.calls.append(("upload", str(pdf_path), upload_url))

    def poll(self, batch_id, on_progress=None):
        self.calls.append(("poll", batch_id))
        if on_progress is not None:
            on_progress({"state": "running", "extracted_pages": 1, "total_pages": 2})
        return "https://cdn.example/result.zip"

    def download(self, url, dest_dir):
        self.calls.append(("download", url))
        if self.mutate_on_download is not None:
            self.mutate_on_download()
        return make_result_zip(Path(dest_dir), md_text=self.md_text, images=self.images)


class MineruSetup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = Path(self._tmp.name) / "config.json"
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg)
        self._env = mock.patch.dict(
            os.environ, {"PAPER_NOTES_CONFIG": str(self.cfg)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def convert(self, *, api=None, confirm_token=None, progress=None):
        return mineru.run_convert(
            self.root, KEY, confirm_token=confirm_token, progress=progress, api=api
        )

    def assert_vault_clean(self, root=None):
        """No stray minerU staging or temp artifacts inside the vault."""
        root = root or self.root
        staging = root / ".paper-notes" / ".staging"
        if staging.exists():
            self.assertEqual(list(staging.iterdir()), [])


class FreshConvertTest(MineruSetup):
    def test_fresh_convert_publishes_md_and_images(self):
        write_paper(self.root)
        events = []
        result = self.convert(
            api=FakeMineruApi(), progress=lambda ev: events.append(ev)
        )
        self.assertEqual(result.action, "converted")
        self.assertEqual(result.citation_key, KEY)
        self.assertEqual(result.images, ["fig1.png"])
        md = paths.mineru_markdown(self.root, KEY)
        self.assertTrue(md.exists())
        text = md.read_text(encoding="utf-8")
        self.assertIn("![[fig1.png]]", text)
        self.assertNotIn("<details>", text)
        self.assertNotIn("legend continued", text)
        self.assertTrue((self.root / "05 Literature" / KEY / "attachments" / "fig1.png").exists())
        # figures/ and Figure解读 are never created
        self.assertFalse((self.root / "05 Literature" / KEY / "figures").exists())
        self.assertFalse((self.root / "05 Literature" / KEY / f"Figure解读_{KEY}.md").exists())
        self.assert_vault_clean()

    def test_progress_event_sequence(self):
        write_paper(self.root)
        events = []
        self.convert(api=FakeMineruApi(), progress=lambda ev: events.append(ev))
        stages = [ev["stage"] for ev in events]
        self.assertEqual(stages[0], mineru.STAGE_UPLOADING)
        self.assertIn(mineru.STAGE_PROCESSING, stages)
        self.assertIn(mineru.STAGE_DOWNLOADING, stages)
        self.assertEqual(stages[-1], mineru.STAGE_COMMITTING)
        processing = next(ev for ev in events if ev["stage"] == mineru.STAGE_PROCESSING)
        self.assertEqual(processing["extracted_pages"], 1)
        self.assertEqual(processing["total_pages"], 2)

    def test_no_primary_pdf_is_user_error(self):
        write_paper(self.root, with_pdf=False)
        with self.assertRaises(mineru.MineruError):
            self.convert(api=FakeMineruApi())

    def test_no_key_is_user_error(self):
        write_paper(self.root)
        config.save_config(config.Config(mineru_key=None), path=self.cfg)
        with self.assertRaises(mineru.MineruError):
            self.convert(api=FakeMineruApi())

    def test_cleaning_migrates_all_images_and_dedups_content(self):
        write_paper(
            self.root,
            attachments=[("fig1.png", PNG_BYTES)],  # identical bytes already present
        )
        result = self.convert(api=FakeMineruApi())
        # identical content reused; no new file added
        self.assertEqual(result.images, [])
        text = paths.mineru_markdown(self.root, KEY).read_text(encoding="utf-8")
        self.assertIn("![[fig1.png]]", text)

    def test_same_name_different_content_is_conflict_zero_writes(self):
        write_paper(
            self.root,
            attachments=[("fig1.png", PNG_BYTES_OTHER)],  # same name, different bytes
        )
        md_before = None
        with self.assertRaises(mineru.MineruConflict):
            self.convert(api=FakeMineruApi())
        self.assertFalse(paths.mineru_markdown(self.root, KEY).exists())
        self.assertEqual(
            (self.root / "05 Literature" / KEY / "attachments" / "fig1.png").read_bytes(),
            PNG_BYTES_OTHER,
        )
        self.assert_vault_clean()

    def test_corrupt_zip_is_error_and_vault_untouched(self):
        write_paper(self.root)
        api = FakeMineruApi()

        def bad_download(url, dest_dir):
            path = Path(dest_dir) / "mineru_result.zip"
            path.write_bytes(b"not a zip")
            return path

        api.download = bad_download
        with self.assertRaises(mineru.MineruError):
            self.convert(api=api)
        self.assertFalse(paths.mineru_markdown(self.root, KEY).exists())
        self.assert_vault_clean()

    def test_fresh_with_confirm_token_is_stale_conflict(self):
        write_paper(self.root)
        with self.assertRaises(mineru.MineruConflict):
            self.convert(
                api=FakeMineruApi(), confirm_token="a" * 64
            )


class ReConvertTest(MineruSetup):
    OLD_MD = "# old transcript\n\n![old](images/fig1.png)\n"

    def test_preview_requires_existing_md(self):
        write_paper(self.root)
        with self.assertRaises(mineru.MineruError):
            mineru.preview_convert(self.root, KEY)

    def test_preview_returns_token_and_plan(self):
        write_paper(self.root, existing_md=self.OLD_MD)
        preview = mineru.preview_convert(self.root, KEY)
        self.assertTrue(preview.existing_md)
        self.assertEqual(preview.citation_key, KEY)
        self.assertTrue(preview.confirmation_token)
        self.assertEqual(preview.plan["action"], "mineru_convert")

    def test_existing_md_without_confirm_is_error(self):
        write_paper(self.root, existing_md=self.OLD_MD)
        with self.assertRaises(mineru.MineruError):
            self.convert(api=FakeMineruApi())

    def test_reconvert_with_token_replaces_md_and_keeps_old_attachments(self):
        write_paper(
            self.root,
            existing_md=self.OLD_MD,
            attachments=[("keep.png", PNG_BYTES_OTHER)],
        )
        preview = mineru.preview_convert(self.root, KEY)
        result = self.convert(api=FakeMineruApi(), confirm_token=preview.confirmation_token)
        self.assertEqual(result.action, "reconverted")
        md = paths.mineru_markdown(self.root, KEY).read_text(encoding="utf-8")
        self.assertIn("![[fig1.png]]", md)
        self.assertNotIn("old transcript", md)
        # old attachment images are never deleted
        self.assertTrue((self.root / "05 Literature" / KEY / "attachments" / "keep.png").exists())

    def test_stale_token_when_pdf_changed(self):
        write_paper(self.root, existing_md=self.OLD_MD)
        preview = mineru.preview_convert(self.root, KEY)
        # replace the PDF after the preview
        make_pdf(self.root / "05 Literature" / KEY / f"{KEY}.pdf")
        with self.assertRaises(mineru.MineruConflict):
            self.convert(api=FakeMineruApi(), confirm_token=preview.confirmation_token)
        self.assertEqual(
            paths.mineru_markdown(self.root, KEY).read_text(encoding="utf-8"),
            self.OLD_MD,
        )
        self.assert_vault_clean()

    def test_stale_token_when_md_changed(self):
        write_paper(self.root, existing_md=self.OLD_MD)
        preview = mineru.preview_convert(self.root, KEY)
        paths.mineru_markdown(self.root, KEY).write_text(
            "# edited between preview and run\n", encoding="utf-8"
        )
        with self.assertRaises(mineru.MineruConflict):
            self.convert(api=FakeMineruApi(), confirm_token=preview.confirmation_token)
        self.assert_vault_clean()

    def test_stale_token_when_attachments_changed(self):
        write_paper(self.root, existing_md=self.OLD_MD)
        preview = mineru.preview_convert(self.root, KEY)
        att = self.root / "05 Literature" / KEY / "attachments"
        att.mkdir(parents=True, exist_ok=True)
        (att / "new.png").write_bytes(PNG_BYTES)
        with self.assertRaises(mineru.MineruConflict):
            self.convert(api=FakeMineruApi(), confirm_token=preview.confirmation_token)
        self.assert_vault_clean()


class CommitTimeConflictTest(MineruSetup):
    def test_state_change_during_network_discards_result_zero_writes(self):
        write_paper(self.root, existing_md="# old\n")
        preview = mineru.preview_convert(self.root, KEY)

        def mutate_on_download():
            # an external process replaces the PDF mid-conversion
            make_pdf(self.root / "05 Literature" / KEY / f"{KEY}.pdf")

        api = FakeMineruApi(mutate_on_download=mutate_on_download)
        with self.assertRaises(mineru.MineruConflict):
            self.convert(api=api, confirm_token=preview.confirmation_token)
        # nothing new was written
        self.assertFalse((self.root / "05 Literature" / KEY / "attachments" / "fig1.png").exists())
        self.assertEqual(
            paths.mineru_markdown(self.root, KEY).read_text(encoding="utf-8"),
            "# old\n",
        )
        self.assert_vault_clean()


class CleaningTest(unittest.TestCase):
    def test_clean_markdown_text_rules(self):
        raw = (
            "A\n\n## Figure 1\n\n![c](images/x.png)\n\n"
            "<details>\n<summary>s</summary>\nx\n</details>\n\n"
            "B\n\n\n\nC\n\n(legend continued on next page)\n"
            "D\n\nZ\n"
        )
        cleaned = mineru.clean_markdown_text(raw)
        self.assertNotIn("<details>", cleaned)
        self.assertIn("**Figure 1**", cleaned)
        self.assertNotIn("legend continued", cleaned)
        self.assertNotIn("\n\n\n", cleaned)
        self.assertTrue(cleaned.endswith("\n"))

    def test_migrate_images_rewrites_embeds_and_rejects_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Path(tmp) / "images"
            images.mkdir()
            (images / "a.png").write_bytes(PNG_BYTES)
            text, entries = mineru.migrate_images("![c](images/a.png)", images)
            self.assertIn("![[a.png]]", text)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].name, "a.png")
            with self.assertRaises(mineru.MineruError):
                mineru.migrate_images("![c](images/missing.png)", images)


if __name__ == "__main__":
    unittest.main()
