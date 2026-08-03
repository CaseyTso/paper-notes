"""Read-only repository index tests (Task 5).

Frozen after the first red run; do not weaken or delete assertions.
"""

import tempfile
import unittest
from pathlib import Path

from paper_notes.repository import InvalidRecord, RepositoryIndex, build_index

FM_TEMPLATE = """schema_version: 1
paper_id: {paper_id}
citation_key: {citation_key}
item_type: article-journal
title: {title}
authors:
- family: Example
  given: A
publication_date: 2026
pdf_status: {pdf_status}
reading_status: unread
"""


def write_paper(
    root: Path,
    key: str,
    paper_id: str = "550e8400-e29b-41d4-a716-446655440000",
    citation_key: str | None = None,
    title: str = "An example paper",
    pdf_status: str = "missing",
    aliases: tuple[str, ...] = (),
) -> Path:
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    fm = FM_TEMPLATE.format(
        paper_id=paper_id,
        citation_key=citation_key or key,
        title=title,
        pdf_status=pdf_status,
    )
    if aliases:
        fm += "citation_key_aliases:\n" + "".join(f"  - {a}\n" for a in aliases)
    (d / f"{key}.md").write_text(f"---\n{fm}---\n# body\n", encoding="utf-8")
    return d


def make_vault() -> Path:
    td = tempfile.TemporaryDirectory()
    return Path(td.name)


class EmptyVaultTest(unittest.TestCase):
    def test_no_literature_root_returns_empty_index(self):
        with tempfile.TemporaryDirectory() as td:
            idx = build_index(Path(td))
            self.assertEqual(idx.by_key, {})
            self.assertEqual(idx.aliases, {})
            self.assertEqual(idx.by_id, {})
            self.assertEqual(idx.invalid, [])

    def test_empty_literature_root_returns_empty_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "05 Literature").mkdir()
            idx = build_index(root)
            self.assertEqual(idx.by_key, {})
            self.assertEqual(idx.invalid, [])


class ValidVaultTest(unittest.TestCase):
    def test_valid_items_indexed_with_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                aliases=("smithExample2026old",),
            )
            write_paper(
                root,
                "liuDeepLearning2026",
                paper_id="123e4567-e89b-12d3-a456-426614174000",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 2)
            self.assertEqual(len(idx.by_id), 2)
            self.assertEqual(idx.aliases, {"smithExample2026old": "smithExample2026"})
            self.assertEqual(idx.invalid, [])
            rec = idx.by_key["smithExample2026"]
            self.assertEqual(rec.path.name, "smithExample2026.md")
            self.assertEqual(rec.paper.title, "An example paper")


