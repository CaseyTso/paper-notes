"""Derived card note creation tests (card create).

Frozen after the first green run; do not weaken or delete assertions.
Cards are created under ``<paper_dir>/cards/`` with minimal relation
frontmatter, the verbatim selection body, an optional anchor link back
to the source Figure解读 note, and a trailing ``## 扩展``.

No network, no real vault: synthetic main notes in temporary
directories.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper_notes import cards

REPO = Path(__file__).resolve().parents[1]

PAPER_ID = "550e8400-e29b-41d4-a716-446655440000"
KEY = "smithExample2026"

SELECTION = """## Figure 2. Example figure

- **是什么**：示例解读
- **发现**：示例发现
"""


def write_paper(root, key=KEY, paper_id=PAPER_ID):
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
    fm = "\n".join(lines)
    (d / f"{key}.md").write_text(f"---\n{fm}\n---\n# body\n", encoding="utf-8")
    return d


class CardCreateCoreTests(unittest.TestCase):
    def test_create_card_minimal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            result = cards.create_card(
                root,
                key=KEY,
                title="Example conclusive finding",
                selection=SELECTION,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.parent.name, "cards")
            self.assertEqual(result.path.stem, "card_Example_conclusive_finding")
            self.assertEqual(result.citation_key, KEY)
            self.assertEqual(result.paper_id, PAPER_ID)
            text = result.path.read_text(encoding="utf-8")
            self.assertIn(f"paper_id: {PAPER_ID}", text)
            self.assertIn(f"citation_key: {KEY}", text)
            self.assertIn(f'paper: "[[{KEY}]]"', text)
            self.assertIn("## Figure 2. Example figure", text)
            self.assertIn("## 扩展", text)
            self.assertNotIn("参见", text)

    def test_create_card_explicit_filename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            result = cards.create_card(
                root,
                key=KEY,
                title="Title",
                selection=SELECTION,
                filename="card_Figure2_MyCard.md",
            )
            self.assertEqual(result.path.name, "card_Figure2_MyCard.md")

    def test_conflict_on_existing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            cards.create_card(root, key=KEY, title="Dup", selection=SELECTION)
            with self.assertRaises(cards.CardConflict):
                cards.create_card(root, key=KEY, title="Dup", selection=SELECTION)

    def test_unknown_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            with self.assertRaises(cards.CardError):
                cards.create_card(root, key="missing2026", title="T", selection=SELECTION)

    def test_empty_title_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            with self.assertRaises(cards.CardError):
                cards.create_card(root, key=KEY, title="  ", selection=SELECTION)

    def test_anchor_inserted_and_linked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / "Figure解读_smithExample2026.md"
            fig.write_text(
                "# fig\n\n" + SELECTION + "\n\n## Next\n",
                encoding="utf-8",
            )
            result = cards.create_card(
                root,
                key=KEY,
                title="Card with anchor",
                selection=SELECTION,
                anchor_name="fig2",
                source_note="Figure解读_smithExample2026",
            )
            self.assertTrue(result.anchor_inserted)
            self.assertIn("^fig2", fig.read_text(encoding="utf-8"))
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn("[[Figure解读_smithExample2026#^fig2|Figure解读_smithExample2026]]", card_text)

    def test_anchor_already_present_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / "Figure解读_smithExample2026.md"
            fig.write_text("# fig\n\n" + SELECTION + "\n^fig2\n\n## Next\n", encoding="utf-8")
            result = cards.create_card(
                root,
                key=KEY,
                title="Card with anchor",
                selection=SELECTION,
                anchor_name="fig2",
                source_note="Figure解读_smithExample2026",
            )
            self.assertFalse(result.anchor_inserted)
            self.assertTrue(any("already present" in w for w in result.warnings))

    def test_slug_sanitizes_unsafe_chars(self):
        self.assertEqual(cards.slugify_card_filename('a/b:c*?"<>|'), "a_b_c")
        self.assertEqual(cards.slugify_card_filename("..."), "card")

    def test_backlink_inserted_after_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / "Figure解读_smithExample2026.md"
            fig.write_text(
                "# fig\n\n" + SELECTION + "\n\n## Next\n",
                encoding="utf-8",
            )
            result = cards.create_card(
                root,
                key=KEY,
                title="Card with backlink",
                selection=SELECTION,
                anchor_name="fig2",
                source_note="Figure解读_smithExample2026",
                backlink=True,
            )
            self.assertTrue(result.anchor_inserted)
            self.assertTrue(result.backlink_inserted)
            fig_text = fig.read_text(encoding="utf-8")
            self.assertIn("^fig2", fig_text)
            self.assertIn("> 卡片：[[card_Card_with_backlink]]", fig_text)

    def test_backlink_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / "Figure解读_smithExample2026.md"
            fig.write_text(
                "# fig\n\n" + SELECTION + "\n^fig2\n> 卡片：[[card_X]]\n\n## Next\n",
                encoding="utf-8",
            )
            result = cards.create_card(
                root,
                key=KEY,
                title="X",
                selection=SELECTION,
                anchor_name="fig2",
                source_note="Figure解读_smithExample2026",
                backlink=True,
            )
            self.assertFalse(result.backlink_inserted)
            self.assertTrue(any("already present" in w for w in result.warnings))

    def test_backlink_requires_anchor(self):
        # backlink without anchor params is a silent no-op (no source edit)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            result = cards.create_card(
                root,
                key=KEY,
                title="NoAnchor",
                selection=SELECTION,
                backlink=True,
            )
            self.assertFalse(result.backlink_inserted)
            self.assertEqual(result.anchor_name, None)


class CardCreateCliTests(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes.cli", *argv],
            capture_output=True,
            text=True,
            cwd=REPO,
        )

    def test_cli_json_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            sel = root / "selection.md"
            sel.write_text(SELECTION, encoding="utf-8")
            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "Conclusive",
                "--selection-file",
                str(sel),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertTrue(env["data"]["path"].endswith("card_Conclusive.md"))
            self.assertTrue(Path(env["data"]["path"]).exists())

    def test_cli_backlink_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / "Figure解读_smithExample2026.md"
            fig.write_text("# fig\n\n" + SELECTION + "\n\n## Next\n", encoding="utf-8")
            sel = root / "selection.md"
            sel.write_text(SELECTION, encoding="utf-8")
            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "Backlinked",
                "--selection-file",
                str(sel),
                "--anchor-name",
                "fig2",
                "--source-note",
                "Figure解读_smithExample2026",
                "--backlink",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            env = json.loads(proc.stdout)
            self.assertTrue(env["data"]["backlink_inserted"])
            self.assertIn(
                "> 卡片：[[card_Backlinked]]",
                fig.read_text(encoding="utf-8"),
            )

    def test_cli_conflict_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            sel = root / "selection.md"
            sel.write_text(SELECTION, encoding="utf-8")
            self._run(
                "--json", "card", "create", "--vault", str(root), "--key", KEY,
                "--title", "Dup", "--selection-file", str(sel),
            )
            proc = self._run(
                "--json", "card", "create", "--vault", str(root), "--key", KEY,
                "--title", "Dup", "--selection-file", str(sel),
            )
            self.assertEqual(proc.returncode, 3)
            import json

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
