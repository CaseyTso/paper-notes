#!/usr/bin/env python3
"""V2-layout policy tests for the MinerU toolchain scripts.

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.

V2 canonical layout (design doc section 5): the cleaned MinerU note is
finalized as `minerUmd_<citation_key>.md` next to the canonical primary
PDF (`<paper_dir>/<citation_key>.pdf`); temporary MinerU images must
never become final `<paper_dir>/figures/` assets; no normal workflow
deletes `<citation_key>.pdf`. Legacy callers (no `--citation-key`)
keep the historical `full.md` name.

All network access is mocked; fixtures are synthetic. `mineru_upload.py`
is imported in-process (scripts/ is a plain directory on sys.path) so
its requests calls can be stubbed; `clean_md.py` is exercised via the
real CLI like the frozen image-pipeline tests.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))  # scripts/ is not a package; import by path

import mineru_upload  # noqa: E402

KEY = "shiauSpatiallyResolvedAnalysis2024"
CLEAN = SCRIPTS / "clean_md.py"


def make_zip():
    """Synthetic MinerU result zip: full.md + one images/ fragment."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("result/full.md", "# MinerU output\n\n![](images/f1.jpg)\n")
        zf.writestr("result/images/f1.jpg", b"jpg-bytes-" * 40)
    return buf.getvalue()


class FakeResp:
    """requests.Response stand-in: JSON body or streaming `.raw` bytes.

    Supports the context-manager protocol used by download_and_extract
    (`with requests.get(...) as r`).
    """

    def __init__(self, json_data=None, status_code=200, raw=None):
        self._json = json_data
        self.status_code = status_code
        self.raw = raw

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class V2MineruLayoutTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    # ---- mineru_upload.py: final note name + canonical PDF preservation ----

    def test_mineru_final_note_named_minerUmd_citation_key(self):
        paper_dir = self.root / "05 Literature" / KEY
        paper_dir.mkdir(parents=True)
        pdf = paper_dir / f"{KEY}.pdf"
        pdf.write_bytes(b"%PDF-1.4 synthetic canonical primary")
        before = pdf.read_bytes()
        zip_bytes = make_zip()

        def fake_get(url, **kwargs):
            if "extract-results" in url:
                return FakeResp(json_data={
                    "code": 0,
                    "data": {"extract_result": [{
                        "state": "done",
                        "full_zip_url": "https://cdn.example/full.zip",
                        "extract_progress": {"extracted_pages": 2,
                                             "total_pages": 2},
                    }]},
                })
            return FakeResp(raw=io.BytesIO(zip_bytes))

        stdout = io.StringIO()
        with mock.patch.object(mineru_upload.requests, "post",
                               return_value=FakeResp(json_data={
                                   "code": 0,
                                   "data": {"batch_id": "BATCH1",
                                            "file_urls": [
                                                "https://oss.example/upload"]}})), \
             mock.patch.object(mineru_upload.requests, "put",
                               return_value=FakeResp(status_code=200)), \
             mock.patch.object(mineru_upload.requests, "get",
                               side_effect=fake_get), \
             mock.patch.object(mineru_upload.time, "sleep"), \
             contextlib.redirect_stdout(stdout):
            rc = mineru_upload.main(
                [str(pdf), str(paper_dir), "--token", "t",
                 "--citation-key", KEY])

        self.assertIsNone(rc)
        final = paper_dir / f"minerUmd_{KEY}.md"
        self.assertTrue(final.is_file(),
                        "cleaned note must be finalized as "
                        f"minerUmd_{KEY}.md, not {sorted(p.name for p in paper_dir.iterdir())}")
        self.assertIn("# MinerU output", final.read_text(encoding="utf-8"))
        self.assertFalse((paper_dir / "full.md").exists(),
                         "v2 final name must replace the legacy full.md name")
        self.assertFalse((paper_dir / "mineru_result.zip").exists())
        # JSON contract preserved (batch_id / full_md_path / status).
        out = json.loads(stdout.getvalue())
        self.assertEqual(out["batch_id"], "BATCH1")
        self.assertEqual(out["status"], "success")
        self.assertTrue(
            out["full_md_path"].endswith(f"minerUmd_{KEY}.md"),
            f"full_md_path must point at the final v2 name: {out['full_md_path']}")
        # Normal workflow never deletes the canonical primary PDF.
        self.assertEqual(pdf.read_bytes(), before)

    def test_mineru_without_citation_key_keeps_legacy_full_md_name(self):
        out = self.root / "legacy_out"
        with mock.patch.object(
                mineru_upload.requests, "get",
                return_value=FakeResp(raw=io.BytesIO(make_zip()))):
            md = mineru_upload.download_and_extract(
                "https://cdn.example/full.zip", str(out))
        self.assertEqual(Path(md).name, "full.md",
                         "legacy callers keep the historical full.md name")
        self.assertTrue((out / "full.md").is_file())

    # ---- temporary MinerU images must never become final figures/ assets ----

    def test_mineru_rejects_figures_output_dir_before_any_write(self):
        paper_dir = self.root / "05 Literature" / KEY
        paper_dir.mkdir(parents=True)
        pdf = paper_dir / f"{KEY}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        figures = paper_dir / "figures"
        with self.assertRaises(SystemExit) as cm:
            mineru_upload.main([str(pdf), str(figures), "--token", "t"])
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(figures.exists(),
                         "figures/ must not be created when MinerU output "
                         "is refused")
        # The direct API entry point enforces the same guard.
        with self.assertRaises(ValueError):
            mineru_upload.download_and_extract(
                "https://cdn.example/full.zip", str(figures))
        self.assertFalse(figures.exists())

    def test_mineru_upload_help_documents_v2_final_name_and_figures_guard(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "mineru_upload.py"),
                            "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--citation-key", r.stdout)
        self.assertIn("minerUmd_<citation_key>.md", r.stdout)
        self.assertIn("figures", r.stdout)

    # ---- clean_md.py: temporary MinerU images never land in figures/ ----

    def test_clean_md_rejects_figures_attachments_dir(self):
        images = self.root / "images"
        images.mkdir()
        name = "65fcdcb78128b8d0dc346e9815d2b156ed0604c1dcab0ce40704a33f7196fafa.jpg"
        (images / name).write_bytes(b"payload")
        md = self.root / "full.md"
        original = f"Before\n\n![](images/{name})\n\nAfter\n"
        md.write_text(original, encoding="utf-8")
        paper_dir = self.root / "05 Literature" / KEY
        figures = paper_dir / "figures"
        r = subprocess.run(
            [sys.executable, str(CLEAN), str(md), "--in-place",
             "--attachments-dir", str(figures)],
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(md.read_text(encoding="utf-8"), original,
                         "markdown must be left untouched on refusal")
        self.assertFalse(figures.exists(),
                         "MinerU JPGs must never become final figures/ assets")

    def test_clean_md_help_documents_canonical_attachments_dir(self):
        r = subprocess.run([sys.executable, str(CLEAN), "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("<paper_dir>/attachments/", r.stdout,
                      "help must document the v2 canonical attachments dir")
        text = (SCRIPTS / "clean_md.py").read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "01 attachments" in line:
                self.assertIn(
                    "legacy", line.lower(),
                    f"clean_md.py:{lineno} uses the legacy '01 attachments' "
                    f"path outside a marked legacy statement: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
