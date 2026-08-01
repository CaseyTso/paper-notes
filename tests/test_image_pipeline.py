#!/usr/bin/env python3
"""Unit tests for clean_md.py image migration pipeline (--attachments-dir).

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CLEAN = Path(__file__).resolve().parents[1] / "scripts/clean_md.py"
NAME = "65fcdcb78128b8d0dc346e9815d2b156ed0604c1dcab0ce40704a33f7196fafa.jpg"
NAME2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"


def run_clean(md, attachments_dir):
    return subprocess.run(
        ["python3", str(CLEAN), str(md), "--in-place", "--attachments-dir", str(attachments_dir)],
        capture_output=True,
        text=True,
    )


class ImageMigrationTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.attachments = self.root / "01 attachments"
        self.attachments.mkdir()
        self.images = self.root / "images"
        self.images.mkdir()
        self.payload = b"payload-" + NAME.encode()
        (self.images / NAME).write_bytes(self.payload)

    def tearDown(self):
        self._td.cleanup()

    def test_relative_image_migrated_in_place_to_embed(self):
        md = self.root / "full.md"
        md.write_text(f"Before\n\n![](images/{NAME})\n\nAfter\n", encoding="utf-8")
        r = run_clean(md, self.attachments)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = md.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"![[{NAME}]]"), 1)
        self.assertNotIn("images/", text)
        self.assertNotIn("base64", text)
        self.assertEqual((self.attachments / NAME).read_bytes(), self.payload)

    def test_second_run_is_idempotent(self):
        md = self.root / "full.md"
        md.write_text(f"Before\n\n![](images/{NAME})\n\nAfter\n", encoding="utf-8")
        first = run_clean(md, self.attachments)
        self.assertEqual(first.returncode, 0, first.stderr)
        text_after_first = md.read_text(encoding="utf-8")
        self.assertEqual(text_after_first.count(f"![[{NAME}]]"), 1)

        second = run_clean(md, self.attachments)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(md.read_text(encoding="utf-8"), text_after_first)
        self.assertEqual(md.read_text(encoding="utf-8").count(f"![[{NAME}]]"), 1)

    def test_missing_source_image_fails_and_leaves_md_untouched(self):
        md = self.root / "missing.md"
        original = "Before\n\n![](images/nothere.jpg)\n\nAfter\n"
        md.write_text(original, encoding="utf-8")
        r = run_clean(md, self.attachments)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(md.read_text(encoding="utf-8"), original)

    def test_same_name_same_content_is_reused(self):
        md = self.root / "reuse.md"
        md.write_text(f"Before\n\n![](images/{NAME})\n\nAfter\n", encoding="utf-8")
        (self.attachments / NAME).write_bytes(self.payload)
        r = run_clean(md, self.attachments)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(md.read_text(encoding="utf-8").count(f"![[{NAME}]]"), 1)
        self.assertEqual((self.attachments / NAME).read_bytes(), self.payload)

    def test_same_name_different_content_fails_and_leaves_md_untouched(self):
        md = self.root / "conflict.md"
        original = f"Before\n\n![](images/{NAME})\n\nAfter\n"
        md.write_text(original, encoding="utf-8")
        (self.attachments / NAME).write_bytes(b"DIFFERENT-CONTENT")
        r = run_clean(md, self.attachments)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(md.read_text(encoding="utf-8"), original)
        self.assertEqual((self.attachments / NAME).read_bytes(), b"DIFFERENT-CONTENT")

    def test_multiple_images_preserve_document_order(self):
        (self.images / NAME2).write_bytes(b"payload-two")
        md = self.root / "multi.md"
        md.write_text(
            f"A\n\n![](images/{NAME})\n\nB\n\n![](images/{NAME2})\n\nC\n",
            encoding="utf-8",
        )
        r = run_clean(md, self.attachments)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = md.read_text(encoding="utf-8")
        self.assertLess(text.index(f"![[{NAME}]]"), text.index(f"![[{NAME2}]]"))
        self.assertEqual((self.attachments / NAME).read_bytes(), self.payload)
        self.assertEqual((self.attachments / NAME2).read_bytes(), b"payload-two")


if __name__ == "__main__":
    unittest.main()