class InvalidLayoutTest(unittest.TestCase):
    def test_missing_main_note_discoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "05 Literature" / "ghost2026"
            d.mkdir(parents=True)
            idx = build_index(root)
            self.assertEqual(idx.by_key, {})
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "missing_main_note")

    def test_key_mismatch_discoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smithExample2026", citation_key="otherExample2026")
            idx = build_index(root)
            self.assertEqual(idx.by_key, {})
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "key_mismatch")

    def test_duplicate_current_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
            )
            # second file in the same directory declaring the same key
            (d / "smithExample2026_copy.md").write_text(
                "---\nschema_version: 1\n"
                "paper_id: 123e4567-e89b-12d3-a456-426614174000\n"
                "citation_key: smithExample2026\n"
                "item_type: article-journal\ntitle: Copy\n"
                "authors:\n- family: Example\n  given: A\n"
                "publication_date: 2026\n"
                "pdf_status: missing\nreading_status: unread\n"
                "---\n# body\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(
                [r.code for r in idx.invalid], ["duplicate_key"]
            )

    def test_duplicate_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
            )
            write_paper(
                root,
                "liuDeepLearning2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_id), 1)
            self.assertEqual(
                [r.code for r in idx.invalid], ["duplicate_uuid"]
            )

    def test_alias_collision_with_other_current_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                aliases=("liuDeepLearning2026",),
            )
            write_paper(
                root,
                "liuDeepLearning2026",
                paper_id="123e4567-e89b-12d3-a456-426614174000",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 2)
            self.assertEqual(
                [r.code for r in idx.invalid], ["alias_collision"]
            )
            self.assertEqual(idx.aliases, {})

    def test_alias_collision_between_items(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                aliases=("sharedAlias2026",),
            )
            write_paper(
                root,
                "liuDeepLearning2026",
                paper_id="123e4567-e89b-12d3-a456-426614174000",
                aliases=("sharedAlias2026",),
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 2)
            # order-independent: an alias declared by two items is a
            # collision for both declarers (no winner)
            self.assertEqual(
                [r.code for r in idx.invalid], ["alias_collision", "alias_collision"]
            )
            self.assertEqual(idx.aliases, {})

    def test_alias_conflict_with_later_current_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "alphaPaper2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                aliases=("zetaPaper2026",),
            )
            write_paper(
                root,
                "zetaPaper2026",
                paper_id="123e4567-e89b-12d3-a456-426614174000",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 2)
            self.assertEqual(
                [r.code for r in idx.invalid], ["alias_collision"]
            )
            self.assertNotIn("zetaPaper2026", idx.aliases)

    def test_schema_failure_discoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "05 Literature" / "broken2026"
            d.mkdir(parents=True)
            (d / "broken2026.md").write_text(
                "---\ncitation_key: broken2026\ntitle: no paper_id\n---\n# body\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertEqual(idx.by_key, {})
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "schema_failure")

    def test_schema_failure_message_has_field_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "05 Literature" / "broken2026"
            d.mkdir(parents=True)
            (d / "broken2026.md").write_text(
                "---\ncitation_key: broken2026\ntitle: no paper_id\n---\n# body\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertIn("paper_id", idx.invalid[0].message)

    def test_backup_note_not_indexed_when_canonical_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "05 Literature" / "ghost2026"
            d.mkdir(parents=True)
            (d / "backup.md").write_text(
                "---\nschema_version: 1\n"
                "paper_id: 550e8400-e29b-41d4-a716-446655440000\n"
                "citation_key: ghost2026\n"
                "item_type: article-journal\ntitle: Backup\n"
                "authors:\n- family: Example\n  given: A\n"
                "publication_date: 2026\n"
                "pdf_status: missing\nreading_status: unread\n"
                "---\n# body\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertEqual(idx.by_key, {})
            self.assertEqual(
                [r.code for r in idx.invalid], ["missing_main_note"]
            )

    def test_canonical_note_preferred_over_earlier_extra(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
            )
            # alphabetically earlier extra file declaring the same key
            (d / "aaa.md").write_text(
                "---\nschema_version: 1\n"
                "paper_id: 123e4567-e89b-12d3-a456-426614174000\n"
                "citation_key: smithExample2026\n"
                "item_type: article-journal\ntitle: Extra\n"
                "authors:\n- family: Example\n  given: A\n"
                "publication_date: 2026\n"
                "pdf_status: missing\nreading_status: unread\n"
                "---\n# body\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(
                idx.by_key["smithExample2026"].path.name, "smithExample2026.md"
            )
            self.assertEqual(
                [r.code for r in idx.invalid], ["duplicate_key"]
            )

    def test_derived_notes_not_parsed_as_main(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
            )
            # spec-named derived notes with broken/incomplete frontmatter
            (d / "minerUmd_smithExample2026.md").write_text(
                "---\ntitle: incomplete derived note\n---\n# mineru\n",
                encoding="utf-8",
            )
            (d / "Figure解读_smithExample2026.md").write_text(
                "---\ntitle: incomplete figure note\n---\n# figure\n",
                encoding="utf-8",
            )
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(idx.invalid, [])

    def test_frontmatter_error_discoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "05 Literature" / "open2026"
            d.mkdir(parents=True)
            (d / "open2026.md").write_text(
                "---\npaper_id: not closed\n", encoding="utf-8"
            )
            idx = build_index(root)
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "schema_failure")


class PdfStatusTest(unittest.TestCase):
    def test_available_without_pdf_reported_separately(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                pdf_status="available",
            )
            idx = build_index(root)
            # schema-valid item stays indexed; mismatch reported separately
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "pdf_status_mismatch")

    def test_missing_but_pdf_present_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                pdf_status="missing",
            )
            (d / "smithExample2026.pdf").write_bytes(b"%PDF-1.4 fake")
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(len(idx.invalid), 1)
            self.assertEqual(idx.invalid[0].code, "pdf_status_mismatch")

    def test_available_with_pdf_consistent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = write_paper(
                root,
                "smithExample2026",
                paper_id="550e8400-e29b-41d4-a716-446655440000",
                pdf_status="available",
            )
            (d / "smithExample2026.pdf").write_bytes(b"%PDF-1.4 fake")
            idx = build_index(root)
            self.assertEqual(len(idx.by_key), 1)
            self.assertEqual(idx.invalid, [])


if __name__ == "__main__":
    unittest.main()
