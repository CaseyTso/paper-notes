#!/usr/bin/env python3
"""Policy tests for figure destinations in the workflow reference docs.

Hidden-check extension (leader-approved): references/mineru_multi_file_batch.md
and references/bulk_cdn_recovery.md previously still directed final PNGs to
"01 attachments". This file locks the same directory policy for every file
under references/:

  1. The two batch/recovery flow docs must document the per-paper figure
     subdirectory (<paper_dir>/Figure_<paper_title>/) as the final PNG
     destination.
  2. No line may pair the renderer (render_pdf_figure) with the attachments
     area: the renderer's output is the per-paper Figure subdirectory now.
  3. Clause-level scan: any clause naming the attachments area
     ("01 attachments" / "附件目录" / "attachments dir" / "attachments
     directory") must be a NEGATIVE statement (MinerU JPG never enters it).
     Clauses are split on ；;→。 so that a negation elsewhere on the same
     line (e.g. "不迁入附件" for clean_md) cannot mask a positive
     destination clause for the renderer.

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REF_DIR = REPO / "references"

# The two batch/recovery flow docs must name the per-paper figure subdir.
FLOW_DOCS = ["mineru_multi_file_batch.md", "bulk_cdn_recovery.md"]

DESTINATION_TERMS = ("01 attachments", "附件目录", "attachments dir",
                     "attachments directory")

NEGATION_MARKERS = (
    "不进入", "不迁入", "不保留", "不写入", "不再", "不把", "不当作", "不落",
    "not", "never", "no longer", "without", "removed", "deleted",
)

CLAUSE_SPLIT = re.compile(r"[；;→。]")


class ReferenceDocsDestinationPolicyTest(unittest.TestCase):
    def test_flow_docs_document_figure_subdir_destination(self):
        # The batch and CDN-recovery flows must tell agents where final PNGs
        # go: <paper_dir>/Figure_<paper_title>/ (same sanitized title, both
        # notes share the embed from there).
        for name in FLOW_DOCS:
            text = (REF_DIR / name).read_text(encoding="utf-8")
            self.assertIn("Figure_<paper_title>", text,
                          f"{name} must document the Figure_<paper_title> destination")

    def test_no_doc_pairs_renderer_with_attachments_area(self):
        # render_pdf_figure.py's output is the per-paper Figure subdirectory;
        # a clause pairing it with the attachments area in a positive way is
        # always wrong now (negative statements like "MinerU JPG 不进入附件
        # 目录" may legitimately mention the renderer on the same line).
        for path in sorted(REF_DIR.glob("*.md")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "render_pdf_figure" not in line:
                    continue
                for clause in CLAUSE_SPLIT.split(line):
                    if not any(term in clause for term in DESTINATION_TERMS):
                        continue
                    if not any(marker in clause for marker in NEGATION_MARKERS):
                        self.fail(
                            f"{path.name}:{lineno} pairs render_pdf_figure with the "
                            f"attachments area: {clause.strip()}")

    def test_no_positive_clause_names_attachments_area(self):
        # Clause-level scan: every clause naming the attachments area must be
        # a negative statement; a positive destination clause (even on a line
        # that has a negation elsewhere) is a violation.
        for path in sorted(REF_DIR.glob("*.md")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for clause in CLAUSE_SPLIT.split(line):
                    if not any(term in clause for term in DESTINATION_TERMS):
                        continue
                    if "--output-dir" in clause:
                        self.fail(
                            f"{path.name}:{lineno} points --output-dir at the "
                            f"attachments area: {clause.strip()}")
                    if not any(marker in clause for marker in NEGATION_MARKERS):
                        self.fail(
                            f"{path.name}:{lineno} has a positive mention of the "
                            f"attachments area: {clause.strip()}")


if __name__ == "__main__":
    unittest.main()
