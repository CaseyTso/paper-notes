"""MinerU key private-config model tests.

The MinerU key lives in the same user-local 0600 config as the
EasyScholar SecretKey and is never rendered by repr/str/redacted config,
never written by the plugin, and never passed on argv (set-key reads
stdin). Synthetic markers are assembled at runtime so the repository
secret scanner never sees a literal credential.
"""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from paper_notes import config

MINERU_SECRET = "sk-mineru-" + "test-token-" + "1234567890abcdef"
ES_SECRET = "sk-" + "easyscholar-" + "test-secret-1234567890"


def _write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class MineruConfigModelTest(unittest.TestCase):
    def test_load_absent_mineru_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path, {"easyscholar_secret_key": ES_SECRET})
            cfg = config.load_config(path=path)
            self.assertEqual(cfg.mineru_key, None)
            self.assertEqual(cfg.easyscholar_secret_key, ES_SECRET)

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save_config(
                config.Config(
                    easyscholar_secret_key=ES_SECRET, mineru_key=MINERU_SECRET
                ),
                path=path,
            )
            cfg = config.load_config(path=path)
            self.assertEqual(cfg.mineru_key, MINERU_SECRET)
            self.assertEqual(cfg.easyscholar_secret_key, ES_SECRET)

    def test_save_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save_config(config.Config(mineru_key=MINERU_SECRET), path=path)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_repr_never_contains_mineru_key(self):
        cfg = config.Config(mineru_key=MINERU_SECRET)
        self.assertNotIn(MINERU_SECRET, repr(cfg))
        self.assertNotIn(MINERU_SECRET, str(cfg))
        self.assertIn(config.MASK, repr(cfg))

    def test_redacted_config_hides_mineru_key(self):
        cfg = config.Config(mineru_key=MINERU_SECRET)
        redacted = config.redacted_config(cfg)
        self.assertNotIn(MINERU_SECRET, json.dumps(redacted))
        self.assertEqual(redacted["mineru_key"], config.MASK)

    def test_redact_config_text_hides_both_keys(self):
        cfg = config.Config(easyscholar_secret_key=ES_SECRET, mineru_key=MINERU_SECRET)
        text = f"failed {MINERU_SECRET} and {ES_SECRET}"
        redacted = config.redact_config_text(text, cfg)
        self.assertNotIn(MINERU_SECRET, redacted)
        self.assertNotIn(ES_SECRET, redacted)


if __name__ == "__main__":
    unittest.main()
