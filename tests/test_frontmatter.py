"""Round-trip frontmatter codec tests (Task 4).

Frozen after the first red run; do not weaken or delete assertions.
"""

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from paper_notes.frontmatter import FrontmatterError, load_paper_note, update_paper_note

FM = """schema_version: 1
paper_id: 550e8400-e29b-41d4-a716-446655440000
citation_key: shiauSpatiallyResolvedAnalysis2024
item_type: article-journal
title: 空间分辨分析肺癌微环境
# keep this comment
authors:
- family: Shiau
  given: Chia-Yu
publication_date: 2024-01-01
pdf_status: available
reading_status: unread
"""

FM_KEYS = [
    "schema_version",
    "paper_id",
    "citation_key",
    "item_type",
    "title",
    "authors",
    "publication_date",
    "pdf_status",
    "reading_status",
]

FM_WITH_YEAR = """schema_version: 1
paper_id: 550e8400-e29b-41d4-a716-446655440000
citation_key: shiauSpatiallyResolvedAnalysis2024
item_type: article-journal
title: 空间分辨分析肺癌微环境
authors:
- family: Shiau
  given: Chia-Yu
publication_date: 2024-01-01
year: 2024
pdf_status: available
reading_status: unread
"""

BODY = "# 正文\n\n这是 **Markdown** 正文。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"


def write_note(directory: Path, body: str = BODY, frontmatter: str = FM, newline: str = "\n") -> Path:
    path = directory / "note.md"
    content = f"---\n{frontmatter}---\n{body}"
    if newline == "\r\n":
        content = content.replace("\n", "\r\n")
    path.write_bytes(content.encode("utf-8"))
    return path


class LoadTest(unittest.TestCase):
    def test_load_returns_paper_and_exact_body(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            paper, doc = load_paper_note(path)
            self.assertEqual(paper.citation_key, "shiauSpatiallyResolvedAnalysis2024")
            self.assertEqual(paper.title, "空间分辨分析肺癌微环境")
            self.assertEqual(paper.reading_status, "unread")
            self.assertEqual(paper.year, 2024)
            self.assertEqual(doc.body, BODY)
            self.assertEqual(doc.newline, "\n")

    def test_load_preserves_key_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            _, doc = load_paper_note(path)
            self.assertEqual(list(doc.frontmatter), FM_KEYS)

    def test_load_crlf_normalized_deterministically(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td), newline="\r\n")
            paper, doc = load_paper_note(path)
            self.assertEqual(paper.reading_status, "unread")
            self.assertEqual(doc.newline, "\r\n")
            self.assertEqual(doc.body, BODY)

    def test_load_year_only_integer_date(self):
        with tempfile.TemporaryDirectory() as td:
            fm = FM.replace("publication_date: 2024-01-01", "publication_date: 2026")
            path = write_note(Path(td), frontmatter=fm)
            paper, _ = load_paper_note(path)
            self.assertEqual(paper.publication_date, "2026")
            self.assertEqual(paper.year, 2026)

    def test_load_empty_body(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td), body="")
            _, doc = load_paper_note(path)
            self.assertEqual(doc.body, "")

    def test_load_missing_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plain.md"
            path.write_text("# no frontmatter here\n", encoding="utf-8")
            with self.assertRaises(FrontmatterError) as ctx:
                load_paper_note(path)
            self.assertEqual(ctx.exception.code, "missing_frontmatter")

    def test_load_unterminated_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "open.md"
            path.write_text("---\ntitle: never closed\n", encoding="utf-8")
            with self.assertRaises(FrontmatterError) as ctx:
                load_paper_note(path)
            self.assertEqual(ctx.exception.code, "unterminated_frontmatter")

    def test_load_invalid_yaml_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.md"
            path.write_text("---\ntitle: [unclosed\n---\nbody\n", encoding="utf-8")
            with self.assertRaises(FrontmatterError) as ctx:
                load_paper_note(path)
            self.assertEqual(ctx.exception.code, "invalid_yaml")


class UpdateTest(unittest.TestCase):
    def test_update_reading_status_keeps_body_exact(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            paper = update_paper_note(path, {"reading_status": "read"})
            self.assertEqual(paper.reading_status, "read")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("---\n" + BODY))
            self.assertIn("reading_status: read", text)
            self.assertNotIn("reading_status: unread", text)

    def test_update_preserves_comments_and_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            update_paper_note(path, {"reading_status": "read"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep this comment", text)
            _, doc = load_paper_note(path)
            self.assertEqual(list(doc.frontmatter), FM_KEYS)

    def test_update_unicode_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            update_paper_note(path, {"title": "新的中文标题"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("title: 新的中文标题", text)
            paper, _ = load_paper_note(path)
            self.assertEqual(paper.title, "新的中文标题")

    def test_update_adds_and_removes_user_field(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            update_paper_note(path, {"custom_note": "hello"})
            self.assertIn("custom_note: hello", path.read_text(encoding="utf-8"))
            update_paper_note(path, {"custom_note": None})
            self.assertNotIn("custom_note", path.read_text(encoding="utf-8"))

    def test_update_derives_year_in_returned_paper_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            paper = update_paper_note(path, {"publication_date": "2025-06-01"})
            self.assertEqual(paper.year, 2025)
            self.assertNotIn("year:", path.read_text(encoding="utf-8"))

    def test_update_preserves_file_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            path.chmod(0o644)
            update_paper_note(path, {"reading_status": "read"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_update_preserves_non_default_file_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            path.chmod(0o600)
            update_paper_note(path, {"reading_status": "read"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_update_crlf_roundtrip_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td), newline="\r\n")
            before = path.read_bytes()
            update_paper_note(path, {"reading_status": "unread"})
            self.assertEqual(path.read_bytes(), before)

    def test_update_forbidden_metric_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            before = path.read_bytes()
            with self.assertRaises(ValidationError):
                update_paper_note(path, {"IF": 10.2})
            self.assertEqual(path.read_bytes(), before)

    def test_update_easyscholar_raw_field_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td))
            before = path.read_bytes()
            with self.assertRaises(ValidationError):
                update_paper_note(path, {"sciif": "12.3"})
            self.assertEqual(path.read_bytes(), before)

    def test_update_year_mismatch_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_note(Path(td), frontmatter=FM_WITH_YEAR)
            before = path.read_bytes()
            with self.assertRaises(ValidationError):
                update_paper_note(path, {"publication_date": "2025-01-01"})
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
