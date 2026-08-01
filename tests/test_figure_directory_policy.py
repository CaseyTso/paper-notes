#!/usr/bin/env python3
"""Policy tests for the per-paper figure subdirectory contract.

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.

Contract under test (per the Figure-directory policy):
  1. Final high-resolution PNGs are rendered by scripts/render_pdf_figure.py
     (exercised via subprocess, never mocked) into
     "<paper_dir>/Figure_<paper_title>/" — a nested directory whose name
     contains spaces — with a SHA256 filename, >=300 dpi, and a bare
     "![[<64hex>.png]]" embed.
  2. The product specs (SKILL.md, references/, READMEs, renderer help)
     document that both notes (minerUmd and Figure解读) reuse the SAME
     embed from that directory, and never treat "01 attachments" as the
     final PNG destination (that directory name may only appear in
     negative statements: MinerU JPG never enters it).
"""

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "scripts/render_pdf_figure.py"
HEX64 = re.compile(r"^[0-9a-f]{64}\.png$")

PRODUCT_FILES = [
    "SKILL.md",
    "references/figure_interpretation.md",
    "references/frontmatter_spec.md",
    "README.md",
    "README.zh-CN.md",
    "scripts/render_pdf_figure.py",
]

# The two notes that share one figure embed per paper.
SHARED_EMBED_DOCS = [
    "SKILL.md",
    "references/figure_interpretation.md",
    "references/frontmatter_spec.md",
]

# A line mentioning the attachments area is only acceptable when it is a
# NEGATIVE statement (MinerU JPG must never enter it).
NEGATION_MARKERS = (
    "不进入", "不迁入", "不保留", "不写入", "不再", "不把", "不当作", "不落",
    "not", "never", "no longer", "without", "removed", "deleted",
)


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


def png_size(data):
    """Width/height from the PNG IHDR (big-endian, offset 16/20)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


class FigureDirectoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(TWO_PAGE_PDF)

    def tearDown(self):
        self._td.cleanup()

    def test_render_into_figure_subdir_under_spaced_title(self):
        # Real CLI run (no mocks): output dir is
        # <paper_dir>/Figure_<paper_title>/ with spaces in the title.
        paper_title = "P16+ Cells Drive Adverse Postischemic Cardiac Remodeling"
        paper_dir = self.root / "05 Literature" / paper_title
        figure_dir = paper_dir / f"Figure_{paper_title}"
        r = subprocess.run(
            [sys.executable, str(RENDER), str(self.pdf),
             "--page", "2", "--bbox", "39.7,45,561.5,576",
             "--dpi", "300", "--output-dir", str(figure_dir)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        name = Path(info["path"]).name
        # SHA256 filename and bare wikilink embed
        self.assertTrue(HEX64.match(name), f"not a 64-hex name: {name}")
        png = (figure_dir / name).read_bytes()
        self.assertEqual(name, hashlib.sha256(png).hexdigest() + ".png")
        self.assertEqual(info["embed"], f"![[{name}]]")
        # The PNG's REAL parent directory is the per-paper Figure subdir.
        self.assertEqual(Path(info["path"]).parent, figure_dir.resolve())
        # Exactly one file in the figure subdir, nothing in paper_dir root.
        self.assertEqual(sorted(p.name for p in figure_dir.iterdir()), [name])
        self.assertEqual(list(paper_dir.iterdir()), [figure_dir])
        # 300 dpi: reported dpi and pixel dimensions from the dpi formula.
        self.assertEqual(info["dpi"], 300)
        expected_w = math.ceil(561.5 * 300 / 72) - math.floor(39.7 * 300 / 72)
        expected_h = math.ceil(576 * 300 / 72) - math.floor(45 * 300 / 72)
        self.assertEqual((info["width"], info["height"]), (expected_w, expected_h))
        self.assertEqual(png_size(png), (expected_w, expected_h))

    def test_spec_documents_figure_subdir_and_shared_embed(self):
        # Every product doc must name Figure_<paper_title> as the destination;
        # the two notes must be specified to reuse the same embed from it.
        for name in PRODUCT_FILES:
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("Figure_<paper_title>", text,
                          f"{name} must document the Figure_<paper_title> destination")
        for name in SHARED_EMBED_DOCS:
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertRegex(text, r"同一[^\n]*embed",
                             f"{name} must state both notes reuse the same embed")

    def test_spec_never_treats_01_attachments_as_final_png_destination(self):
        # "01 attachments" may only appear in negative statements (MinerU JPG
        # never enters it). A positive mention -- --output-dir pointing there,
        # or any un-negated line naming the attachments area -- is a
        # violation of the figure-directory policy. Only directory-semantic
        # terms are scanned: "01 attachments", "附件目录", "attachments dir"
        # (bare "附件" / "attachments" also cover Zotero API fields like
        # attachments[].key and the --attachments-dir compat flag name, which
        # are not destinations).
        for name in PRODUCT_FILES:
            text = (REPO / name).read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if not any(term in line for term in
                           ("01 attachments", "附件目录", "attachments dir",
                            "attachments directory")):
                    continue
                if "--output-dir" in line:
                    self.fail(
                        f"{name}:{lineno} points --output-dir at the attachments area: "
                        f"{line.strip()}")
                if not any(marker in line for marker in NEGATION_MARKERS):
                    self.fail(
                        f"{name}:{lineno} mentions the attachments area without a "
                        f"negation: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
