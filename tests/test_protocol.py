"""Protocol envelope and CLI boundary tests (Task 2).

Frozen after the first red run; do not weaken or delete assertions.
"""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from paper_notes import cli
from paper_notes.protocol import (
    EXIT_CONFLICT,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
    Envelope,
    Issue,
    conflict,
    error,
    exit_code_for,
    needs_confirmation,
    success,
)

REPO = Path(__file__).resolve().parents[1]

SECRET = "sk-test-9f8e7d6c5b4a"


class EnvelopeBuilderTest(unittest.TestCase):
    def test_success_with_data(self):
        env = success({"a": 1})
        self.assertEqual(env.protocol_version, 1)
        self.assertEqual(env.status, "success")
        self.assertEqual(env.data, {"a": 1})
        self.assertEqual(env.warnings, [])
        self.assertEqual(env.errors, [])

    def test_success_defaults_empty(self):
        env = success()
        self.assertEqual(env.data, {})
        self.assertEqual(env.warnings, [])

    def test_success_with_warnings(self):
        env = success(
            {"run_id": "r1"}, [Issue(code="notice", message="old alias kept")]
        )
        self.assertEqual(env.status, "success")
        self.assertEqual(len(env.warnings), 1)
        self.assertEqual(env.warnings[0].code, "notice")
        self.assertEqual(env.warnings[0].message, "old alias kept")

    def test_needs_confirmation(self):
        env = needs_confirmation(
            {"plan": "preview"}, [Issue(code="pending", message="confirm required")]
        )
        self.assertEqual(env.status, "needs_confirmation")
        self.assertEqual(env.data, {"plan": "preview"})
        self.assertEqual(env.warnings[0].code, "pending")

    def test_conflict_with_errors(self):
        env = conflict(
            [
                Issue(
                    code="duplicate_key",
                    message="key already exists",
                    path="05 Literature/x/x.md",
                )
            ]
        )
        self.assertEqual(env.status, "conflict")
        self.assertEqual(env.errors[0].code, "duplicate_key")
        self.assertEqual(env.errors[0].path, "05 Literature/x/x.md")

    def test_error_with_errors(self):
        env = error([Issue(code="invalid_field", message="bad", field="citation_key")])
        self.assertEqual(env.status, "error")
        self.assertEqual(env.errors[0].field, "citation_key")

    def test_issue_optional_fields_default_none(self):
        issue = Issue(code="c", message="m")
        self.assertIsNone(issue.path)
        self.assertIsNone(issue.field)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValidationError):
            Envelope(status="bogus")

    def test_protocol_version_is_fixed(self):
        with self.assertRaises(ValidationError):
            Envelope(status="success", protocol_version=2)

    def test_serialize_single_line_round_trip(self):
        env = success({"version": "0.1.0"})
        raw = env.model_dump_json()
        self.assertEqual(raw.count("\n"), 0)
        parsed = json.loads(raw)
        self.assertEqual(parsed["protocol_version"], 1)
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["data"], {"version": "0.1.0"})
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(parsed["errors"], [])

    def test_exit_code_constants(self):
        self.assertEqual(EXIT_OK, 0)
        self.assertEqual(EXIT_USER_ERROR, 2)
        self.assertEqual(EXIT_CONFLICT, 3)
        self.assertEqual(EXIT_INTERNAL_ERROR, 4)

    def test_exit_code_for_status(self):
        self.assertEqual(exit_code_for(success()), 0)
        self.assertEqual(exit_code_for(needs_confirmation()), 0)
        self.assertEqual(
            exit_code_for(conflict([Issue(code="c", message="m")])), EXIT_CONFLICT
        )
        self.assertEqual(
            exit_code_for(error([Issue(code="c", message="m")])), EXIT_USER_ERROR
        )


class CliProtocolTest(unittest.TestCase):
    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_json_version_single_doc(self):
        result = subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", "version"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().count("\n"), 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(result.stderr, "")

    def test_json_unknown_command_usage_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", "bogus"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "usage_error")
        self.assertIn("invalid choice", result.stderr)

    def test_human_mode_keeps_text_output(self):
        rc, out, err = self._run_main(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "paper-notes 1")
        self.assertEqual(err, "")

    def test_no_command_human_usage(self):
        rc, out, err = self._run_main([])
        self.assertEqual(rc, 2)
        self.assertIn("usage", out)

    def test_no_command_json_envelope(self):
        rc, out, err = self._run_main(["--json"])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "missing_command")
        self.assertIn("usage", err)

    def test_user_error_exit_2(self):
        with mock.patch(
            "paper_notes.cli.run_command",
            side_effect=cli.UserError("invalid input"),
        ):
            rc, out, err = self._run_main(["--json", "version"])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "user_error")
        self.assertEqual(payload["errors"][0]["message"], "invalid input")

    def test_conflict_exit_3(self):
        with mock.patch(
            "paper_notes.cli.run_command",
            side_effect=cli.ConflictError("duplicate citation key"),
        ):
            rc, out, err = self._run_main(["--json", "version"])
        self.assertEqual(rc, 3)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "conflict")
        self.assertEqual(payload["errors"][0]["code"], "conflict")
        self.assertEqual(payload["errors"][0]["message"], "duplicate citation key")

    def test_internal_error_exit_4_no_secret_leak(self):
        with mock.patch(
            "paper_notes.cli.run_command",
            side_effect=RuntimeError(f"boom {SECRET}"),
        ):
            rc, out, err = self._run_main(["--json", "version"])
        self.assertEqual(rc, 4)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "internal_error")
        self.assertEqual(payload["errors"][0]["message"], "Internal error")
        self.assertNotIn(SECRET, out)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err, "")

    def test_human_internal_error_traceback_stderr(self):
        with mock.patch(
            "paper_notes.cli.run_command", side_effect=RuntimeError("boom")
        ):
            rc, out, err = self._run_main(["version"])
        self.assertEqual(rc, 4)
        self.assertIn("Traceback", err)

    def test_human_returned_error_envelope(self):
        with mock.patch(
            "paper_notes.cli.run_command",
            return_value=error([Issue(code="x", message="something bad")]),
        ):
            rc, out, err = self._run_main(["version"])
        self.assertEqual(rc, 2)
        self.assertIn("something bad", err)


if __name__ == "__main__":
    unittest.main()
