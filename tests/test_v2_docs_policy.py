#!/usr/bin/env python3
"""V2 documentation policy tests for the Obsidian-native workflow (Task 20).

Red-light discipline: this file is FROZEN after its first red run.
Do not modify after the SHA256 is recorded.

Contract under test (plan Task 20; design doc sections 2-6, 10, 15):

  1. SKILL.md documents the canonical citation-key directory tree
     (``05 Literature/<citation_key>/``) with the main item, the
     canonical primary PDF, the derived notes, and the
     ``attachments/`` / ``cards/`` / ``figures/`` directories.
  2. The main item ``<citation_key>.md`` is the only authoritative
     bibliographic record; ``frontmatter_spec.md`` documents the
     canonical fields and forbids volatile metric fields
     (EasyScholar / IF / JCI / JCR / CAS).
  3. The directory semantics of ``figures/`` (final high-resolution
     figure assets), ``cards/`` (cards derived from this single
     paper) and ``attachments/`` (additional PDFs and supplementary
     files) are documented in ``frontmatter_spec.md``.
  4. No document instructs writing an active ``zotero://`` link into
     a note; ``zotero://`` may appear only inside clearly marked
     legacy/migration statements.
  5. No document instructs deleting the canonical primary PDF;
     SKILL.md states the normal workflow never deletes
     ``<citation_key>.pdf``.
  6. EasyScholar metrics are documented as volatile UI-only data
     that is never written to Markdown.
  7. MinerU conversion and Figure interpretation remain Hermesian +
     paper-notes skill workflows (the Obsidian plugin never starts
     them).
  8. The English README is the main entry and links the Chinese
     README, which links back.
  9. The legacy ``Figure_<paper_title>`` token may appear only in
     clearly marked legacy/migration statements, never as current
     behavior (satisfies the frozen figure-directory tests).
  10. ``references/cli_protocol.md`` and ``references/migration.md``
      exist and document the JSON envelope / confirmation flow and
      the run-based migration lifecycle.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DOCS = [
    "SKILL.md",
    "README.md",
    "README.zh-CN.md",
] + sorted(f"references/{p.name}" for p in (REPO / "references").glob("*.md"))

SKILL_PATH = REPO / "SKILL.md"
FMS_PATH = REPO / "references/frontmatter_spec.md"

# Markers that explicitly identify legacy/migration statements. A token
# that is allowed to survive only inside such statements must co-occur
# with one of these on the same line.
LEGACY_MARKERS = (
    "legacy", "migration", "迁移", "旧", "历史", "不再", "废弃", "弃用",
    "removed", "remove", "removing",
)

# Canonical tree tokens the SKILL.md must document (design section 5).
CANONICAL_TOKENS = (
    "05 Literature/<citation_key>/",
    "<citation_key>.md",
    "<citation_key>.pdf",
    "minerUmd_<citation_key>.md",
    "Figure解读_<citation_key>.md",
    "attachments/",
    "cards/",
    "figures/",
)

DELETE_VERBS = ("删除", "删掉", "delete", "unlink", "os.remove")
DELETE_NEGATIONS = ("不删除", "never", "不得", "不要", "勿", "not",
                    "no longer", "保留")


def _lines(name: str) -> list[str]:
    return (REPO / name).read_text(encoding="utf-8").splitlines()


class V2DocsPolicyTest(unittest.TestCase):
    def test_skill_documents_canonical_directory_tree(self):
        text = SKILL_PATH.read_text(encoding="utf-8")
        for token in CANONICAL_TOKENS:
            self.assertIn(
                token, text,
                f"SKILL.md must document the canonical token {token!r} "
                "in the citation-key directory tree")

    def test_main_item_yaml_authority_and_forbidden_metrics(self):
        text = FMS_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"唯一权威|authoritative",
            "frontmatter_spec.md must state the main item is the only "
            "authoritative bibliographic record")
        for field in ("schema_version", "paper_id", "citation_key",
                      "pdf_status", "reading_status"):
            self.assertIn(field, text,
                          f"frontmatter_spec.md must document {field}")
        self.assertRegex(
            text, r"禁止[^\n]{0,60}EasyScholar|EasyScholar[^\n]{0,60}禁止",
            "frontmatter_spec.md must forbid volatile metric fields "
            "(EasyScholar) in the schema")

    def test_directory_semantics_documented(self):
        text = FMS_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"figures/[^\n]*(最终|final|高清)",
            "figures/ must be documented as the final high-resolution "
            "figure asset directory")
        self.assertRegex(
            text, r"cards/[^\n]*(派生|derived|卡片)",
            "cards/ must be documented as holding cards derived from "
            "this single paper")
        self.assertRegex(
            text, r"attachments/[^\n]*(补充|附加|supplementary)",
            "attachments/ must be documented as holding additional "
            "PDFs and supplementary files")

    def test_no_active_zotero_uri_output(self):
        # zotero:// may survive only inside marked legacy/migration
        # statements (e.g. migration removes those links).
        for name in DOCS:
            for lineno, line in enumerate(_lines(name), 1):
                if "zotero://" in line:
                    self.assertTrue(
                        any(marker in line for marker in LEGACY_MARKERS),
                        f"{name}:{lineno} writes an active zotero:// link "
                        f"outside a marked legacy/migration statement: "
                        f"{line.strip()}")

    def test_no_instruction_to_delete_canonical_pdf(self):
        sk = SKILL_PATH.read_text(encoding="utf-8")
        # The normal workflow must explicitly preserve the canonical PDF.
        self.assertRegex(
            sk, r"不删除[^\n]*<citation_key>\.pdf|<citation_key>\.pdf[^\n]*不删除",
            "SKILL.md must state the normal workflow never deletes "
            "<citation_key>.pdf")
        for name in DOCS:
            for lineno, line in enumerate(_lines(name), 1):
                if "<citation_key>.pdf" not in line:
                    continue
                if any(verb in line for verb in DELETE_VERBS):
                    self.assertTrue(
                        any(neg in line for neg in DELETE_NEGATIONS),
                        f"{name}:{lineno} instructs deleting the canonical "
                        f"primary PDF: {line.strip()}")

    def test_easyscholar_ui_only_warning(self):
        sk = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("EasyScholar", sk,
                      "SKILL.md must warn about EasyScholar data handling")
        for name in ("SKILL.md", "README.md", "README.zh-CN.md"):
            for lineno, line in enumerate(_lines(name), 1):
                if "EasyScholar" not in line:
                    continue
                self.assertTrue(
                    ("UI" in line or "仅" in line)
                    and ("不写入" in line or "never" in line or "not" in line),
                    f"{name}:{lineno} mentions EasyScholar without the "
                    f"UI-only / never-written-to-Markdown warning: "
                    f"{line.strip()}")

    def test_hermesian_remains_responsible_for_mineru_figure(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("Hermesian", readme,
                      "README.md must state MinerU/Figure generation "
                      "remains a Hermesian + paper-notes workflow")
        self.assertRegex(
            readme, r"Hermesian[^\n]*(MinerU|Figure)",
            "README.md must tie Hermesian to MinerU/Figure generation")

    def test_english_readme_links_chinese(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        zh = (REPO / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("[中文说明](README.zh-CN.md)", readme)
        self.assertIn("[English](README.md)", zh)

    def test_legacy_figure_title_token_only_in_marked_legacy_statement(self):
        # The frozen figure-directory tests still require the
        # Figure_<paper_title> token in several docs; every occurrence
        # must live inside an explicit legacy/migration statement and
        # never be presented as current behavior.
        for name in DOCS:
            for lineno, line in enumerate(_lines(name), 1):
                if "Figure_<paper_title>" not in line:
                    continue
                self.assertTrue(
                    any(marker in line for marker in LEGACY_MARKERS),
                    f"{name}:{lineno} presents the legacy "
                    f"Figure_<paper_title> token as current behavior: "
                    f"{line.strip()}")

    def test_cli_protocol_and_migration_reference_docs_exist(self):
        cli = REPO / "references/cli_protocol.md"
        mig = REPO / "references/migration.md"
        self.assertTrue(cli.is_file(), "references/cli_protocol.md missing")
        self.assertTrue(mig.is_file(), "references/migration.md missing")
        cli_text = cli.read_text(encoding="utf-8")
        mig_text = mig.read_text(encoding="utf-8")
        self.assertIn("protocol_version", cli_text)
        self.assertIn("--json", cli_text)
        self.assertIn("needs_confirmation", cli_text)
        for token in ("run_id", "rollback", "confirmation"):
            self.assertIn(token, mig_text,
                          f"references/migration.md must document {token}")


if __name__ == "__main__":
    unittest.main()
