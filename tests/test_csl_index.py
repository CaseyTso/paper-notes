"""CSL-JSON index tests (Task 15).

Frozen discipline: written before ``paper_notes/csl.py`` exists; the first
run must be red, then the implementation turns it green. Do not weaken or
delete assertions.
"""

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from paper_notes.models import Paper
from paper_notes.repository import RepositoryIndex, build_index

REPO = Path(__file__).resolve().parents[1]

FM_TEMPLATE = """schema_version: 1
paper_id: {paper_id}
citation_key: {citation_key}
item_type: {item_type}
title: {title}
authors:
{author_block}publication_date: {publication_date}
pdf_status: missing
reading_status: unread
"""


def write_paper(
    root: Path,
    key: str,
    paper_id: str = "550e8400-e29b-41d4-a716-446655440000",
    item_type: str = "article-journal",
    title: str = "An example paper",
    authors: str = "- family: Example\n  given: A\n",
    publication_date: str = "2026",
    aliases: tuple[str, ...] = (),
) -> Path:
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    fm = FM_TEMPLATE.format(
        paper_id=paper_id,
        citation_key=key,
        item_type=item_type,
        title=title,
        author_block=authors,
        publication_date=publication_date,
    )
    if aliases:
        fm += "citation_key_aliases:\n" + "".join(f"  - {a}\n" for a in aliases)
    (d / f"{key}.md").write_text(f"---\n{fm}---\n# body\n", encoding="utf-8")
    return d


def make_index() -> RepositoryIndex:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    write_paper(
        root,
        "smith2026",
        paper_id="550e8400-e29b-41d4-a716-446655440000",
        title="Alpha paper",
        publication_date="2026",
    )
    write_paper(
        root,
        "jones2025",
        paper_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        title="Beta paper",
        publication_date="2025-06",
        aliases=("jonesOld2024",),
    )
    return build_index(root)


class PaperToCslTest(unittest.TestCase):
    def test_journal_article_field_mapping(self):
        paper = Paper(
            paper_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            citation_key="smith2026",
            item_type="article-journal",
            title="An example paper",
            authors=[{"family": "Smith", "given": "J."}],
            publication_date="2026-05-01",
        )
        from paper_notes.csl import paper_to_csl

        csl = paper_to_csl(paper)
        self.assertEqual(csl["id"], "smith2026")
        self.assertEqual(csl["type"], "article-journal")
        self.assertEqual(csl["title"], "An example paper")
        self.assertEqual(csl["author"], [{"family": "Smith", "given": "J."}])
        self.assertEqual(csl["issued"], {"date-parts": [[2026, 5, 1]]})

    def test_preprint_type_mapping(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="pre2026",
            item_type="preprint",
            title="Preprint title",
        )
        from paper_notes.csl import paper_to_csl

        self.assertEqual(paper_to_csl(paper)["type"], "preprint")

    def test_literal_group_author(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="consortium2026",
            title="Consortium paper",
            authors=[{"literal": "The Consortium"}],
        )
        from paper_notes.csl import paper_to_csl

        self.assertEqual(paper_to_csl(paper)["author"], [{"literal": "The Consortium"}])

    def test_mixed_structured_and_literal_authors(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="mixed2026",
            title="Mixed authors",
            authors=[
                {"family": "Smith", "given": "J."},
                {"literal": "Study Group"},
            ],
        )
        from paper_notes.csl import paper_to_csl

        self.assertEqual(
            paper_to_csl(paper)["author"],
            [{"family": "Smith", "given": "J."}, {"literal": "Study Group"}],
        )

    def test_partial_dates_year_only(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="yearOnly",
            title="Year only",
            publication_date="2026",
        )
        from paper_notes.csl import paper_to_csl

        self.assertEqual(paper_to_csl(paper)["issued"], {"date-parts": [[2026]]})

    def test_partial_dates_year_month(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="yearMonth",
            title="Year month",
            publication_date="2025-06",
        )
        from paper_notes.csl import paper_to_csl

        self.assertEqual(paper_to_csl(paper)["issued"], {"date-parts": [[2025, 6]]})

    def test_missing_date_omits_issued(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="noDate",
            title="No date",
        )
        from paper_notes.csl import paper_to_csl

        self.assertNotIn("issued", paper_to_csl(paper))

    def test_missing_authors_omits_author(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="noAuthor",
            title="No author",
        )
        from paper_notes.csl import paper_to_csl

        self.assertNotIn("author", paper_to_csl(paper))

    def test_does_not_leak_internal_fields(self):
        paper = Paper(
            paper_id="550e8400-e29b-41d4-a716-446655440000",
            citation_key="clean2026",
            title="Clean",
            authors=[{"family": "Smith", "given": "J."}],
            publication_date="2026",
            metadata_sources=["crossref"],
            field_provenance={"title": "crossref"},
        )
        from paper_notes.csl import paper_to_csl

        csl = paper_to_csl(paper)
        self.assertNotIn("paper_id", csl)
        self.assertNotIn("metadata_sources", csl)
        self.assertNotIn("field_provenance", csl)


