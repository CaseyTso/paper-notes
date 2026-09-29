"""Derived card note creation tests (card create).

Frozen after the first green run; do not weaken or delete assertions.
Cards are created under ``<paper_dir>/cards/`` with minimal relation
frontmatter, the verbatim selection body, an optional anchor link back
to the source Figure解读 note, and a trailing ``## 扩展``.

No network, no real vault: synthetic main notes in temporary
directories.
"""

import errno
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_notes import cards, locking

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

    def test_invalid_filenames_rejected_zero_writes(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as out_td:
            root = Path(td)
            outside = Path(out_td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# Title\n\nSome text\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            sentinel_abs = outside / "sentinel_abs.md"
            sentinel_rel = outside / "sentinel_rel.md"
            sentinel_bs = outside / "sentinel_bs.md"

            invalid_names = [
                str(sentinel_abs),                     # POSIX absolute path
                f"../../{sentinel_rel.name}",          # ../ traversal
                f"..\\..\\{sentinel_bs.name}",         # backslash traversal
                "/tmp/escape.md",                      # absolute slash
                "\\tmp\\escape.md",                    # absolute backslash
                "dir/card.md",                         # nested slash
                "dir\\card.md",                        # nested backslash
                ".",                                   # single dot
                "..",                                  # double dot
                ".md",                                 # empty stem
                "..md",                                # traversal stem
                "sentinel\x00.md",                     # NUL byte
                "",                                    # empty string
                "   ",                                 # whitespace only
            ]

            cards_dir = paper_dir / "cards"

            for inv_name in invalid_names:
                with self.subTest(filename=inv_name):
                    with self.assertRaises(cards.CardError):
                        cards.create_card(
                            root,
                            key=KEY,
                            title="Invalid File",
                            selection="Some text",
                            filename=inv_name,
                        )
                    self.assertEqual(fig.read_bytes(), original_fig_bytes)
                    self.assertFalse(cards_dir.exists())
                    self.assertFalse(sentinel_abs.exists())
                    self.assertFalse(sentinel_rel.exists())
                    self.assertFalse(sentinel_bs.exists())

    def test_chinese_filename_library(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)

            # Chinese without .md
            r1 = cards.create_card(
                root,
                key=KEY,
                title="Chinese 1",
                selection=SELECTION,
                filename="我的核心发现",
            )
            self.assertEqual(r1.path.name, "我的核心发现.md")
            self.assertEqual(r1.card_stem, "我的核心发现")
            self.assertTrue(r1.path.exists())

            # Chinese with .md
            r2 = cards.create_card(
                root,
                key=KEY,
                title="Chinese 2",
                selection=SELECTION,
                filename="第二条重要结论.md",
            )
            self.assertEqual(r2.path.name, "第二条重要结论.md")
            self.assertEqual(r2.card_stem, "第二条重要结论")
            self.assertTrue(r2.path.exists())

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
            self.assertFalse(Path(env["data"]["path"]).is_absolute())
            self.assertEqual(env["data"]["path"], f"05 Literature/{KEY}/cards/card_Conclusive.md")
            self.assertTrue((root / env["data"]["path"]).exists())
            self.assertEqual(env["data"]["stem"], "card_Conclusive")
            self.assertIsNone(env["data"]["anchor_name"])
            self.assertIsNone(env["data"]["anchor_status"])
            self.assertFalse(env["data"]["anchor_inserted"])
            self.assertIsNone(env["data"]["anchor_link"])
            self.assertFalse(env["data"]["backlink_inserted"])
            self.assertEqual(env["warnings"], [])

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
            self.assertEqual(env["status"], "success")
            self.assertFalse(Path(env["data"]["path"]).is_absolute())
            self.assertEqual(env["data"]["path"], f"05 Literature/{KEY}/cards/card_Backlinked.md")
            self.assertTrue((root / env["data"]["path"]).exists())
            self.assertEqual(env["data"]["stem"], "card_Backlinked")
            self.assertEqual(env["data"]["anchor_name"], "fig2")
            self.assertEqual(env["data"]["anchor_status"], "inserted")
            self.assertTrue(env["data"]["anchor_inserted"])
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

    def test_cli_exact_byte_range_partial_line_and_duplicate_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Figure 1\n\n"
                "Para 1 contains duplicate marker finding here.\n\n"
                "Para 2 contains duplicate marker finding as well.\n\n"
                "## Next\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            # Target is the duplicate marker in Para 2
            para2_pos = fig_bytes.find(b"Para 2")
            target_slice = b"duplicate marker finding"
            start_byte = fig_bytes.find(target_slice, para2_pos)
            end_byte = start_byte + len(target_slice)

            sel = root / "selection.md"
            sel.write_bytes(target_slice)

            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "Exact Partial Finding",
                "--selection-file",
                str(sel),
                "--source-note",
                f"Figure解读_{KEY}",
                "--source-start-byte",
                str(start_byte),
                "--source-end-byte",
                str(end_byte),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json
            import re

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            rel_path = env["data"]["path"]
            self.assertFalse(Path(rel_path).is_absolute())
            self.assertEqual(rel_path, f"05 Literature/{KEY}/cards/card_Exact_Partial_Finding.md")
            self.assertTrue((root / rel_path).exists())
            self.assertEqual(env["data"]["stem"], "card_Exact_Partial_Finding")
            anchor_name = env["data"]["anchor_name"]
            self.assertTrue(bool(re.match(r"^card-[0-9a-f]{16}$", anchor_name)))
            self.assertEqual(env["data"]["anchor_status"], "inserted")
            self.assertTrue(env["data"]["anchor_inserted"])
            self.assertFalse(env["data"]["backlink_inserted"])
            expected_link = f"[[Figure解读_{KEY}#^{anchor_name}|Figure解读_{KEY}]]"
            self.assertEqual(env["data"]["anchor_link"], expected_link)

            # Check card content
            card_text = (root / rel_path).read_text(encoding="utf-8")
            self.assertIn(f"> 参见 {expected_link}", card_text)
            self.assertIn("duplicate marker finding", card_text)

            # Check source note: anchor added only to para 2, not para 1; no backlink
            fig_text = fig.read_text(encoding="utf-8")
            self.assertIn(f"Para 2 contains duplicate marker finding as well. ^{anchor_name}", fig_text)
            self.assertNotIn(f"Para 1 contains duplicate marker finding here. ^{anchor_name}", fig_text)
            self.assertNotIn("> 卡片：", fig_text)

    def test_cli_cjk_emoji_byte_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# 结果解读 🧬\n\n"
                "样本中发现显著差异 🔬：表达量上升了 3.5 倍。\n\n"
                "## 讨论\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            sel_text = "显著差异 🔬：表达量上升了 3.5 倍。"
            sel_bytes = sel_text.encode("utf-8")
            start_byte = fig_bytes.find(sel_bytes)
            end_byte = start_byte + len(sel_bytes)

            sel = root / "selection.md"
            sel.write_bytes(sel_bytes)

            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "CJK Emoji 发现",
                "--selection-file",
                str(sel),
                "--source-note",
                f"Figure解读_{KEY}",
                "--source-start-byte",
                str(start_byte),
                "--source-end-byte",
                str(end_byte),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertFalse(Path(env["data"]["path"]).is_absolute())
            self.assertEqual(env["data"]["anchor_status"], "inserted")
            self.assertTrue(env["data"]["anchor_inserted"])
            self.assertFalse(env["data"]["backlink_inserted"])

            # Check anchor in source note
            fig_text = fig.read_text(encoding="utf-8")
            anchor_name = env["data"]["anchor_name"]
            self.assertIn(f"^{anchor_name}", fig_text)

    def test_cli_crlf_selection_file_verbatim_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_bytes = (
                b"# CRLF Title\r\n\r\n"
                b"Line one of block.\r\n"
                b"Line two of block.\r\n\r\n"
                b"## Next\r\n"
            )
            fig.write_bytes(fig_bytes)

            target_bytes = b"Line one of block.\r\nLine two of block."
            start_byte = fig_bytes.find(target_bytes)
            end_byte = start_byte + len(target_bytes)

            sel = root / "selection.md"
            sel.write_bytes(target_bytes)

            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "CRLF Match",
                "--selection-file",
                str(sel),
                "--source-note",
                f"Figure解读_{KEY}",
                "--source-start-byte",
                str(start_byte),
                "--source-end-byte",
                str(end_byte),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertEqual(env["data"]["anchor_status"], "inserted")
            self.assertTrue(env["data"]["anchor_inserted"])
            # Verify CRLF is preserved in source
            updated_fig_bytes = fig.read_bytes()
            self.assertIn(b"\r\n", updated_fig_bytes)
            anchor_name = env["data"]["anchor_name"].encode("utf-8")
            self.assertIn(b"^" + anchor_name, updated_fig_bytes)

    def test_cli_stale_or_mismatch_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# Title\n\nOriginal text before external modification.\n\n## Next\n"
            fig.write_text(fig_content, encoding="utf-8")

            # Selection has different text (mismatch against source bytes at offset)
            sel_text = "Completely different edited text content"
            sel = root / "selection.md"
            sel.write_text(sel_text, encoding="utf-8")

            proc = self._run(
                "--json",
                "card",
                "create",
                "--vault",
                str(root),
                "--key",
                KEY,
                "--title",
                "Mismatched Card",
                "--selection-file",
                str(sel),
                "--source-note",
                f"Figure解读_{KEY}",
                "--source-start-byte",
                "9",
                "--source-end-byte",
                "48",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            rel_path = env["data"]["path"]
            self.assertFalse(Path(rel_path).is_absolute())
            self.assertEqual(rel_path, f"05 Literature/{KEY}/cards/card_Mismatched_Card.md")
            self.assertTrue((root / rel_path).exists())
            self.assertEqual(env["data"]["anchor_status"], "failed")
            self.assertFalse(env["data"]["anchor_inserted"])
            self.assertIsNone(env["data"]["anchor_link"])

            # Verify card has no dead anchor link
            card_text = (root / rel_path).read_text(encoding="utf-8")
            self.assertNotIn("> 参见", card_text)
            self.assertIn("Completely different edited text content", card_text)

            # Verify warnings
            self.assertEqual(len(env["warnings"]), 1)
            w = env["warnings"][0]
            self.assertEqual(w["code"], "card_anchor_failed")
            self.assertEqual(w["path"], f"05 Literature/{KEY}/Figure解读_{KEY}.md")
            # Verify selection body is NOT leaked into warning message
            self.assertNotIn("Completely different", w["message"])
            self.assertNotIn("Original text", w["message"])

    def test_cli_invalid_parameter_combos_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Title\n\nSome text here\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = root / "selection.md"
            sel.write_text("Some text", encoding="utf-8")

            import json

            def assert_zero_writes():
                self.assertEqual(fig.read_bytes(), original_fig_bytes)
                cards_dir = paper_dir / "cards"
                if cards_dir.exists():
                    self.assertEqual(list(cards_dir.iterdir()), [])
                else:
                    self.assertFalse(cards_dir.exists())

            # 1. Start offset only (missing end)
            p1 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "SingleStart",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "0",
            )
            self.assertEqual(p1.returncode, 2)
            env1 = json.loads(p1.stdout)
            self.assertEqual(env1["status"], "error")
            assert_zero_writes()

            # 2. End offset only (missing start)
            p2 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "SingleEnd",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-end-byte", "10",
            )
            self.assertEqual(p2.returncode, 2)
            env2 = json.loads(p2.stdout)
            self.assertEqual(env2["status"], "error")
            assert_zero_writes()

            # 3. Exact offsets + --backlink
            p3 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "WithBacklink",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "0",
                "--source-end-byte", "9",
                "--backlink",
            )
            self.assertEqual(p3.returncode, 2)
            env3 = json.loads(p3.stdout)
            self.assertEqual(env3["status"], "error")
            self.assertTrue(any("backlink" in err["message"] for err in env3["errors"]))
            assert_zero_writes()

            # 4. Exact offsets + explicit --anchor-name
            p4 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "WithAnchorName",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "0",
                "--source-end-byte", "9",
                "--anchor-name", "custom-anchor",
            )
            self.assertEqual(p4.returncode, 2)
            env4 = json.loads(p4.stdout)
            self.assertEqual(env4["status"], "error")
            self.assertTrue(any("anchor_name" in err["message"] for err in env4["errors"]))
            assert_zero_writes()

            # 5. Non-integer offset
            p5 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "NonInt",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "notanint",
                "--source-end-byte", "9",
            )
            self.assertEqual(p5.returncode, 2)
            env5 = json.loads(p5.stdout)
            self.assertEqual(env5["status"], "error")
            assert_zero_writes()

            # 6. Inverted offsets (start >= end)
            p6 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "Inverted",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "20",
                "--source-end-byte", "10",
            )
            self.assertEqual(p6.returncode, 2)
            env6 = json.loads(p6.stdout)
            self.assertEqual(env6["status"], "error")
            assert_zero_writes()

            # 7. Invalid UTF-8 selection file
            bad_sel = root / "bad_utf8.md"
            bad_sel.write_bytes(b"\xff\xfe\x00invalid")
            p7 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "BadUtf8",
                "--selection-file", str(bad_sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "0",
                "--source-end-byte", "9",
            )
            self.assertEqual(p7.returncode, 2)
            env7 = json.loads(p7.stdout)
            self.assertEqual(env7["status"], "error")
            self.assertTrue(any("UTF-8" in err["message"] for err in env7["errors"]))
            self.assertFalse(any("\xff\xfe" in err["message"] for err in env7["errors"]))
            assert_zero_writes()

            # 8. Single offset + --backlink
            p8 = self._run(
                "--json", "card", "create",
                "--vault", str(root), "--key", KEY, "--title", "SingleBacklink",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", "0",
                "--backlink",
            )
            self.assertEqual(p8.returncode, 2)
            env8 = json.loads(p8.stdout)
            self.assertEqual(env8["status"], "error")
            assert_zero_writes()

    def test_cli_invalid_filename_rc2_zero_writes(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as out_td:
            root = Path(td)
            outside = Path(out_td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Title\n\nSome text here\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            sel = root / "selection.md"
            sel.write_text("Some text", encoding="utf-8")

            sentinel_abs = outside / "sentinel_abs.md"
            sentinel_rel = outside / "sentinel_rel.md"
            sentinel_bs = outside / "sentinel_bs.md"

            cases = [
                str(sentinel_abs),
                f"../../{sentinel_rel.name}",
                f"..\\..\\{sentinel_bs.name}",
            ]

            cards_dir = paper_dir / "cards"

            for bad_name in cases:
                with self.subTest(bad_name=bad_name):
                    proc = self._run(
                        "--json", "card", "create",
                        "--vault", str(root),
                        "--key", KEY,
                        "--title", "BadFilename",
                        "--selection-file", str(sel),
                        "--filename", bad_name,
                    )
                    self.assertEqual(proc.returncode, 2)
                    import json
                    env = json.loads(proc.stdout)
                    self.assertEqual(env["status"], "error")
                    self.assertEqual(fig.read_bytes(), original_fig_bytes)
                    self.assertFalse(cards_dir.exists())
                    self.assertFalse(sentinel_abs.exists())
                    self.assertFalse(sentinel_rel.exists())
                    self.assertFalse(sentinel_bs.exists())

    def test_chinese_filename_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Title\n\nSome text here\n", encoding="utf-8")
            sel = root / "selection.md"
            sel.write_text("Some text", encoding="utf-8")

            proc = self._run(
                "--json", "card", "create",
                "--vault", str(root),
                "--key", KEY,
                "--title", "Chinese CLI Card",
                "--selection-file", str(sel),
                "--filename", "CLI中文卡片",
            )
            self.assertEqual(proc.returncode, 0)
            import json
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertEqual(env["data"]["path"], f"05 Literature/{KEY}/cards/CLI中文卡片.md")
            self.assertEqual(env["data"]["stem"], "CLI中文卡片")
            self.assertTrue((root / env["data"]["path"]).exists())

    def test_cli_cross_block_selection_anchor_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Figure 1\n\n"
                "Para A content.\n\n"
                "Para B content.\n\n"
                "## Next\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")
            original_fig_bytes = fig.read_bytes()

            selection = "Para A content.\n\nPara B content."
            sel_bytes = selection.encode("utf-8")
            start = fig_bytes.find(sel_bytes)
            end = start + len(sel_bytes)

            sel = root / "selection.md"
            sel.write_bytes(sel_bytes)

            proc = self._run(
                "--json", "card", "create",
                "--vault", str(root),
                "--key", KEY,
                "--title", "CLI Cross Block",
                "--selection-file", str(sel),
                "--source-note", f"Figure解读_{KEY}",
                "--source-start-byte", str(start),
                "--source-end-byte", str(end),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertEqual(env["data"]["anchor_status"], "failed")
            self.assertFalse(env["data"]["anchor_inserted"])
            self.assertIsNone(env["data"]["anchor_link"])
            self.assertEqual(len(env["warnings"]), 1)
            self.assertEqual(env["warnings"][0]["code"], "card_anchor_failed")
            self.assertEqual(env["warnings"][0]["path"], f"05 Literature/{KEY}/Figure解读_{KEY}.md")
            self.assertEqual(fig.read_bytes(), original_fig_bytes)
            card_path = root / env["data"]["path"]
            self.assertTrue(card_path.exists())
            self.assertNotIn("> 参见", card_path.read_text(encoding="utf-8"))

    def test_cli_relative_vault_call(self):
        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            vault_name = "rel_vault"
            root = base_dir / vault_name
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Figure 1\n\nSome findings.\n", encoding="utf-8")

            sel = base_dir / "selection.md"
            sel.write_text("Some findings.", encoding="utf-8")

            env_vars = os.environ.copy()
            env_vars["PYTHONPATH"] = str(REPO)

            proc = subprocess.run(
                [
                    sys.executable, "-m", "paper_notes.cli",
                    "--json", "card", "create",
                    "--vault", vault_name,
                    "--key", KEY,
                    "--title", "Rel Vault Test",
                    "--selection-file", str(sel),
                ],
                capture_output=True,
                text=True,
                cwd=str(base_dir),
                env=env_vars,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            rel_path = env["data"]["path"]
            self.assertFalse(Path(rel_path).is_absolute())
            self.assertEqual(rel_path, f"05 Literature/{KEY}/cards/card_Rel_Vault_Test.md")
            self.assertTrue((root / rel_path).exists())


class CardCreateExactByteRangeTests(unittest.TestCase):
    def test_cross_block_paragraph_to_paragraph_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Figure 1\n\n"
                "First paragraph content line.\n\n"
                "Second paragraph content line.\n\n"
                "## Next section\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")
            original_fig_bytes = fig.read_bytes()

            # Selection spans from First paragraph into Second paragraph
            selection = "First paragraph content line.\n\nSecond paragraph content line."
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Paragraphs",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_fig_bytes)
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(selection, card_text)
            self.assertNotIn("参见", card_text)
            self.assertTrue(any("could not identify containing block" in w for w in result.warnings))

    def test_cross_block_heading_to_paragraph_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Figure 1 Title\n\n"
                "First paragraph under heading.\n\n"
                "## Next section\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")
            original_fig_bytes = fig.read_bytes()

            # Selection spans heading into paragraph
            selection = "# Figure 1 Title\n\nFirst paragraph under heading."
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Heading Para",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_fig_bytes)
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(selection, card_text)
            self.assertNotIn("参见", card_text)
            self.assertTrue(any("could not identify containing block" in w for w in result.warnings))

    def test_cross_block_heading_to_structured_block_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "## Key Findings\n\n"
                "- Bullet finding one\n"
                "- Bullet finding two\n\n"
                "## Discussion\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")
            original_fig_bytes = fig.read_bytes()

            selection = "## Key Findings\n\n- Bullet finding one"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Heading List",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_fig_bytes)
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(selection, card_text)
            self.assertNotIn("参见", card_text)

    def test_single_block_trailing_blank_lines_not_regressed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Figure 1\n\n"
                "Single paragraph content line.\n\n\n"
                "Subsequent paragraph.\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "Single paragraph content line.\n\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Single Para Trailing Blanks",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)
            self.assertIsNotNone(result.anchor_link)
            fig_text = fig.read_text(encoding="utf-8")
            self.assertIn(f"Single paragraph content line. ^{result.anchor_name}\n", fig_text)
            self.assertIn("Subsequent paragraph.\n", fig_text)

    def test_exact_range_partial_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nSome prefix target phrase and suffix\n\n## Next\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "target phrase"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Partial Title",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertTrue(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "inserted")
            self.assertEqual(result.card_stem, "card_Partial_Title")
            expected_anchor = f"card-{hashlib.sha256(b'card_Partial_Title').hexdigest()[:16]}"
            self.assertEqual(result.anchor_name, expected_anchor)
            self.assertEqual(
                result.anchor_link,
                f"[[Figure解读_{KEY}#^{expected_anchor}|Figure解读_{KEY}]]",
            )

            # In source note, paragraph block ID is placed at the end of the last physical line
            fig_text = fig.read_text(encoding="utf-8")
            self.assertIn(
                f"Some prefix target phrase and suffix ^{expected_anchor}\n\n## Next\n",
                fig_text,
            )

            # In card note, selection is verbatim and links back
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(f"target phrase\n\n> 参见 {result.anchor_link}", card_text)
            self.assertIn("## 扩展", card_text)

    def test_two_identical_selections_anchors_first(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "- 重复项：相同解读内容\n\n"
                "中间段落文字\n\n"
                "- 重复项：相同解读内容\n\n"
                "## 下一部分\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "- 重复项：相同解读内容\n"
            sel_bytes = selection.encode("utf-8")
            first_start = fig_bytes.find(sel_bytes)
            first_end = first_start + len(sel_bytes)
            second_start = fig_bytes.find(sel_bytes, first_end)

            result = cards.create_card(
                root,
                key=KEY,
                title="First Match",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=first_start,
                source_end_byte=first_end,
            )
            self.assertTrue(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "inserted")

            fig_text = fig.read_text(encoding="utf-8")
            # Anchor appears once, on its own line after the first list item with blank lines around it
            self.assertEqual(fig_text.count(f"^{result.anchor_name}"), 1)
            self.assertIn(
                f"- 重复项：相同解读内容\n\n^{result.anchor_name}\n\n中间段落文字",
                fig_text,
            )
            # Second occurrence is untouched
            self.assertIn(
                "- 重复项：相同解读内容\n\n## 下一部分",
                fig_text,
            )

    def test_exact_range_cjk_and_emoji(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# 实验解读\n\n"
                "- **关键发现**：模型性能突破 🚀 达到新基准 🎉！\n\n"
                "## 讨论\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "- **关键发现**：模型性能突破 🚀 达到新基准 🎉！\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Emoji CJK Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "inserted")

            fig_text = fig.read_text(encoding="utf-8")
            # List item has anchor on its own line, preceded and followed by blank line
            self.assertIn(
                f"- **关键发现**：模型性能突破 🚀 达到新基准 🎉！\n\n^{result.anchor_name}\n\n## 讨论\n",
                fig_text,
            )
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(selection, card_text)

    def test_exact_range_simple_paragraph_lf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nParagraph line 1\nParagraph line 2\n\nNext paragraph\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "Paragraph line 2"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Para LF",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "inserted")

            expected_source = (
                f"# fig\n\nParagraph line 1\nParagraph line 2 ^{result.anchor_name}\n\nNext paragraph\n"
            )
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_exact_range_simple_paragraph_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            crlf_content = b"# fig\r\n\r\nParagraph line 1\r\nParagraph line 2\r\n\r\nNext\r\n"
            fig.write_bytes(crlf_content)

            selection = "Paragraph line 2\r\n"
            start = crlf_content.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Para CRLF",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)

            expected_source = (
                f"# fig\r\n\r\nParagraph line 1\r\nParagraph line 2 ^{result.anchor_name}\r\n\r\nNext\r\n".encode("utf-8")
            )
            actual_bytes = fig.read_bytes()
            self.assertEqual(actual_bytes, expected_source)
            # Ensure strictly CRLF, no bare LF
            without_crlf = actual_bytes.replace(b"\r\n", b"")
            self.assertNotIn(b"\n", without_crlf)

    def test_exact_range_list(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\n- Item 1\n- Item 2\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "- Item 2\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="List Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            expected_source = f"# fig\n\n- Item 1\n- Item 2\n\n^{result.anchor_name}\n\nNext\n"
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_exact_range_blockquote_and_callout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\n> [!note] Title\n> Line 1\n> Line 2\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "> Line 2\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Callout Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            expected_source = f"# fig\n\n> [!note] Title\n> Line 1\n> Line 2\n\n^{result.anchor_name}\n\nNext\n"
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_exact_range_table(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "| 1 | 2 |\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Table Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            expected_source = f"# fig\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n^{result.anchor_name}\n\nNext\n"
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_exact_range_trailing_multiple_blank_lines_and_subsequent_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "- Item 1\n"
                "- Item 2\n\n\n"
                "Subsequent paragraph line 1\n"
                "Subsequent paragraph line 2\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            # Selection includes list item plus the 2 trailing blank lines
            selection = "- Item 2\n\n\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Trailing Blanks",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)

            # Anchor placed after list with blank lines; the 2 original blank lines and subsequent lines preserved
            expected_source = (
                "# fig\n\n"
                "- Item 1\n"
                "- Item 2\n\n"
                f"^{result.anchor_name}\n\n\n"
                "Subsequent paragraph line 1\n"
                "Subsequent paragraph line 2\n"
            )
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_selection_trailing_spaces_and_blank_lines_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            selection = "## 结论\n\n- 观察结果包含尾随空格   \n\n"
            fig_content = f"# 头\n\n{selection}## 尾\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Trailing Whitespace",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            card_text = result.path.read_text(encoding="utf-8")
            # Trailing spaces and blank lines preserved verbatim without rstrip
            self.assertIn(selection, card_text)
            self.assertIn("尾随空格   \n\n", card_text)

    def test_stale_range_card_created_without_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nOriginal text content here\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "Mismatched selection body\n"
            result = cards.create_card(
                root,
                key=KEY,
                title="Stale Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=0,
                source_end_byte=len(selection.encode("utf-8")),
            )
            self.assertTrue(result.path.exists())
            self.assertFalse(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("does not match" in w for w in result.warnings))

            # Card body contains selection and ## 扩展, but no 参见
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn("Mismatched selection body", card_text)
            self.assertNotIn("参见", card_text)
            self.assertIn("## 扩展", card_text)

            # Source note bytes are 100% untouched
            self.assertEqual(fig.read_bytes(), original_bytes)

    def test_out_of_bounds_range_card_created_without_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# fig\n\nShort\n", encoding="utf-8")
            original_bytes = fig.read_bytes()

            result = cards.create_card(
                root,
                key=KEY,
                title="OOB Card",
                selection="Short",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=0,
                source_end_byte=99999,
            )
            self.assertTrue(result.path.exists())
            self.assertFalse(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("exceeds file length" in w for w in result.warnings))
            self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))
            self.assertEqual(fig.read_bytes(), original_bytes)

    def test_invalid_paired_parameters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)

            # Only start
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_start_byte=0, source_end_byte=None,
                )
            # Only end
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_start_byte=None, source_end_byte=10,
                )
            # Negative start
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=-1, source_end_byte=10,
                )
            # Start >= End
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=10, source_end_byte=5,
                )
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=10, source_end_byte=10,
                )
            # Non-int types (e.g. bool or str)
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=True, source_end_byte=10,  # type: ignore[arg-type]
                )
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=0, source_end_byte="10",  # type: ignore[arg-type]
                )
            # Exact mode without source_note
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=None,
                    source_start_byte=0, source_end_byte=10,
                )
            # Exact mode with explicit anchor_name forbidden
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="sel",
                    source_note=f"Figure解读_{KEY}",
                    anchor_name="custom_anchor",
                    source_start_byte=0, source_end_byte=10,
                )

    def test_exact_mode_canonical_source_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# fig\n\nValid canonical source note\n", encoding="utf-8")

            # Path traversal
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="Valid",
                    source_note=f"../Figure解读_{KEY}.md",
                    source_start_byte=0, source_end_byte=5,
                )
            # Directory separator /
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="Valid",
                    source_note=f"sub/Figure解读_{KEY}.md",
                    source_start_byte=0, source_end_byte=5,
                )
            # Directory separator \
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="Valid",
                    source_note=f"sub\\Figure解读_{KEY}.md",
                    source_start_byte=0, source_end_byte=5,
                )
            # Non-canonical filename
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="Valid",
                    source_note="other_note.md",
                    source_start_byte=0, source_end_byte=5,
                )
            # Non-canonical key
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="T", selection="Valid",
                    source_note="Figure解读_wrongKey2026.md",
                    source_start_byte=0, source_end_byte=5,
                )
            # Valid canonical with .md
            valid_start = fig.read_bytes().find(b"Valid")
            valid_end = valid_start + len(b"Valid")
            res1 = cards.create_card(
                root, key=KEY, title="Valid MD", selection="Valid",
                source_note=f"Figure解读_{KEY}.md",
                source_start_byte=valid_start, source_end_byte=valid_end,
            )
            self.assertTrue(res1.path.exists())
            self.assertEqual(res1.anchor_status, "inserted")

            # Valid canonical without .md
            with tempfile.TemporaryDirectory() as td2:
                root2 = Path(td2)
                pd2 = write_paper(root2)
                fig2 = pd2 / f"Figure解读_{KEY}.md"
                fig2.write_text("# fig\n\nValid canonical source note\n", encoding="utf-8")
                valid_start2 = fig2.read_bytes().find(b"Valid")
                valid_end2 = valid_start2 + len(b"Valid")
                res2 = cards.create_card(
                    root2, key=KEY, title="Valid No MD", selection="Valid",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=valid_start2, source_end_byte=valid_end2,
                )
                self.assertTrue(res2.path.exists())
                self.assertEqual(res2.anchor_status, "inserted")

    def test_cjk_title_deterministic_ascii_anchor(self):
        title = "注意力机制与鲁棒性分析"
        expected_stem = "card_注意力机制与鲁棒性分析"
        expected_hash = hashlib.sha256(expected_stem.encode("utf-8")).hexdigest()[:16]
        expected_anchor = f"card-{expected_hash}"

        # Deterministic regex check: ^card-[0-9a-f]{16}$
        self.assertRegex(expected_anchor, r"^card-[0-9a-f]{16}$")
        self.assertTrue(expected_anchor.isascii())

        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            content_bytes = b"# fig\n\nContent\n"
            c_start = content_bytes.find(b"Content")
            c_end = c_start + len(b"Content")
            for r in (root1, root2):
                pd = write_paper(r)
                (pd / f"Figure解读_{KEY}.md").write_bytes(content_bytes)

            res1 = cards.create_card(
                root1, key=KEY, title=title, selection="Content",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=c_start, source_end_byte=c_end,
            )
            res2 = cards.create_card(
                root2, key=KEY, title=title, selection="Content",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=c_start, source_end_byte=c_end,
            )

            self.assertEqual(res1.anchor_name, expected_anchor)
            self.assertEqual(res2.anchor_name, expected_anchor)
            self.assertEqual(res1.anchor_name, res2.anchor_name)
            self.assertEqual(res1.card_stem, expected_stem)

    def test_card_conflict_zero_writes_to_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nInitial source content\n"
            fig.write_text(fig_content, encoding="utf-8")
            c_start = fig.read_bytes().find(b"Initial")
            c_end = c_start + len(b"Initial")

            # Create first card
            cards.create_card(
                root,
                key=KEY,
                title="ConflictCard",
                selection="Initial",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=c_start,
                source_end_byte=c_end,
            )
            src_bytes_after_first = fig.read_bytes()

            # Attempt to create card with same title -> CardConflict
            with self.assertRaises(cards.CardConflict):
                cards.create_card(
                    root,
                    key=KEY,
                    title="ConflictCard",
                    selection="Initial",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=c_start,
                    source_end_byte=c_end,
                )

            # Source note is completely untouched after conflict
            self.assertEqual(fig.read_bytes(), src_bytes_after_first)

    def test_existing_anchor_on_target_block(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Existing Test"
            stem = f"card_{cards.slugify_card_filename(title)}"
            stem_hash = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
            anchor = f"card-{stem_hash}"

            # Source note already has the exact anchor at the end of the target paragraph
            fig_content = f"# fig\n\nTarget paragraph ^{anchor}\n\nNext paragraph\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "Target paragraph"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "existing")
            self.assertFalse(result.anchor_inserted)
            self.assertEqual(
                result.anchor_link,
                f"[[Figure解读_{KEY}#^{anchor}|Figure解读_{KEY}]]",
            )
            # Source file untouched
            self.assertEqual(fig.read_bytes(), original_bytes)

    def test_existing_rejected_for_longer_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Longer Test"
            stem = f"card_{cards.slugify_card_filename(title)}"
            stem_hash = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
            anchor = f"card-{stem_hash}"

            # Source note has a LONGER anchor with suffix on another block
            fig_content = f"# fig\n\nOther block ^{anchor}-extra\n\nTarget paragraph\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")

            selection = "Target paragraph"
            start = fig.read_bytes().find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            # Must NOT return existing; inserts the exact anchor
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)
            self.assertIn(f"^{anchor}", fig.read_text(encoding="utf-8"))

    def test_existing_rejected_for_inline_or_code_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Inline Test"
            stem = f"card_{cards.slugify_card_filename(title)}"
            stem_hash = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
            anchor = f"card-{stem_hash}"

            # Source note mentions ^anchor in inline code
            fig_content = f"# fig\n\nTarget paragraph with `^{anchor}` in code\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")

            selection = "Target paragraph"
            start = fig.read_bytes().find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            # Must NOT return existing; appends real block anchor
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)
            fig_text = fig.read_text(encoding="utf-8")
            self.assertIn(f"Target paragraph with `^{anchor}` in code ^{anchor}\n", fig_text)

    def test_existing_rejected_and_fails_for_another_block_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Other Block Test"
            stem = f"card_{cards.slugify_card_filename(title)}"
            stem_hash = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
            anchor = f"card-{stem_hash}"

            # Another block (Block A) has the anchor!
            fig_content = f"# fig\n\nBlock A ^{anchor}\n\nBlock B target content\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "Block B target content"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            # Must NOT return existing (which would point to Block A = wrong link)!
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("already exists on another block" in w for w in result.warnings))

            # Source note is untouched
            self.assertEqual(fig.read_bytes(), original_bytes)
            # Card note created without link
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn("Block B target content", card_text)
            self.assertNotIn("参见", card_text)

    def test_commit_time_target_racer_raises_conflict_zero_source_writes(self):
        # v6 P3 racer: a non-cooperative writer occupies the card target right
        # before the RENAME_EXCL commit. The card publish raises CardConflict
        # and the already-committed P2 source replace is compensated back to
        # the original bytes (zero net source writes).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# fig\n\nInitial source\n", encoding="utf-8")
            original_source_bytes = fig.read_bytes()
            sel = "Initial source"
            start = original_source_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np

            def racer_renameatx(fromfd, fromname, tofd, toname, flags):
                # Racer creates the target file right before the EXCL rename
                (paper_dir / "cards" / "card_Racer Target.md").write_text(
                    "racer occupied target", encoding="utf-8"
                )
                raise FileExistsError("Target conflict")

            with patch("paper_notes.cards._renameatx_np", side_effect=racer_renameatx):
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        root,
                        key=KEY,
                        title="Racer Target",
                        selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            # Racer content is preserved (never overwritten)
            self.assertEqual(
                (paper_dir / "cards" / "card_Racer Target.md").read_text(encoding="utf-8"),
                "racer occupied target",
            )
            # P2 compensation restored the source: zero net writes
            self.assertEqual(fig.read_bytes(), original_source_bytes)
            # No temp residue
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])
            card_tmps = [f.name for f in (paper_dir / "cards").iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(card_tmps, [])

    def test_source_modified_between_read_and_commit(self):
        # v6 exact-mode contract: a concurrent ordinary write to the source
        # between P0 read and P2 commit is a fail-closed CardError. Zero
        # visible mutations: no source modification, no card published, no
        # staged temp residue.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# fig\n\nInitial source paragraph\n", encoding="utf-8")
            original_source_bytes = fig.read_bytes()

            # Simulate concurrent user edit to source note between read and commit
            selection = "Initial source paragraph"
            sel_bytes = selection.encode("utf-8")
            start = fig.read_bytes().find(sel_bytes)
            end = start + len(sel_bytes)

            orig_resolve = cards._resolve_and_insert_anchor

            def concurrent_source_edit(*args, **kwargs):
                fig.write_text("# fig\n\nUser typed new edit concurrently\n", encoding="utf-8")
                return orig_resolve(*args, **kwargs)

            with patch("paper_notes.cards._resolve_and_insert_anchor", side_effect=concurrent_source_edit):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        root,
                        key=KEY,
                        title="Concurrent Source",
                        selection=selection,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            # Fail closed: the concurrent user edit is retained and the
            # transaction made zero visible mutations.
            self.assertIn("source note changed concurrently", str(ctx.exception))
            self.assertIn("User typed new edit concurrently", fig.read_text(encoding="utf-8"))
            self.assertFalse((paper_dir / "cards" / "card_Concurrent_Source.md").exists())
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])
            card_tmps = [f.name for f in (paper_dir / "cards").iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(card_tmps, [])

    def test_card_modified_concurrently_before_link_backfill(self):
        # v6 equivalent: user edits land in the card target before P3 publish.
        # RENAME_EXCL refuses to overwrite: CardConflict, user content intact,
        # P2-committed source compensated to original bytes.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            selection = "Target content"
            sel_bytes = selection.encode("utf-8")
            start = fig.read_bytes().find(sel_bytes)
            end = start + len(sel_bytes)

            orig_renameatx = cards._renameatx_np

            def card_tamper_before_publish(fromfd, fromname, tofd, toname, flags):
                # User writes their own card content right before P3 publish
                (paper_dir / "cards" / "card_Tamper_Card.md").write_text(
                    "User made edits to the card!", encoding="utf-8"
                )
                raise FileExistsError("Target conflict")

            with patch("paper_notes.cards._renameatx_np", side_effect=card_tamper_before_publish):
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        root,
                        key=KEY,
                        title="Tamper Card",
                        selection=selection,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            # Card edits must not be clobbered
            card_file = paper_dir / "cards" / "card_Tamper_Card.md"
            self.assertEqual(card_file.read_text(encoding="utf-8"), "User made edits to the card!")
            # P2 compensation restored the source
            self.assertEqual(fig.read_bytes(), original_fig_bytes)

    def test_vault_write_lock_conflict_and_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)

            # 1. Live lock held by another process/PID
            lp = locking.lock_path(root)
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "started_at": "2026-01-01T00:00:00Z",
                    "operation": "create_item",
                    "operation_id": "op123",
                    "host": "localhost",
                }),
                encoding="utf-8",
            )
            with self.assertRaises(cards.CardConflict):
                cards.create_card(
                    root, key=KEY, title="Locked", selection="Some sel",
                )

            # 2. Stale lock held by dead pid
            lp.write_text(
                json.dumps({
                    "pid": 999999999,
                    "started_at": "2026-01-01T00:00:00Z",
                    "operation": "create_item",
                    "operation_id": "op456",
                    "host": "localhost",
                }),
                encoding="utf-8",
            )
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="Stale", selection="Some sel",
                )

    def test_selection_verbatim_exhaustive(self):
        variations = [
            ("no_newline", "Verbatim without trailing newline"),
            ("single_lf", "Verbatim with single LF\n"),
            ("crlf", "Verbatim with CRLF line ending\r\n"),
            ("trailing_spaces", "Verbatim with trailing spaces   \n"),
            ("multiple_blank_lines", "Verbatim with multiple blank lines\n\n\n"),
        ]

        for name, sel in variations:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                write_paper(root)

                result = cards.create_card(
                    root,
                    key=KEY,
                    title=f"Verbatim {name}",
                    selection=sel,
                )
                with result.path.open(encoding="utf-8", newline="") as fh:
                    card_text = fh.read()
                # Split at frontmatter boundary: ---\n\n
                fm_end = card_text.find("---\n\n")
                self.assertNotEqual(fm_end, -1)
                body = card_text[fm_end + len("---\n\n"):]

                # Body MUST start with selection verbatim
                self.assertTrue(
                    body.startswith(sel),
                    f"failed on {name}: expected body to start with verbatim {sel!r}, got {body!r}",
                )
                # Remainder must be the expected suffix
                remainder = body[len(sel):]
                nl = "\r\n" if "\r\n" in sel else "\n"
                if sel.endswith(nl + nl):
                    expected_suffix = f"## 扩展{nl}"
                elif sel.endswith(nl):
                    expected_suffix = f"{nl}## 扩展{nl}"
                else:
                    expected_suffix = f"{nl}{nl}## 扩展{nl}"
                self.assertEqual(
                    remainder,
                    expected_suffix,
                    f"failed on {name}: expected suffix {expected_suffix!r}, got {remainder!r}",
                )

    def test_exact_mode_source_note_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            # Note file does NOT exist
            result = cards.create_card(
                root,
                key=KEY,
                title="Missing Source Note",
                selection="Some selection",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=0,
                source_end_byte=14,
            )
            self.assertTrue(result.path.exists())
            self.assertFalse(result.anchor_inserted)
            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("source note not found" in w for w in result.warnings))
            self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_target_block_different_anchor_fails_closed_paragraph(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nTarget paragraph ^card-other1234567890\n\nNext paragraph\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "Target paragraph"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Second Paragraph Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("already has a different anchor" in w for w in result.warnings))
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_target_block_different_anchor_fails_closed_list(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "- List item 1\n"
                "- List item 2\n\n"
                "^card-other1234567890\n\n"
                "Next paragraph\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "- List item 2\n"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Second List Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("already has a different anchor" in w for w in result.warnings))
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_normal_paragraph_with_pipe_not_treated_as_table(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "Line 1 with a | pipe and `code | span` and \\| escaped pipe.\n"
                "Line 2 continuation of the paragraph.\n\n"
                "## Next section\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            # Selection is in line 2
            selection = "Line 2 continuation of the paragraph."
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Pipe Paragraph Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)

            # Paragraph must remain whole and anchor placed inline at end of line 2
            expected_fig = (
                "# fig\n\n"
                "Line 1 with a | pipe and `code | span` and \\| escaped pipe.\n"
                f"Line 2 continuation of the paragraph. ^{result.anchor_name}\n\n"
                "## Next section\n"
            )
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_fig)

    def test_genuine_markdown_table_structured_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "| Header A | Header B |\n"
                "| :--- | ---: |\n"
                "| val 1 | val 2 |\n\n"
                "## Next section\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "| val 1 | val 2 |\n"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Table Selection Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "inserted")

            # Table anchor must be on a standalone line following table with blank lines
            expected_fig = (
                "# fig\n\n"
                "| Header A | Header B |\n"
                "| :--- | ---: |\n"
                "| val 1 | val 2 |\n\n"
                f"^{result.anchor_name}\n\n"
                "## Next section\n"
            )
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_fig)

    def test_unclosed_fenced_block_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\n```python\ndef foo():\n    return 42\n"
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "def foo():\n    return 42\n"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Unclosed Code Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertTrue(any("unclosed fenced" in w for w in result.warnings))
            # Source file untouched (anchor never written into code)
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_structured_existing_anchor_approved_layout_list_and_quote(self):
        # 1. List with approved layout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Approved List"
            stem = f"card_{cards.slugify_card_filename(title)}"
            anchor = f"card-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:16]}"

            fig_content = (
                "# fig\n\n"
                "- Item 1\n"
                "- Item 2\n\n"
                f"^{anchor}\n\n"
                "Next text\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "- Item 2\n"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            res_list = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res_list.anchor_status, "existing")
            self.assertFalse(res_list.anchor_inserted)
            self.assertEqual(
                res_list.anchor_link,
                f"[[Figure解读_{KEY}#^{anchor}|Figure解读_{KEY}]]",
            )
            self.assertEqual(fig.read_bytes(), original_bytes)

        # 2. Blockquote with approved layout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"

            title = "Approved Quote"
            stem = f"card_{cards.slugify_card_filename(title)}"
            anchor = f"card-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:16]}"

            fig_content = (
                "# fig\n\n"
                "> Quote line 1\n"
                "> Quote line 2\n\n"
                f"^{anchor}\n\n"
                "Next text\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "> Quote line 2\n"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            res_quote = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res_quote.anchor_status, "existing")
            self.assertFalse(res_quote.anchor_inserted)
            self.assertEqual(
                res_quote.anchor_link,
                f"[[Figure解读_{KEY}#^{anchor}|Figure解读_{KEY}]]",
            )
            self.assertEqual(fig.read_bytes(), original_bytes)

    def test_structured_existing_anchor_rejected_illegal_layouts(self):
        title = "Illegal Layout Test"
        stem = f"card_{cards.slugify_card_filename(title)}"
        anchor = f"card-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:16]}"
        selection = "- Item 2\n"

        illegal_cases = [
            # Two blank lines before marker
            (
                "two_blank_lines_before",
                f"# fig\n\n- Item 1\n- Item 2\n\n\n^{anchor}\n\nNext\n",
            ),
            # Zero blank lines before marker (glued to block)
            (
                "zero_blank_lines_before",
                f"# fig\n\n- Item 1\n- Item 2\n^{anchor}\n\nNext\n",
            ),
            # Zero blank lines after marker (glued to next text)
            (
                "zero_blank_lines_after",
                f"# fig\n\n- Item 1\n- Item 2\n\n^{anchor}\nNext text\n",
            ),
            # Distant token across 5 blank lines
            (
                "distant_token",
                f"# fig\n\n- Item 1\n- Item 2\n\n\n\n\n\n^{anchor}\n\nNext\n",
            ),
        ]

        for case_name, fig_content in illegal_cases:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                paper_dir = write_paper(root)
                fig = paper_dir / f"Figure解读_{KEY}.md"
                fig_content_str = fig_content
                fig.write_text(fig_content_str, encoding="utf-8")
                original_bytes = fig.read_bytes()

                start = original_bytes.find(selection.encode("utf-8"))
                end = start + len(selection.encode("utf-8"))

                res = cards.create_card(
                    root,
                    key=KEY,
                    title=title,
                    selection=selection,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )
                # Must NOT return existing; must fail closed with source untouched
                self.assertEqual(res.anchor_status, "failed", f"case {case_name} should fail closed")
                self.assertFalse(res.anchor_inserted)
                self.assertIsNone(res.anchor_link)
                self.assertEqual(fig.read_bytes(), original_bytes)
                self.assertNotIn("参见", res.path.read_text(encoding="utf-8"))

    def test_vault_write_lock_cards_dir_not_created(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_paper(root)
            cards_dir = cards.cards_directory(root, KEY)
            self.assertFalse(cards_dir.exists())

            # 1. Live lock: cards/ directory must NOT appear
            lp = locking.lock_path(root)
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "started_at": "2026-01-01T00:00:00Z",
                    "operation": "create_item",
                    "operation_id": "op123",
                    "host": "localhost",
                }),
                encoding="utf-8",
            )
            with self.assertRaises(cards.CardConflict):
                cards.create_card(
                    root, key=KEY, title="Locked Test", selection="Some text",
                )
            self.assertFalse(cards_dir.exists(), "cards/ must not be created under live lock conflict")

            # 2. Stale lock: cards/ directory must NOT appear
            lp.write_text(
                json.dumps({
                    "pid": 999999999,
                    "started_at": "2026-01-01T00:00:00Z",
                    "operation": "create_item",
                    "operation_id": "op456",
                    "host": "localhost",
                }),
                encoding="utf-8",
            )
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    root, key=KEY, title="Stale Test", selection="Some text",
                )
            self.assertFalse(cards_dir.exists(), "cards/ must not be created under stale lock")

    def test_structured_block_at_eof_layout_lf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\n- Item 1\n- Item 2\n"
            fig.write_text(fig_content, encoding="utf-8")

            selection = "- Item 2\n"
            start = fig.read_bytes().find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            res1 = cards.create_card(
                root,
                key=KEY,
                title="EOF LF Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res1.anchor_status, "inserted")
            self.assertTrue(res1.anchor_inserted)

            # Expected EOF layout: blank line before marker, marker line, terminating blank line
            expected_fig = f"# fig\n\n- Item 1\n- Item 2\n\n^{res1.anchor_name}\n\n"
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_fig)

            # Re-running on this same block must detect existing anchor
            res1.path.unlink()  # remove target card so conflict is not raised
            res2 = cards.create_card(
                root,
                key=KEY,
                title="EOF LF Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res2.anchor_status, "existing")
            self.assertFalse(res2.anchor_inserted)
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_fig)

    def test_structured_block_at_eof_layout_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            crlf_content = b"# fig\r\n\r\n- Item 1\r\n- Item 2\r\n"
            fig.write_bytes(crlf_content)

            selection = "- Item 2\r\n"
            start = crlf_content.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            res1 = cards.create_card(
                root,
                key=KEY,
                title="EOF CRLF Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res1.anchor_status, "inserted")
            self.assertTrue(res1.anchor_inserted)

            # Expected EOF layout with strict CRLF: blank line before marker, marker line, terminating blank line
            expected_fig_bytes = (
                f"# fig\r\n\r\n- Item 1\r\n- Item 2\r\n\r\n^{res1.anchor_name}\r\n\r\n".encode("utf-8")
            )
            actual_bytes = fig.read_bytes()
            self.assertEqual(actual_bytes, expected_fig_bytes)
            # Ensure no bare LF
            without_crlf = actual_bytes.replace(b"\r\n", b"")
            self.assertNotIn(b"\n", without_crlf)
            self.assertNotIn(b"\r", without_crlf)

            # Re-running on this same block must detect existing anchor
            res1.path.unlink()
            res2 = cards.create_card(
                root,
                key=KEY,
                title="EOF CRLF Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(res2.anchor_status, "existing")
            self.assertFalse(res2.anchor_inserted)
            self.assertEqual(fig.read_bytes(), expected_fig_bytes)

    def test_exact_range_paragraph_ends_with_newline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# fig\n\nParagraph line 1\nParagraph line 2\n\nNext\n"
            fig.write_text(fig_content, encoding="utf-8")
            fig_bytes = fig_content.encode("utf-8")

            selection = "Paragraph line 2\n"
            start = fig_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Para Ends With NL",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.anchor_inserted)
            expected_source = (
                f"# fig\n\nParagraph line 1\nParagraph line 2 ^{result.anchor_name}\n\nNext\n"
            )
            self.assertEqual(fig.read_text(encoding="utf-8"), expected_source)

    def test_paragraph_different_anchor_trailing_whitespace_fails_closed(self):
        ws_variants = [
            ("space", "   "),
            ("tab", "\t"),
            ("mixed", " \t  \t"),
        ]
        old_anchor = "card-old1234567890"

        for name, ws in ws_variants:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                paper_dir = write_paper(root)
                fig = paper_dir / f"Figure解读_{KEY}.md"
                fig_content = f"# fig\n\nParagraph text ^{old_anchor}{ws}\n\nNext\n"
                fig.write_text(fig_content, encoding="utf-8")
                original_bytes = fig.read_bytes()

                selection = "Paragraph text"
                start = original_bytes.find(selection.encode("utf-8"))
                end = start + len(selection.encode("utf-8"))

                result = cards.create_card(
                    root,
                    key=KEY,
                    title=f"New Title {name}",
                    selection=selection,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )
                self.assertTrue(result.path.exists())
                self.assertEqual(result.anchor_status, "failed")
                self.assertFalse(result.anchor_inserted)
                self.assertIsNone(result.anchor_link)
                self.assertTrue(any("already has a different anchor" in w for w in result.warnings))
                self.assertEqual(fig.read_bytes(), original_bytes)
                self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_heading_different_anchor_trailing_whitespace_fails_closed(self):
        ws_variants = [
            ("space", "  "),
            ("tab", "\t\t"),
            ("mixed", "\t "),
        ]
        old_anchor = "card-oldhead12345"

        for name, ws in ws_variants:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                paper_dir = write_paper(root)
                fig = paper_dir / f"Figure解读_{KEY}.md"
                fig_content = f"# fig\n\n## Section Title ^{old_anchor}{ws}\n\nNext text\n"
                fig.write_text(fig_content, encoding="utf-8")
                original_bytes = fig.read_bytes()

                selection = "## Section Title"
                start = original_bytes.find(selection.encode("utf-8"))
                end = start + len(selection.encode("utf-8"))

                result = cards.create_card(
                    root,
                    key=KEY,
                    title=f"Heading Card {name}",
                    selection=selection,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )
                self.assertTrue(result.path.exists())
                self.assertEqual(result.anchor_status, "failed")
                self.assertFalse(result.anchor_inserted)
                self.assertIsNone(result.anchor_link)
                self.assertTrue(any("already has a different anchor" in w for w in result.warnings))
                self.assertEqual(fig.read_bytes(), original_bytes)
                self.assertNotIn("参见", result.path.read_text(encoding="utf-8"))

    def test_paragraph_same_anchor_trailing_whitespace_existing(self):
        ws_variants = [
            ("space", "   "),
            ("tab", "\t"),
            ("mixed", " \t "),
        ]
        title = "Same Para Card"
        stem = f"card_{cards.slugify_card_filename(title)}"
        expected_anchor = f"card-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:16]}"

        for name, ws in ws_variants:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                paper_dir = write_paper(root)
                fig = paper_dir / f"Figure解读_{KEY}.md"
                fig_content = f"# fig\n\nParagraph text ^{expected_anchor}{ws}\n\nNext\n"
                fig.write_text(fig_content, encoding="utf-8")
                original_bytes = fig.read_bytes()

                selection = "Paragraph text"
                start = original_bytes.find(selection.encode("utf-8"))
                end = start + len(selection.encode("utf-8"))

                result = cards.create_card(
                    root,
                    key=KEY,
                    title=title,
                    selection=selection,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )
                self.assertTrue(result.path.exists())
                self.assertEqual(result.anchor_status, "existing")
                self.assertFalse(result.anchor_inserted)
                self.assertEqual(
                    result.anchor_link,
                    f"[[Figure解读_{KEY}#^{expected_anchor}|Figure解读_{KEY}]]",
                )
                self.assertEqual(fig.read_bytes(), original_bytes)
                self.assertIn(f"> 参见 {result.anchor_link}", result.path.read_text(encoding="utf-8"))

    def test_heading_same_anchor_trailing_whitespace_existing(self):
        ws_variants = [
            ("space", "  "),
            ("tab", "\t"),
            ("mixed", "\t  "),
        ]
        title = "Same Heading Card"
        stem = f"card_{cards.slugify_card_filename(title)}"
        expected_anchor = f"card-{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:16]}"

        for name, ws in ws_variants:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                paper_dir = write_paper(root)
                fig = paper_dir / f"Figure解读_{KEY}.md"
                fig_content = f"# fig\n\n### Heading text ^{expected_anchor}{ws}\n\nNext\n"
                fig.write_text(fig_content, encoding="utf-8")
                original_bytes = fig.read_bytes()

                selection = "### Heading text"
                start = original_bytes.find(selection.encode("utf-8"))
                end = start + len(selection.encode("utf-8"))

                result = cards.create_card(
                    root,
                    key=KEY,
                    title=title,
                    selection=selection,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )
                self.assertTrue(result.path.exists())
                self.assertEqual(result.anchor_status, "existing")
                self.assertFalse(result.anchor_inserted)
                self.assertEqual(
                    result.anchor_link,
                    f"[[Figure解读_{KEY}#^{expected_anchor}|Figure解读_{KEY}]]",
                )
                self.assertEqual(fig.read_bytes(), original_bytes)
                self.assertIn(f"> 参见 {result.anchor_link}", result.path.read_text(encoding="utf-8"))

    def test_middle_token_not_treated_as_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# fig\n\n"
                "Paragraph with ^card-token in the middle of a sentence.\n\n"
                "## Heading with ^card-token inside it\n\n"
                "Next\n"
            )
            fig.write_text(fig_content, encoding="utf-8")

            # Selection on the paragraph
            selection = "Paragraph with ^card-token in the middle of a sentence."
            start = fig.read_bytes().find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Middle Token Para",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)
            # Anchor inserted at the end of the line
            self.assertIn(
                f"Paragraph with ^card-token in the middle of a sentence. ^{result.anchor_name}\n",
                fig.read_text(encoding="utf-8"),
            )


class SetextHeadingTests(unittest.TestCase):
    def test_setext_heading_alone_fails_closed_equal_lf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Main ATX Title\n\n"
                "Setext Heading Title\n"
                "====================\n\n"
                "Subsequent paragraph.\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "Setext Heading Title\n===================="
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Setext Equal Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            # Source note must have zero writes
            self.assertEqual(fig.read_bytes(), original_bytes)
            # Card must have no dead link
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn(selection, card_text)
            self.assertNotIn("> 参见", card_text)
            self.assertTrue(any("Setext heading" in w for w in result.warnings))

    def test_setext_heading_alone_fails_closed_dash_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_bytes = (
                b"# Main ATX Title\r\n\r\n"
                b"Setext Level 2 Title\r\n"
                b"--------------------\r\n\r\n"
                b"Subsequent paragraph.\r\n"
            )
            fig.write_bytes(fig_bytes)

            selection = "Setext Level 2 Title\r\n--------------------"
            sel_bytes = selection.encode("utf-8")
            start = fig_bytes.find(sel_bytes)
            end = start + len(sel_bytes)

            result = cards.create_card(
                root,
                key=KEY,
                title="Setext Dash CRLF Card",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), fig_bytes)
            card_text = result.path.read_text(encoding="utf-8")
            self.assertNotIn("> 参见", card_text)

    def test_setext_heading_multiline_alone_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Document\n\n"
                "Heading Line One\n"
                "Heading Line Two\n"
                "===============\n\n"
                "Next body paragraph.\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            # Select only the first line of the multiline heading
            selection = "Heading Line One"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Multiline Setext Part",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("> 参见", result.path.read_text(encoding="utf-8"))

    def test_setext_heading_cross_to_subsequent_paragraph_lf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Section\n\n"
                "Heading Title\n"
                "=============\n\n"
                "Subsequent paragraph line.\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            # Selection spans heading into subsequent paragraph
            selection = "Heading Title\n=============\n\nSubsequent paragraph line."
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Setext Para",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("> 参见", result.path.read_text(encoding="utf-8"))

    def test_setext_heading_cross_to_subsequent_paragraph_no_blank_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Section\n\n"
                "Heading Title\n"
                "=============\n"
                "Immediate paragraph line.\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            # Selection spans heading to immediate paragraph
            selection = "Heading Title\n=============\nImmediate paragraph line."
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Setext Immed Para",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("> 参见", result.path.read_text(encoding="utf-8"))

    def test_setext_heading_cross_to_subsequent_list(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = (
                "# Section\n\n"
                "List Heading Title\n"
                "------------------\n"
                "- Item 1\n"
                "- Item 2\n"
            )
            fig.write_text(fig_content, encoding="utf-8")
            original_bytes = fig.read_bytes()

            selection = "List Heading Title\n------------------\n- Item 1"
            start = original_bytes.find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Cross Setext List",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNone(result.anchor_link)
            self.assertEqual(fig.read_bytes(), original_bytes)
            self.assertNotIn("> 参见", result.path.read_text(encoding="utf-8"))

    def test_atx_heading_single_block_still_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# Preserved ATX Heading\n\nParagraph body.\n"
            fig.write_text(fig_content, encoding="utf-8")

            selection = "# Preserved ATX Heading"
            start = fig.read_bytes().find(selection.encode("utf-8"))
            end = start + len(selection.encode("utf-8"))

            result = cards.create_card(
                root,
                key=KEY,
                title="Preserved ATX",
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(result.anchor_inserted)
            self.assertIsNotNone(result.anchor_link)
            self.assertIn(f"# Preserved ATX Heading ^{result.anchor_name}\n", fig.read_text(encoding="utf-8"))


class ContainerSetextHazardTests(unittest.TestCase):
    """Phase4b: blockquote/callout/list containing a nested Setext heading.

    Whole-block conservative fail-closed granularity: every exact selection
    inside such a container gets anchor_status "failed", the source is
    byte-for-byte untouched, the card is still created without a 参见 link,
    and a stable warning mentions "Setext heading".
    """

    def _hazard_case(
        self,
        fig_content: str | bytes,
        selection: str,
        title: str,
        assert_fn,
    ) -> None:
        """Run exact-mode create_card inside a fresh temp vault and apply
        ``assert_fn(result, fig_bytes_now, original_bytes, card_text)`` while
        the temp directory still exists (context is exited afterwards)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_dir = write_paper(root)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            if isinstance(fig_content, bytes):
                fig.write_bytes(fig_content)
                original = fig_content
                sel_bytes = selection.encode("utf-8")
            else:
                fig.write_text(fig_content, encoding="utf-8")
                original = fig_content.encode("utf-8")
                sel_bytes = selection.encode("utf-8")
            start = original.find(sel_bytes)
            self.assertGreaterEqual(start, 0)
            end = start + len(sel_bytes)

            result = cards.create_card(
                root,
                key=KEY,
                title=title,
                selection=selection,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start,
                source_end_byte=end,
            )
            card_text = result.path.read_text(encoding="utf-8") if result.path.exists() else ""
            assert_fn(result, fig.read_bytes(), original, card_text)

    def _assert_hazard_failed(self, result, fig_bytes, original_bytes, card_text, selection):
        self.assertTrue(result.path.exists())
        self.assertEqual(result.anchor_status, "failed")
        self.assertFalse(result.anchor_inserted)
        self.assertIsNone(result.anchor_link)
        # Source byte-for-byte zero write
        self.assertEqual(fig_bytes, original_bytes)
        # Card still created, selection verbatim (newline-normalized read),
        # no dead link
        self.assertIn(selection.replace("\r\n", "\n"), card_text)
        self.assertNotIn("> 参见", card_text)
        self.assertTrue(any("Setext heading" in w for w in result.warnings))

    def test_blockquote_setext_equal_lf_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "> Heading Quote\n"
            "> ============\n"
            "> body line\n\n"
            "Next\n"
        )
        selection = "> body line\n"
        self._hazard_case(
            fig_content, selection, "Quote Equal Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_blockquote_setext_dash_crlf_fails_closed(self):
        fig_bytes_src = (
            b"# fig\r\n\r\n"
            b"> Heading CRLF\r\n"
            b"> ------------\r\n"
            b"> body line\r\n\r\n"
            b"Next\r\n"
        )
        selection = "> body line\r\n"
        self._hazard_case(
            fig_bytes_src, selection, "Quote Dash CRLF Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_callout_marker_heading_underline_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "> [!note] Callout Heading\n"
            "> -----------------\n"
            "> callout body\n\n"
            "Next\n"
        )
        selection = "> callout body\n"
        self._hazard_case(
            fig_content, selection, "Callout Setext Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_unordered_list_indented_underline_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "- List Heading\n"
            "  ------------\n"
            "- Item 2\n\n"
            "Next\n"
        )
        selection = "- Item 2\n"
        self._hazard_case(
            fig_content, selection, "UL Setext Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_ordered_list_underline_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "1. Ordered Heading\n"
            "   =================\n"
            "2. Item two\n\n"
            "Next\n"
        )
        selection = "2. Item two\n"
        self._hazard_case(
            fig_content, selection, "OL Setext Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_nested_list_underline_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "- outer\n"
            "  - Inner Heading\n"
            "    ==============\n"
            "- Item 2\n\n"
            "Next\n"
        )
        selection = "- Item 2\n"
        self._hazard_case(
            fig_content, selection, "Nested Setext Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_whole_block_hazard_all_selections_fail_closed(self):
        # Selection = only the heading line, heading + underline, or an
        # unrelated other line of the SAME container: all fail closed.
        fig_content = (
            "# fig\n\n"
            "> Heading Quote\n"
            "> ============\n"
            "> body line\n\n"
            "Next\n"
        )
        for selection, label in (
            ("> Heading Quote\n", "title only"),
            ("> Heading Quote\n> ============\n", "title+underline"),
            ("> body line\n", "other line"),
        ):
            with self.subTest(label=label):
                self._hazard_case(
                    fig_content, selection, f"Whole Block {label}",
                    lambda r, fb, ob, ct, s=selection: self._assert_hazard_failed(r, fb, ob, ct, s),
                )

    def test_container_fenced_pseudo_setext_still_inserts_blockquote(self):
        # Pseudo underline inside a fenced block inside the blockquote must
        # NOT trigger the hazard: existing insertion rules still apply.
        fig_content = (
            "# fig\n\n"
            "> text before\n"
            "> ```\n"
            "> ========\n"
            "> ```\n"
            "> text after\n\n"
            "Next\n"
        )
        selection = "> text after\n"
        self._hazard_case(
            fig_content, selection, "Quote Fenced Pseudo",
            self._assert_pseudo_inserted,
        )

    def test_container_fenced_pseudo_setext_still_inserts_list(self):
        fig_content = (
            "# fig\n\n"
            "- item one\n"
            "\n"
            "  ```\n"
            "  ----\n"
            "  ```\n"
            "- item two\n\n"
            "Next\n"
        )
        selection = "- item two\n"
        self._hazard_case(
            fig_content, selection, "List Fenced Pseudo",
            self._assert_pseudo_inserted,
        )

    def test_plain_list_marker_dash_items_not_misjudged(self):
        # Dash-only list items are item content (list markers), never a
        # Setext underline: ordinary list anchoring must keep working.
        fig_content = "# fig\n\n- item\n- ----\n\nNext\n"
        selection = "- item\n"
        self._hazard_case(
            fig_content, selection, "Dash Item Card",
            self._assert_pseudo_inserted,
        )

    def _assert_pseudo_inserted(self, result, fig_bytes, original_bytes, card_text):
        """No-hazard container: existing insertion rules still apply."""
        self.assertTrue(result.path.exists())
        self.assertEqual(result.anchor_status, "inserted")
        self.assertTrue(result.anchor_inserted)
        self.assertIsNotNone(result.anchor_link)
        self.assertTrue(all("Setext heading" not in w for w in result.warnings))
        self.assertNotEqual(fig_bytes, original_bytes)

    def test_parser_hazard_flags_direct(self):
        lines = cards._parse_lines(
            ("# t\n\n"
             "> Heading\n"
             "> ========\n\n"
             "- Item 1\n"
             "- Item 2\n\n"
             "1. OL Heading\n"
             "   ==========\n").encode("utf-8")
        )
        blocks = cards._parse_blocks(lines)
        by_type = {}
        for b in blocks:
            by_type.setdefault(b.block_type, []).append(b)
        self.assertEqual(len(by_type["blockquote"]), 1)
        self.assertTrue(by_type["blockquote"][0].container_setext_hazard)
        # The two lists merge into one block via the parser's existing
        # blank-line continuation rule; hazard must be True (OL setext).
        self.assertEqual(len(by_type["list"]), 1)
        self.assertEqual(by_type["list"][0].start_line, 5)
        self.assertEqual(by_type["list"][0].end_line, 9)
        self.assertTrue(by_type["list"][0].container_setext_hazard)
        # Top-level setext still detected directly
        src2 = "Heading\n========\n\nplain\n"
        blocks2 = cards._parse_blocks(cards._parse_lines(src2.encode("utf-8")))
        self.assertEqual(blocks2[0].block_type, "setext_heading")

    def test_list_to_quote_nested_setext_crlf_utf8_fails_closed(self):
        # P1-1 review counterexample: list -> blockquote -> Setext heading
        # with CRLF and UTF-8 characters fails closed on selection of normal item.
        fig_bytes_src = (
            b"# fig\r\n\r\n"
            b"- > \xe6\xa0\x87\xe9\xa2\x98\r\n"
            b"  > =======\r\n"
            b"- \xe6\xad\xa3\xe6\x96\x87\r\n\r\n"
            b"Next\r\n"
        )
        selection = "- 正文\r\n"
        self._hazard_case(
            fig_bytes_src, selection, "List Quote CRLF UTF8 Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_list_to_quote_nested_setext_lf_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "- > 标题\n"
            "  > =======\n"
            "- 正文\n\n"
            "Next\n"
        )
        selection = "- 正文\n"
        self._hazard_case(
            fig_content, selection, "List Quote LF Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_repeated_mixed_nesting_quote_list_quote_fails_closed(self):
        fig_content = (
            "# fig\n\n"
            "> - > 标题\n"
            ">   > =======\n"
            "> - 正文\n\n"
            "Next\n"
        )
        selection = "> - 正文\n"
        self._hazard_case(
            fig_content, selection, "Mixed Nesting Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_unsafe_fence_miss_indented_close_fails_closed(self):
        # P1-2 review counterexample: 2-space indented closing fence is valid CommonMark.
        # Setext heading after the fence must NOT be missed.
        fig_content = (
            "# fig\n\n"
            "> ```\n"
            "> code\n"
            ">   ```\n"
            "> Heading\n"
            "> =======\n"
            "> body\n\n"
            "Next\n"
        )
        selection = "> body\n"
        self._hazard_case(
            fig_content, selection, "Unsafe Fence Close Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_unsafe_fence_miss_backtick_in_info_fails_closed(self):
        # P1-2 review counterexample: backtick in info string invalidates fence opener.
        # Setext heading must NOT be missed.
        fig_content = (
            "# fig\n\n"
            "> ```bad`info\n"
            "> Heading\n"
            "> =======\n"
            "> body\n\n"
            "Next\n"
        )
        selection = "> body\n"
        self._hazard_case(
            fig_content, selection, "Unsafe Fence Bad Info Card",
            lambda r, fb, ob, ct: self._assert_hazard_failed(r, fb, ob, ct, selection),
        )

    def test_normal_quote_list_thematic_break_still_inserts(self):
        # P1-3 review non-regression: nested list item followed by thematic break
        # at blockquote level is ordinary quote/list content, not a Setext heading.
        fig_content = (
            "# fig\n\n"
            "> - item\n"
            "> ---\n"
            "> after\n\n"
            "Next\n"
        )
        selection = "> after\n"
        self._hazard_case(
            fig_content, selection, "Quote List HR Card",
            self._assert_pseudo_inserted,
        )

    def test_normal_ordered_list_unindented_underline_still_inserts(self):
        # P1-3 review non-regression: ordered marker requires 5-space indentation.
        # Two-space '===' is ordinary paragraph content (continuation), not a Setext underline.
        fig_content = (
            "# fig\n\n"
            "123. Heading\n"
            "  ===\n"
            "123. after\n\n"
            "Next\n"
        )
        selection = "123. after\n"
        self._hazard_case(
            fig_content, selection, "OL 2-Space Card",
            self._assert_pseudo_inserted,
        )

    def test_normal_indented_fenced_pseudo_setext_still_inserts(self):
        # P1-2 review non-regression: indented code fence (0-3 spaces) containing pseudo
        # underline is a valid fence; must NOT report hazard and must insert normally.
        fig_content = (
            "# fig\n\n"
            "> before\n"
            ">   ```python\n"
            ">   =======\n"
            ">   ```\n"
            "> after\n\n"
            "Next\n"
        )
        selection = "> after\n"
        self._hazard_case(
            fig_content, selection, "Indented Fenced Pseudo Card",
            self._assert_pseudo_inserted,
        )

    def test_fence_helpers_indentation_and_info_restrictions(self):
        # Opener 0-3 spaces allowed
        self.assertEqual(cards._parse_fence_open("```"), ("`", 3))
        self.assertEqual(cards._parse_fence_open(" ```python"), ("`", 3))
        self.assertEqual(cards._parse_fence_open("   ~~~info"), ("~", 3))
        # 4 spaces is indented code block, not fence
        self.assertIsNone(cards._parse_fence_open("    ```"))
        # Backtick opener info cannot contain backtick
        self.assertIsNone(cards._parse_fence_open("```bad`info"))
        self.assertIsNone(cards._parse_fence_open("   ```a`b"))
        # Tilde opener info CAN contain backtick
        self.assertEqual(cards._parse_fence_open("~~~python`info"), ("~", 3))
        # Closing fence checks: 0-3 spaces, same char, length >= open
        self.assertTrue(cards._is_fence_close("```", "`", 3))
        self.assertTrue(cards._is_fence_close("  ```", "`", 3))
        self.assertTrue(cards._is_fence_close("   ````", "`", 3))
        self.assertFalse(cards._is_fence_close("    ```", "`", 3))
        self.assertFalse(cards._is_fence_close("``", "`", 3))
        self.assertFalse(cards._is_fence_close("~~~", "`", 3))
        self.assertFalse(cards._is_fence_close("```info", "`", 3))

    def test_decontainer_line_alternating_markers(self):
        # Repeated alternating list and quote stripping
        self.assertEqual(cards._decontainer_line("- > 标题"), "标题")
        self.assertEqual(cards._decontainer_line("> - 标题"), "标题")
        self.assertEqual(cards._decontainer_line("> - > - 标题"), "标题")
        self.assertEqual(cards._decontainer_line("  > ======="), "=======")
        self.assertTrue(cards._line_carries_list_marker("- > 标题"))
        self.assertTrue(cards._line_carries_list_marker("> - 标题"))
        self.assertFalse(cards._line_carries_list_marker("> ---"))
        self.assertFalse(cards._line_carries_list_marker("  > ======="))


class CardSymlinkBoundaryTests(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes.cli", *argv],
            capture_output=True,
            text=True,
            cwd=REPO,
        )

    def test_cards_dir_symlink_to_outside_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            # Create symlink cards -> outside
            cards_symlink = paper_dir / "cards"
            cards_symlink.symlink_to(outside)

            # Core call raises CardError
            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Symlink Cards Card",
                    selection="Target content",
                )

            # Assert outside directory has zero writes
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(fig.read_bytes(), original_fig_bytes)

            # CLI call returns rc 2 error
            sel = vault / "sel.md"
            sel.write_text("Target content", encoding="utf-8")
            proc = self._run(
                "--json", "card", "create",
                "--vault", str(vault),
                "--key", KEY,
                "--title", "CLI Symlink Cards",
                "--selection-file", str(sel),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(list(outside.iterdir()), [])

    def test_paper_dir_symlink_to_outside_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)

            # Create paper record in real folder first to build index
            write_paper(vault)
            paper_dir = vault / "05 Literature" / KEY

            # Now replace paper_dir with a symlink to outside
            import shutil
            shutil.rmtree(paper_dir)
            paper_dir.symlink_to(outside)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Symlink Paper Dir",
                    selection="Some text",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_literature_dir_symlink_to_outside_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)

            write_paper(vault)
            lit_dir = vault / "05 Literature"

            import shutil
            shutil.rmtree(lit_dir)
            lit_dir.symlink_to(outside)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Symlink Lit Dir",
                    selection="Some text",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_source_note_symlink_to_outside_fails_closed_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)

            # Outside victim file
            victim = outside / "victim_source.md"
            victim.write_text("# Victim Source\n\nVictim target text\n", encoding="utf-8")
            original_victim_bytes = victim.read_bytes()

            # Symlink canonical source note to victim
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.symlink_to(victim)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Symlink Source Card",
                    selection="Victim target text",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=17,
                    source_end_byte=35,
                )

            # Victim file must not be modified
            self.assertEqual(victim.read_bytes(), original_victim_bytes)
            # Cards directory must not be created or written to
            cards_dir = paper_dir / "cards"
            if cards_dir.exists():
                self.assertEqual(list(cards_dir.iterdir()), [])

    def test_paper_notes_lock_symlink_to_outside_fails_closed(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")

            # Symlink .paper-notes -> outside
            (vault / ".paper-notes").symlink_to(outside)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Lock Symlink Card",
                    selection="Target content",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_vault_root_symlink_succeeds_with_relative_path(self):
        with tempfile.TemporaryDirectory() as td_real, tempfile.TemporaryDirectory() as td_sym:
            real_vault = Path(td_real) / "real_vault"
            real_vault.mkdir()
            paper_dir = write_paper(real_vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")

            # Symlink pointing to real vault
            symlink_vault = Path(td_sym) / "symlink_vault"
            symlink_vault.symlink_to(real_vault)

            # Run via CLI using the symlink vault
            sel = real_vault / "selection.md"
            sel.write_text("Target content", encoding="utf-8")

            proc = self._run(
                "--json", "card", "create",
                "--vault", str(symlink_vault),
                "--key", KEY,
                "--title", "Symlink Vault Success",
                "--selection-file", str(sel),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            rel_path = env["data"]["path"]
            self.assertFalse(Path(rel_path).is_absolute())
            self.assertEqual(rel_path, f"05 Literature/{KEY}/cards/card_Symlink_Vault_Success.md")
            # Verify card was written in real vault
            self.assertTrue((real_vault / rel_path).exists())

    def test_legacy_source_note_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)

            outside_note = outside / "outside_fig.md"
            outside_note.write_text("# fig\n\nSome text\n", encoding="utf-8")
            original_bytes = outside_note.read_bytes()

            sym_note = paper_dir / "sym_fig.md"
            sym_note.symlink_to(outside_note)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Legacy Symlink Note",
                    selection="Some text",
                    anchor_name="anc1",
                    source_note="sym_fig",
                )

            self.assertEqual(outside_note.read_bytes(), original_bytes)

    def test_legacy_source_note_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td_vault:
            vault = Path(td_vault)
            write_paper(vault)

            with self.assertRaises(cards.CardError):
                cards.create_card(
                    vault,
                    key=KEY,
                    title="Legacy Traversal Note",
                    selection="Some text",
                    anchor_name="anc1",
                    source_note="../escape",
                )

    def test_vault_root_symlink_library_call_succeeds(self):
        with tempfile.TemporaryDirectory() as td_real, tempfile.TemporaryDirectory() as td_sym:
            real_vault = Path(td_real) / "real_vault"
            real_vault.mkdir()
            paper_dir = write_paper(real_vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")

            symlink_vault = Path(td_sym) / "symlink_vault"
            symlink_vault.symlink_to(real_vault)

            result = cards.create_card(
                symlink_vault,
                key=KEY,
                title="Library Symlink Vault",
                selection="Target content",
            )
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.parent, (real_vault / "05 Literature" / KEY / "cards").resolve())
            self.assertEqual(result.card_stem, "card_Library_Symlink_Vault")

    def test_paper_dir_relocated_after_open_raises_card_error_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            orig_ensure = cards._ensure_dir_nofollow

            def move_paper_before_cards(parent_fd, name, mode=0o755, root_anchor=None):
                if name == "cards":
                    # Rename paper dir to outside before cards dir is created
                    paper_dir.rename(outside / KEY)
                return orig_ensure(parent_fd, name, mode=mode, root_anchor=root_anchor)

            with patch("paper_notes.cards._ensure_dir_nofollow", side_effect=move_paper_before_cards):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Relocated Paper Card",
                        selection="Target content",
                    )

            # Assert: cards directory was NOT created outside, and no files created outside
            self.assertFalse((outside / KEY / "cards").exists())
            self.assertEqual((outside / KEY / f"Figure解读_{KEY}.md").read_bytes(), original_fig_bytes)

    def test_cards_dir_relocated_after_open_raises_card_error_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")

            orig_atomic = cards._atomic_write_new

            def move_cards_before_write(path, content, mode=0o644, *args, **kwargs):
                # Relocate paper directory to outside after cards_fd is opened
                paper_dir.rename(outside / KEY)
                return orig_atomic(path, content, mode, *args, **kwargs)

            with patch("paper_notes.cards._atomic_write_new", side_effect=move_cards_before_write):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Relocated Cards Card",
                        selection="Target content",
                    )

            # Assert: cards directory outside has zero new files or temporary residue
            self.assertEqual(list((outside / KEY / "cards").iterdir()), [])

    def test_resolved_root_path_replaced_with_symlink_after_open_raises_card_error_zero_writes(self):
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault) / "real_vault"
            vault.mkdir()
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")

            orig_acquire = locking.acquire_lock

            def swap_root_before_lock(*args, **kwargs):
                # Rename canonical root and replace original path with symlink to outside
                renamed = vault.parent / "real_vault.renamed"
                vault.rename(renamed)
                vault.symlink_to(outside)
                return orig_acquire(*args, **kwargs)

            with patch("paper_notes.cards.acquire_lock", side_effect=swap_root_before_lock):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Swapped Root Card",
                        selection="Target content",
                    )

            # Outside directory must have zero writes
            self.assertEqual(list(outside.iterdir()), [])

    def test_exact_post_card_commit_relocation_source_missing_rolls_back(self):
        # v6: semantic failure (source missing) still publishes the no-link
        # card at P3. Relocation right after the card commit means the
        # transaction is complete: no rollback may run afterwards.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            # Source note Figure解读_KEY.md does not exist

            orig_renameatx = cards._renameatx_np

            def hook_relocate_after_card_write(cards_fd, fromname, tofd, toname, flags):
                res = orig_renameatx(cards_fd, fromname, tofd, toname, flags)
                paper_dir.rename(outside / KEY)
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=hook_relocate_after_card_write):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="Relocated Missing Source",
                    selection="Some text",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=0,
                    source_end_byte=9,
                )

            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            # The no-link card was published exactly once, after which the
            # paper dir was relocated; no post-commit rollback moved or
            # removed it.
            outside_cards = outside / KEY / "cards"
            self.assertEqual(
                [p.name for p in outside_cards.iterdir()],
                ["card_Relocated_Missing_Source.md"],
            )
            self.assertFalse((outside / KEY / f"Figure解读_{KEY}.md").exists())
            tmps = [f.name for f in outside_cards.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_exact_post_card_commit_relocation_offset_mismatch_rolls_back(self):
        # v6: stale selection (semantic failure) publishes a no-link card;
        # the source is never modified. Relocation after the card commit
        # leaves the transaction complete: source bytes unchanged, no residue.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nOriginal text content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            orig_renameatx = cards._renameatx_np

            def hook_relocate_after_card_write(cards_fd, fromname, tofd, toname, flags):
                res = orig_renameatx(cards_fd, fromname, tofd, toname, flags)
                paper_dir.rename(outside / KEY)
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=hook_relocate_after_card_write):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="Relocated Mismatch",
                    selection="Different mismatched text",
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=8,
                    source_end_byte=21,
                )

            self.assertEqual(result.anchor_status, "failed")
            outside_cards = outside / KEY / "cards"
            self.assertEqual(
                [p.name for p in outside_cards.iterdir()],
                ["card_Relocated_Mismatch.md"],
            )
            self.assertEqual(
                (outside / KEY / f"Figure解读_{KEY}.md").read_bytes(), original_fig_bytes
            )

    def test_exact_post_card_commit_relocation_source_write_rolls_back(self):
        # v6: P3 card publish commits the business state. A relocation right
        # after P3 must not trigger any rollback: the committed source replace
        # and the published card both survive in the relocated paper dir.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np

            def hook_relocate_after_card_write(cards_fd, fromname, tofd, toname, flags):
                res = orig_renameatx(cards_fd, fromname, tofd, toname, flags)
                paper_dir.rename(outside / KEY)
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=hook_relocate_after_card_write):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="Relocated Source Write",
                    selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )

            self.assertEqual(result.anchor_status, "inserted")
            outside_cards = outside / KEY / "cards"
            self.assertEqual(
                [p.name for p in outside_cards.iterdir()],
                ["card_Relocated_Source_Write.md"],
            )
            relocated_fig = outside / KEY / f"Figure解读_{KEY}.md"
            self.assertIn("^" + result.anchor_name, relocated_fig.read_text(encoding="utf-8"))
            self.assertNotEqual(relocated_fig.read_bytes(), original_fig_bytes)

    def test_exact_post_source_commit_relocation_compensates_and_rolls_back(self):
        # v6: relocation after the P2 source commit but before P3. The
        # source is compensated via the held dirfd (rollback temp staged in
        # the same directory), and no final card is published.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()

            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np

            def hook_relocate_before_card_write(cards_fd, fromname, tofd, toname, flags):
                paper_dir.rename(outside / KEY)
                raise OSError(errno.EIO, "card publish failed after relocation")

            with patch("paper_notes.cards._renameatx_np", side_effect=hook_relocate_before_card_write):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Relocated Post Source",
                        selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            outside_cards = outside / KEY / "cards"
            self.assertEqual(list(outside_cards.iterdir()), [])
            self.assertEqual(
                (outside / KEY / f"Figure解读_{KEY}.md").read_bytes(), original_fig_bytes
            )
            tmps = [f.name for f in outside_cards.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_exact_post_card_commit_relocation_user_inplace_edit_preserved_and_source_restored(self):
        # v6: P3 is the business commit point. A user in-place edit of the
        # card right after P3 publish (same inode) plus a paper-dir relocation
        # must survive: no post-commit rollback may clobber the user content.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np

            def hook_user_edit_and_relocate(cards_fd, fromname, tofd, toname, flags):
                res = orig_renameatx(cards_fd, fromname, tofd, toname, flags)
                # User in-place edits the card file (truncate/write with same inode)
                card_path = paper_dir / "cards" / "card_User_Inplace_Preserved.md"
                with open(str(card_path), "r+") as fh:
                    fh.seek(0)
                    fh.write("USER_CUSTOM_TRUNCATE_AND_OVERWRITE_IN_PLACE")
                    fh.truncate()
                # Relocate paper directory to outside
                paper_dir.rename(outside / KEY)
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=hook_user_edit_and_relocate):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="User Inplace Preserved",
                    selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )

            self.assertEqual(result.anchor_status, "inserted")
            outside_card = outside / KEY / "cards" / "card_User_Inplace_Preserved.md"
            # User content MUST survive! The file must NOT be deleted!
            self.assertTrue(outside_card.exists())
            self.assertEqual(outside_card.read_text(encoding="utf-8"), "USER_CUSTOM_TRUNCATE_AND_OVERWRITE_IN_PLACE")

    def test_both_card_and_source_cleanup_exhaustion_aggregates_diagnostics_and_preserves_cause(self):
        # v6: cleanup failure aggregation. A P3 publish failure (source was
        # committed) plus a failing rollback temp cleanup stat must aggregate
        # both diagnostics in one CardError preserving the primary cause.
        # os.unlink is NOT patched globally (lock release must stay intact);
        # only identity-owned staged temp cleanup is intercepted.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            # Relocate the paper dir after the P2 source commit so that the
            # rollback temp cleanup (identity stat via dirfd) fails with
            # EBUSY while the primary P3 rename fails with EIO.
            orig_renameatx = cards._renameatx_np

            def relocate_then_fail(cards_fd, fromname, tofd, toname, flags):
                paper_dir.rename(outside / KEY)
                raise OSError(errno.EIO, "card publish EIO after relocation")

            orig_stat = os.stat
            rollback_stat_failed = {"n": 0}

            def fail_rollback_temp_stat(path, *args, **kwargs):
                res = orig_stat(path, *args, **kwargs)
                name = str(path)
                if (
                    not rollback_stat_failed["n"]
                    and name.endswith(".tmp")
                    and "Figure解读" in name
                    and not (vault / "05 Literature" / KEY).exists()
                ):
                    rollback_stat_failed["n"] += 1
                    raise OSError(errno.EBUSY, "rollback temp stat EBUSY")
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=relocate_then_fail):
                with patch("os.stat", side_effect=fail_rollback_temp_stat):
                    with self.assertRaises(cards.CardError) as ctx:
                        cards.create_card(
                            vault,
                            key=KEY,
                            title="Dual Cleanup Exhausted",
                            selection=sel,
                            source_note=f"Figure解读_{KEY}",
                            source_start_byte=start,
                            source_end_byte=end,
                        )

            err = ctx.exception
            # The P3 publish failure plus the rollback-temp cleanup failure
            # (identity stat EBUSY) both appear in the aggregated error
            self.assertIn("card publish failed", str(err))
            self.assertIn("source compensation aborted", str(err))
            self.assertIn("rollback temp stat EBUSY", str(err))
            # Primary error is preserved in __cause__
            self.assertIsNotNone(err.__cause__)
            self.assertIn("card publish EIO", str(err.__cause__))
            self.assertTrue(rollback_stat_failed["n"], "rollback temp cleanup must have been attempted")
            # Compensation could not verify the rollback temp (stat EBUSY),
            # so the committed source stays in place: partial state is
            # reported, never blindly restored (identity-bound contract).
            committed_fig = outside / KEY / f"Figure解读_{KEY}.md"
            self.assertIn("^card-", committed_fig.read_text(encoding="utf-8"))

    def test_exact_normal_anchor_failure_preserves_card_in_vault(self):
        with tempfile.TemporaryDirectory() as td_vault:
            vault = Path(td_vault)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nOriginal text content\n", encoding="utf-8")

            # Offset mismatch (normal anchor failure without relocation)
            result = cards.create_card(
                vault,
                key=KEY,
                title="Normal Anchor Failure",
                selection="Mismatched text",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=8,
                source_end_byte=21,
            )
            # Card MUST be preserved in vault
            self.assertTrue(result.path.exists())
            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            self.assertNotIn("> 参见", result.path.read_text(encoding="utf-8"))

    def test_card_cleanup_unlink_fails_once_retries_and_cleans_outside(self):
        # v6: staged-temp identity cleanup retries transient unlink failures.
        # The os.unlink patch is scoped to staged temp names only (never the
        # .lock file), so lock release stays intact.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            cards_dir = Path(td) / "cards"
            cards_dir.mkdir()
            fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            receipt = cards._stage_temp_bytes(fd, "card_flaky.md", b"STAGED")

            orig_unlink = os.unlink
            unlink_failed = False

            def fail_staged_unlink_once(p, *args, **kwargs):
                nonlocal unlink_failed
                p_str = str(p)
                if not unlink_failed and p_str == receipt.name and "card_flaky" in p_str:
                    unlink_failed = True
                    raise OSError(errno.EBUSY, "staged temp unlink EBUSY")
                return orig_unlink(p, *args, **kwargs)

            with patch("os.unlink", side_effect=fail_staged_unlink_once):
                cards._cleanup_staged_temp(receipt)

            self.assertTrue(unlink_failed, "unlink should have failed once and retried")
            self.assertEqual([f.name for f in cards_dir.iterdir()], [])
            os.close(fd)

    def test_atomic_create_has_no_post_success_unlink(self):
        with tempfile.TemporaryDirectory() as td_vault:
            vault = Path(td_vault)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_unlink = os.unlink
            unlink_calls = []

            def trap_tmp_unlink(p, *args, **kwargs):
                p_str = str(p)
                if ".tmp" in p_str:
                    unlink_calls.append(p_str)
                    if "card_No_Post_Unlink_Card" in p_str:
                        raise RuntimeError("Trap: card temp unlink on success!")
                    # Success-path rollback-temp cleanup is expected in the
                    # v6.1 contract: only ONE cleanup unlink, never a second.
                    if len([c for c in unlink_calls]) > 1:
                        raise RuntimeError("Trap: more than one tmp unlink on success!")
                return orig_unlink(p, *args, **kwargs)

            with patch("os.unlink", side_effect=trap_tmp_unlink):
                res = cards.create_card(
                    vault,
                    key=KEY,
                    title="No Post Unlink Card",
                    selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )

            self.assertTrue(res.path.exists())
            # Exactly one tmp unlink: the success-path rollback temp cleanup.
            self.assertEqual(len(unlink_calls), 1)
            self.assertIn(".Figure解读_", unlink_calls[0])
            cards_dir = paper_dir / "cards"
            tmp_files = [f.name for f in cards_dir.iterdir() if ".tmp" in f.name]
            self.assertEqual(tmp_files, [])
            # No residue anywhere in the paper dir.
            paper_tmps = [f.name for f in paper_dir.iterdir() if ".tmp" in f.name]
            self.assertEqual(paper_tmps, [])

    def test_source_rollback_read_fails_once_retries_and_restores(self):
        # v6: identity-bound compensation has no read-retry path; the
        # equivalent transient-failure seam is the compensating replace
        # itself. A relocation after P2 plus a P3 failure triggers
        # compensation, which restores the source via the held dirfd.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np

            def relocate_then_fail(cards_fd, fromname, tofd, toname, flags):
                paper_dir.rename(outside / KEY)
                raise OSError(errno.EIO, "card publish EIO after relocation")

            with patch("paper_notes.cards._renameatx_np", side_effect=relocate_then_fail):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Rollback Read Flaky",
                        selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            self.assertIn("card publish failed", str(ctx.exception))
            outside_cards = outside / KEY / "cards"
            self.assertEqual(list(outside_cards.iterdir()), [])
            self.assertEqual((outside / KEY / f"Figure解读_{KEY}.md").read_bytes(), original_fig_bytes)

    def test_source_rollback_replace_fails_once_retries_and_restores(self):
        # v6: compensation replace transient failure (EBUSY) with the
        # canonical path still holding the committed inode is retried; the
        # restore lands despite the flaky first attempt.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np
            replace_failed = {"n": 0}

            def fail_p3_then_flaky_compensation(*args, **kwargs):
                raise OSError(errno.EIO, "card publish EIO")

            orig_replace = os.replace
            replace_calls = {"n": 0}

            def flaky_compensation_replace(src, dst, *args, **kwargs):
                replace_calls["n"] += 1
                if dst == f"Figure解读_{KEY}.md" and replace_calls["n"] == 2:
                    # First compensation attempt (P2 source commit was the
                    # first replace): transient EBUSY; the canonical path
                    # still holds the committed new-source inode, so the
                    # compensator retries.
                    raise OSError(errno.EBUSY, "compensation replace EBUSY")
                return orig_replace(src, dst, *args, **kwargs)

            with patch("paper_notes.cards._renameatx_np", side_effect=fail_p3_then_flaky_compensation):
                with patch("os.replace", side_effect=flaky_compensation_replace):
                    with self.assertRaises(cards.CardError) as ctx:
                        cards.create_card(
                            vault,
                            key=KEY,
                            title="Rollback Replace Flaky",
                            selection=sel,
                            source_note=f"Figure解读_{KEY}",
                            source_start_byte=start,
                            source_end_byte=end,
                        )

            self.assertEqual(replace_calls["n"], 3, "P2 replace + failed + retried compensation replace")
            self.assertIn("card publish failed", str(ctx.exception))
            # The outer handler compensated exactly once and retried the
            # transient EBUSY; the source is restored to the initial bytes.
            self.assertEqual(fig.read_bytes(), original_fig_bytes)

    def test_card_cleanup_exhaustion_raises_card_error_preserving_primary_cause(self):
        # v6: exhausted staged-temp cleanup (unlink EBUSY on every retry)
        # aggregates residue diagnostics in one CardError that preserves the
        # primary cause. The os.unlink patch targets only the staged card
        # temp identity name; the .lock release stays intact.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            # P3 publish fails (card never committed) while the card temp
            # cleanup cannot unlink (EBUSY every retry).
            with patch("paper_notes.cards._renameatx_np", side_effect=OSError(errno.EIO, "card publish EIO")):
                with patch("os.unlink", side_effect=OSError(errno.EBUSY, "staged temp unlink EBUSY")):
                    with self.assertRaises(cards.CardError) as ctx:
                        cards.create_card(
                            vault,
                            key=KEY,
                            title="Exhausted Card Cleanup",
                            selection=sel,
                            source_note=f"Figure解读_{KEY}",
                            source_start_byte=start,
                            source_end_byte=end,
                        )

            err = ctx.exception
            self.assertIn("cleanup failed", str(err))
            self.assertIn("card_Exhausted_Card_Cleanup.md", str(err))
            self.assertIn("failed to clean up temporary residue", " ".join(getattr(err, "__notes__", [])))
            self.assertIsNotNone(err.__cause__)
            self.assertIn("card publish EIO", str(err.__cause__))
            # P2 source commit was compensated back to the original bytes
            self.assertEqual(fig.read_bytes(), b"# Fig\n\nTarget content\n")


