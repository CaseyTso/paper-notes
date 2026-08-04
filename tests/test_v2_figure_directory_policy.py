#!/usr/bin/env python3
"""V2-layout policy tests for scripts/render_pdf_figure.py.

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.

V2 canonical paper layout (design doc section 5):

    05 Literature/<citation_key>/
    ├── <citation_key>.md      <- main item
    ├── <citation_key>.pdf     <- canonical primary PDF
    ├── minerUmd_<citation_key>.md
    ├── Figure解读_<citation_key>.md
    ├── attachments/
    ├── cards/
    └── figures/               <- final hash PNG figure assets

Contract under test:
  1. The renderer accepts the canonical primary PDF
     (<paper_dir>/<citation_key>.pdf) and writes hash PNGs into
     <paper_dir>/figures/.
  2. Help/docstrings no longer require an "original Zotero PDF"; they
     require the canonical/original-quality PDF.
  3. The legacy Figure_<paper_title> path token may remain only in
     clearly marked migration/legacy statements (the frozen
     Figure-directory policy test still needs that token).
  4. The normal rendering flow never deletes <citation_key>.pdf.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "scripts/render_pdf_figure.py"
KEY = "shiauSpatiallyResolvedAnalysis2024"
HEX64 = re.compile(r"^[0-9a-f]{64}\.png$")


def build_pdf(contents, size=(612, 792)):
    """Build a minimal valid multi-page PDF (base-14 Helvetica, vector ops).

    contents: list of content-stream strings, one per page.
    """
    n = len(contents)
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}

    def add(num, body):
        offsets[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    add(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i in range(n):
        cnum = 3 + n + i
        add(3 + i, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
            f"/Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> /Contents {cnum} 0 R >>"
        ).encode())
    for i, c in enumerate(contents):
        body = f"<< /Length {len(c)} >>\nstream\n".encode() + c.encode() + b"\nendstream"
        add(3 + n + i, body)
    add(3 + 2 * n, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    xref = len(out)
    out.extend(f"xref\n0 {3 + 2 * n + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n" + b"".join(
        f"{offsets[i]:010d} 00000 n \n".encode() for i in range(1, 3 + 2 * n + 1)))
    out.extend(f"trailer\n<< /Size {3 + 2 * n + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(out)


PAGE1 = "BT /F1 24 Tf 72 720 Td (Hello Figure One) Tj ET\n72 700 m 540 700 l S\n"
PAGE2 = "BT /F1 24 Tf 72 360 Td (Second Page Figure) Tj ET\n72 200 200 100 re S\n"
TWO_PAGE_PDF = build_pdf([PAGE1, PAGE2])


class V2FigureDirectoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        # Canonical v2 layout: <paper_dir>/<citation_key>.pdf is the
        # primary PDF; final PNGs go to <paper_dir>/figures/.
        self.paper_dir = self.root / "05 Literature" / KEY
        self.paper_dir.mkdir(parents=True)
        self.pdf = self.paper_dir / f"{KEY}.pdf"
        self.pdf.write_bytes(TWO_PAGE_PDF)
        self.figures = self.paper_dir / "figures"

    def tearDown(self):
        self._td.cleanup()

    def test_renderer_accepts_canonical_primary_pdf_into_figures_dir(self):
        # Real CLI run (no mocks): the input is the canonical primary PDF
        # and the hash PNG lands in <paper_dir>/figures/.
        r = subprocess.run(
            [sys.executable, str(RENDER), str(self.pdf),
             "--page", "2", "--bbox", "39.7,45,561.5,576",
             "--dpi", "300", "--output-dir", str(self.figures)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        name = Path(info["path"]).name
        self.assertTrue(HEX64.match(name), f"not a 64-hex name: {name}")
        png = (self.figures / name).read_bytes()
        self.assertEqual(name, hashlib.sha256(png).hexdigest() + ".png")
        self.assertEqual(info["embed"], f"![[{name}]]")
        self.assertEqual(Path(info["path"]).parent, self.figures.resolve())
        self.assertEqual(sorted(p.name for p in self.figures.iterdir()), [name])
        # Normal flow never deletes the canonical primary PDF.
        self.assertEqual(self.pdf.read_bytes(), TWO_PAGE_PDF)

    def test_render_twice_into_figures_dir_is_idempotent_and_pdf_intact(self):
        cmd = [sys.executable, str(RENDER), str(self.pdf),
               "--page", "2", "--bbox", "39.7,45,561.5,576",
               "--dpi", "300", "--output-dir", str(self.figures)]
        ino_before = self.pdf.stat().st_ino
        first = subprocess.run(cmd, capture_output=True, text=True)
        second = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        i1, i2 = json.loads(first.stdout), json.loads(second.stdout)
        self.assertEqual(i1["path"], i2["path"])
        self.assertEqual(sorted(p.name for p in self.figures.iterdir()),
                         [Path(i1["path"]).name])
        # The canonical primary PDF survives byte- and inode-identically.
        self.assertEqual(self.pdf.read_bytes(), TWO_PAGE_PDF)
        self.assertEqual(self.pdf.stat().st_ino, ino_before)

    def test_help_and_docstring_require_canonical_pdf_not_zotero(self):
        r = subprocess.run([sys.executable, str(RENDER), "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Zotero PDF", r.stdout,
                         "help must not require an original Zotero PDF")
        self.assertIn("original-quality", r.stdout)
        self.assertIn("<paper_dir>/figures", r.stdout,
                      "help must document the v2 <paper_dir>/figures destination")
        text = (REPO / "scripts/render_pdf_figure.py").read_text(encoding="utf-8")
        self.assertNotIn("Zotero PDF", text,
                         "docstring must not require an original Zotero PDF")
        self.assertIn("canonical", text)
        self.assertIn("original-quality", text)

    def test_legacy_figure_title_token_only_in_marked_legacy_statement(self):
        # The frozen Figure-directory policy test still needs the
        # Figure_<paper_title> token in this file; it may only appear in
        # clearly marked migration/legacy statements.
        text = (REPO / "scripts/render_pdf_figure.py").read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "Figure_<paper_title>" in line:
                self.assertIn(
                    "legacy", line.lower(),
                    f"render_pdf_figure.py:{lineno} uses the legacy "
                    f"Figure_<paper_title> token outside a marked legacy "
                    f"statement: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