class RenderIndexTest(unittest.TestCase):
    def test_deterministic_byte_output(self):
        from paper_notes.csl import render_alias_map, render_library

        first = render_library(make_index())
        second = render_library(make_index())
        self.assertEqual(first, second)
        self.assertEqual(render_alias_map(make_index()), render_alias_map(make_index()))

    def test_library_sorted_by_citation_key(self):
        from paper_notes.csl import render_library

        index = make_index()
        data = json.loads(render_library(index))
        keys = [entry["id"] for entry in data]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(keys, ["jones2025", "smith2026"])

    def test_current_keys_only_in_library(self):
        from paper_notes.csl import render_library

        index = make_index()
        data = json.loads(render_library(index))
        self.assertEqual({entry["id"] for entry in data}, set(index.by_key))
        # Aliases never appear as CSL ids.
        self.assertNotIn("jonesOld2024", {entry["id"] for entry in data})

    def test_aliases_in_alias_map(self):
        from paper_notes.csl import render_alias_map

        index = make_index()
        data = json.loads(render_alias_map(index))
        self.assertEqual(data, {"jonesOld2024": "jones2025"})


class RebuildIndexTest(unittest.TestCase):
    def test_rebuild_writes_both_files(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smith2026", title="Alpha")
            result = rebuild_indexes(root)
            library = root / ".paper-notes" / "library.json"
            aliases = root / ".paper-notes" / "citation-aliases.json"
            self.assertTrue(library.is_file())
            self.assertTrue(aliases.is_file())
            self.assertEqual(result.library_path, library)
            self.assertEqual(result.aliases_path, aliases)
            data = json.loads(library.read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in data], ["smith2026"])
            self.assertEqual(json.loads(aliases.read_text(encoding="utf-8")), {})

    def test_rebuild_is_deterministic(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smith2026", title="Alpha")
            write_paper(
                root,
                "jones2025",
                paper_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                aliases=("jonesOld",),
            )
            rebuild_indexes(root)
            first = (root / ".paper-notes" / "library.json").read_bytes()
            rebuild_indexes(root)
            second = (root / ".paper-notes" / "library.json").read_bytes()
            self.assertEqual(first, second)

    def test_rebuild_replaces_existing_files(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            notes = root / ".paper-notes"
            notes.mkdir(parents=True)
            (notes / "library.json").write_text("stale", encoding="utf-8")
            (notes / "citation-aliases.json").write_text("stale", encoding="utf-8")
            write_paper(root, "smith2026", title="Alpha")
            rebuild_indexes(root)
            data = json.loads((notes / "library.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in data], ["smith2026"])

    def test_rebuild_preserves_existing_mode(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            notes = root / ".paper-notes"
            notes.mkdir(parents=True)
            target = notes / "library.json"
            target.write_text("stale", encoding="utf-8")
            target.chmod(0o600)
            write_paper(root, "smith2026", title="Alpha")
            rebuild_indexes(root)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_rebuild_leaves_no_temp_files(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smith2026", title="Alpha")
            rebuild_indexes(root)
            leftover = [
                p.name
                for p in (root / ".paper-notes").iterdir()
                if p.suffix == ".tmp"
            ]
            self.assertEqual(leftover, [])

    def test_rebuild_excludes_invalid_entries(self):
        from paper_notes.csl import rebuild_indexes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smith2026", title="Alpha")
            bad = root / "05 Literature" / "broken2026"
            bad.mkdir(parents=True)
            (bad / "broken2026.md").write_text(
                "---\nschema_version: 1\ncitation_key: broken2026\n"
                "title: Broken\n---\n",
                encoding="utf-8",
            )
            result = rebuild_indexes(root)
            self.assertGreaterEqual(result.invalid_count, 1)
            data = json.loads(
                (root / ".paper-notes" / "library.json").read_text(encoding="utf-8")
            )
            self.assertEqual([entry["id"] for entry in data], ["smith2026"])


class CliIndexTest(unittest.TestCase):
    def _run_cli(self, *argv: str):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *argv],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_cli_index_rebuild_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root, "smith2026", title="Alpha")
            result = self._run_cli("index", "rebuild", "--vault", str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["papers"], 1)
            self.assertEqual(payload["data"]["aliases_count"], 0)
            self.assertTrue(payload["data"]["aliases"].endswith("citation-aliases.json"))
            self.assertTrue(
                Path(payload["data"]["library"]).is_file()
            )


if __name__ == "__main__":
    unittest.main()
