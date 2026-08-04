"""Pandoc alias filter and manuscript validation tests (Task 15).

Frozen discipline: written before ``paper_notes/csl.py`` exists; the first
run must be red. Pandoc integration tests run against the real ``pandoc``
binary when available (it is required on the target Mac) and skip with an
explicit reason otherwise.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper_notes.repository import build_index

REPO = Path(__file__).resolve().parents[1]
FILTER = REPO / "paper_notes" / "assets" / "resolve_citation_aliases.lua"

PANDOC = shutil.which("pandoc")

# Keep TemporaryDirectory objects alive for the lifetime of the module;
# make_vault() returns only the Path, and garbage collection would
# otherwise delete the vault mid-test (fixture lifecycle fix, assertions
# unchanged).
_KEEPALIVE: list[tempfile.TemporaryDirectory] = []

FM_TEMPLATE = """schema_version: 1
paper_id: {paper_id}
citation_key: {citation_key}
item_type: article-journal
title: {title}
authors:
- family: Example
  given: A
publication_date: 2026
pdf_status: missing
reading_status: unread
"""


def write_paper(
    root: Path,
    key: str,
    paper_id: str = "550e8400-e29b-41d4-a716-446655440000",
    aliases: tuple[str, ...] = (),
) -> Path:
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    fm = FM_TEMPLATE.format(paper_id=paper_id, citation_key=key, title=key)
    if aliases:
        fm += "citation_key_aliases:\n" + "".join(f"  - {a}\n" for a in aliases)
    (d / f"{key}.md").write_text(f"---\n{fm}---\n# body\n", encoding="utf-8")
    return d


def make_vault() -> Path:
    td = tempfile.TemporaryDirectory()
    _KEEPALIVE.append(td)
    root = Path(td.name)
    write_paper(
        root,
        "smith2026",
        paper_id="550e8400-e29b-41d4-a716-446655440000",
        aliases=("smithOld2020",),
    )
    return root


def run_pandoc(manuscript: str, env: dict[str, str] | None = None) -> dict:
    """Run pandoc over ``manuscript`` with the bundled filter; return AST."""
    assert PANDOC, "pandoc is required for integration tests"
    merged = dict(env or {})
    result = subprocess.run(
        [PANDOC, "-f", "markdown", "-t", "json", "--lua-filter", str(FILTER)],
        input=manuscript,
        capture_output=True,
        text=True,
        env=merged,
    )
    if result.returncode != 0:
        raise AssertionError(f"pandoc failed: {result.stderr}")
    return json.loads(result.stdout)


def collect_cite_ids(ast: dict) -> list[str]:
    ids: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("t") == "Cite":
                for citation in node.get("c", [[]])[0]:
                    ids.append(citation["citationId"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ast)
    return ids


@unittest.skipUnless(PANDOC, "pandoc is not installed; alias filter tests skipped")
class AliasFilterTest(unittest.TestCase):
    def test_filter_rewrites_alias_to_current_key(self):
        root = make_vault()
        aliases_file = root / ".paper-notes" / "citation-aliases.json"
        aliases_file.parent.mkdir(parents=True)
        aliases_file.write_text('{"smithOld2020": "smith2026"}', encoding="utf-8")
        ast = run_pandoc(
            "Text [@smithOld2020] here.\n",
            env={"PAPER_NOTES_ALIASES": str(aliases_file)},
        )
        self.assertEqual(collect_cite_ids(ast), ["smith2026"])

    def test_filter_old_and_new_together_produce_one_identity(self):
        root = make_vault()
        aliases_file = root / ".paper-notes" / "citation-aliases.json"
        aliases_file.parent.mkdir(parents=True)
        aliases_file.write_text('{"smithOld2020": "smith2026"}', encoding="utf-8")
        ast = run_pandoc(
            "Text [@smithOld2020; @smith2026] here.\n",
            env={"PAPER_NOTES_ALIASES": str(aliases_file)},
        )
        # Old and new keys resolve to a single current identity.
        self.assertEqual(collect_cite_ids(ast), ["smith2026"])

    def test_filter_leaves_current_and_unknown_keys(self):
        root = make_vault()
        aliases_file = root / ".paper-notes" / "citation-aliases.json"
        aliases_file.parent.mkdir(parents=True)
        aliases_file.write_text('{"smithOld2020": "smith2026"}', encoding="utf-8")
        ast = run_pandoc(
            "Text [@smith2026] and [@unknownKey] here.\n",
            env={"PAPER_NOTES_ALIASES": str(aliases_file)},
        )
        self.assertEqual(collect_cite_ids(ast), ["smith2026", "unknownKey"])

    def test_filter_is_noop_without_aliases_file(self):
        root = make_vault()
        ast = run_pandoc("Text [@smithOld2020] here.\n")
        self.assertEqual(collect_cite_ids(ast), ["smithOld2020"])

    def test_filter_ignores_code_blocks_and_code_spans(self):
        root = make_vault()
        aliases_file = root / ".paper-notes" / "citation-aliases.json"
        aliases_file.parent.mkdir(parents=True)
        aliases_file.write_text('{"smithOld2020": "smith2026"}', encoding="utf-8")
        manuscript = (
            "Real [@smithOld2020] here.\n\n"
            "```python\n"
            "x = \"[@fake]\"\n"
            "```\n\n"
            "Inline `[@alsonotarealcite]` stays code.\n"
        )
        ast = run_pandoc(
            manuscript, env={"PAPER_NOTES_ALIASES": str(aliases_file)}
        )
        # Only the real citation survives; code examples produce no Cite.
        self.assertEqual(collect_cite_ids(ast), ["smith2026"])


@unittest.skipUnless(PANDOC, "pandoc is not installed; validation tests skipped")
class ManuscriptValidationTest(unittest.TestCase):
    def test_known_and_alias_keys_validate(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text(
            "Intro [@smith2026] and [@smithOld2020].\n", encoding="utf-8"
        )
        report = validate_manuscript(root, manuscript)
        self.assertEqual(report.unknown, [])
        self.assertEqual(report.citations, ["smith2026", "smith2026"])

    def test_unknown_key_returns_source_location(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text(
            "Line one.\n\nLine three cites [@missingKey] here.\n",
            encoding="utf-8",
        )
        report = validate_manuscript(root, manuscript)
        self.assertEqual(len(report.unknown), 1)
        entry = report.unknown[0]
        self.assertEqual(entry.key, "missingKey")
        self.assertEqual(entry.line, 3)
        self.assertGreater(entry.column, 0)

    def test_code_examples_are_ignored_through_ast(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text(
            "Real [@smith2026].\n\n"
            "```python\n"
            "x = \"[@fake]\"\n"
            "```\n\n"
            "Inline `[@alsofake]` end.\n",
            encoding="utf-8",
        )
        report = validate_manuscript(root, manuscript)
        self.assertEqual(report.unknown, [])
        self.assertEqual(report.citations, ["smith2026"])

    def test_unknown_key_location_skips_inline_code_span(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text(
            "Inline `[@missingKey]` first.\n\n"
            "Real cite [@missingKey] on line three.\n",
            encoding="utf-8",
        )
        report = validate_manuscript(root, manuscript)
        self.assertEqual(len(report.unknown), 1)
        entry = report.unknown[0]
        self.assertEqual(entry.key, "missingKey")
        # The first AST citation is on line 3; the inline-code occurrence
        # on line 1 must not be reported as its location.
        self.assertEqual(entry.line, 3)

    def test_unknown_key_location_skips_code_block(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text(
            "```python\n"
            "x = \"[@missingKey]\"\n"
            "```\n\n"
            "Real cite [@missingKey] on line five.\n",
            encoding="utf-8",
        )
        report = validate_manuscript(root, manuscript)
        self.assertEqual(len(report.unknown), 1)
        self.assertEqual(report.unknown[0].line, 5)

    def test_validate_is_read_only(self):
        from paper_notes.csl import validate_manuscript

        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text("Intro [@smith2026].\n", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        validate_manuscript(root, manuscript)
        after = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unknown_key_blocks_export_through_cli(self):
        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text("Cites [@ghostKey] here.\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "paper_notes",
                "--json",
                "index",
                "validate-manuscript",
                "--vault",
                str(root),
                "--input",
                str(manuscript),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "unknown_citation_key")
        self.assertIn("ghostKey", payload["errors"][0]["message"])
        self.assertIn(str(manuscript), payload["errors"][0]["message"])

    def test_valid_manuscript_passes_cli(self):
        root = make_vault()
        manuscript = root / "manuscript.md"
        manuscript.write_text("Cites [@smith2026] and [@smithOld2020].\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "paper_notes",
                "--json",
                "index",
                "validate-manuscript",
                "--vault",
                str(root),
                "--input",
                str(manuscript),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"]["citations"], ["smith2026", "smith2026"])


if __name__ == "__main__":
    unittest.main()
