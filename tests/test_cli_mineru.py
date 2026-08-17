"""CLI integration tests for ``config mineru``.

Runs through a real subprocess with an injected config path. The key is
delivered on stdin only; it must never appear on argv, stdout, or stderr.
Synthetic markers are assembled at runtime so the repository secret
scanner never sees a literal credential.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper_notes import config

MINERU_SECRET = "sk-mineru-" + "test-token-" + "1234567890abcdef"
ES_SECRET = "sk-" + "easyscholar-" + "test-secret-1234567890"

REPO = Path(__file__).resolve().parents[1]


class MineruKeyCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"

    def _run(self, *argv, input_text=None):
        env = dict(os.environ)
        env["PAPER_NOTES_CONFIG"] = str(self.cfg_path)
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *argv],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            input=input_text,
        )

    def test_status_reports_not_configured(self):
        result = self._run("config", "mineru", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["data"]["configured"])

    def test_set_key_stdin_writes_config_without_leaking(self):
        result = self._run(
            "config", "mineru", "set-key", "--stdin", input_text=MINERU_SECRET + "\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["data"]["configured"])
        self.assertNotIn(MINERU_SECRET, result.stdout)
        self.assertNotIn(MINERU_SECRET, result.stderr)
        self.assertEqual(
            config.load_config(path=self.cfg_path).mineru_key, MINERU_SECRET
        )

    def test_set_key_requires_stdin_flag(self):
        result = self._run(
            "config", "mineru", "set-key", input_text=MINERU_SECRET + "\n"
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn(MINERU_SECRET, result.stdout)
        self.assertNotIn(MINERU_SECRET, result.stderr)

    def test_set_key_empty_stdin_is_error(self):
        result = self._run(
            "config", "mineru", "set-key", "--stdin", input_text="  \n"
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertFalse(self.cfg_path.exists())

    def test_delete_key_idempotent_when_unset(self):
        result = self._run("config", "mineru", "delete-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["data"]["configured"])

    def test_delete_key_removes_and_preserves_easyscholar(self):
        config.save_config(
            config.Config(easyscholar_secret_key=ES_SECRET, mineru_key=MINERU_SECRET),
            path=self.cfg_path,
        )
        result = self._run("config", "mineru", "delete-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["data"]["configured"])
        cfg = config.load_config(path=self.cfg_path)
        self.assertIsNone(cfg.mineru_key)
        self.assertEqual(cfg.easyscholar_secret_key, ES_SECRET)

    def test_status_reports_configured_after_set(self):
        result = self._run(
            "config", "mineru", "set-key", "--stdin", input_text=MINERU_SECRET + "\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self._run("config", "mineru", "status")
        payload = json.loads(status.stdout)
        self.assertTrue(payload["data"]["configured"])
        self.assertNotIn(MINERU_SECRET, status.stdout)

    def test_replace_key_overwrites(self):
        second = "sk-mineru-" + "second-" + "token-9876543210"
        self._run("config", "mineru", "set-key", "--stdin", input_text=MINERU_SECRET + "\n")
        result = self._run(
            "config", "mineru", "set-key", "--stdin", input_text=second + "\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.load_config(path=self.cfg_path).mineru_key, second)


def _make_pdf(path, *, pages=1):
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), "hello")
    doc.save(str(path))
    doc.close()
    return path


def _write_item(vault, key, *, pdf=True, existing_md=False):
    d = vault / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(
        [
            "schema_version: 1",
            "paper_id: 550e8400-e29b-41d4-a716-446655440000",
            f"citation_key: {key}",
            "item_type: article-journal",
            "title: An example paper",
            "authors:",
            "- family: Smith",
            "  given: John",
            "publication_date: 2026-05-01",
            "year: 2026",
            f"pdf_status: {'available' if pdf else 'missing'}",
            "reading_status: unread",
        ]
    )
    (d / f"{key}.md").write_text(f"---\n{fm}\n---\n# body\n", encoding="utf-8")
    if pdf:
        _make_pdf(d / f"{key}.pdf")
    if existing_md:
        (d / f"minerUmd_{key}.md").write_text("# old\n", encoding="utf-8")


class MineruConvertCliTest(unittest.TestCase):
    """CLI surface for ``mineru convert`` (no-network paths only)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.cfg_path = self.root / "config.json"

    def _run(self, *argv):
        env = dict(os.environ)
        env["PAPER_NOTES_CONFIG"] = str(self.cfg_path)
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *argv],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_convert_no_key_is_error_rc2(self):
        _write_item(self.vault, "smithExample2026")
        result = self._run(
            "mineru", "convert", "--vault", str(self.vault), "--key", "smithExample2026"
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("MinerU key", payload["errors"][0]["message"])

    def test_convert_no_pdf_is_error_rc2(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026", pdf=False)
        result = self._run(
            "mineru", "convert", "--vault", str(self.vault), "--key", "smithExample2026"
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_convert_existing_md_without_confirm_is_error_rc2(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026", existing_md=True)
        result = self._run(
            "mineru", "convert", "--vault", str(self.vault), "--key", "smithExample2026"
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_dry_run_returns_needs_confirmation_rc0(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026", existing_md=True)
        result = self._run(
            "mineru", "convert", "--dry-run", "--vault", str(self.vault),
            "--key", "smithExample2026",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertTrue(payload["data"]["confirmation_token"])
        self.assertEqual(payload["data"]["plan"]["action"], "mineru_convert")
        self.assertNotIn(MINERU_SECRET, result.stdout)

    def test_dry_run_without_existing_md_is_error_rc2(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026")
        result = self._run(
            "mineru", "convert", "--dry-run", "--vault", str(self.vault),
            "--key", "smithExample2026",
        )
        self.assertEqual(result.returncode, 2)

    def test_combining_dry_run_and_confirm_token_is_error_rc2(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026", existing_md=True)
        result = self._run(
            "mineru", "convert", "--dry-run", "--confirm-token", "x",
            "--vault", str(self.vault), "--key", "smithExample2026",
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_fresh_convert_with_token_is_conflict_rc3(self):
        config.save_config(config.Config(mineru_key=MINERU_SECRET), path=self.cfg_path)
        _write_item(self.vault, "smithExample2026")
        result = self._run(
            "mineru", "convert", "--confirm-token", "a" * 64,
            "--vault", str(self.vault), "--key", "smithExample2026",
        )
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