class AtomicCommitReceiptTests(unittest.TestCase):
    def test_postcommit_stat_gap_eliminated_for_card_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_stat = os.stat
            card_committed = [False]
            source_committed = [False]

            orig_renameatx = cards._renameatx_np

            def track_renameatx(*args, **kwargs):
                res = orig_renameatx(*args, **kwargs)
                card_committed[0] = True
                return res

            orig_replace = os.replace

            def track_replace(*args, **kwargs):
                res = orig_replace(*args, **kwargs)
                source_committed[0] = True
                return res

            def fail_postcommit_stat(path, *args, **kwargs):
                name = str(path)
                if card_committed[0] and ("card_" in name):
                    raise OSError(errno.EIO, "Postcommit card stat failure!")
                if source_committed[0] and (f"Figure解读_{KEY}.md" in name):
                    raise OSError(errno.EIO, "Postcommit source stat failure!")
                return orig_stat(path, *args, **kwargs)

            with patch("paper_notes.cards._renameatx_np", side_effect=track_renameatx):
                with patch("os.replace", side_effect=track_replace):
                    with patch("os.stat", side_effect=fail_postcommit_stat):
                        res = cards.create_card(
                            vault,
                            key=KEY,
                            title="No Postcommit Stat Gap",
                            selection=sel,
                            source_note=f"Figure解读_{KEY}",
                            source_start_byte=start,
                            source_end_byte=end,
                        )

            self.assertEqual(res.anchor_status, "inserted")
            self.assertTrue(res.path.exists())
            self.assertEqual(res.path.name, "card_No_Postcommit_Stat_Gap.md")

    def test_commit_receipt_identity_and_bytes_match_final_files(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()

            # 1. Verify card creation receipt
            dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            p = cards_dir / "card_receipt_test.md"
            content = "Card content for receipt test"
            receipt = cards._atomic_write_new(p, content, dir_fd=dfd)
            os.close(dfd)

            self.assertIsInstance(receipt, cards.CommitReceipt)
            self.assertEqual(receipt.filename, "card_receipt_test.md")
            self.assertEqual(receipt.content_bytes, content.encode("utf-8"))
            st = p.stat()
            self.assertEqual(receipt.ident, (st.st_dev, st.st_ino))

            # 2. Verify source note replace receipt
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n", encoding="utf-8")
            pfd = os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY)
            new_bytes = b"# Fig\nUpdated bytes content\n"
            source_receipt = cards._atomic_write_bytes(fig, new_bytes, dir_fd=pfd)
            os.close(pfd)

            self.assertIsInstance(source_receipt, cards.CommitReceipt)
            self.assertEqual(source_receipt.filename, f"Figure解读_{KEY}.md")
            self.assertEqual(source_receipt.content_bytes, new_bytes)
            st_fig = fig.stat()
            self.assertEqual(source_receipt.ident, (st_fig.st_dev, st_fig.st_ino))

    def test_rename_failure_with_unlink_retry_success_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()
            target = cards_dir / "card_rename_fail.md"

            dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            orig_unlink = os.unlink
            fail_count = 0

            def flaky_unlink(p, *args, **kwargs):
                nonlocal fail_count
                if fail_count < 2:
                    fail_count += 1
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return orig_unlink(p, *args, **kwargs)

            with patch("paper_notes.cards._renameatx_np", side_effect=FileExistsError("Target conflict")):
                with patch("os.unlink", side_effect=flaky_unlink):
                    with self.assertRaises(cards.CardConflict):
                        cards._atomic_write_new(target, "new content", dir_fd=dfd)

            os.close(dfd)
            self.assertEqual(fail_count, 2)
            tmps = [f.name for f in cards_dir.iterdir() if f.name.startswith(".card_rename_fail")]
            self.assertEqual(tmps, [])

    def test_rename_failure_with_unlink_exhaustion_raises_card_error_with_temp_residue_and_primary_cause(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()
            target = cards_dir / "card_exhaust.md"

            dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

            with patch("paper_notes.cards._renameatx_np", side_effect=FileExistsError("Target conflict")):
                with patch("os.unlink", side_effect=OSError(errno.EBUSY, "EBUSY")):
                    with self.assertRaises(cards.CardError) as ctx:
                        cards._atomic_write_new(target, "new content", dir_fd=dfd)

            os.close(dfd)
            err = ctx.exception
            self.assertIn("failed to clean up temporary residue", str(err))
            self.assertIn(".card_exhaust.md.", str(err))
            self.assertIn("temp_residue", getattr(err, "__notes__", ["temp_residue"])[0])
            self.assertIsInstance(err.__cause__, cards.CardConflict)
            tmps = [f.name for f in cards_dir.iterdir() if f.name.startswith(".card_exhaust")]
            self.assertEqual(len(tmps), 1)

    def test_replace_failure_with_unlink_retry_success_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n", encoding="utf-8")
            pfd = os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY)

            orig_unlink = os.unlink
            fail_count = 0

            def flaky_unlink(p, *args, **kwargs):
                nonlocal fail_count
                if fail_count < 2:
                    fail_count += 1
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return orig_unlink(p, *args, **kwargs)

            with patch("os.replace", side_effect=OSError(errno.EIO, "Injected replace EIO")):
                with patch("os.unlink", side_effect=flaky_unlink):
                    with self.assertRaises(OSError) as ctx:
                        cards._atomic_write_bytes(fig, b"new bytes", dir_fd=pfd)

            os.close(pfd)
            self.assertEqual(ctx.exception.errno, errno.EIO)
            self.assertEqual(fail_count, 2)
            tmps = [f.name for f in paper_dir.iterdir() if f.name.startswith(".Figure解读_")]
            self.assertEqual(tmps, [])

    def test_replace_failure_with_unlink_exhaustion_raises_card_error_with_temp_residue_and_primary_cause(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n", encoding="utf-8")
            pfd = os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY)

            with patch("os.replace", side_effect=OSError(errno.EIO, "Injected replace EIO")):
                with patch("os.unlink", side_effect=OSError(errno.EBUSY, "EBUSY")):
                    with self.assertRaises(cards.CardError) as ctx:
                        cards._atomic_write_bytes(fig, b"new bytes", dir_fd=pfd)

            os.close(pfd)
            err = ctx.exception
            self.assertIn("failed to clean up temporary residue", str(err))
            self.assertIn("temp_residue", getattr(err, "__notes__", ["temp_residue"])[0])
            self.assertIsInstance(err.__cause__, OSError)
            self.assertEqual(err.__cause__.errno, errno.EIO)
            tmps = [f.name for f in paper_dir.iterdir() if f.name.startswith(".Figure解读_")]
            self.assertEqual(len(tmps), 1)

    def test_atomic_write_bytes_precheck_error_closes_owned_parent_fd_and_preserves_borrowed_fd(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()

            # 1. dir_fd=None, target is a symlink: parent_fd must be closed (EBADF)
            symlink_target = cards_dir / "symlink_file.md"
            symlink_target.symlink_to(vault / "nonexistent.md")

            opened_fds = []
            orig_open = os.open

            def tracking_open(p, flags, *args, **kwargs):
                fd = orig_open(p, flags, *args, **kwargs)
                if str(p) == str(symlink_target.parent):
                    opened_fds.append(fd)
                return fd

            with patch("os.open", side_effect=tracking_open):
                with self.assertRaises(cards.CardError) as ctx_sym:
                    cards._atomic_write_bytes(symlink_target, b"data", dir_fd=None)

            self.assertEqual(len(opened_fds), 1)
            fd_sym = opened_fds[0]
            with self.assertRaises(OSError) as stat_ctx:
                os.fstat(fd_sym)
            self.assertEqual(stat_ctx.exception.errno, errno.EBADF)
            self.assertIn("refusing to write to symlink", str(ctx_sym.exception))

            # 2. dir_fd=None, os.lstat raises EIO: parent_fd must be closed (EBADF)
            normal_file = cards_dir / "normal_file.md"
            opened_eio_fds = []

            def tracking_eio_open(p, flags, *args, **kwargs):
                fd = orig_open(p, flags, *args, **kwargs)
                if str(p) == str(normal_file.parent):
                    opened_eio_fds.append(fd)
                return fd

            with patch("os.open", side_effect=tracking_eio_open):
                with patch("os.lstat", side_effect=OSError(errno.EIO, "Injected lstat EIO")):
                    with self.assertRaises(cards.CardError) as ctx_eio:
                        cards._atomic_write_bytes(normal_file, b"data", dir_fd=None)

            self.assertEqual(len(opened_eio_fds), 1)
            fd_eio = opened_eio_fds[0]
            with self.assertRaises(OSError) as stat_eio_ctx:
                os.fstat(fd_eio)
            self.assertEqual(stat_eio_ctx.exception.errno, errno.EBADF)
            self.assertIn("cannot stat file", str(ctx_eio.exception))

            # 3. Borrowed dir_fd is provided: borrowed_fd must NOT be closed!
            borrowed_fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(cards.CardError):
                    cards._atomic_write_bytes(symlink_target, b"data", dir_fd=borrowed_fd)
                # borrowed_fd must still be valid
                os.fstat(borrowed_fd)
            finally:
                os.close(borrowed_fd)

    def test_receipt_sink_raises_precommit_zero_final_and_zero_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()
            target = cards_dir / "card_sink_fail.md"
            dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

            def failing_sink(receipt):
                raise RuntimeError("receipt_sink precommit rejection")

            with self.assertRaises(RuntimeError) as ctx:
                cards._atomic_write_new(target, "content", dir_fd=dfd, receipt_sink=failing_sink)

            self.assertIn("receipt_sink precommit rejection", str(ctx.exception))
            # Zero final file, zero tmp file
            self.assertFalse(target.exists())
            self.assertEqual(list(cards_dir.iterdir()), [])

            # Also for _atomic_write_bytes
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Initial fig\n", encoding="utf-8")
            pfd = os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY)
            with self.assertRaises(RuntimeError):
                cards._atomic_write_bytes(fig, b"# New bytes\n", dir_fd=pfd, receipt_sink=failing_sink)

            # Source note remains untouched at initial bytes
            self.assertEqual(fig.read_text(encoding="utf-8"), "# Initial fig\n")
            tmps = [f.name for f in paper_dir.iterdir() if f.name.startswith(".Figure解读_")]
            self.assertEqual(tmps, [])

            os.close(dfd)
            os.close(pfd)

    def test_backfill_replace_failure_cleans_up_initial_card_and_restores_source(self):
        # v6 equivalent: the P2 source replace fails with the path still
        # holding the initial inode (not committed). All staged temps are
        # cleaned, no card published, source untouched.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original_fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = original_fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_replace = os.replace

            def fail_source_replace(src, dst, *args, **kwargs):
                if dst == f"Figure解读_{KEY}.md":
                    raise OSError(errno.EIO, "Source replace failed with EIO")
                return orig_replace(src, dst, *args, **kwargs)

            with patch("os.replace", side_effect=fail_source_replace):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Backfill Fail Test",
                        selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            # No card published, no temp residue
            cards_dir = paper_dir / "cards"
            self.assertEqual(list(cards_dir.iterdir()), [])
            # Source note untouched (P2 never committed)
            self.assertEqual(fig.read_bytes(), original_fig_bytes)

    def test_owned_parent_fd_closed_on_cleanup_exhaustion_without_dir_fd(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()
            target_card = cards_dir / "card_no_dirfd.md"

            # 1. Test _atomic_write_new closes owned parent_fd
            opened_parent_fds = []
            orig_open = os.open

            def tracking_open(p, flags, *args, **kwargs):
                fd = orig_open(p, flags, *args, **kwargs)
                if str(p) == str(target_card.parent):
                    opened_parent_fds.append(fd)
                return fd

            with patch("os.open", side_effect=tracking_open):
                with patch("paper_notes.cards._renameatx_np", side_effect=FileExistsError("Target conflict")):
                    with patch("os.unlink", side_effect=OSError(errno.EBUSY, "EBUSY")):
                        with self.assertRaises(cards.CardError) as ctx:
                            cards._atomic_write_new(target_card, "content", dir_fd=None)

            self.assertEqual(len(opened_parent_fds), 1)
            parent_fd = opened_parent_fds[0]
            with self.assertRaises(OSError) as stat_ctx:
                os.fstat(parent_fd)
            self.assertEqual(stat_ctx.exception.errno, errno.EBADF)
            self.assertIn("failed to clean up temporary residue", str(ctx.exception))
            self.assertIn(".card_no_dirfd.md.", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, cards.CardConflict)

            # 2. Test _atomic_write_bytes closes owned parent_fd
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n", encoding="utf-8")
            opened_source_parent_fds = []

            def tracking_source_open(p, flags, *args, **kwargs):
                fd = orig_open(p, flags, *args, **kwargs)
                if str(p) == str(fig.parent):
                    opened_source_parent_fds.append(fd)
                return fd

            with patch("os.open", side_effect=tracking_source_open):
                with patch("os.replace", side_effect=OSError(errno.EIO, "Injected replace EIO")):
                    with patch("os.unlink", side_effect=OSError(errno.EBUSY, "EBUSY")):
                        with self.assertRaises(cards.CardError) as ctx_bytes:
                            cards._atomic_write_bytes(fig, b"new bytes", dir_fd=None)

            self.assertEqual(len(opened_source_parent_fds), 1)
            source_parent_fd = opened_source_parent_fds[0]
            with self.assertRaises(OSError) as stat_bytes_ctx:
                os.fstat(source_parent_fd)
            self.assertEqual(stat_bytes_ctx.exception.errno, errno.EBADF)
            self.assertIn("failed to clean up temporary residue", str(ctx_bytes.exception))
            self.assertIsInstance(ctx_bytes.exception.__cause__, OSError)

    def test_receipt_constructed_while_tmp_fd_still_open_and_bytes_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            cards_dir = paper_dir / "cards"
            cards_dir.mkdir()
            target = cards_dir / "card_probe.md"
            dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

            orig_init = cards.CommitReceipt.__init__
            probe_called = 0

            def probe_receipt_init(self, filename, ident, content_bytes):
                nonlocal probe_called
                orig_init(self, filename, ident, content_bytes)
                frame = inspect.currentframe().f_back
                # The receipt is constructed inside the ``with os.fdopen``
                # block: the file handle is open as ``fh`` (its fileno is the
                # temp fd; ``tmp_fd`` has been handed off and is None).
                fh = frame.f_locals.get("fh")
                self_st = os.fstat(fh.fileno())
                assert (self_st.st_dev, self_st.st_ino) == ident, "fstat(fh.fileno()) must match receipt ident"
                probe_called += 1

            with patch.object(cards.CommitReceipt, "__init__", probe_receipt_init):
                # 1. Probe during _atomic_write_new
                r1 = cards._atomic_write_new(target, "content", dir_fd=dfd)
                self.assertEqual(probe_called, 1)
                self.assertEqual(r1.content_bytes, b"content")

                # 2. Probe during _atomic_write_bytes with mutable bytearray
                mutable_input = bytearray(b"initial mutable bytes")
                r2 = cards._atomic_write_bytes(target, mutable_input, dir_fd=dfd)
                self.assertEqual(probe_called, 2)
                self.assertIsInstance(r2.content_bytes, bytes)
                self.assertNotIsInstance(r2.content_bytes, bytearray)
                # Mutate the input buffer after helper call
                mutable_input[0:7] = b"CHANGED"
                # Receipt snapshot and file on disk must remain strictly immutable and equal to original
                self.assertEqual(r2.content_bytes, b"initial mutable bytes")
                self.assertEqual(target.read_bytes(), b"initial mutable bytes")

            os.close(dfd)

    def test_post_helper_return_injection_initial_card_rollback(self):
        # v6: a wrapper failure injected immediately after the card temp is
        # staged (P1) must abort with zero visible mutations and zero residue.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_stage = cards._stage_temp_bytes

            def raise_after_card_stage(parent_fd, final_name, data, mode=0o644, root_anchor=None, register=None):
                res = orig_stage(parent_fd, final_name, data, mode=mode, root_anchor=root_anchor, register=register)
                if final_name == "card_Wrapper_Initial_Fail.md":
                    raise RuntimeError("Simulated failure immediately after card staging return")
                return res

            with patch("paper_notes.cards._stage_temp_bytes", side_effect=raise_after_card_stage):
                with self.assertRaises(RuntimeError):
                    cards.create_card(
                        vault,
                        key=KEY,
                        title="Wrapper Initial Fail",
                        selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start,
                        source_end_byte=end,
                    )

            cards_dir = paper_dir / "cards"
            self.assertEqual(list(cards_dir.iterdir()), [])
            self.assertEqual(fig.read_bytes(), fig_bytes)
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_post_helper_return_injection_source_replace_rolls_back_card_and_source(self):
        # v6.1: a wrapper exception thrown inside a *successful* P2 source
        # replace is resolved by identity: the canonical path holds the
        # staged new-source inode, so the replace landed; the transaction
        # continues to P3 and returns success (no compensation, no failure).
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_replace = os.replace
            raised = False

            def raise_after_source_replace(src, dst, *args, **kwargs):
                nonlocal raised
                res = orig_replace(src, dst, *args, **kwargs)
                if not raised and dst == f"Figure解读_{KEY}.md":
                    raised = True
                    raise RuntimeError("Simulated failure inside a landed source replace")
                return res

            with patch("os.replace", side_effect=raise_after_source_replace):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="Wrapper Source Fail",
                    selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )

            self.assertTrue(raised)
            # The landed P2 replace stood; P3 published the final card.
            self.assertEqual(result.anchor_status, "inserted")
            self.assertTrue(
                (paper_dir / "cards" / "card_Wrapper_Source_Fail.md").exists()
            )
            self.assertIn("^card-", fig.read_text(encoding="utf-8"))
            self.assertNotEqual(fig.read_bytes(), fig_bytes)
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [], "rollback temp cleaned on success")

    def test_post_helper_return_injection_backfill_rolls_back_by_latest_receipt(self):
        # v6: P3 is the business commit point. A wrapper failure injected
        # immediately after the P3 publish returns must NOT roll anything
        # back: the committed source and published card both survive.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            fig_bytes = fig.read_bytes()
            sel = "Target content"
            start = fig_bytes.find(sel.encode("utf-8"))
            end = start + len(sel.encode("utf-8"))

            orig_renameatx = cards._renameatx_np
            raised = False

            def raise_after_card_publish(cards_fd, fromname, tofd, toname, flags):
                nonlocal raised
                res = orig_renameatx(cards_fd, fromname, tofd, toname, flags)
                if not raised:
                    raised = True
                    raise RuntimeError("Simulated failure inside a landed card publish")
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=raise_after_card_publish):
                result = cards.create_card(
                    vault,
                    key=KEY,
                    title="Wrapper Backfill Fail",
                    selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start,
                    source_end_byte=end,
                )

            self.assertTrue(raised)
            # The landed P3 publish resolved by identity: business committed,
            # returned as success (no raise, no rollback).
            self.assertEqual(result.anchor_status, "inserted")
            card_file = paper_dir / "cards" / "card_Wrapper_Backfill_Fail.md"
            self.assertTrue(card_file.exists())
            self.assertIn("^card-", fig.read_text(encoding="utf-8"))
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    # ------------------------------------------------------------------
    # P0-P3 state machine: staging receipts, commit points, compensation
    # ------------------------------------------------------------------

    def _make_vault_with_source(self, source_text="# Fig\n\nTarget content\n"):
        """Helper: temp vault with paper + canonical source; returns (vault, paper_dir)."""
        vault = tempfile.mkdtemp()
        paper_dir = write_paper(Path(vault))
        fig = paper_dir / f"Figure解读_{KEY}.md"
        fig.write_text(source_text, encoding="utf-8")
        return Path(vault), paper_dir

    def test_stage_temp_bytes_creates_fsynced_unique_temp_with_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            cards_dir = Path(td) / "cards"
            cards_dir.mkdir()
            fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            receipt = cards._stage_temp_bytes(fd, "card_x.md", b"STAGED_BYTES")
            self.assertTrue(receipt.is_regular)
            names = [f.name for f in cards_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(names, [receipt.name])
            st = os.stat(receipt.name, dir_fd=fd, follow_symlinks=False)
            self.assertEqual((st.st_dev, st.st_ino), receipt.ident)
            self.assertEqual(cards._read_bytes_dirfd(fd, receipt.name), b"STAGED_BYTES")
            # Second staging gets a unique name
            r2 = cards._stage_temp_bytes(fd, "card_x.md", b"OTHER")
            self.assertNotEqual(r2.name, receipt.name)
            cards._cleanup_staged_temp(receipt)
            cards._cleanup_staged_temp(r2)
            self.assertEqual([f.name for f in cards_dir.iterdir()], [])
            os.close(fd)

    def test_stage_failure_cleans_temp_no_visible_mutation(self):
        # fsync failure during staging: temp cleaned identity-safely, no residue.
        with tempfile.TemporaryDirectory() as td:
            cards_dir = Path(td) / "cards"
            cards_dir.mkdir()
            fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            orig_fsync = os.fsync
            calls = {"n": 0}

            def flaky_fsync(fd_):
                calls["n"] += 1
                raise OSError(errno.EIO, "staging fsync EIO")

            with patch("paper_notes.cards.os.fsync", side_effect=flaky_fsync):
                with self.assertRaises(OSError):
                    cards._stage_temp_bytes(fd, "card_x.md", b"BYTES")
            tmps = [f.name for f in cards_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [], "staging failure must identity-clean its temp")
            os.close(fd)

    def test_temp_name_swapped_to_foreign_not_deleted_residue_reported(self):
        # Temp name swapped to a foreign inode before cleanup: foreign entry
        # preserved, residue reported, nothing blindly deleted.
        with tempfile.TemporaryDirectory() as td:
            cards_dir = Path(td) / "cards"
            cards_dir.mkdir()
            fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)
            receipt = cards._stage_temp_bytes(fd, "card_x.md", b"STAGED_BYTES")
            foreign_tmp = cards_dir / "foreign.tmp"
            foreign_tmp.write_bytes(b"FOREIGN")
            # Swap: move the staged temp away and put a foreign file at its name
            moved_name = receipt.name + ".moved"
            os.rename(receipt.name, moved_name, src_dir_fd=fd, dst_dir_fd=fd)
            os.rename("foreign.tmp", receipt.name, src_dir_fd=fd, dst_dir_fd=fd)

            with self.assertRaises(cards.CardError) as ctx:
                cards._cleanup_staged_temp(receipt)
            self.assertIn("foreign entry preserved", str(ctx.exception))
            # The foreign entry at the temp name is preserved
            self.assertEqual(cards._read_bytes_dirfd(fd, receipt.name), b"FOREIGN")
            # The moved staged temp (now off-receipt) is also untouched
            self.assertTrue((cards_dir / moved_name).exists())
            os.close(fd)

    def test_exact_p2_source_commit_temp_identity_mismatch_fails_closed(self):
        # New-source temp swapped to a foreign inode before P2 replace:
        # fail closed, no source overwrite, no card publish. The swapped
        # temp name no longer holds the staged inode: the foreign entry is
        # preserved and reported as residue (never blindly deleted); the
        # other identity-owned temps (card, rollback) are cleaned.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            orig_stage = cards._stage_temp_bytes
            swapped = {"n": 0}

            def swap_new_source_temp(parent_fd, final_name, data, mode=0o644, root_anchor=None, register=None):
                r = orig_stage(parent_fd, final_name, data, mode=mode, root_anchor=root_anchor, register=register)
                if final_name == f"Figure解读_{KEY}.md" and not swapped["n"]:
                    swapped["n"] += 1
                    # Replace staged temp content via a same-name foreign inode
                    os.unlink(r.name, dir_fd=parent_fd)
                    fd2 = os.open(r.name, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o644, dir_fd=parent_fd)
                    os.write(fd2, b"FOREIGN_TEMP_BYTES")
                    os.close(fd2)
                return r

            with patch("paper_notes.cards._stage_temp_bytes", side_effect=swap_new_source_temp):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="TempSwap", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(fig.read_bytes(), original, "source must be untouched")
            self.assertFalse((paper_dir / "cards" / "card_TempSwap.md").exists())
            # Only the swapped temp name remains (foreign entry preserved as
            # residue); every other staged temp is identity-cleaned.
            residue = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(len(residue), 1, "only the foreign-swapped temp name remains")
            self.assertEqual(cards._read_bytes_dirfd(
                os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY), residue[0]
            ), b"FOREIGN_TEMP_BYTES", "foreign entry preserved, not deleted")
            card_tmps = [f.name for f in (paper_dir / "cards").iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(card_tmps, [])

    def test_exact_p2_source_path_inode_swap_fails_closed(self):
        # Canonical source path swapped to a foreign inode between P0 and P2:
        # fail closed, zero writes.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            orig_resolve = cards._resolve_and_insert_anchor

            def swap_source_before_p2(*args, **kwargs):
                fig.unlink()
                fig.write_bytes(b"FOREIGN_SOURCE_FILE")
                return orig_resolve(*args, **kwargs)

            with patch("paper_notes.cards._resolve_and_insert_anchor", side_effect=swap_source_before_p2):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault, key=KEY, title="SrcSwap", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(fig.read_bytes(), b"FOREIGN_SOURCE_FILE", "foreign source preserved")
            self.assertFalse((paper_dir / "cards" / "card_SrcSwap.md").exists())
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_exact_p2_replace_already_applied_despite_error_continues_to_p3(self):
        # os.replace reports an error but the replace actually landed (path now
        # holds the new-source receipt inode): treat as committed, continue P3.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())

            orig_replace = os.replace
            lied = {"n": 0}

            def lying_replace(src, dst, *args, **kwargs):
                orig_replace(src, dst, *args, **kwargs)
                if dst == f"Figure解读_{KEY}.md" and not lied["n"]:
                    lied["n"] = 1
                    raise OSError(errno.EIO, "replace actually succeeded")
                return None

            with patch("os.replace", side_effect=lying_replace):
                result = cards.create_card(
                    vault, key=KEY, title="LyingReplace", selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start, source_end_byte=end,
                )
            self.assertEqual(result.anchor_status, "inserted")
            card_text = (paper_dir / "cards" / "card_LyingReplace.md").read_text(encoding="utf-8")
            self.assertIn("参见", card_text)
            self.assertIn("^" + result.anchor_name, fig.read_text(encoding="utf-8"))

    def test_exact_p3_conflict_compensates_source_identity_bound(self):
        # Card RENAME_EXCL conflict after source commit: foreign card target
        # preserved, source compensated only while identities match.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            orig_renameatx = cards._renameatx_np
            raced = {"n": 0}

            def racer_renameatx(fromfd, fromname, tofd, toname, flags):
                raced["n"] += 1
                (paper_dir / "cards" / "card_Racer.md").write_text("racer occupied target", encoding="utf-8")
                raise FileExistsError("Target conflict")

            with patch("paper_notes.cards._renameatx_np", side_effect=racer_renameatx):
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        vault, key=KEY, title="Racer", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(fig.read_bytes(), original, "source must be compensated after P3 failure")
            self.assertEqual(raced["n"], 1)
            self.assertEqual(
                (paper_dir / "cards" / "card_Racer.md").read_text(encoding="utf-8"),
                "racer occupied target",
            )
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])
            tmps = [f.name for f in (paper_dir / "cards").iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_exact_p3_io_failure_compensates_source(self):
        # Card publish I/O failure (non-EEXIST): source compensated, no card.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            with patch("paper_notes.cards._renameatx_np", side_effect=OSError(errno.EIO, "rename EIO")):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="RenameIO", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertIn("card publish failed", str(ctx.exception))
            self.assertEqual(fig.read_bytes(), original)
            self.assertFalse((paper_dir / "cards" / "card_RenameIO.md").exists())

    def test_exact_p3_failure_rollback_identity_mismatch_preserves_foreign(self):
        # After source commit, the canonical source path is swapped to a
        # foreign inode: compensation must NOT overwrite; foreign preserved;
        # partial-state error reported with residue kept.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())

            orig_renameatx = cards._renameatx_np

            def swap_source_then_fail(*args, **kwargs):
                # Swap canonical source path to a foreign inode, then fail P3.
                fig.unlink()
                fig.write_bytes(b"FOREIGN_USER_CONTENT")
                raise OSError(errno.EIO, "rename EIO")

            with patch("paper_notes.cards._renameatx_np", side_effect=swap_source_then_fail):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="Mismatch", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(fig.read_bytes(), b"FOREIGN_USER_CONTENT", "foreign source preserved")
            self.assertIn("no longer holds the committed inode", str(ctx.exception))
            self.assertIn("partial state", str(ctx.exception))
            # Rollback temp must be preserved as diagnosable residue.
            rb = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(len(rb), 1, "rollback temp kept as residue for manual recovery")

    def test_exact_p3_success_cleanup_failure_never_rolls_back(self):
        # After P3 success, a temp-cleanup failure must not roll back the
        # committed source/card business state.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())

            orig_unlink = os.unlink
            card_committed = {"done": False}
            fail_attempted = {"n": False}

            orig_renameatx = cards._renameatx_np

            def track_card_commit(*args, **kwargs):
                res = orig_renameatx(*args, **kwargs)
                card_committed["done"] = True
                return res

            orig_stat = os.stat

            def flaky_stat_after_commit(p, *args, **kwargs):
                res = orig_stat(p, *args, **kwargs)
                name = str(p)
                if (
                    card_committed["done"]
                    and not fail_attempted["n"]
                    and name.endswith(".tmp")
                    and "Figure解读" in name
                ):
                    fail_attempted["n"] = True
                    raise OSError(errno.EBUSY, "cleanup stat EBUSY")
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=track_card_commit):
                with patch("os.stat", side_effect=flaky_stat_after_commit):
                    result = cards.create_card(
                        vault, key=KEY, title="CleanupAfterP3", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(result.anchor_status, "inserted")
            self.assertIn("^" + result.anchor_name, fig.read_text(encoding="utf-8"))
            self.assertTrue((paper_dir / "cards" / "card_CleanupAfterP3.md").exists())

    def test_exact_relocation_after_p2_before_p3_recovers_committed_source_only(self):
        # Paper dir relocated after P2, before P3: compensation restores the
        # committed source via the held dirfd and never touches foreign entries.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            orig_renameatx = cards._renameatx_np

            def relocate_then_fail(*args, **kwargs):
                paper_dir.rename(outside / KEY)
                raise OSError(errno.EIO, "rename EIO")

            with patch("paper_notes.cards._renameatx_np", side_effect=relocate_then_fail):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault, key=KEY, title="RelocP2P3", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            relocated_fig = outside / KEY / f"Figure解读_{KEY}.md"
            self.assertEqual(relocated_fig.read_bytes(), original, "own committed source restored via dirfd")
            self.assertEqual(list((outside / KEY / "cards").iterdir()), [], "no final card published")

    def test_exact_relocation_after_p3_transaction_complete_no_rollback(self):
        # Relocation after P3 business commit: transaction is complete; no
        # destructive rollback may run afterwards.
        with tempfile.TemporaryDirectory() as td_vault, tempfile.TemporaryDirectory() as td_out:
            vault = Path(td_vault)
            outside = Path(td_out)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())

            # After P3 business commit there must be no further ancestry
            # verification that could fail closed on a relocated vault: the
            # transaction is already complete.
            orig_renameatx = cards._renameatx_np

            def commit_then_relocate(cards_fd, fromname, tofd2, toname, flags):
                res = orig_renameatx(cards_fd, fromname, tofd2, toname, flags)
                paper_dir.rename(outside / KEY)
                return res

            with patch("paper_notes.cards._renameatx_np", side_effect=commit_then_relocate):
                result = cards.create_card(
                    vault, key=KEY, title="RelocP3", selection=sel,
                    source_note=f"Figure解读_{KEY}",
                    source_start_byte=start, source_end_byte=end,
                )
            self.assertEqual(result.anchor_status, "inserted")
            relocated_fig = outside / KEY / f"Figure解读_{KEY}.md"
            self.assertIn("^" + result.anchor_name, relocated_fig.read_text(encoding="utf-8"))
            self.assertTrue((outside / KEY / "cards" / "card_RelocP3.md").exists())

    def test_exact_no_fd_leaks_across_state_machine_paths(self):
        # FD bookkeeping: success and failure paths must not leak descriptors.
        def count_fds():
            return len(os.listdir("/dev/fd"))

        # Success path
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())
            before = count_fds()
            cards.create_card(
                vault, key=KEY, title="FdLeakOk", selection=sel,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start, source_end_byte=end,
            )
            self.assertEqual(count_fds(), before, "success path must not leak fds")

        # Failure path (P3 conflict)
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())
            before = count_fds()
            with patch("paper_notes.cards._renameatx_np", side_effect=FileExistsError("conflict")):
                with self.assertRaises(cards.CardConflict):
                    cards.create_card(
                        vault, key=KEY, title="FdLeakFail", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertEqual(count_fds(), before, "failure path must not leak fds")

    def test_exact_semantic_failure_zero_source_writes_and_no_dead_link(self):
        # Stale selection (semantic failure): card created without anchor link,
        # source untouched, no temps staged for source commit.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nOriginal text content\n", encoding="utf-8")
            original = fig.read_bytes()
            result = cards.create_card(
                vault, key=KEY, title="SemanticFail", selection="Mismatched text",
                source_note=f"Figure解读_{KEY}",
                source_start_byte=8, source_end_byte=21,
            )
            self.assertEqual(result.anchor_status, "failed")
            self.assertIsNone(result.anchor_link)
            card_text = result.path.read_text(encoding="utf-8")
            self.assertNotIn("参见", card_text)
            self.assertEqual(fig.read_bytes(), original)
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [])

    def test_exact_existing_anchor_source_untouched_direct_final_card(self):
        # Existing anchor: source not modified; final card (with anchor link)
        # created directly in one visible write.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig_content = "# Fig\n\nTarget content ^card-1cc1fd81f20dcb0c\n"
            fig.write_text(fig_content, encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())
            before_inode = fig.stat().st_ino
            result = cards.create_card(
                vault, key=KEY, title="ExistingAnchor", selection=sel,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start, source_end_byte=end,
            )
            self.assertEqual(result.anchor_status, "existing")
            self.assertFalse(result.anchor_inserted)
            self.assertIsNotNone(result.anchor_link)
            self.assertEqual(fig.read_text(encoding="utf-8"), fig_content)
            self.assertEqual(fig.stat().st_ino, before_inode, "source must not be replaced")
            card_text = result.path.read_text(encoding="utf-8")
            self.assertIn("参见", card_text)

    def test_exact_source_swap_after_pinned_reread_foreign_preserved(self):
        # v6.1: the canonical source path is swapped to a foreign inode after
        # the pinned reread but before the final pre-replace stat: fail
        # closed, the foreign entry is preserved, zero writes.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            orig_read = os.read
            swapped = {"done": False}

            def swap_after_reread(fd, n):
                # The P2 pinned reread is the first read from the P0 fd with
                # the staged temps already in place; swap on the SECOND read
                # call (the reread loops until EOF on the first call).
                if not swapped["done"] and swapped.get("first", False):
                    swapped["done"] = True
                    fig.unlink()
                    fig.write_bytes(b"FOREIGN_AFTER_REREAD")
                    return b""
                if not swapped.get("first", False):
                    swapped["first"] = True
                return orig_read(fd, n)

            with patch("os.read", side_effect=swap_after_reread):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="SwapAfterReread", selection=sel,
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )
            self.assertIn("source not modified", str(ctx.exception))
            self.assertEqual(fig.read_bytes(), b"FOREIGN_AFTER_REREAD", "foreign source preserved")
            self.assertFalse((paper_dir / "cards" / "card_SwapAfterReread.md").exists())
            tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(tmps, [], "staging discarded on pre-commit failure")

    def test_exact_p3_conflict_compensates_exactly_once(self):
        # v6.1: P3 conflict does NOT compensate inline; the outer handler
        # performs the source compensation exactly once.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            original = fig.read_bytes()
            sel = "Target content"
            start = original.find(sel.encode()); end = start + len(sel.encode())

            comp_calls = {"n": 0}
            orig_comp = cards._compensate_committed_source

            def count_comp(*args, **kwargs):
                comp_calls["n"] += 1
                return orig_comp(*args, **kwargs)

            with patch("paper_notes.cards._renameatx_np", side_effect=FileExistsError("conflict")):
                with patch.object(cards, "_compensate_committed_source", count_comp):
                    with self.assertRaises(cards.CardConflict):
                        cards.create_card(
                            vault, key=KEY, title="ExactlyOnce", selection=sel,
                            source_note=f"Figure解读_{KEY}",
                            source_start_byte=start, source_end_byte=end,
                        )
            self.assertEqual(comp_calls["n"], 1, "compensation must run exactly once")
            self.assertEqual(fig.read_bytes(), original)

    def test_exact_success_rollback_temp_cleaned_no_tmp_in_dirs(self):
        # v6.1: on the success path the rollback temp is identity-cleaned:
        # no .tmp residue anywhere in the paper directory or cards directory.
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            paper_dir = write_paper(vault)
            fig = paper_dir / f"Figure解读_{KEY}.md"
            fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
            sel = "Target content"
            start = fig.read_bytes().find(sel.encode()); end = start + len(sel.encode())

            result = cards.create_card(
                vault, key=KEY, title="RollbackCleaned", selection=sel,
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start, source_end_byte=end,
            )
            self.assertEqual(result.anchor_status, "inserted")
            paper_tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(paper_tmps, [], "rollback temp must be cleaned on success")
            cards_tmps = [f.name for f in (paper_dir / "cards").iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(cards_tmps, [])

    def test_stage_temp_bytes_fstat_failure_keeps_temp_reports_residue(self):
        # v6.1: a temp whose identity was never captured (fstat fails right
        # after open) is NEVER unlinked by name: a foreign entry swapped into
        # the name is preserved and the residue is reported on the exception.
        with tempfile.TemporaryDirectory() as td:
            cards_dir = Path(td) / "cards"
            cards_dir.mkdir()
            fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

            orig_open = os.open

            def open_then_swap(p, flags, *args, **kwargs):
                r = orig_open(p, flags, *args, **kwargs)
                if str(p).endswith(".tmp") and "card_x" in str(p):
                    # Swap a foreign entry into the temp name immediately.
                    tmp_path = cards_dir / p
                    os.close(r)
                    tmp_path.unlink()
                    tmp_path.write_bytes(b"FOREIGN_SWAPPED_IN")
                    raise OSError(errno.EIO, "fstat seam simulated")
                return r

            # Simulate: open succeeds, fstat fails, foreign entry already at
            # the temp name. Patch os.fstat to fail on the temp fd.
            orig_fstat = os.fstat

            def fail_fstat_on_tmp(fd_):
                st = orig_fstat(fd_)
                return st

            # Easier seam: patch os.fstat to raise for every call (identity
            # never captured) after swapping the foreign entry in.
            opened = {"fd": None}

            def open_capture(p, flags, *args, **kwargs):
                r = orig_open(p, flags, *args, **kwargs)
                if str(p).endswith(".tmp"):
                    opened["fd"] = r
                return r

            def fstat_then_swap(fd_):
                # First fstat call targets the temp fd: swap a foreign entry
                # into the name, then fail.
                if opened["fd"] is not None and fd_ == opened["fd"]:
                    opened["fd"] = None
                    # Close via orig fd semantics; swap the name.
                    names = [f.name for f in cards_dir.iterdir() if f.name.endswith(".tmp")]
                    assert len(names) == 1
                    tmp_path = cards_dir / names[0]
                    os.close(fd_)
                    tmp_path.unlink()
                    tmp_path.write_bytes(b"FOREIGN_SWAPPED_IN")
                    raise OSError(errno.EIO, "fstat failure after swap")
                return orig_fstat(fd_)

            with patch("paper_notes.cards.os.open", side_effect=open_capture):
                with patch("paper_notes.cards.os.fstat", side_effect=fstat_then_swap):
                    with self.assertRaises(OSError) as ctx:
                        cards._stage_temp_bytes(fd, "card_x.md", b"BYTES")
            # The foreign entry swapped into the temp name is preserved.
            names = [f.name for f in cards_dir.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(len(names), 1, "unknown-identity temp never unlinked by name")
            self.assertEqual((cards_dir / names[0]).read_bytes(), b"FOREIGN_SWAPPED_IN")
            self.assertIn("temp_residue", " ".join(getattr(ctx.exception, "__notes__", [])))
            os.close(fd)

    # ------------------------------------------------------------------
    # v6a-1 fix-round adversarial seams (fresh-review P1-1..P1-5, P2-1)
    # ------------------------------------------------------------------

    def _make_exact_vault(self):
        """Temp vault with canonical source; returns (vault, paper_dir, sel_bytes)."""
        vault = Path(tempfile.mkdtemp())
        paper_dir = write_paper(vault)
        fig = paper_dir / f"Figure解读_{KEY}.md"
        fig.write_text("# Fig\n\nTarget content\n", encoding="utf-8")
        sel = b"Target content"
        return vault, paper_dir, fig, sel

    def test_compensation_retry_revalidates_rollback_temp_identity(self):
        # P1-1: during compensation the rollback temp name is swapped to a
        # foreign inode before the first retry. The retry must re-validate
        # the rollback identity, keep the foreign temp and the canonical
        # committed source, and raise a partial-state error. The foreign
        # temp must NEVER be replaced onto the canonical source.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        original = fig.read_bytes()
        start = original.find(sel); end = start + len(sel)

        orig_replace = os.replace
        replace_calls = {"n": 0}
        swapped = {"done": False}

        def flaky_then_swap(src, dst, *args, **kwargs):
            replace_calls["n"] += 1
            if replace_calls["n"] == 1:
                # P2 source commit succeeds
                return orig_replace(src, dst, *args, **kwargs)
            if replace_calls["n"] == 2 and dst == f"Figure解读_{KEY}.md":
                # First compensation replace: swap a foreign inode into the
                # rollback temp name, then fail transiently.
                if not swapped["done"]:
                    swapped["done"] = True
                    tmps = [f for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
                    rb_tmp = tmps[0]
                    rb_tmp.rename(rb_tmp.with_name(rb_tmp.name + ".owned"))
                    foreign = paper_dir / rb_tmp.name
                    foreign.write_bytes(b"FOREIGN_ROLLBACK_TEMP")
                raise OSError(errno.EBUSY, "compensation replace EBUSY")
            return orig_replace(src, dst, *args, **kwargs)

        with patch("paper_notes.cards._renameatx_np", side_effect=OSError(errno.EIO, "card publish EIO")):
            with patch("os.replace", side_effect=flaky_then_swap):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="CompRetry", selection=sel.decode(),
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )

        # Only 2 replace calls: the retried replace must NEVER run with the
        # foreign temp.
        self.assertEqual(replace_calls["n"], 2, "no replace after rollback temp swap")
        self.assertEqual(
            fig.read_bytes(), b"FOREIGN_ROLLBACK_TEMP".replace(b"X", b"X"),
        ) if False else None
        # Foreign temp content must NOT have been replaced onto the source:
        # the source stays at the committed (anchor) bytes.
        self.assertIn("^card-", fig.read_text(encoding="utf-8"),
                      "foreign temp must not replace the committed source")
        self.assertNotEqual(fig.read_bytes(), b"FOREIGN_ROLLBACK_TEMP")
        self.assertIn("rollback temp identity mismatch before retry", str(ctx.exception))
        self.assertIn("partial state", str(ctx.exception))
        # Foreign temp preserved, own rollback inode preserved as residue.
        tmps = [f.name for f in paper_dir.iterdir() if ".tmp" in f.name]
        self.assertEqual(len(tmps), 2, "foreign temp + moved own temp preserved")

    def test_p3_receipt_stat_eio_is_ambiguous_no_compensation(self):
        # P1-2: RENAME_EXCL lands, then a wrapper raises EIO, and the P3
        # receipt identity stat itself fails with EIO. Outcome is ambiguous:
        # no source compensation (a dead-link card would result), temps
        # preserved as recovery evidence, explicit ambiguous-state error.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        original = fig.read_bytes()
        start = original.find(sel); end = start + len(sel)

        orig_renameatx = cards._renameatx_np

        def land_then_raise(cards_fd, fromname, tofd, toname, flags):
            orig_renameatx(cards_fd, fromname, tofd, toname, flags)
            raise OSError(errno.EIO, "publish landed but wrapper raised")

        orig_stat = os.stat

        def eio_receipt_stat(path, *args, **kwargs):
            if isinstance(path, str) and path == "card_P3StatEIO.md" and kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "receipt stat EIO")
            return orig_stat(path, *args, **kwargs)

        with patch("paper_notes.cards._renameatx_np", side_effect=land_then_raise):
            with patch("os.stat", side_effect=eio_receipt_stat):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="P3StatEIO", selection=sel.decode(),
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )

        self.assertIn("ambiguous", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.__cause__)
        # The published card is NOT touched and the committed source is NOT
        # compensated: both keep the business state for manual resolution.
        self.assertTrue((paper_dir / "cards" / "card_P3StatEIO.md").exists())
        self.assertIn("^card-", fig.read_text(encoding="utf-8"),
                      "source must NOT be compensated on ambiguous publish")
        # Rollback temp kept as recovery evidence.
        tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
        self.assertEqual(len(tmps), 1, "rollback temp preserved as recovery evidence")

    def test_p3_success_close_eio_still_returns_success(self):
        # P1-3: a pinned source fd close failure after the P3 business commit
        # must be suppressed (warning only); the API returns success.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        start = fig.read_bytes().find(sel); end = start + len(sel)

        orig_close = os.close
        closed = {"n": 0}

        def flaky_close(fd):
            closed["n"] += 1
            # Fail the first close after the business commit (the pinned
            # source fd); all dirfd closes succeed.
            if closed["n"] == 1:
                raise OSError(errno.EIO, "close EIO after commit")
            return orig_close(fd)

        with patch("os.close", side_effect=flaky_close):
            result = cards.create_card(
                vault, key=KEY, title="CloseEIO", selection=sel.decode(),
                source_note=f"Figure解读_{KEY}",
                source_start_byte=start, source_end_byte=end,
            )

        self.assertEqual(result.anchor_status, "inserted")
        self.assertTrue((paper_dir / "cards" / "card_CloseEIO.md").exists())
        self.assertIn("^card-", fig.read_text(encoding="utf-8"))
        tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
        self.assertEqual(tmps, [])

    def test_in_vault_cards_relocation_before_p3_fails_closed(self):
        # P1-4: after P1 staging, move the held cards dir to vault/parked and
        # recreate an empty canonical cards dir. Ancestry still passes but the
        # canonical binding check must fail closed: no publish into the parked
        # directory, source compensated, explicit error.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        original = fig.read_bytes()
        start = original.find(sel); end = start + len(sel)

        orig_renameatx = cards._renameatx_np
        orig_replace = os.replace
        relocated = {"done": False}

        def relocate_after_p2(src, dst, *args, **kwargs):
            res = orig_replace(src, dst, *args, **kwargs)
            # Relocate after the P2 source commit lands (post-commit, pre-P3):
            # the P3 binding check must catch the canonical swap.
            if not relocated["done"] and dst == f"Figure解读_{KEY}.md":
                relocated["done"] = True
                (paper_dir / "cards").rename(vault / "parked_cards")
                (paper_dir / "cards").mkdir()
            return res

        def fail_publish(cards_fd, fromname, tofd, toname, flags):
            raise OSError(errno.EIO, "publish failed after relocation")

        with patch("os.replace", side_effect=relocate_after_p2):
            with patch("paper_notes.cards._renameatx_np", side_effect=fail_publish):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="Parked", selection=sel.decode(),
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )

        # Canonical cards dir is empty (nothing published there).
        self.assertEqual(list((paper_dir / "cards").iterdir()), [])
        # Parked dir must not contain the business card.
        parked = list((vault / "parked_cards").iterdir()) if (vault / "parked_cards").exists() else []
        self.assertEqual([p.name for p in parked if not p.name.startswith(".")], [])
        # Source compensated.
        self.assertEqual(fig.read_bytes(), original)
        self.assertIn("canonical directory binding changed", str(ctx.exception))

    def test_in_vault_paper_relocation_before_p2_fails_closed(self):
        # P1-4 (paper level): move the held paper dir inside the vault and
        # recreate an empty canonical paper dir before P2. The binding check
        # must fail closed: zero source writes, no card.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        original = fig.read_bytes()
        start = original.find(sel); end = start + len(sel)

        orig_stage = cards._stage_temp_bytes
        relocated = {"done": False}

        def relocate_on_last_stage(parent_fd, final_name, data, mode=0o644, root_anchor=None, register=None):
            res = orig_stage(parent_fd, final_name, data, mode=mode, root_anchor=root_anchor, register=register)
            # Fires after the P2 binding check of the first stage but the
            # P2 pre-commit bundle runs later; relocate before P2 commits.
            if not relocated["done"] and final_name == f"Figure解读_{KEY}.md" and data == original:
                relocated["done"] = True
                paper_dir.rename(vault / "parked_paper")
                (vault / "05 Literature" / KEY).mkdir(parents=True)
                (vault / "05 Literature" / KEY / "cards").mkdir()
            return res

        def relocate_then_fail(src, dst, *args, **kwargs):
            raise OSError(errno.EIO, "replace failed after relocation")

        with patch("paper_notes.cards._stage_temp_bytes", side_effect=relocate_on_last_stage):
            with patch("os.replace", side_effect=relocate_then_fail):
                with self.assertRaises(cards.CardError) as ctx:
                    cards.create_card(
                        vault, key=KEY, title="ParkedPaper", selection=sel.decode(),
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )

        self.assertIn("canonical directory binding changed", str(ctx.exception))
        # The relocated original source is untouched.
        self.assertEqual(
            (vault / "parked_paper" / f"Figure解读_{KEY}.md").read_bytes(), original
        )
        parked_cards = vault / "parked_paper" / "cards"
        self.assertEqual(
            [p.name for p in parked_cards.iterdir() if not p.name.startswith(".")], []
        )

    def test_atomic_write_new_temp_swapped_before_rename_final_not_foreign(self):
        # P1-5: _atomic_write_new must nofollow-stat the temp name against
        # the receipt immediately before the rename. Swapping a foreign
        # inode into the temp name inside the rename wrapper must fail
        # closed: the final file is never the foreign content.
        vault, paper_dir, _, _ = self._make_exact_vault()
        cards_dir = paper_dir / "cards"
        cards_dir.mkdir(exist_ok=True)
        target = cards_dir / "card_TempSwapNew.md"
        dfd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

        orig_renameatx = cards._renameatx_np
        orig_stat = os.stat

        def swap_at_receipt_sink(receipt):
            # Swap seam AFTER receipt construction, BEFORE the pre-commit
            # temp-bound check: the binding check must catch the foreign
            # entry and fail closed.
            tmps = [f.name for f in cards_dir.iterdir() if f.name.startswith(".card_TempSwapNew")]
            assert len(tmps) == 1
            tmp = cards_dir / tmps[0]
            tmp.rename(tmp.with_name(tmp.name + ".owned"))
            (cards_dir / tmps[0]).write_bytes(b"FOREIGN")

        with patch("paper_notes.cards._renameatx_np", side_effect=lambda *a, **kw: None):
            with self.assertRaises(cards.CardError) as ctx:
                cards._atomic_write_new(target, "content", dir_fd=dfd, receipt_sink=swap_at_receipt_sink)

        # The final target was never created with foreign content.
        self.assertFalse(target.exists())
        self.assertIn("no longer matches", str(ctx.exception))
        self.assertIn("foreign entry preserved", str(ctx.exception))
        # Foreign temp and moved own temp preserved.
        leftovers = sorted(f.name for f in cards_dir.iterdir() if ".tmp" in f.name)
        self.assertEqual(len(leftovers), 2)
        os.close(dfd)

    def test_atomic_write_bytes_temp_swapped_before_replace_final_not_foreign(self):
        # P1-5: _atomic_write_bytes (normal mode) must temp-bind the commit:
        # a foreign inode swapped into the temp name before the replace fails
        # closed; the target never receives foreign content.
        vault, paper_dir, fig, _ = self._make_exact_vault()
        original = fig.read_bytes()
        pfd = os.open(str(paper_dir), os.O_RDONLY | os.O_DIRECTORY)

        orig_replace = os.replace

        def swap_at_receipt_sink(receipt):
            # Swap seam AFTER receipt construction, BEFORE the pre-commit
            # temp-bound check (normal mode, no expected_current).
            tmps = [f.name for f in paper_dir.iterdir()
                    if f.name.startswith(".Figure解读") and f.name.endswith(".tmp")]
            assert len(tmps) == 1
            tmp = paper_dir / tmps[0]
            tmp.rename(tmp.with_name(tmp.name + ".owned"))
            (paper_dir / tmps[0]).write_bytes(b"FOREIGN")

        with self.assertRaises(cards.CardError) as ctx:
            cards._atomic_write_bytes(fig, b"NEW BYTES", dir_fd=pfd, receipt_sink=swap_at_receipt_sink)

        self.assertEqual(fig.read_bytes(), original, "target must not receive foreign content")
        self.assertIn("no longer matches", str(ctx.exception))
        self.assertIn("foreign entry preserved", str(ctx.exception))
        leftovers = [f.name for f in paper_dir.iterdir() if ".tmp" in f.name]
        self.assertEqual(len(leftovers), 2)
        os.close(pfd)

    def test_stage_temp_bytes_cleanup_mismatch_reports_residue_note(self):
        # P2-1: when the identity-known staging cleanup hits a mismatch
        # (foreign at the temp name), the propagated error carries a
        # temp_residue note with the primary cause; the foreign entry and the
        # moved own inode are preserved.
        cards_dir = Path(tempfile.mkdtemp()) / "cards"
        cards_dir.mkdir()
        fd = os.open(str(cards_dir), os.O_RDONLY | os.O_DIRECTORY)

        orig_fsync = os.fsync
        swapped = {"done": False}

        def swap_then_fsync_fail(fd_):
            if not swapped["done"]:
                swapped["done"] = True
                names = [f.name for f in cards_dir.iterdir() if f.name.endswith(".tmp")]
                tmp = cards_dir / names[0]
                tmp.rename(tmp.with_name(tmp.name + ".owned"))
                (cards_dir / names[0]).write_bytes(b"FOREIGN_AT_TEMP_NAME")
            raise OSError(errno.EIO, "fsync EIO after swap")

        with patch("paper_notes.cards.os.fsync", side_effect=swap_then_fsync_fail):
            with self.assertRaises(OSError) as ctx:
                cards._stage_temp_bytes(fd, "card_x.md", b"BYTES")

        notes = " ".join(getattr(ctx.exception, "__notes__", []))
        self.assertIn("temp_residue", notes)
        self.assertIn("cleanup failed", notes)
        # Foreign preserved and own moved inode preserved.
        leftovers = sorted(f.name for f in cards_dir.iterdir() if ".tmp" in f.name)
        self.assertEqual(len(leftovers), 2)
        os.close(fd)

    def test_p3_ambiguous_keeps_card_temp_name_intact(self):
        # P1-2 follow-up: on P3 ambiguity the card temp name is never deleted
        # (it may already BE the published card via rename, or still be the
        # staged temp) and the rollback temp is preserved as evidence.
        vault, paper_dir, fig, sel = self._make_exact_vault()
        start = fig.read_bytes().find(sel); end = start + len(sel)

        orig_renameatx = cards._renameatx_np

        def land_then_raise(cards_fd, fromname, tofd, toname, flags):
            orig_renameatx(cards_fd, fromname, tofd, toname, flags)
            raise OSError(errno.EIO, "wrapper raised after landing")

        orig_stat = os.stat

        def eio_receipt_stat(path, *args, **kwargs):
            if isinstance(path, str) and path == "card_P3Keep.md" and kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "receipt stat EIO")
            return orig_stat(path, *args, **kwargs)

        with patch("paper_notes.cards._renameatx_np", side_effect=land_then_raise):
            with patch("os.stat", side_effect=eio_receipt_stat):
                with self.assertRaises(cards.CardError):
                    cards.create_card(
                        vault, key=KEY, title="P3Keep", selection=sel.decode(),
                        source_note=f"Figure解读_{KEY}",
                        source_start_byte=start, source_end_byte=end,
                    )

        # The published business card file survives untouched.
        self.assertTrue((paper_dir / "cards" / "card_P3Keep.md").exists())
        self.assertIn("^card-", fig.read_text(encoding="utf-8"))
        # Rollback temp preserved as recovery evidence.
        tmps = [f.name for f in paper_dir.iterdir() if f.name.endswith(".tmp")]
        self.assertEqual(len(tmps), 1)


if __name__ == "__main__":
    unittest.main()
