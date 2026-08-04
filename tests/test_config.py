"""Private configuration tests (Task 16).

Frozen after the first red run; do not weaken or delete assertions.
Secrets in these tests are synthetic markers assembled at runtime so
the repository secret scanner never sees a literal credential. The
config lives outside the vault at
``~/Library/Application Support/paper-notes/config.json`` with mode
``0600``; JSON output and exception messages must redact the secret.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes import config

SECRET = "sk-" + "easyscholar-" + "test-secret-1234567890"
SECOND_SECRET = "sk-" + "second-" + "marker-0987654321"

REPO = Path(__file__).resolve().parents[1]


class DefaultPathTest(unittest.TestCase):
    def test_default_path_is_macos_application_support(self):
        with mock.patch("paper_notes.config.Path.home", return_value=Path("/Users/tester")):
            self.assertEqual(
                config.default_config_path(),
                Path(
                    "/Users/tester/Library/Application Support/paper-notes/config.json"
                ),
            )

    def test_env_override_redirects_path(self):
        with mock.patch.dict(os.environ, {"PAPER_NOTES_CONFIG": "/tmp/x/cfg.json"}):
            self.assertEqual(config.default_config_path(), Path("/tmp/x/cfg.json"))


class SaveLoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "config.json"

    def test_save_creates_parents_and_mode_0600(self):
        config.save_config(config.Config(easyscholar_secret_key=SECRET), path=self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_save_then_load_roundtrip(self):
        config.save_config(config.Config(easyscholar_secret_key=SECRET), path=self.path)
        loaded = config.load_config(path=self.path)
        self.assertEqual(loaded.easyscholar_secret_key, SECRET)

    def test_load_missing_returns_empty_config(self):
        loaded = config.load_config(path=self.path)
        self.assertIsNone(loaded.easyscholar_secret_key)

    def test_load_ignores_unknown_keys(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"easyscholar_secret_key": SECRET, "future_field": 1})
        )
        loaded = config.load_config(path=self.path)
        self.assertEqual(loaded.easyscholar_secret_key, SECRET)

    def test_load_corrupt_json_raises_config_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(config.ConfigError):
            config.load_config(path=self.path)

    def test_rewrite_existing_file_keeps_mode_0600(self):
        config.save_config(config.Config(easyscholar_secret_key=SECRET), path=self.path)
        config.save_config(
            config.Config(easyscholar_secret_key=SECOND_SECRET), path=self.path
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        loaded = config.load_config(path=self.path)
        self.assertEqual(loaded.easyscholar_secret_key, SECOND_SECRET)

    def test_save_without_key_writes_null(self):
        config.save_config(config.Config(), path=self.path)
        loaded = config.load_config(path=self.path)
        self.assertIsNone(loaded.easyscholar_secret_key)


class RedactionTest(unittest.TestCase):
    def test_mask_secret_never_contains_value(self):
        masked = config.mask_secret(SECRET)
        self.assertEqual(masked, "****")
        self.assertNotIn(SECRET, masked)

    def test_redacted_config_hides_secret(self):
        cfg = config.Config(easyscholar_secret_key=SECRET)
        redacted = config.redacted_config(cfg)
        self.assertNotIn(SECRET, json.dumps(redacted))

    def test_redact_text_replaces_secret(self):
        text = f"easyscholar failed for key {SECRET}"
        redacted = config.redact_text(text, SECRET)
        self.assertNotIn(SECRET, redacted)
        self.assertIn("****", redacted)

    def test_redact_text_without_secret_passthrough(self):
        self.assertEqual(config.redact_text("plain message", None), "plain message")


class ConfigReprTest(unittest.TestCase):
    """repr/str must never render the secret value (repair R1)."""

    def test_repr_never_contains_secret(self):
        cfg = config.Config(easyscholar_secret_key=SECRET)
        self.assertNotIn(SECRET, repr(cfg))
        self.assertNotIn(SECRET, str(cfg))

    def test_repr_shows_mask_when_secret_present(self):
        cfg = config.Config(easyscholar_secret_key=SECRET)
        self.assertIn(config.MASK, repr(cfg))
        self.assertIn(config.MASK, str(cfg))

    def test_repr_without_secret_shows_none(self):
        self.assertEqual(
            repr(config.Config()), "Config(easyscholar_secret_key=None)"
        )


class ConfigImportCliTest(unittest.TestCase):
    """CLI integration for ``config easyscholar import-zotero``.

    Runs through a real subprocess with an injected config path and a
    synthetic Zotero ``prefs.js``; the secret never appears on stdout.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"
        self.prefs = self.root / "prefs.js"
        self.prefs.write_text(
            'user_pref("extensions.zotero.easyscholar.secretKey", "%s");\n'
            'user_pref("extensions.zotero.plain", "value");\n' % SECRET
        )

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

    def test_dry_run_reports_found_without_writing_or_leaking(self):
        result = self._run(
            "config", "easyscholar", "import-zotero", "--dry-run", "--prefs", str(self.prefs)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["data"]["found"])
        self.assertFalse(payload["data"]["written"])
        self.assertNotIn(SECRET, result.stdout)
        self.assertFalse(self.cfg_path.exists())

    def test_dry_run_not_found(self):
        self.prefs.write_text('user_pref("extensions.zotero.plain", "value");\n')
        result = self._run(
            "config", "easyscholar", "import-zotero", "--dry-run", "--prefs", str(self.prefs)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["data"]["found"])

    def test_without_confirmation_returns_needs_confirmation(self):
        result = self._run(
            "config", "easyscholar", "import-zotero", "--prefs", str(self.prefs)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertNotIn(SECRET, result.stdout)
        self.assertFalse(self.cfg_path.exists())

    def test_confirmed_import_writes_config_without_leaking(self):
        result = self._run(
            "config",
            "easyscholar",
            "import-zotero",
            "--confirmed",
            "--prefs",
            str(self.prefs),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["data"]["written"])
        self.assertNotIn(SECRET, result.stdout)
        self.assertEqual(
            config.load_config(path=self.cfg_path).easyscholar_secret_key, SECRET
        )

    def test_dry_run_and_confirmed_are_mutually_exclusive(self):
        result = self._run(
            "config",
            "easyscholar",
            "import-zotero",
            "--dry-run",
            "--confirmed",
            "--prefs",
            str(self.prefs),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SECRET, result.stdout)

    def test_import_reports_missing_prefs_file(self):
        result = self._run(
            "config", "easyscholar", "import-zotero", "--dry-run", "--prefs", str(self.root / "nope.js")
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")


if __name__ == "__main__":
    unittest.main()
