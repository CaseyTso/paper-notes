#!/usr/bin/env python3
"""
Render a region of a PDF page to a high-resolution lossless PNG figure.

Figures embedded in notes must come from the ORIGINAL Zotero PDF, never
from low-resolution MinerU JPG fragments. This CLI renders a full figure
area (bbox, in PDF points, from the caption page) at >=300 dpi.

Usage:
    python3 render_pdf_figure.py PDF --page 2 --bbox 39.7,45,561.5,576 \
        --dpi 300 --output-dir "<paper_dir>/Figure_<paper_title>"

Semantics:
  - --page is 1-based; --bbox is x0,y0,x1,y1 in PDF points (top-left
    origin); --dpi must be >= 300.
  - Everything is validated BEFORE any write: on any error the exit
    code is non-zero and OUT receives no new files (OUT is not even
    created if it did not exist).
  - The output filename is the SHA256 of the PNG bytes (64 hex + ".png"),
    so identical content is idempotently reused; writes are atomic via a
    temp file + os.replace.
  - stdout carries exactly one JSON line:
    {"path", "embed", "width", "height", "page", "bbox", "dpi"}
    where embed is the Obsidian embed "![[<64hex>.png]]". Diagnostics go
    to stderr.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

MIN_DPI = 300


def parse_bbox(text, page_rect):
    """Parse and validate 'x0,y0,x1,y1' against the page rectangle."""
    parts = text.split(",")
    if len(parts) != 4:
        raise ValueError(f"--bbox must be x0,y0,x1,y1 (4 numbers), got {text!r}")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"--bbox must contain only numbers, got {text!r}")
    for v in values:
        if not math.isfinite(v):
            raise ValueError(f"--bbox must be finite, got {text!r}")
    x0, y0, x1, y1 = values
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"--bbox must satisfy x0<x1 and y0<y1, got {text!r}")
    if not (0 <= x0 and 0 <= y0 and x1 <= page_rect.width and y1 <= page_rect.height):
        raise ValueError(
            f"--bbox {text!r} is outside page {page_rect.width}x{page_rect.height}")
    return fitz.Rect(x0, y0, x1, y1)


def render_png(pdf_path, page_number, bbox_text, dpi, out_dir):
    """Validate, render, and atomically write the PNG. Returns result dict.

    Raises ValueError/RuntimeError/OSError on any problem; no file is
    written unless the whole pipeline succeeded.
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    if not pdf_path.is_file():
        raise ValueError(f"PDF file not found: {pdf_path}")
    if not isinstance(dpi, int) or dpi < MIN_DPI:
        raise ValueError(f"--dpi must be an integer >= {MIN_DPI}, got {dpi!r}")
    if page_number < 1:
        raise ValueError(f"--page is 1-based and must be >= 1, got {page_number}")

    doc = fitz.open(str(pdf_path))
    try:
        if page_number > doc.page_count:
            raise ValueError(
                f"--page {page_number} out of range "
                f"(document has {doc.page_count} pages)")
        page = doc.load_page(page_number - 1)
        bbox = parse_bbox(bbox_text, page.rect)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=matrix, clip=bbox, alpha=False)
        png = pix.tobytes("png")
    finally:
        doc.close()

    hexname = hashlib.sha256(png).hexdigest() + ".png"
    target = out_dir / hexname

    # Atomic write: only now may OUT be created, and only via temp+rename.
    if target.exists():
        if target.read_bytes() != png:
            raise ValueError(
                f"refusing to overwrite existing file with different content: {target}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".render-", suffix=".tmp", dir=str(out_dir))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    return {
        "path": str(target.resolve()),
        "embed": f"![[{hexname}]]",
        "width": pix.width,
        "height": pix.height,
        "page": page_number,
        "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
        "dpi": dpi,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a PDF page region to a 300dpi+ lossless PNG figure")
    parser.add_argument("pdf", help="Path to the ORIGINAL Zotero PDF")
    parser.add_argument("--page", type=int, required=True,
                        help="1-based page number containing the figure")
    parser.add_argument("--bbox", required=True,
                        help="Figure bounds in PDF points: x0,y0,x1,y1")
    parser.add_argument("--dpi", type=int, default=300,
                        help=f"Rendering resolution (>= {MIN_DPI}); default 300")
    parser.add_argument("--output-dir", required=True,
                        help="Destination directory "
                             "(e.g. '<paper_dir>/Figure_<paper_title>')")
    args = parser.parse_args(argv)

    try:
        result = render_png(args.pdf, args.page, args.bbox, args.dpi, args.output_dir)
    except Exception as exc:  # any failure: clean error, non-zero exit, no writes
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
