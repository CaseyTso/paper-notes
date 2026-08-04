"""EasyScholar query adapter tests (Task 16).

Frozen after the first red run; do not weaken or delete assertions.
All HTTP is mocked — no live request is ever made and no real secret
exists. The secret is a synthetic marker assembled at runtime. The
adapter returns stable normalized metric fields and must never write
to Markdown or leak the secret in errors/output.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes.adapters import easyscholar

REPO = Path(__file__).resolve().parents[1]
SECRET = "sk-" + "easyscholar-" + "test-secret-1234567890"

RAW_PAYLOAD = {
    "code": 0,
    "msg": "成功",
    "data": [
        {
            "name": "Nature Medicine",
            "abbreviation": "Nat Med",
            "level": "SCI",
            "issn": "1078-8956",
            "sciif": "82.9",
            "sciif5": "83.2",
            "jci": "8.11",
            "jcr": "Q1",
            "cas": "1区",
        }
    ],
}

EXPECTED_NORMALIZED = {
    "source": "easyscholar",
    "journal": "Nature Medicine",
    "abbreviation": "Nat Med",
    "issn": "1078-8956",
    "level": "SCI",
    "metrics": {
        "if": 82.9,
        "if5": 83.2,
        "jci": 8.11,
        "jcr_partition": "Q1",
        "cas_partition": "1区",
    },
}


def _json_response(payload: dict, status: int = 200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


class QueryTest(unittest.TestCase):
    def test_query_normalizes_stable_fields(self):
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(RAW_PAYLOAD)) as get:
            result = easyscholar.EasyScholarAdapter(SECRET).query(
                journal="Nature Medicine"
            )
        expected = dict(EXPECTED_NORMALIZED)
        self.assertEqual(
            {k: v for k, v in result.items() if k != "queried_at"}, expected
        )
        self.assertIn("T", result["queried_at"])
        get.assert_called_once()
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["secretKey"], SECRET)
        self.assertEqual(params["scienceName"], "Nature Medicine")

    def test_query_by_issn(self):
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(RAW_PAYLOAD)) as get:
            easyscholar.EasyScholarAdapter(SECRET).query(issn="1078-8956")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["issn"], "1078-8956")
        self.assertNotIn("scienceName", params)

    def test_query_requires_journal_or_issn(self):
        with self.assertRaises(ValueError):
            easyscholar.EasyScholarAdapter(SECRET).query()

    def test_http_error_raises_without_leaking_secret(self):
        response = mock.Mock()
        response.status_code = 500
        with mock.patch.object(easyscholar.requests, "get", return_value=response):
            with self.assertRaises(easyscholar.EasyScholarError) as ctx:
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_invalid_credentials_raise_nonzero_error(self):
        payload = {"code": 4002, "msg": "invalid secret key", "data": None}
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(payload)):
            with self.assertRaises(easyscholar.EasyScholarError) as ctx:
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_rate_limit_raises_nonzero_error(self):
        payload = {"code": 4003, "msg": "request too frequent", "data": None}
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(payload)):
            with self.assertRaises(easyscholar.EasyScholarError):
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")

    def test_invalid_json_raises(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.side_effect = ValueError("bad json")
        with mock.patch.object(easyscholar.requests, "get", return_value=response):
            with self.assertRaises(easyscholar.EasyScholarError):
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")

    def test_empty_data_raises(self):
        payload = {"code": 0, "msg": "成功", "data": []}
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(payload)):
            with self.assertRaises(easyscholar.EasyScholarError):
                easyscholar.EasyScholarAdapter(SECRET).query(journal="No Such Journal")

    def test_non_numeric_metrics_become_none(self):
        payload = {
            "code": 0,
            "msg": "成功",
            "data": [
                {
                    "name": "Journal X",
                    "abbreviation": None,
                    "level": None,
                    "issn": None,
                    "sciif": "n/a",
                    "sciif5": "",
                    "jci": None,
                    "jcr": None,
                    "cas": None,
                }
            ],
        }
        with mock.patch.object(easyscholar.requests, "get", return_value=_json_response(payload)):
            result = easyscholar.EasyScholarAdapter(SECRET).query(journal="Journal X")
        self.assertEqual(
            result["metrics"],
            {
                "if": None,
                "if5": None,
                "jci": None,
                "jcr_partition": None,
                "cas_partition": None,
            },
        )
        self.assertIsNone(result["abbreviation"])
        self.assertIsNone(result["issn"])

    def test_request_transport_error_raises(self):
        import requests

        with mock.patch.object(
            easyscholar.requests, "get", side_effect=requests.ConnectionError("boom")
        ):
            with self.assertRaises(easyscholar.EasyScholarError):
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")


class NoWriteGuaranteeTest(unittest.TestCase):
    def test_query_leaves_fixture_markdown_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                root / "note.md": b"# Note\n\nbody\n",
                root / "sub" / "other.md": b"# Other\n\nbody\n",
            }
            (root / "sub").mkdir()
            for path, blob in files.items():
                path.write_bytes(blob)
            before = {str(p): hashlib.sha256(blob).hexdigest() for p, blob in files.items()}
            with mock.patch.object(
                easyscholar.requests, "get", return_value=_json_response(RAW_PAYLOAD)
            ):
                easyscholar.EasyScholarAdapter(SECRET).query(journal="Nature Medicine")
            after = {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
            }
            self.assertEqual(before, after)
            on_disk = {str(p) for p in root.rglob("*") if p.is_file()}
            self.assertEqual(on_disk, {str(p) for p in files})


class MetricsCliTest(unittest.TestCase):
    """CLI integration for ``metrics query`` with mocked HTTP."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"
        from paper_notes import config as cfgmod

        cfgmod.save_config(
            cfgmod.Config(easyscholar_secret_key=SECRET), path=self.cfg_path
        )
        self._env = mock.patch.dict(os.environ, {"PAPER_NOTES_CONFIG": str(self.cfg_path)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run(self, *argv):
        from paper_notes.cli import main

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--json", *argv])
        return rc, buf.getvalue()

    def test_metrics_query_success_json(self):
        with mock.patch.object(
            easyscholar.requests, "get", return_value=_json_response(RAW_PAYLOAD)
        ):
            rc, out = self._run(
                "metrics", "query", "--journal", "Nature Medicine", "--issn", "1078-8956"
            )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "success")
        metrics = payload["data"]["metrics"]
        self.assertEqual(metrics["journal"], "Nature Medicine")
        self.assertEqual(metrics["metrics"]["jcr_partition"], "Q1")
        self.assertNotIn(SECRET, out)

    def test_metrics_query_without_key_is_nonzero(self):
        from paper_notes import config as cfgmod

        cfgmod.save_config(cfgmod.Config(), path=self.cfg_path)
        rc, out = self._run("metrics", "query", "--journal", "Nature Medicine")
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn(SECRET, out)

    def test_metrics_query_error_is_nonzero_and_redacted(self):
        payload = {"code": 4002, "msg": "invalid secret key", "data": None}
        with mock.patch.object(
            easyscholar.requests, "get", return_value=_json_response(payload)
        ):
            rc, out = self._run("metrics", "query", "--journal", "Nature Medicine")
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn(SECRET, out)


if __name__ == "__main__":
    unittest.main()
