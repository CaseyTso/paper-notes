"""Topic MOC creation tests (moc create).

Frozen after the first green run; do not weaken or delete assertions.
MOC notes live in ``05 Literature/MOCs/`` and carry ``kind: topic-moc``
frontmatter plus an empty four-column table.

No network, no real vault: synthetic notes in temporary directories.
"""

import tempfile
import unittest
from pathlib import Path

from paper_notes.paths import moc_folder, LITERATURE_ROOT
from paper_notes import mocs


class MocPathTests(unittest.TestCase):
    def test_moc_folder_is_under_literature_root(self) -> None:
        root = Path("/tmp/fake-vault")
        self.assertEqual(moc_folder(root), root / LITERATURE_ROOT / "MOCs")


class MocCreateCoreTests(unittest.TestCase):
    """Tests for ``mocs.create_moc`` — atomic create, never overwrite."""

    EMPTY_TABLE_BODY = (
        "---\n"
        "kind: topic-moc\n"
        "title: 拟时序分析\n"
        "---\n\n"
        "| Title | Figure解读 | 总结 | 卡片 |\n"
        "| ----- | -------- | --- | --- |\n"
    )

    def test_create_moc_happy_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = mocs.create_moc(root, title="拟时序分析")
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.name, "拟时序分析.md")
            self.assertEqual(result.title, "拟时序分析")
            text = result.path.read_text(encoding="utf-8")
            self.assertEqual(text, self.EMPTY_TABLE_BODY)

    def test_folder_created_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mocs.create_moc(root, title="测试主题")
            self.assertTrue(moc_folder(root).is_dir())

    def test_duplicate_filename_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mocs.create_moc(root, title="重复主题")
            with self.assertRaises(mocs.MocConflict):
                mocs.create_moc(root, title="重复主题")

    def test_empty_title_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(mocs.MocError):
                mocs.create_moc(root, title="   ")

    def test_title_with_slash_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(mocs.MocError):
                mocs.create_moc(root, title="a/b")

    def test_title_with_backslash_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(mocs.MocError):
                mocs.create_moc(root, title="a\\b")

    def test_title_with_dotdot_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(mocs.MocError):
                mocs.create_moc(root, title="..")

    def test_title_dot_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(mocs.MocError):
                mocs.create_moc(root, title=".")


class MocCreateCliTests(unittest.TestCase):
    """CLI subprocess tests for ``paper-notes moc create``."""

    REPO = Path(__file__).resolve().parents[1]

    def _run(self, *argv):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-m", "paper_notes.cli", *argv],
            capture_output=True,
            text=True,
            cwd=self.REPO,
        )

    def test_cli_json_success(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proc = self._run(
                "--json",
                "moc",
                "create",
                "--vault",
                str(root),
                "--title",
                "拟时序分析",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "success")
            self.assertEqual(env["data"]["title"], "拟时序分析")
            self.assertEqual(env["data"]["path"], "05 Literature/MOCs/拟时序分析.md")
            self.assertEqual(env["data"]["kind"], "topic-moc")
            self.assertTrue((root / env["data"]["path"]).exists())

    def test_cli_conflict_exit_code(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._run(
                "--json", "moc", "create", "--vault", str(root), "--title", "重复主题"
            )
            proc = self._run(
                "--json", "moc", "create", "--vault", str(root), "--title", "重复主题"
            )
            self.assertEqual(proc.returncode, 3)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "conflict")
            self.assertEqual(env["errors"][0]["code"], "conflict")

    def test_cli_empty_title_exit_code(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proc = self._run(
                "--json", "moc", "create", "--vault", str(root), "--title", "   "
            )
            self.assertEqual(proc.returncode, 2)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "error")


if __name__ == "__main__":
    unittest.main()
