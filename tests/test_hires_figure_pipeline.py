#!/usr/bin/env python3
"""Unit tests for render_pdf_figure.py (high-resolution PDF figure renderer).

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.

All fixtures are synthetic PDFs built in-memory (stdlib only); the
renderer under test is exercised via subprocess, never mocked.
"""

import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

RENDER = Path(__file__).resolve().parents[1] / "scripts/render_pdf_figure.py"
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


def png_size(data):
    """Width/height from the PNG IHDR (big-endian, offset 16/20)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def decode_png(data):
    """Fully decode a PNG to (width, height, raw RGB pixel bytes)."""
    w, h = png_size(data)
    pos, idat = 8, b""
    while pos < len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        if data[pos + 4:pos + 8] == b"IDAT":
            idat += data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    raw = zlib.decompress(idat)
    bpp, stride = 3, w * 3
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        filt = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if filt == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif filt == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif filt == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif filt == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out.extend(line)
        prev = line
    return w, h, bytes(out)


def run_render(pdf_path, out_dir, page=2, bbox="39.7,45,561.5,576", dpi=300):
    return subprocess.run(
        [sys.executable, str(RENDER), str(pdf_path),
         "--page", str(page), "--bbox", bbox,
         "--dpi", str(dpi), "--output-dir", str(out_dir)],
        capture_output=True, text=True,
    )


class RenderPdfFigureTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(TWO_PAGE_PDF)
        self.out = self.root / "figures"  # must NOT pre-exist (atomicity check)

    def tearDown(self):
        self._td.cleanup()

    # ---- success paths ----

    def test_rendered_png_dimensions_follow_dpi_formula(self):
        # 2175 = ceil(561.5*300/72) - floor(39.7*300/72); 2213 = ceil(576*300/72) - floor(45*300/72)
        r = run_render(self.pdf, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        expected_w = math.ceil(561.5 * 300 / 72) - math.floor(39.7 * 300 / 72)
        expected_h = math.ceil(576 * 300 / 72) - math.floor(45 * 300 / 72)
        self.assertEqual((info["width"], info["height"]), (expected_w, expected_h))
        png = Path(info["path"]).read_bytes()
        self.assertEqual(png_size(png), (expected_w, expected_h))

    def test_vector_text_and_lines_are_rendered(self):
        # Region containing the text baseline and the stroked line: ink present.
        # NOTE: clip coordinates are TOP-DOWN (PyMuPDF page convention):
        # the line at bottom-up y=700 is top-down y=92, text spans ~46-79.
        r = run_render(self.pdf, self.out, page=1, bbox="60,40,560,100")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, _, pixels = decode_png(Path(json.loads(r.stdout)["path"]).read_bytes())
        self.assertLess(min(pixels), 100, "vector text/line should produce dark pixels")
        # Empty region of the same page: all-white, nothing rendered.
        r2 = run_render(self.pdf, self.root / "out2", page=1, bbox="10,10,20,20")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        _, _, pixels2 = decode_png(Path(json.loads(r2.stdout)["path"]).read_bytes())
        self.assertEqual(min(pixels2), 255)
        self.assertEqual(max(pixels2), 255)

    def test_deterministic_png_bytes_and_name(self):
        r1 = run_render(self.pdf, self.out)
        r2 = run_render(self.pdf, self.root / "out2")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        i1, i2 = json.loads(r1.stdout), json.loads(r2.stdout)
        self.assertEqual(Path(i1["path"]).name, Path(i2["path"]).name)
        self.assertEqual(
            Path(i1["path"]).read_bytes(), Path(i2["path"]).read_bytes())

    def test_second_run_same_output_dir_is_idempotent(self):
        first = run_render(self.pdf, self.out)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_render(self.pdf, self.out)
        self.assertEqual(second.returncode, 0, second.stderr)
        files = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(files), 1, f"expected exactly one file, got {files}")
        i1, i2 = json.loads(first.stdout), json.loads(second.stdout)
        self.assertEqual(i1["path"], i2["path"])
        self.assertEqual(self.out.iterdir().__next__().read_bytes(),
                         Path(i1["path"]).read_bytes())

    def test_multiple_figures_accumulate_in_output_dir(self):
        r1 = run_render(self.pdf, self.out, page=1, bbox="0,0,306,396")
        r2 = run_render(self.pdf, self.out, page=2, bbox="0,0,306,396")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(names), 2)
        i1, i2 = json.loads(r1.stdout), json.loads(r2.stdout)
        self.assertNotEqual(i1["path"], i2["path"])
        for info in (i1, i2):
            png = Path(info["path"]).read_bytes()
            self.assertEqual(png_size(png), (1275, 1650))
            self.assertTrue(HEX64.match(Path(info["path"]).name))

    def test_different_pages_render_different_content(self):
        r1 = run_render(self.pdf, self.out, page=1, bbox="0,0,612,792")
        r2 = run_render(self.pdf, self.out, page=2, bbox="0,0,612,792")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertNotEqual(Path(json.loads(r1.stdout)["path"]).name,
                            Path(json.loads(r2.stdout)["path"]).name)

    def test_stdout_is_single_json_with_contract(self):
        r = run_render(self.pdf, self.out, page=2, bbox="39.7,45,561.5,576", dpi=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\n", r.stdout.strip(), "stdout must be a single JSON line")
        info = json.loads(r.stdout)
        self.assertEqual(set(info), {"path", "embed", "width", "height", "page", "bbox", "dpi"})
        name = Path(info["path"]).name
        self.assertTrue(HEX64.match(name))
        self.assertEqual(info["embed"], f"![[{name}]]")
        self.assertEqual(info["path"], str((self.out / name).resolve()))
        self.assertEqual(info["page"], 2)
        self.assertEqual(info["bbox"], [39.7, 45.0, 561.5, 576.0])
        self.assertEqual(info["dpi"], 300)
        self.assertEqual((info["width"], info["height"]), png_size(Path(info["path"]).read_bytes()))

    # ---- failure paths: non-zero exit, zero writes ----

    def test_out_of_bounds_page_fails_without_writes(self):
        for bad in (0, 3):  # 0 is invalid; 3 > doc's 2 pages
            with self.subTest(page=bad):
                r = run_render(self.pdf, self.out, page=bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(self.out.exists(),
                                 "OUT dir must not be created on failure")

    def test_invalid_bbox_rejected_without_writes(self):
        cases = [
            "10,10,20",            # wrong arity
            "20,20,10,10",         # x0 >= x1
            "0,0,9999,9999",       # beyond page bounds
            "nan,0,10,10",         # non-finite
            "1,2,3,inf",           # non-finite
            "a,b,c,d",             # non-numeric
        ]
        for bbox in cases:
            with self.subTest(bbox=bbox):
                r = run_render(self.pdf, self.out, bbox=bbox)
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(self.out.exists(),
                                 "OUT dir must not be created on failure")

    def test_dpi_below_minimum_rejected_without_writes(self):
        r = run_render(self.pdf, self.out, dpi=299)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists(), "OUT dir must not be created on failure")
        ok = run_render(self.pdf, self.root / "ok_out", dpi=300)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_corrupt_pdf_rejected_without_writes(self):
        for name, payload in [("garbage.pdf", b"this is not a pdf at all"),
                              ("empty.pdf", b"")]:
            with self.subTest(name=name):
                bad = self.root / name
                bad.write_bytes(payload)
                r = run_render(bad, self.out)
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(self.out.exists(),
                                 "OUT dir must not be created on failure")

    def test_missing_pdf_file_rejected_without_writes(self):
        r = run_render(self.root / "nope.pdf", self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists(), "OUT dir must not be created on failure")


if __name__ == "__main__":
    unittest.main()
