"""Citation-aware rename-key transaction tests (Task 13).

Frozen after the first red run; do not weaken or delete assertions.

Contract (spec §4.3/§8.2 + plan Task 13):
- Preview is read-only (zero writes, no lock, hook 0) and returns a
  deterministic plan + token binding paper_id, canonical old/new keys,
  sorted moves/edits/occurrences, and every scanned/affected input file
  (path/type/sha256/mode).
- Confirm re-resolves and re-plans under the write lock; a stale token
  (file add/delete/edit/type change, target dir appeared, plan change)
  is a read-only ItemConflict with zero writes and hook 0.
- The rename updates Pandoc ``[@old]`` / ``[@old; @other]`` and wikilink
  ``[[old]]`` / ``[[old|x]]`` / ``[[old#h]]`` in markdown bodies only,
  via a line-based state machine. Fenced code (any length / 0-3 space
  indent), inline code spans, 4-space indented code, HTML ``<pre>`` /
  ``<code>`` regions, escaped ``\\[@old]`` / ``\\[[old]]`` and every
  YAML frontmatter block are never touched.
- Directory rename, key-file renames, frontmatter edits, derived-note
  relationship fields, alias append and all vault edits are one
  all-or-nothing transaction; every injectable stage failure restores
  the fixture byte/type/mode/tree identical with zero staging residue.
- paper_id never changes; the old key becomes a reserved alias; both
  new current key and old alias resolve to the same paper_id.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_notes import citations, cli, fsops, items
from paper_notes.frontmatter import load_paper_note
from paper_notes.repository import build_index

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "vault" / "rename_cases"

OLD = "shiauSpatiallyResolvedAnalysis2024"
NEW = "shiauSpatiallyResolvedMethods2025"
ALIAS = "shiauSpatiallyResolved2023"
OTHER = "jonesOther2026"
PAPER_ID = "550e8400-e29b-41d4-a716-446655440001"

LIT = "05 Literature"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_vault(root=None):
    """Copy the fixture vault into a fresh temp dir (never mutated in
    place); accepts an existing destination directory."""
    if root is None:
        root = Path(tempfile.mkdtemp())
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    return root


def vault_manifest(root):
    """Sorted {relpath: (type, sha_or_link, mode)} over the whole vault,
    skipping only the .paper-notes transaction area."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".paper-notes")
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries[str(p.relative_to(root))] = ("symlink", os.readlink(p), None)
            else:
                entries[str(p.relative_to(root))] = (
                    "dir",
                    None,
                    stat.S_IMODE(st.st_mode),
                )
        for f in sorted(filenames):
            p = Path(dirpath) / f
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries[str(p.relative_to(root))] = ("symlink", os.readlink(p), None)
            else:
                entries[str(p.relative_to(root))] = (
                    "file",
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                    stat.S_IMODE(st.st_mode),
                )
    return dict(sorted(entries.items()))


def manifest_diff(before, after):
    added = sorted(p for p in after if p not in before)
    removed = sorted(p for p in before if p not in after)
    changed = sorted(p for p in before if p in after and before[p] != after[p])
    return added, removed, changed


def staging_residue(root):
    staging = Path(root) / ".paper-notes" / ".staging"
    if not staging.is_dir():
        return []
    return sorted(str(p.relative_to(staging)) for p in staging.rglob("*"))


def item(root, key):
    return Path(root) / LIT / key


def note(root, key):
    return item(root, key) / f"{key}.md"


def workdir(root, key=OLD):
    """Hidden same-filesystem work directory the rename transaction
    edits in (the real item never sits at the final ``<new>`` path while
    being edited)."""
    return item(root, key).parent / f".{key}.rename-work"


def preview_token(root, key=OLD, new_key=NEW):
    return citations.preview_rename_key(root, key=key, new_key=new_key)


def confirm(root, token, key=OLD, new_key=NEW, hook=None):
    return citations.confirm_rename_key(
        root, key=key, new_key=new_key, confirm_token=token, rebuild_hook=hook
    )


def write_raw_paper(root, key, paper_id, aliases=()):
    """Minimal dynamic main note (only the mandatory file)."""
    d = item(root, key)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "schema_version: 1",
        f"paper_id: {paper_id}",
        f"citation_key: {key}",
        "item_type: article-journal",
        "title: A minimal paper",
        "authors:",
        "- family: Writer",
        "  given: Ann",
        "publication_date: 2025-01-01",
        "year: 2025",
        "pdf_status: missing",
        "reading_status: unread",
    ]
    if aliases:
        lines.append("citation_key_aliases:")
        lines.extend(f"  - {a}" for a in aliases)
    lines.append("---")
    lines.append("# Minimal")
    lines.append(f"")
    lines.append(f"citing [@{key}]")
    (d / f"{key}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def get_token(root, key=OLD, new_key=NEW):
    return preview_token(root, key=key, new_key=new_key).confirmation_token


# ---------------------------------------------------------------------------
# 1. pure scanner
# ---------------------------------------------------------------------------


class ScannerTest(unittest.TestCase):
    def hits(self, text, old=OLD):
        return citations.scan_markdown(text, old)

    def rewrite(self, text, old=OLD, new=NEW):
        return citations.apply_hits(text, new, self.hits(text, old))

    def test_pandoc_single_and_multi_citation_hits(self):
        hits = self.hits("[@shiauSpatiallyResolvedAnalysis2024] and [@shiauSpatiallyResolvedAnalysis2024; @jonesOther2026]")
        self.assertEqual([h.kind for h in hits], ["pandoc", "pandoc"])
        # old key is 34 chars: [@old] -> id slice (2, 36); second id (44, 78)
        self.assertEqual([(h.start, h.end) for h in hits], [(2, 36), (44, 78)])

    def test_pandoc_multi_only_replaces_old_id(self):
        text = "[@shiauSpatiallyResolvedAnalysis2024; @jonesOther2026]"
        self.assertEqual(
            self.rewrite(text),
            "[@shiauSpatiallyResolvedMethods2025; @jonesOther2026]",
        )

    def test_wikilink_forms_hit(self):
        text = "[[shiauSpatiallyResolvedAnalysis2024]] [[shiauSpatiallyResolvedAnalysis2024|x]] [[shiauSpatiallyResolvedAnalysis2024#h]] [[shiauSpatiallyResolvedAnalysis2024#^b]]"
        rewritten = self.rewrite(text)
        self.assertEqual(
            rewritten,
            "[[shiauSpatiallyResolvedMethods2025]] [[shiauSpatiallyResolvedMethods2025|x]] [[shiauSpatiallyResolvedMethods2025#h]] [[shiauSpatiallyResolvedMethods2025#^b]]",
        )
        self.assertEqual([h.kind for h in self.hits(text)], ["wikilink"] * 4)

    def test_fenced_code_immune(self):
        text = (
            "```markdown\n"
            "[@shiauSpatiallyResolvedAnalysis2024]\n"
            "[[shiauSpatiallyResolvedAnalysis2024]]\n"
            "```\n"
            "~~~~\n"
            "[@shiauSpatiallyResolvedAnalysis2024]\n"
            "~~~~\n"
            "````\n"
            "[@shiauSpatiallyResolvedAnalysis2024]\n"
            "````\n"
            "   ```md\n"
            "   [@shiauSpatiallyResolvedAnalysis2024]\n"
            "   ```\n"
            "live [@shiauSpatiallyResolvedAnalysis2024]\n"
        )
        self.assertEqual(self.rewrite(text), text.replace(
            "live [@shiauSpatiallyResolvedAnalysis2024]",
            "live [@shiauSpatiallyResolvedMethods2025]",
        ))
        self.assertEqual(len(self.hits(text)), 1)

    def test_inline_code_immune_and_multiline_span(self):
        text = "`[@shiauSpatiallyResolvedAnalysis2024]` and `` [[shiauSpatiallyResolvedAnalysis2024]] ``\n`unclosed span\n[@shiauSpatiallyResolvedAnalysis2024] swallowed\n` closed\nlive [@shiauSpatiallyResolvedAnalysis2024]"
        rewritten = self.rewrite(text)
        self.assertIn("`[@shiauSpatiallyResolvedAnalysis2024]`", rewritten)
        self.assertIn("`` [[shiauSpatiallyResolvedAnalysis2024]] ``", rewritten)
        self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] swallowed", rewritten)
        self.assertIn("live [@shiauSpatiallyResolvedMethods2025]", rewritten)

    def test_indented_code_block_immune(self):
        text = (
            "    [@shiauSpatiallyResolvedAnalysis2024]\n"
            "    [[shiauSpatiallyResolvedAnalysis2024]]\n"
            "\n"
            "live [@shiauSpatiallyResolvedAnalysis2024]\n"
        )
        rewritten = self.rewrite(text)
        self.assertIn("    [@shiauSpatiallyResolvedAnalysis2024]", rewritten)
        self.assertIn("    [[shiauSpatiallyResolvedAnalysis2024]]", rewritten)
        self.assertIn("live [@shiauSpatiallyResolvedMethods2025]", rewritten)

    def test_escaped_literals_immune(self):
        text = r"\[@shiauSpatiallyResolvedAnalysis2024] and \[[shiauSpatiallyResolvedAnalysis2024]] and \\[@shiauSpatiallyResolvedAnalysis2024]"
        rewritten = self.rewrite(text)
        self.assertIn(r"\[@shiauSpatiallyResolvedAnalysis2024]", rewritten)
        self.assertIn(r"\[[shiauSpatiallyResolvedAnalysis2024]]", rewritten)
        # double backslash: the bracket is live again
        self.assertIn(r"\\[@shiauSpatiallyResolvedMethods2025]", rewritten)
        self.assertEqual(len(self.hits(text)), 1)

    def test_html_pre_code_immune(self):
        text = (
            "<pre>\n"
            "[@shiauSpatiallyResolvedAnalysis2024]\n"
            "[[shiauSpatiallyResolvedAnalysis2024]]\n"
            "</pre>\n"
            "<code>[[shiauSpatiallyResolvedAnalysis2024]]</code>\n"
            "<pre class=\"x\">[@shiauSpatiallyResolvedAnalysis2024]</pre>\n"
            "live [@shiauSpatiallyResolvedAnalysis2024]\n"
        )
        rewritten = self.rewrite(text)
        self.assertIn("[@shiauSpatiallyResolvedAnalysis2024]", rewritten.split("live")[0])
        self.assertIn("[[shiauSpatiallyResolvedAnalysis2024]]", rewritten.split("live")[0])
        self.assertIn("live [@shiauSpatiallyResolvedMethods2025]", rewritten)
        self.assertEqual(len(self.hits(text)), 1)

    def test_key_boundary_prefix_and_suffix_not_matched(self):
        text = "[@shiauSpatiallyResolvedAnalysis2024x] [@xshiauSpatiallyResolvedAnalysis2024] [@shiauSpatiallyResolvedAnalysis2024.]"
        self.assertEqual(self.rewrite(text), text)
        self.assertEqual(self.hits(text), [])

    def test_email_like_not_matched(self):
        text = "user@shiauSpatiallyResolvedAnalysis2024.example and [@shiauSpatiallyResolvedAnalysis2024]"
        self.assertEqual(
            self.rewrite(text),
            "user@shiauSpatiallyResolvedAnalysis2024.example and [@shiauSpatiallyResolvedMethods2025]",
        )

    def test_no_hits_empty(self):
        text = "plain text without citations"
        self.assertEqual(self.rewrite(text), text)
        self.assertEqual(self.hits(text), [])

    def test_frontmatter_block_never_scanned(self):
        text = (
            "---\n"
            "citation_key: shiauSpatiallyResolvedAnalysis2024\n"
            "paper: \"[[shiauSpatiallyResolvedAnalysis2024]]\"\n"
            "---\n"
            "body [@shiauSpatiallyResolvedAnalysis2024]"
        )
        rewritten = self.rewrite(text)
        self.assertIn("citation_key: shiauSpatiallyResolvedAnalysis2024", rewritten)
        self.assertIn("paper: \"[[shiauSpatiallyResolvedAnalysis2024]]\"", rewritten)
        self.assertIn("body [@shiauSpatiallyResolvedMethods2025]", rewritten)

    def test_crlf_body_preserved(self):
        text = "line one [@shiauSpatiallyResolvedAnalysis2024]\r\nline two [[shiauSpatiallyResolvedAnalysis2024]]\r\n"
        rewritten = self.rewrite(text)
        self.assertEqual(rewritten, "line one [@shiauSpatiallyResolvedMethods2025]\r\nline two [[shiauSpatiallyResolvedMethods2025]]\r\n")

    def test_unicode_body_preserved(self):
        text = "中文 [@shiauSpatiallyResolvedAnalysis2024] 引用 [[shiauSpatiallyResolvedAnalysis2024]]。"
        rewritten = self.rewrite(text)
        self.assertEqual(rewritten, "中文 [@shiauSpatiallyResolvedMethods2025] 引用 [[shiauSpatiallyResolvedMethods2025]]。")


# ---------------------------------------------------------------------------
# 2. preview
# ---------------------------------------------------------------------------


class PreviewTest(unittest.TestCase):
    def test_preview_plan_structure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview_token(root)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.action, "rename_key")
            self.assertEqual(str(result.paper_id), PAPER_ID)
            self.assertEqual(result.citation_key, OLD)
            self.assertEqual(result.new_key, NEW)
            self.assertEqual(result.requested_key, OLD)
            self.assertEqual(result.resolved_as, "key")
            # moves: directory + the four key files
            self.assertEqual(
                [(m.source, m.target, m.kind) for m in result.moves],
                [
                    (item(root, OLD), item(root, NEW), "directory"),
                    (item(root, OLD) / f"{OLD}.md", item(root, NEW) / f"{NEW}.md", "file"),
                    (item(root, OLD) / f"{OLD}.pdf", item(root, NEW) / f"{NEW}.pdf", "file"),
                    (item(root, OLD) / f"minerUmd_{OLD}.md", item(root, NEW) / f"minerUmd_{NEW}.md", "file"),
                    (item(root, OLD) / f"Figure解读_{OLD}.md", item(root, NEW) / f"Figure解读_{NEW}.md", "file"),
                ],
            )
            # edits: main + 2 derived + 2 cards + 4 occurrence files,
            # compared in the same (path, kind) sort order the plan uses
            self.assertEqual(
                sorted((str(e.path), e.kind) for e in result.edits),
                sorted(
                    [
                        (str(note(root, OLD)), "main_note"),
                        (str(item(root, OLD) / f"minerUmd_{OLD}.md"), "derived_note"),
                        (str(item(root, OLD) / f"Figure解读_{OLD}.md"), "derived_note"),
                        (str(item(root, OLD) / "cards" / "card-concept.md"), "derived_note"),
                        (str(item(root, OLD) / "cards" / "card-method.md"), "derived_note"),
                        (str(root / "notes" / "crlf-note.md"), "occurrences"),
                        (str(root / "notes" / "reading-notes.md"), "occurrences"),
                        (str(root / "notes" / "中文笔记.md"), "occurrences"),
                        (str(root / LIT / "smithExample2026" / "smithExample2026.md"), "occurrences"),
                    ]
                ),
            )
            # 22 occurrences: 11 pandoc + 11 wikilink
            self.assertEqual(len(result.occurrences), 22)
            self.assertEqual(
                sum(1 for o in result.occurrences if o.kind == "pandoc"), 11
            )
            self.assertEqual(
                sum(1 for o in result.occurrences if o.kind == "wikilink"), 11
            )
            # files: 11 scanned .md + the primary PDF
            self.assertEqual(len(result.plan["files"]), 12)
            self.assertTrue(all(f["type"] == "file" for f in result.plan["files"]))
            self.assertTrue(all(f["sha256"] for f in result.plan["files"]))

    def test_preview_occurrence_fields_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview_token(root)
            reading = [o for o in result.occurrences if str(o.path).endswith("reading-notes.md")]
            pandoc = next(o for o in reading if o.kind == "pandoc" and o.line == 3)
            self.assertEqual(pandoc.column, 34)
            self.assertEqual(pandoc.old, OLD)
            self.assertEqual(pandoc.new, NEW)
            self.assertIn("[@", pandoc.context)
            wikilink = next(o for o in reading if o.kind == "wikilink" and o.line == 7)
            self.assertEqual(wikilink.column, 18)
            # the hidden area and clean file contribute nothing
            self.assertFalse(any("hidden" in str(o.path) for o in result.occurrences))
            self.assertFalse(any(str(o.path).endswith("clean.md") for o in result.occurrences))

    def test_preview_is_read_only_no_writes_no_lock_no_hook(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            result = citations.preview_rename_key(root, key=OLD, new_key=NEW)
            self.assertEqual(vault_manifest(root), before)
            self.assertFalse((root / ".paper-notes").exists())
            self.assertEqual(staging_residue(root), [])
            self.assertIsNotNone(result.confirmation_token)
            self.assertIsNotNone(result.plan)

    def test_preview_token_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            first = preview_token(root).confirmation_token
            second = preview_token(root).confirmation_token
            self.assertEqual(first, second)

    def test_preview_resolves_alias_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview_token(root, key=ALIAS)
            self.assertEqual(result.citation_key, OLD)  # canonical old
            self.assertEqual(result.resolved_as, "alias")
            self.assertEqual(result.requested_key, ALIAS)
            self.assertEqual(str(result.paper_id), PAPER_ID)

    def test_preview_unknown_key_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            with self.assertRaises(items.ItemError):
                preview_token(root, key="ghost2026")

    def test_preview_invalid_new_key_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            for bad in ("1bad-start", "has space", "bad/key", "../escape", "bad;key"):
                with self.assertRaises(items.ItemError):
                    preview_token(root, new_key=bad)

    def test_preview_same_key_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            with self.assertRaises(items.ItemError):
                preview_token(root, new_key=OLD)

    def test_preview_repository_identity_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            write_raw_paper(root, "dupKey2026", PAPER_ID)  # duplicate paper_id
            with self.assertRaises(items.ItemConflict):
                preview_token(root)

    def test_preview_warns_on_skipped_symlink_binary_and_broken_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (root / "notes" / "link.md").symlink_to(root / "notes" / "clean.md")
            (root / "notes" / "binary.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
            (root / "notes" / "broken-fm.md").write_text("---\ntitle: x\n", encoding="utf-8")
            result = preview_token(root)
            messages = " ".join(result.warnings)
            self.assertIn("link.md", messages)
            self.assertIn("binary.md", messages)
            self.assertIn("broken-fm.md", messages)


# ---------------------------------------------------------------------------
# 3. confirm success
# ---------------------------------------------------------------------------


class ConfirmSuccessTest(unittest.TestCase):
    def test_confirm_renames_directory_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            result = confirm(root, get_token(root), hook=hook)
            self.assertEqual(result.status, "renamed")
            self.assertEqual(result.citation_key, NEW)
            self.assertEqual(result.old_key, OLD)
            self.assertEqual(str(result.paper_id), PAPER_ID)
            self.assertEqual(result.path, str(note(root, NEW)))
            self.assertTrue(item(root, NEW).is_dir())
            self.assertFalse(item(root, OLD).exists())
            for name in (f"{NEW}.md", f"{NEW}.pdf", f"minerUmd_{NEW}.md", f"Figure解读_{NEW}.md"):
                self.assertTrue((item(root, NEW) / name).is_file(), name)
            for name in (f"{OLD}.md", f"{OLD}.pdf", f"minerUmd_{OLD}.md", f"Figure解读_{OLD}.md"):
                self.assertFalse((item(root, NEW) / name).exists(), name)
            # attachments / cards / figures / unknown files preserved
            for rel in ("attachments/fig_extra.png", "attachments/data.csv", "figures/a1b2c3d4e5f6.png", "stray-notes.txt", "cards/card-concept.md", "cards/card-method.md"):
                self.assertTrue((item(root, NEW) / rel).is_file(), rel)
            hook.assert_called_once()

    def test_post_rename_index_verifies_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            index = build_index(root)
            self.assertEqual(str(index.by_key[NEW].paper.paper_id), PAPER_ID)
            self.assertEqual(index.aliases[OLD], NEW)
            self.assertEqual(index.aliases[ALIAS], NEW)
            self.assertEqual(str(index.by_id[__import__("uuid").UUID(PAPER_ID)].path), str(note(root, NEW)))
            self.assertEqual(index.by_key[NEW].paper.citation_key, NEW)
            self.assertIn(OLD, index.by_key[NEW].paper.citation_key_aliases)

    def test_confirm_main_note_key_and_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            paper, _ = load_paper_note(note(root, NEW))
            self.assertEqual(paper.citation_key, NEW)
            self.assertEqual(str(paper.paper_id), PAPER_ID)
            self.assertEqual(paper.citation_key_aliases, [ALIAS, OLD])  # existing kept, old appended
            # untouched fields survive
            self.assertEqual(paper.title, "Spatially resolved analysis of pulmonary adenocarcinoma")
            self.assertEqual(paper.pdf_status, "available")

    def test_confirm_derived_notes_relationship_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            derived = [
                item(root, NEW) / f"minerUmd_{NEW}.md",
                item(root, NEW) / f"Figure解读_{NEW}.md",
                item(root, NEW) / "cards" / "card-concept.md",
                item(root, NEW) / "cards" / "card-method.md",
            ]
            for path in derived:
                # semantic assertions through the real frontmatter parser
                # (ruamel's legal output may quote the scalar either way,
                # so the relationship fields are asserted by value, not by
                # a hard-coded serialized spelling)
                doc = citations._load_light_doc(path)
                self.assertEqual(doc.frontmatter["citation_key"], NEW, str(path))
                self.assertEqual(doc.frontmatter["paper"], f"[[{NEW}]]", str(path))
                self.assertEqual(doc.frontmatter["paper_id"], PAPER_ID, str(path))
                # the old key must not remain anywhere in the file
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(f"citation_key: {OLD}", text, str(path))
                self.assertNotIn(f"[[{OLD}]]", text, str(path))
                self.assertNotIn(f"[@{OLD}]", text, str(path))

    def test_confirm_body_edits_and_immunity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            text = (root / "notes" / "reading-notes.md").read_text(encoding="utf-8")
            self.assertIn("[@shiauSpatiallyResolvedMethods2025]", text)
            self.assertIn("[@shiauSpatiallyResolvedMethods2025; @jonesOther2026]", text)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025]]", text)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025|the paper]]", text)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025#Methods]]", text)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025#^b1]]", text)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025|display]]", text)
            # immune regions keep the old key verbatim
            self.assertIn(r"\[@shiauSpatiallyResolvedAnalysis2024]", text)
            self.assertIn(r"\[[shiauSpatiallyResolvedAnalysis2024]]", text)
            self.assertIn("`[@shiauSpatiallyResolvedAnalysis2024]`", text)
            self.assertIn("`` [[shiauSpatiallyResolvedAnalysis2024]] ``", text)
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] inside backticks", text)
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] inside tildes", text)
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] indented fence", text)
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] indented code block", text)
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] inside pre", text)
            self.assertIn("[[shiauSpatiallyResolvedAnalysis2024]] inside code", text)
            # boundary keys untouched
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024x]", text)
            self.assertIn("user@shiauSpatiallyResolvedAnalysis2024.example", text)
            # other papers updated too
            smith = (root / LIT / "smithExample2026" / "smithExample2026.md").read_text(encoding="utf-8")
            self.assertIn("[@shiauSpatiallyResolvedMethods2025]", smith)

    def test_confirm_crlf_and_unicode_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            crlf = (root / "notes" / "crlf-note.md").read_bytes()
            self.assertIn(b"\r\n", crlf)
            self.assertNotIn(b"\n", crlf.replace(b"\r\n", b""))  # no bare LF
            self.assertIn(b"[@shiauSpatiallyResolvedMethods2025]", crlf)
            self.assertNotIn(b"[@shiauSpatiallyResolvedAnalysis2024]", crlf)
            uni = (root / "notes" / "中文笔记.md").read_text(encoding="utf-8")
            self.assertIn("这是一篇中文笔记", uni)
            self.assertIn("[@shiauSpatiallyResolvedMethods2025]", uni)
            self.assertIn("[[shiauSpatiallyResolvedMethods2025]]", uni)
            self.assertNotIn(OLD, uni)

    def test_confirm_untouched_files_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            confirm(root, get_token(root))
            after = vault_manifest(root)
            added, removed, changed = manifest_diff(before, after)

            def mapped(p: str) -> str:
                """Old-subtree manifest path -> its post-rename path (the
                directory rename also renames the top-level key files)."""
                prefix = f"{LIT}/{OLD}"
                if not (p == prefix or p.startswith(prefix + "/")):
                    return p
                if p == prefix:
                    return f"{LIT}/{NEW}"
                child = p[len(prefix) + 1:]
                head, _, tail = child.partition("/")
                if tail:
                    return f"{LIT}/{NEW}/{child}"
                renamed = {
                    f"{OLD}.md": f"{NEW}.md",
                    f"{OLD}.pdf": f"{NEW}.pdf",
                    f"minerUmd_{OLD}.md": f"minerUmd_{NEW}.md",
                    f"Figure解读_{OLD}.md": f"Figure解读_{NEW}.md",
                }
                return f"{LIT}/{NEW}/" + renamed.get(head, head)

            # removed == the whole old item subtree; added == the new subtree
            self.assertEqual(
                removed,
                [p for p in before if p == f"{LIT}/{OLD}" or p.startswith(f"{LIT}/{OLD}/")],
            )
            self.assertEqual(
                added,
                [p for p in after if p == f"{LIT}/{NEW}" or p.startswith(f"{LIT}/{NEW}/")],
            )
            # path-mapped per-file assertions over the moved subtree:
            # edited notes change content (mode preserved); everything else
            # (attachments / figures / stray / unknown files) is byte- and
            # mode-identical; the primary PDF moves content-agnostically.
            edited_in_subtree = {
                f"{LIT}/{OLD}/{OLD}.md",
                f"{LIT}/{OLD}/minerUmd_{OLD}.md",
                f"{LIT}/{OLD}/Figure解读_{OLD}.md",
                f"{LIT}/{OLD}/cards/card-concept.md",
                f"{LIT}/{OLD}/cards/card-method.md",
            }
            for old_rel in removed:
                new_rel = mapped(old_rel)
                self.assertIn(new_rel, after, f"{old_rel} -> {new_rel}")
                self.assertEqual(before[old_rel][0], after[new_rel][0], old_rel)  # type
                self.assertEqual(before[old_rel][2], after[new_rel][2], old_rel)  # mode
                if before[old_rel][0] == "file":
                    if old_rel in edited_in_subtree:
                        self.assertNotEqual(
                            before[old_rel][1], after[new_rel][1], old_rel
                        )
                    else:
                        self.assertEqual(
                            before[old_rel][1], after[new_rel][1], old_rel
                        )
            # the only same-path files whose content changed are the four
            # global occurrence files (everything else that changed was a
            # removed/added pair inside the moved subtree)
            self.assertEqual(
                changed,
                [
                    f"{LIT}/smithExample2026/smithExample2026.md",
                    "notes/crlf-note.md",
                    "notes/reading-notes.md",
                    "notes/中文笔记.md",
                ],
            )
            # jones paper, clean note, hidden area untouched byte + mode
            for rel in (
                f"{LIT}/jonesOther2026/jonesOther2026.md",
                "notes/clean.md",
                ".hidden-dir/hidden.md",
            ):
                self.assertEqual(before[rel], after[rel], rel)
                self.assertNotIn(rel, changed)
            self.assertTrue(all(not p.startswith(".hidden-dir") for p in changed))

    def test_confirm_mode_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (root / "notes" / "reading-notes.md").chmod(0o640)
            (root / LIT / OLD / f"{OLD}.pdf").chmod(0o600)
            confirm(root, get_token(root))
            self.assertEqual(stat.S_IMODE((root / "notes" / "reading-notes.md").stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE((item(root, NEW) / f"{NEW}.pdf").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((item(root, NEW) / f"{NEW}.md").stat().st_mode), 0o644)

    def test_confirm_no_staging_residue_and_lock_released(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_confirm_minimal_item_only_main_note(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_raw_paper(root, OLD, PAPER_ID, aliases=(ALIAS,))
            result = preview_token(root)
            self.assertEqual([(m.source.name, m.target.name, m.kind) for m in result.moves], [
                (OLD, NEW, "directory"),
                (f"{OLD}.md", f"{NEW}.md", "file"),
            ])
            self.assertEqual(len(result.edits), 1)
            confirm(root, result.confirmation_token)
            self.assertTrue(note(root, NEW).is_file())
            self.assertFalse(item(root, OLD).exists())
            index = build_index(root)
            self.assertEqual(index.aliases[OLD], NEW)

    def test_confirm_via_alias_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = preview_token(root, key=ALIAS)
            self.assertEqual(result.citation_key, OLD)
            confirm(root, result.confirmation_token, key=ALIAS)
            index = build_index(root)
            self.assertIn(NEW, index.by_key)
            self.assertEqual(index.aliases[OLD], NEW)

    def test_confirm_optional_path_combos(self):
        # pdf but no derived notes
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"minerUmd_{OLD}.md").unlink()
            (item(root, OLD) / f"Figure解读_{OLD}.md").unlink()
            shutil.rmtree(item(root, OLD) / "cards")
            result = preview_token(root)
            self.assertEqual(len(result.moves), 3)  # dir + main + pdf
            confirm(root, result.confirmation_token)
            self.assertTrue((item(root, NEW) / f"{NEW}.pdf").is_file())
            self.assertFalse((item(root, NEW) / f"minerUmd_{NEW}.md").exists())
        # derived notes but no pdf
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"{OLD}.pdf").unlink()
            result = preview_token(root)
            self.assertEqual(len(result.moves), 4)  # dir + main + 2 derived
            confirm(root, result.confirmation_token)
            self.assertTrue((item(root, NEW) / f"minerUmd_{NEW}.md").is_file())
            self.assertFalse((item(root, NEW) / f"{NEW}.pdf").exists())


# ---------------------------------------------------------------------------
# 4. collisions: zero writes before any mutation
# ---------------------------------------------------------------------------


class CollisionTest(unittest.TestCase):
    def assert_zero_writes(self, root):
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])

    def test_same_key_is_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            with self.assertRaises(items.ItemError):
                preview_token(root, new_key=OLD)
            self.assertEqual(vault_manifest(root), before)
            self.assert_zero_writes(root)

    def test_new_key_collides_current_key_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, get_token(root), new_key=OTHER, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            hook.assert_not_called()
            self.assert_zero_writes(root)

    def test_new_key_collides_alias_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root, new_key=ALIAS)
            self.assertEqual(vault_manifest(root), before)
            self.assert_zero_writes(root)

    def test_target_directory_occupied_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, NEW)).mkdir()
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)
            self.assert_zero_writes(root)

    def test_target_directory_is_file_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (Path(root) / LIT / NEW).write_text("occupied", encoding="utf-8")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)

    def test_target_directory_is_symlink_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (Path(root) / LIT / NEW).symlink_to(Path(root) / LIT / OTHER)
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)

    def test_item_directory_symlink_item_conflict(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            make_vault(root)
            shutil.rmtree(item(root, OLD))
            (item(root, OLD).parent / "real-old").mkdir()
            (item(root, OLD).parent / "real-old" / f"{OLD}.md").write_text(
                (FIXTURE / LIT / OLD / f"{OLD}.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (item(root, OLD)).symlink_to(item(root, OLD).parent / "real-old")
            with self.assertRaises(items.ItemConflict):
                preview_token(root)

    def test_main_note_symlink_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"{OLD}.md").unlink()
            (item(root, OLD) / f"{OLD}.md").symlink_to(item(root, OLD) / f"minerUmd_{OLD}.md")
            with self.assertRaises(items.ItemConflict):
                preview_token(root)

    def test_derived_note_symlink_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"minerUmd_{OLD}.md").unlink()
            (item(root, OLD) / f"minerUmd_{OLD}.md").symlink_to(item(root, OLD) / f"{OLD}.md")
            with self.assertRaises(items.ItemConflict):
                preview_token(root)

    def test_old_pdf_symlink_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"{OLD}.pdf").unlink()
            (item(root, OLD) / f"{OLD}.pdf").symlink_to(item(root, OLD) / f"{OLD}.md")
            with self.assertRaises(items.ItemConflict):
                preview_token(root)

    def test_key_file_rename_target_occupied_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / f"{NEW}.md").write_text("already here", encoding="utf-8")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)

    def test_orphan_target_directory_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            orphan = item(root, NEW)
            orphan.mkdir()
            (orphan / "unindexed.md").write_text("# orphan", encoding="utf-8")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)

    def test_invalid_key_item_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            for bad in ("1abc", "a b", "a/b", "../x"):
                with self.assertRaises(items.ItemError):
                    preview_token(root, new_key=bad)


# ---------------------------------------------------------------------------
# 5. confirmation token: wrong / replay / stale
# ---------------------------------------------------------------------------


class TokenTest(unittest.TestCase):
    def assert_conflict_zero_writes(self, root, hook):
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])
        hook.assert_not_called()

    def test_wrong_token_conflict_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, "0" * 64, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assert_conflict_zero_writes(root, hook)

    def test_replay_after_success_does_not_reexecute(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            confirm(root, token)
            # The requested key is now a reserved alias and the current
            # key equals the requested new key: the exact transition the
            # token authorized has already been consumed. Task-13
            # hardening turns this into a stale ItemConflict (rc3, zero
            # writes, hook 0) instead of a plain same-key user error —
            # nothing re-executes.
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assert_conflict_zero_writes(root, mock.Mock())

    def test_stale_token_file_added(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            (root / "notes" / "new-note.md").write_text("new [@shiauSpatiallyResolvedAnalysis2024]", encoding="utf-8")
            before = vault_manifest(root)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assert_conflict_zero_writes(root, hook)

    def test_stale_token_file_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            (root / "notes" / "clean.md").unlink()
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assertEqual(vault_manifest(root), before)

    def test_stale_token_file_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            (root / "notes" / "clean.md").write_text("edited\n", encoding="utf-8")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assertEqual(vault_manifest(root), before)

    def test_stale_token_file_type_changed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            (root / "notes" / "clean.md").unlink()
            (root / "notes" / "clean.md").symlink_to(root / "notes" / "reading-notes.md")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assertEqual(vault_manifest(root), before)

    def test_stale_token_target_dir_appeared(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            (item(root, NEW)).mkdir()
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assertEqual(vault_manifest(root), before)

    def test_stale_token_main_note_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            note_path = note(root, OLD)
            note_path.write_text(note_path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                confirm(root, token)
            self.assertEqual(vault_manifest(root), before)


# ---------------------------------------------------------------------------
# 6. rollback: injected failure at every stage restores byte-identical vault
# ---------------------------------------------------------------------------


class RollbackTest(unittest.TestCase):
    def assert_restored(self, root, before, hook):
        self.assertEqual(vault_manifest(root), before)
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])

    def test_failure_at_directory_rename_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            # Task-13 hardening replaces the bare os.rename for the item
            # directory move with an atomic no-replace primitive; the
            # failure injection point moves with it.
            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_not_called()

    def test_failure_at_global_edit_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            target = root / "notes" / "reading-notes.md"
            real_write = fsops.write_target

            def failing_write(op, path, content):
                if path == target:
                    raise fsops.OperationConflict("simulated mid-write failure")
                return real_write(op, path, content)

            with mock.patch("paper_notes.citations.fsops.write_target", side_effect=failing_write):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_not_called()

    def test_failure_at_item_edit_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            # after the directory rename the derived note lives in the
            # hidden work directory
            target = workdir(root) / f"minerUmd_{OLD}.md"
            real_write = fsops.write_target

            def failing_write(op, path, content):
                if path == target:
                    raise fsops.OperationConflict("simulated mid-write failure")
                return real_write(op, path, content)

            with mock.patch("paper_notes.citations.fsops.write_target", side_effect=failing_write):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_not_called()

    def test_failure_at_file_rename_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            # the new main note is created inside the hidden work directory
            target = workdir(root) / f"{NEW}.md"
            real_write = fsops.write_target

            def failing_write(op, path, content):
                if path == target:
                    raise fsops.OperationConflict("simulated mid-write failure")
                return real_write(op, path, content)

            with mock.patch("paper_notes.citations.fsops.write_target", side_effect=failing_write):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_not_called()

    def test_failure_at_hook_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock(side_effect=RuntimeError("rebuild boom"))
            with self.assertRaises(items.ItemError):
                confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_called_once()  # the hook itself ran and failed

    def test_failure_at_post_verify_restores_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            with mock.patch("paper_notes.citations._post_verify", side_effect=RuntimeError("verify boom")):
                with self.assertRaises(items.ItemError):
                    confirm(root, get_token(root), hook=hook)
            self.assert_restored(root, before, hook)
            hook.assert_not_called()

    def test_commit_conflict_surfaces_item_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            real_commit = fsops.commit

            def conflicted_commit(op):
                conflicts = real_commit(op)
                return conflicts + [str(note(root, NEW))]

            with mock.patch("paper_notes.citations.fsops.commit", side_effect=conflicted_commit):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            hook.assert_called_once()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_racer_edit_during_write_window_conflicts_and_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            target = root / "notes" / "reading-notes.md"
            real_write = fsops.write_target

            def racer_write(op, path, content):
                if path == target:
                    target.write_text("racer edit", encoding="utf-8")  # external change
                return real_write(op, path, content)

            with mock.patch("paper_notes.citations.fsops.write_target", side_effect=racer_write):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer's edit is preserved; everything else is restored
            self.assertEqual((root / "notes" / "reading-notes.md").read_text(encoding="utf-8"), "racer edit")
            self.assertTrue(item(root, OLD).is_dir())
            self.assertFalse(item(root, NEW).exists())
            self.assertIn("[@shiauSpatiallyResolvedAnalysis2024]",
                          (root / "notes" / "crlf-note.md").read_text(encoding="utf-8"))
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())


# ---------------------------------------------------------------------------
# 7. CLI envelopes and exit codes
# ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def run_cli(self, *args, cwd=REPO):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            input="",
            timeout=90,
        )

    def test_cli_dry_run_needs_confirmation_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli("item", "rename-key", "--vault", str(root), "--key", OLD, "--new-key", NEW, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            self.assertEqual(payload["data"]["action"], "rename_key")
            self.assertIn("confirmation_token", payload["data"])
            self.assertEqual(payload["data"]["plan"]["old_key"], OLD)
            self.assertEqual(payload["data"]["plan"]["new_key"], NEW)
            self.assertEqual(payload["data"]["plan"]["paper_id"], PAPER_ID)

    def test_cli_confirm_success_rc0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            result = self.run_cli("item", "rename-key", "--vault", str(root), "--key", OLD, "--new-key", NEW, "--confirm-token", token)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["action"], "renamed")
            self.assertEqual(payload["data"]["citation_key"], NEW)
            self.assertEqual(payload["data"]["old_key"], OLD)
            self.assertTrue(item(root, NEW).is_dir())
            self.assertFalse(item(root, OLD).exists())

    def test_cli_confirm_wrong_token_rc3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli("item", "rename-key", "--vault", str(root), "--key", OLD, "--new-key", NEW, "--confirm-token", "f" * 64)
            self.assertEqual(result.returncode, 3, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "conflict")

    def test_cli_invalid_key_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli("item", "rename-key", "--vault", str(root), "--key", OLD, "--new-key", "bad key", "--dry-run")
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_dry_run_with_confirm_token_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            result = self.run_cli("item", "rename-key", "--vault", str(root), "--key", OLD, "--new-key", NEW, "--dry-run", "--confirm-token", "x")
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")

    def test_cli_secret_marker_never_leaks(self):
        # In-process CLI harness: an in-process mock cannot cross a
        # subprocess boundary, so the real CLI main() is driven directly
        # with stdout/stderr captured (argparse usage errors surface as
        # SystemExit and are normalized to their exit code).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            secret = "«redacted:sk-…»"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch(
                "paper_notes.citations.preview_rename_key",
                side_effect=RuntimeError(secret),
            ):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        rc = cli.main(
                            [
                                "--json",
                                "item",
                                "rename-key",
                                "--vault",
                                str(root),
                                "--key",
                                OLD,
                                "--new-key",
                                NEW,
                                "--dry-run",
                            ]
                        )
                    except SystemExit as exc:
                        rc = exc.code if isinstance(exc.code, int) else 2
            self.assertEqual(rc, 4)
            self.assertEqual(len(out.getvalue().strip().splitlines()), 1)
            self.assertNotIn(secret, out.getvalue())
            self.assertNotIn(secret, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["errors"][0]["message"], "Internal error")


# ---------------------------------------------------------------------------
# 8. Task-13 first-round management hardening (A-J reproductions + boundaries)
# ---------------------------------------------------------------------------


class ScannerHtmlCloseTest(unittest.TestCase):
    """A: after a cross-line HTML </pre>/</code> close, the suffix must
    keep being scanned and every hit column stays relative to the
    original full line (the closing line is never trimmed)."""

    def test_html_close_then_suffix_scanned_with_full_line_columns(self):
        text = "<pre>\n[@shiauSpatiallyResolvedAnalysis2024]\n</pre> cite [@shiauSpatiallyResolvedAnalysis2024]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 2)
        # the @ sits at column 13 of the ORIGINAL line "</pre> cite [@old]"
        self.assertEqual(hits[0].column, 13)
        self.assertEqual(hits[0].start, 14)
        self.assertEqual(hits[0].end, 48)
        self.assertEqual(hits[0].kind, "pandoc")
        self.assertEqual(
            citations.apply_hits(text, NEW, hits),
            "<pre>\n[@shiauSpatiallyResolvedAnalysis2024]\n"
            "</pre> cite [@shiauSpatiallyResolvedMethods2025]\n",
        )

    def test_html_close_suffix_with_prefix_and_tail(self):
        text = (
            "aa<pre>\n"
            "[@shiauSpatiallyResolvedAnalysis2024]\n"
            "</pre> bb [@shiauSpatiallyResolvedAnalysis2024] cc "
            "[[shiauSpatiallyResolvedAnalysis2024]] dd\n"
        )
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 2)
        self.assertEqual([h.kind for h in hits], ["pandoc", "wikilink"])
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(hits[0].column, 11)
        self.assertEqual(hits[1].line, 2)
        # the first "[" of "[[old]]" sits at column 51 of the original line
        self.assertEqual(hits[1].column, 51)
        rewritten = citations.apply_hits(text, NEW, hits)
        self.assertIn("</pre> bb [@shiauSpatiallyResolvedMethods2025] cc [[shiauSpatiallyResolvedMethods2025]] dd", rewritten)
        self.assertIn("aa<pre>\n[@shiauSpatiallyResolvedAnalysis2024]", rewritten)

    def test_html_close_suffix_crlf(self):
        text = "<pre>\r\n[@shiauSpatiallyResolvedAnalysis2024]\r\n</pre> cite [@shiauSpatiallyResolvedAnalysis2024]\r\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(hits[0].column, 13)
        rewritten = citations.apply_hits(text, NEW, hits)
        self.assertEqual(
            rewritten,
            "<pre>\r\n[@shiauSpatiallyResolvedAnalysis2024]\r\n"
            "</pre> cite [@shiauSpatiallyResolvedMethods2025]\r\n",
        )

    def test_code_close_then_suffix_scanned(self):
        text = "<code>\n[@shiauSpatiallyResolvedAnalysis2024]\n</code> then [@shiauSpatiallyResolvedAnalysis2024]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(hits[0].column, 14)
        self.assertEqual(
            citations.apply_hits(text, NEW, hits),
            "<code>\n[@shiauSpatiallyResolvedAnalysis2024]\n"
            "</code> then [@shiauSpatiallyResolvedMethods2025]\n",
        )


class ScannerInlineCloseTest(unittest.TestCase):
    """B: after a multi-line inline code span closes, the same-line
    suffix must keep being scanned; the code content stays immune."""

    def test_inline_code_close_then_suffix_scanned(self):
        text = "`code\nend` cite [@shiauSpatiallyResolvedAnalysis2024]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 1)
        self.assertEqual(hits[0].column, 11)
        self.assertEqual(
            citations.apply_hits(text, NEW, hits),
            "`code\nend` cite [@shiauSpatiallyResolvedMethods2025]\n",
        )

    def test_inline_code_close_then_wikilink_suffix(self):
        text = "`code\nend` [[shiauSpatiallyResolvedAnalysis2024]]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "wikilink")
        self.assertEqual(hits[0].line, 1)
        self.assertEqual(hits[0].column, 5)
        self.assertEqual(
            citations.apply_hits(text, NEW, hits),
            "`code\nend` [[shiauSpatiallyResolvedMethods2025]]\n",
        )

    def test_inline_code_close_then_both(self):
        text = "`code\nend` [@shiauSpatiallyResolvedAnalysis2024] and [[shiauSpatiallyResolvedAnalysis2024]]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 2)
        self.assertEqual([h.kind for h in hits], ["pandoc", "wikilink"])
        self.assertEqual(
            citations.apply_hits(text, NEW, hits),
            "`code\nend` [@shiauSpatiallyResolvedMethods2025] and [[shiauSpatiallyResolvedMethods2025]]\n",
        )

    def test_inline_code_unclosed_span_still_immune_then_suffix(self):
        text = "`open\n[@shiauSpatiallyResolvedAnalysis2024] swallowed\nmore ` tail [@shiauSpatiallyResolvedAnalysis2024]\n"
        hits = citations.scan_markdown(text, OLD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(hits[0].column, 13)
        rewritten = citations.apply_hits(text, NEW, hits)
        self.assertIn("[@shiauSpatiallyResolvedAnalysis2024] swallowed", rewritten)
        self.assertIn("more ` tail [@shiauSpatiallyResolvedMethods2025]", rewritten)


# ---------------------------------------------------------------------------
# C/D/E: the token binds the WHOLE old item subtree (files, dirs incl.
# empty ones, modes, types) plus every scanned .md (even skipped ones).
# ---------------------------------------------------------------------------


class SubtreeTokenTest(unittest.TestCase):
    def assert_stale_zero_writes(self, root, hook):
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())
        self.assertEqual(staging_residue(root), [])
        hook.assert_not_called()

    def test_attachment_add_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            racer = item(root, OLD) / "attachments" / "racer.bin"
            racer.write_bytes(b"racer")
            before = vault_manifest(root)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assertTrue(racer.is_file())
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_attachment_edit_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            data = item(root, OLD) / "attachments" / "data.csv"
            data.write_text("edited", encoding="utf-8")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(data.read_text(encoding="utf-8"), "edited")
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_attachment_delete_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            data = item(root, OLD) / "attachments" / "data.csv"
            data.unlink()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertFalse(data.exists())
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_attachment_chmod_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            data = item(root, OLD) / "attachments" / "data.csv"
            data.chmod(0o600)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o600)
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_empty_dir_add_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            racer = item(root, OLD) / "empty-dir"
            racer.mkdir()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertTrue(racer.is_dir())
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_empty_dir_delete_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            empty = item(root, OLD) / "empty-dir"
            empty.mkdir()
            token = get_token(root)
            empty.rmdir()
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertFalse(empty.exists())
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_empty_dir_chmod_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            empty = item(root, OLD) / "empty-dir"
            empty.mkdir()
            token = get_token(root)
            empty.chmod(0o700)
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o700)
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_empty_dir_type_change_after_preview_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            empty = item(root, OLD) / "empty-dir"
            empty.mkdir()
            token = get_token(root)
            empty.rmdir()
            empty.write_text("now a file", encoding="utf-8")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertTrue(empty.is_file())
            self.assertFalse(item(root, NEW).exists())
            self.assert_stale_zero_writes(root, hook)

    def test_empty_dir_present_at_preview_moves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            (item(root, OLD) / "empty-dir").mkdir()
            result = preview_token(root)
            confirm(root, result.confirmation_token)
            self.assertTrue((item(root, NEW) / "empty-dir").is_dir())
            self.assertFalse(item(root, OLD).exists())

    def test_subtree_symlink_preview_structural_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            link = item(root, OLD) / "attachments" / "link.md"
            link.symlink_to(item(root, OLD) / f"{OLD}.md")
            before = vault_manifest(root)
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertEqual(vault_manifest(root), before)
            self.assertTrue(link.is_symlink())

    def test_subtree_fifo_preview_structural_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            pipe = item(root, OLD) / "attachments" / "pipe"
            os.mkfifo(pipe)
            # NOTE: vault_manifest() reads file bytes and would block on a
            # fifo, so zero-write proof here is lock + staging residue only.
            with self.assertRaises(items.ItemConflict):
                preview_token(root)
            self.assertTrue(stat.S_ISFIFO(pipe.lstat().st_mode))
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])

    def test_skipped_nonutf8_md_bound_to_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            binary = root / "notes" / "binary.md"
            binary.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
            token = get_token(root)
            binary.write_bytes(b"\xfe\xff other bytes")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assertEqual(binary.read_bytes(), b"\xfe\xff other bytes")
            self.assert_stale_zero_writes(root, hook)

    def test_skipped_malformed_md_bound_to_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            broken = root / "notes" / "broken-fm.md"
            broken.write_text("---\ntitle: x\n", encoding="utf-8")
            token = get_token(root)
            broken.write_text("---\ntitle: x\nchanged\n", encoding="utf-8")
            hook = mock.Mock()
            with self.assertRaises(items.ItemConflict):
                confirm(root, token, hook=hook)
            self.assert_stale_zero_writes(root, hook)


# ---------------------------------------------------------------------------
# F: every key source is re-verified against its plan-time expected
# type/hash/mode when used and again after the read; symlink/dir/deleted
# sources fail closed.
# ---------------------------------------------------------------------------


class PdfSourceGuardTest(unittest.TestCase):
    def test_pdf_bytes_replaced_at_execute_entry_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old_pdf = item(root, OLD) / f"{OLD}.pdf"
            real_begin = fsops.begin_operation
            external = b"EXTERNAL-PDF-BYTES"

            def racer_begin(vault, opid):
                old_pdf.write_bytes(external)
                return real_begin(vault, opid)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation",
                side_effect=racer_begin,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer's external bytes are preserved; nothing was written
            self.assertEqual(old_pdf.read_bytes(), external)
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_pdf_replaced_between_read_and_write_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            pdf_exec = workdir(root) / f"{OLD}.pdf"  # post-move source path
            real_read = citations._read_bytes

            def racy_read(path):
                data = real_read(path)
                if path == pdf_exec:
                    path.write_bytes(b"RACER-PDF")  # read -> write race
                return data

            with mock.patch("paper_notes.citations._read_bytes", side_effect=racy_read):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer's bytes survive the rollback at the restored path
            self.assertEqual((item(root, OLD) / f"{OLD}.pdf").read_bytes(), b"RACER-PDF")
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_pdf_deleted_at_execute_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old_pdf = item(root, OLD) / f"{OLD}.pdf"
            real_begin = fsops.begin_operation

            def racer_begin(vault, opid):
                old_pdf.unlink()
                return real_begin(vault, opid)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation",
                side_effect=racer_begin,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertFalse(old_pdf.exists())
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_pdf_symlink_at_execute_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old_pdf = item(root, OLD) / f"{OLD}.pdf"
            real_begin = fsops.begin_operation

            def racer_begin(vault, opid):
                old_pdf.unlink()
                old_pdf.symlink_to(item(root, OLD) / f"{OLD}.md")
                return real_begin(vault, opid)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation",
                side_effect=racer_begin,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(old_pdf.is_symlink())
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_pdf_dir_at_execute_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old_pdf = item(root, OLD) / f"{OLD}.pdf"
            real_begin = fsops.begin_operation

            def racer_begin(vault, opid):
                old_pdf.unlink()
                old_pdf.mkdir()
                return real_begin(vault, opid)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation",
                side_effect=racer_begin,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(old_pdf.is_dir())
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_pdf_chmod_at_execute_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old_pdf = item(root, OLD) / f"{OLD}.pdf"
            real_begin = fsops.begin_operation

            def racer_begin(vault, opid):
                old_pdf.chmod(0o600)
                return real_begin(vault, opid)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation",
                side_effect=racer_begin,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(stat.S_IMODE(old_pdf.stat().st_mode), 0o600)
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# G/H: each stage verifies the plan's expected fingerprint BEFORE and
# AFTER stage_target — a racer in the window is a conflict with the
# external bytes preserved, never the new baseline; new targets must be
# expected-absent before and after their stage.
# ---------------------------------------------------------------------------


class StageExpectedTest(unittest.TestCase):
    def test_edit_racer_between_precheck_and_stage_conflicts_and_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            target = root / "notes" / "reading-notes.md"
            real_stage = fsops.stage_target

            def racer_stage(op, path):
                if path == target:
                    target.write_text("EXTERNAL MANUAL", encoding="utf-8")
                return real_stage(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.stage_target",
                side_effect=racer_stage,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer edit is preserved; the transaction fully restored
            self.assertEqual(target.read_text(encoding="utf-8"), "EXTERNAL MANUAL")
            self.assertTrue(item(root, OLD).is_dir())
            self.assertFalse(item(root, NEW).exists())
            self.assertIn(
                "[@shiauSpatiallyResolvedAnalysis2024]",
                (root / "notes" / "crlf-note.md").read_text(encoding="utf-8"),
            )
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_new_target_racer_before_stage_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_note = workdir(root) / f"{NEW}.md"
            real_stage = fsops.stage_target

            def racer_stage(op, path):
                if path == new_note:
                    new_note.write_text("EXTERNAL TARGET", encoding="utf-8")
                return real_stage(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.stage_target",
                side_effect=racer_stage,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the external file survives (the work-directory rollback
            # moves it back with the item subtree; its bytes are
            # untouched)
            self.assertEqual(
                (item(root, OLD) / f"{NEW}.md").read_text(encoding="utf-8"),
                "EXTERNAL TARGET",
            )
            self.assertTrue(item(root, OLD).is_dir())
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_new_pdf_target_racer_before_stage_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_pdf = workdir(root) / f"{NEW}.pdf"
            real_stage = fsops.stage_target

            def racer_stage(op, path):
                if path == new_pdf:
                    new_pdf.write_bytes(b"EXTERNAL PDF TARGET")
                return real_stage(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.stage_target",
                side_effect=racer_stage,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(
                (item(root, OLD) / f"{NEW}.pdf").read_bytes(),
                b"EXTERNAL PDF TARGET",
            )
            self.assertTrue(item(root, OLD).is_dir())
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# I: the item directory move is atomic no-replace — an existing target
# (even an empty directory or a symlink) is never silently replaced.
# ---------------------------------------------------------------------------


class DirRenameRaceTest(unittest.TestCase):
    def test_empty_racer_target_dir_preserved_inode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            racer = item(root, NEW)
            real_rename = citations._rename_dir_noreplace

            def racer_rename(src, dst):
                if dst == racer and not racer.exists() and not racer.is_symlink():
                    racer.mkdir()  # empty racer appears after the fresh plan
                return real_rename(src, dst)

            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=racer_rename,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
                racer_ino = racer.stat().st_ino
            # the racer's empty dir is preserved in place (same inode);
            # the old item directory is untouched; zero writes, hook 0
            self.assertTrue(racer.is_dir())
            self.assertEqual(racer.stat().st_ino, racer_ino)
            self.assertTrue(item(root, OLD).is_dir())
            self.assertTrue(note(root, OLD).is_file())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_nonempty_racer_target_dir_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            racer = item(root, NEW)
            real_rename = citations._rename_dir_noreplace

            def racer_rename(src, dst):
                if dst == racer and not racer.exists() and not racer.is_symlink():
                    racer.mkdir()
                    (racer / "keep.txt").write_text("racer content", encoding="utf-8")
                return real_rename(src, dst)

            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=racer_rename,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(
                (racer / "keep.txt").read_text(encoding="utf-8"), "racer content"
            )
            self.assertTrue(item(root, OLD).is_dir())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_symlink_racer_target_dir_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            racer = item(root, NEW)
            real_rename = citations._rename_dir_noreplace

            def racer_rename(src, dst):
                if dst == racer and not racer.exists() and not racer.is_symlink():
                    racer.symlink_to(item(root, OLD))
                return real_rename(src, dst)

            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=racer_rename,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(racer.is_symlink())
            self.assertTrue(item(root, OLD).is_dir())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_rollback_does_not_clobber_racer_old_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            old, new, work = item(root, OLD), item(root, NEW), workdir(root)
            real_rename = citations._rename_dir_noreplace
            racer_placed = False

            def racer_rename(src, dst):
                nonlocal racer_placed
                if src == old and dst == work and not racer_placed:
                    racer_placed = True
                    real_rename(src, dst)
                    old.mkdir()  # racer recreates the old path mid-flight
                    return
                return real_rename(src, dst)

            real_write = fsops.write_target

            def failing_write(op, path, content):
                if path == root / "notes" / "reading-notes.md":
                    raise fsops.OperationConflict("simulated failure")
                return real_write(op, path, content)

            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=racer_rename,
            ), mock.patch(
                "paper_notes.citations.fsops.write_target",
                side_effect=failing_write,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer's old_dir is preserved in place; restoring the
            # item into it would require overwriting the racer —
            # explicitly refused, so the item remains in the hidden work
            # directory (never clobbered into the racer). The managed NEW
            # key files were rolled back; the original main note
            # (restored from its backup) is what survives at the work
            # path.
            self.assertTrue(old.is_dir())
            self.assertEqual(sorted(p.name for p in old.iterdir()), [])
            self.assertTrue(work.is_dir())
            self.assertTrue((work / f"{OLD}.md").is_file())
            self.assertFalse((work / f"{NEW}.md").exists())
            self.assertTrue((work / "attachments" / "fig_extra.png").is_file())
            self.assertFalse(new.exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# J: a replayed token after a consumed transition is a stale ItemConflict
# (rc3, zero writes, hook 0), never a plain same-key user error.
# ---------------------------------------------------------------------------


class ReplayStaleTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            input="",
            timeout=90,
        )

    def test_cli_replay_after_success_rc3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            token = get_token(root)
            first = self.run_cli(
                "item", "rename-key", "--vault", str(root),
                "--key", OLD, "--new-key", NEW, "--confirm-token", token,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cli(
                "item", "rename-key", "--vault", str(root),
                "--key", OLD, "--new-key", NEW, "--confirm-token", token,
            )
            self.assertEqual(second.returncode, 3, second.stderr)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["status"], "conflict")
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# low-level OSError / OperationConflict text never reaches the public
# ItemError / ItemConflict messages (stable, sanitized wording).
# ---------------------------------------------------------------------------


class SanitizationTest(unittest.TestCase):
    def test_low_level_oserror_marker_never_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            secret = "«redacted:os-error-marker»"
            with mock.patch(
                "paper_notes.citations._rename_dir_noreplace",
                side_effect=OSError(secret),
            ):
                with self.assertRaises(items.ItemError) as cm:
                    confirm(root, get_token(root))
            self.assertNotIn(secret, str(cm.exception))
            self.assertEqual(staging_residue(root), [])
            self.assertTrue(item(root, OLD).is_dir())

    def test_operation_conflict_marker_never_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            secret = "«redacted:op-conflict-marker»"
            real_stage = fsops.stage_target

            def marker_stage(op, path):
                if path == root / "notes" / "reading-notes.md":
                    raise fsops.OperationConflict(secret)
                return real_stage(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.stage_target",
                side_effect=marker_stage,
            ):
                with self.assertRaises(items.ItemConflict) as cm:
                    confirm(root, get_token(root))
            self.assertNotIn(secret, str(cm.exception))
            self.assertIn("concurrent change", str(cm.exception))
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# K: remediation-2 (Defect 1) — the EXACT plan subtree manifest is
# re-verified at every execution boundary: before any mutation, right
# after the atomic old->work move (closing check->move), and on the
# untouched parts immediately before finalization. Any late
# add/delete/edit/chmod/type/symlink/empty-dir change is a read-only
# ItemConflict; external state keeps its exact original path; the
# transaction writes 0, hook 0, staging 0.
# ---------------------------------------------------------------------------


def inject_at_begin(root, fn):
    real_begin = fsops.begin_operation

    def racer_begin(vault, opid):
        fn()
        return real_begin(vault, opid)

    return mock.patch(
        "paper_notes.citations.fsops.begin_operation", side_effect=racer_begin
    )


def inject_before_move(root, fn):
    real_rename = citations._rename_dir_noreplace
    fired = False

    def racer_rename(src, dst):
        nonlocal fired
        if not fired and src == item(root, OLD):
            fired = True
            fn()
        return real_rename(src, dst)

    return mock.patch(
        "paper_notes.citations._rename_dir_noreplace", side_effect=racer_rename
    )


def inject_late_in_work(root, fn):
    real_stage_expected = citations._stage_expected
    fired = False

    def racer_stage(op, path, expected):
        nonlocal fired
        if not fired:
            fired = True
            fn()
        return real_stage_expected(op, path, expected)

    return mock.patch(
        "paper_notes.citations._stage_expected", side_effect=racer_stage
    )


class SubtreeExecutionRaceTest(unittest.TestCase):
    def assert_conflict_clean(self, root, hook):
        self.assertFalse(item(root, NEW).exists())
        hook.assert_not_called()
        self.assertEqual(staging_residue(root), [])
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_entry_injection_add_edit_empty_dir_conflicts(self):
        # the manager's exact repro: at _execute entry, add
        # attachments/late.bin, edit attachments/data.csv, and add an
        # empty directory — all under the OLD item
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            token = get_token(root)
            before = vault_manifest(root)
            late = item(root, OLD) / "attachments" / "late.bin"
            data = item(root, OLD) / "attachments" / "data.csv"
            empty = item(root, OLD) / "empty-dir"

            def inject():
                late.write_bytes(b"LATE-BIN")
                data.write_text("EDITED", encoding="utf-8")
                empty.mkdir()

            with inject_at_begin(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, token, hook=hook)
            # external state preserved at exact original paths
            self.assertEqual(late.read_bytes(), b"LATE-BIN")
            self.assertEqual(data.read_text(encoding="utf-8"), "EDITED")
            self.assertTrue(empty.is_dir())
            # every non-racer entry byte-identical to the pre-confirm state
            after = vault_manifest(root)
            racer_rel = {
                f"{LIT}/{OLD}/attachments/late.bin",
                f"{LIT}/{OLD}/attachments/data.csv",
                f"{LIT}/{OLD}/empty-dir",
            }
            for rel in after:
                if rel in racer_rel:
                    continue
                self.assertEqual(after[rel], before[rel], rel)
            self.assert_conflict_clean(root, hook)

    def test_subtree_add_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            before = vault_manifest(root)
            racer = item(root, OLD) / "attachments" / "late.bin"

            with inject_before_move(root, lambda: racer.write_bytes(b"LATE")):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(racer.read_bytes(), b"LATE")
            # the only manifest difference is the racer's own entry
            after = vault_manifest(root)
            expected = dict(before)
            expected[f"{LIT}/{OLD}/attachments/late.bin"] = (
                "file",
                hashlib.sha256(b"LATE").hexdigest(),
                stat.S_IMODE(racer.stat().st_mode),
            )
            self.assertEqual(after, expected)
            self.assert_conflict_clean(root, hook)

    def test_subtree_delete_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            data = item(root, OLD) / "attachments" / "data.csv"

            with inject_before_move(root, data.unlink):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertFalse(data.exists())
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_subtree_edit_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            data = item(root, OLD) / "attachments" / "data.csv"

            def edit():
                data.write_text("EDITED", encoding="utf-8")

            with inject_before_move(root, edit):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(data.read_text(encoding="utf-8"), "EDITED")
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_subtree_chmod_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            data = item(root, OLD) / "attachments" / "data.csv"

            with inject_before_move(root, lambda: data.chmod(0o600)):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o600)
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_subtree_file_to_dir_type_change_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            data = item(root, OLD) / "attachments" / "data.csv"

            def to_dir():
                data.unlink()
                data.mkdir()

            with inject_before_move(root, to_dir):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(data.is_dir())
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_subtree_symlink_added_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            link = item(root, OLD) / "attachments" / "link"

            def add_link():
                link.symlink_to(item(root, OLD) / f"{OLD}.md")

            with inject_before_move(root, add_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(item(root, OLD) / f"{OLD}.md"))
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_subtree_empty_dir_added_before_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            empty = item(root, OLD) / "empty-dir"

            with inject_before_move(root, empty.mkdir):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(empty.is_dir())
            self.assertEqual(sorted(empty.iterdir()), [])
            self.assertFalse(item(root, NEW).exists())
            self.assert_conflict_clean(root, hook)

    def test_late_add_after_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            racer = workdir(root) / "attachments" / "late.bin"

            def add():
                racer.write_bytes(b"LATE")

            with inject_late_in_work(root, add):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            # the racer rides back with the subtree restore to its exact
            # original path, bytes untouched
            self.assertEqual(
                (item(root, OLD) / "attachments" / "late.bin").read_bytes(), b"LATE"
            )
            self.assert_conflict_clean(root, hook)

    def test_late_delete_after_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            data = workdir(root) / "attachments" / "data.csv"

            with inject_late_in_work(root, data.unlink):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertFalse((item(root, OLD) / "attachments" / "data.csv").exists())
            self.assert_conflict_clean(root, hook)

    def test_late_empty_dir_added_after_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            empty = workdir(root) / "empty-dir"

            with inject_late_in_work(root, empty.mkdir):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue((item(root, OLD) / "empty-dir").is_dir())
            self.assert_conflict_clean(root, hook)

    def test_late_symlink_added_after_move_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            link = workdir(root) / "attachments" / "link"

            def add_link():
                link.symlink_to(workdir(root) / f"{OLD}.md")

            with inject_late_in_work(root, add_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue((item(root, OLD) / "attachments" / "link").is_symlink())
            self.assert_conflict_clean(root, hook)


# ---------------------------------------------------------------------------
# L: remediation-2 (Defect 2) — delete sources that are not already part
# of the managed transaction get the plan expected-state guard through
# precheck -> stage -> postcheck before the delete; main/derived sources
# already staged/written keep the fsops managed-state verification
# (never compared to the original plan bytes). A racer between guard and
# stage, or after stage before delete, is a conflict with external
# bytes/inode/mode/type/path preserved.
# ---------------------------------------------------------------------------


class PdfDeleteRaceTest(unittest.TestCase):
    def test_delete_stage_race_conflicts_and_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            source = workdir(root) / f"{OLD}.pdf"  # the moved delete source
            real_stage = fsops.stage_target

            def racer_stage(op, path):
                if path == source:
                    source.write_bytes(b"EXTERNAL-PDF-AFTER-GUARD")
                return real_stage(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.stage_target",
                side_effect=racer_stage,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(
                (item(root, OLD) / f"{OLD}.pdf").read_bytes(),
                b"EXTERNAL-PDF-AFTER-GUARD",
            )
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_after_stage_before_delete_race_conflicts_and_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            source = workdir(root) / f"{OLD}.pdf"
            real_delete = fsops.delete_target

            def racer_delete(op, path):
                if path == source:
                    source.write_bytes(b"EXTERNAL-PDF-AFTER-STAGE")
                return real_delete(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.delete_target",
                side_effect=racer_delete,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(
                (item(root, OLD) / f"{OLD}.pdf").read_bytes(),
                b"EXTERNAL-PDF-AFTER-STAGE",
            )
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_managed_delete_source_racer_conflicts(self):
        # the main note source is already staged/written (edited) before
        # its delete; a racer edit after the write is caught by the fsops
        # managed-state verification (compared to the managed write, NOT
        # to the original plan bytes)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            source = workdir(root) / f"{OLD}.md"
            real_delete = fsops.delete_target

            def racer_delete(op, path):
                if path == source:
                    source.write_text("EXTERNAL-MAIN", encoding="utf-8")
                return real_delete(op, path)

            with mock.patch(
                "paper_notes.citations.fsops.delete_target",
                side_effect=racer_delete,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(
                (item(root, OLD) / f"{OLD}.md").read_text(encoding="utf-8"),
                "EXTERNAL-MAIN",
            )
            self.assertFalse(item(root, NEW).exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())


# ---------------------------------------------------------------------------
# M: remediation-2 (Defect 3) — the final old->new rename is atomic
# no-replace and happens only at finalization, so a final-target racer at
# the exact ``<new>`` path blocks the rename and stays byte/inode/mode/
# type exact at that path; the failed operation restores the old item
# byte/type/mode-identically.
# ---------------------------------------------------------------------------


class FinalTargetRacerTest(unittest.TestCase):
    def final_racer(self, root, fn):
        real_rename = citations._rename_dir_noreplace
        work, new_dir = workdir(root), item(root, NEW)

        def racer_rename(src, dst):
            if src == work and dst == new_dir:
                fn()
            return real_rename(src, dst)

        return mock.patch(
            "paper_notes.citations._rename_dir_noreplace", side_effect=racer_rename
        )

    def assert_old_restored_exact(self, root, before, after):
        added, removed, changed = manifest_diff(before, after)
        self.assertEqual(removed, [])
        self.assertEqual(changed, [])

    def test_final_key_target_racer_exact_path_and_inode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_dir, work = item(root, NEW), workdir(root)
            racer = new_dir / f"{NEW}.md"
            captured = {}

            def inject():
                new_dir.mkdir()
                racer.write_text("EXTERNAL TARGET", encoding="utf-8")
                st = racer.stat()
                captured["dev"] = st.st_dev
                captured["ino"] = st.st_ino
                captured["mode"] = stat.S_IMODE(st.st_mode)

            before = vault_manifest(root)
            with self.final_racer(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            after = vault_manifest(root)
            # old item restored byte/type/mode-identically; the only
            # additions are the racer's own paths
            self.assertEqual(
                [p for p in after if p not in before],
                [f"{LIT}/{NEW}", f"{LIT}/{NEW}/{NEW}.md"],
            )
            self.assert_old_restored_exact(root, before, after)
            # the racer remains at the exact new path, unchanged
            # (dev, ino, type, mode, bytes)
            st = racer.stat()
            self.assertEqual(racer.read_text(encoding="utf-8"), "EXTERNAL TARGET")
            self.assertEqual(st.st_dev, captured["dev"])
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertEqual(stat.S_IMODE(st.st_mode), captured["mode"])
            self.assertTrue(stat.S_ISREG(st.st_mode))
            self.assertEqual(sorted(p.name for p in new_dir.iterdir()), [f"{NEW}.md"])
            self.assertTrue(work is not None and not work.exists())
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def test_final_empty_dir_racer_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_dir = item(root, NEW)
            captured = {}

            def inject():
                new_dir.mkdir()
                st = new_dir.stat()
                captured["dev"] = st.st_dev
                captured["ino"] = st.st_ino

            before = vault_manifest(root)
            with self.final_racer(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            after = vault_manifest(root)
            self.assertEqual([p for p in after if p not in before], [f"{LIT}/{NEW}"])
            self.assert_old_restored_exact(root, before, after)
            st = new_dir.stat()
            self.assertEqual(st.st_dev, captured["dev"])
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertTrue(new_dir.is_dir())
            self.assertEqual(sorted(p.name for p in new_dir.iterdir()), [])
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_final_nonempty_dir_racer_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_dir = item(root, NEW)
            captured = {}

            def inject():
                new_dir.mkdir()
                (new_dir / "keep.txt").write_text("racer content", encoding="utf-8")
                st = new_dir.stat()
                captured["dev"] = st.st_dev
                captured["ino"] = st.st_ino

            before = vault_manifest(root)
            with self.final_racer(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            after = vault_manifest(root)
            self.assertEqual(
                [p for p in after if p not in before],
                [f"{LIT}/{NEW}", f"{LIT}/{NEW}/keep.txt"],
            )
            self.assert_old_restored_exact(root, before, after)
            st = new_dir.stat()
            self.assertEqual(st.st_dev, captured["dev"])
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertEqual(
                (new_dir / "keep.txt").read_text(encoding="utf-8"), "racer content"
            )
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])

    def test_final_symlink_dir_racer_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            new_dir = item(root, NEW)
            target = item(root, OLD)

            def inject():
                new_dir.symlink_to(target)

            before = vault_manifest(root)
            with self.final_racer(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            after = vault_manifest(root)
            self.assertEqual([p for p in after if p not in before], [f"{LIT}/{NEW}"])
            self.assert_old_restored_exact(root, before, after)
            self.assertTrue(new_dir.is_symlink())
            self.assertEqual(os.readlink(new_dir), str(target))
            hook.assert_not_called()
            self.assertEqual(staging_residue(root), [])


# ---------------------------------------------------------------------------
# N: remediation-2 — work-directory failure stages leave zero residue and
# success never leaks the hidden work directory.
# ---------------------------------------------------------------------------


class WorkResidueTest(unittest.TestCase):
    def test_work_dir_failure_leaves_zero_residue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            before = vault_manifest(root)
            hook = mock.Mock()
            target = root / "notes" / "reading-notes.md"
            real_write = fsops.write_target

            def failing_write(op, path, content):
                if path == target:
                    raise fsops.OperationConflict("simulated mid-write failure")
                return real_write(op, path, content)

            with mock.patch(
                "paper_notes.citations.fsops.write_target",
                side_effect=failing_write,
            ):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(vault_manifest(root), before)
            self.assertEqual(staging_residue(root), [])
            self.assertFalse(workdir(root).exists())
            self.assertFalse((root / ".paper-notes" / "write.lock").exists())
            hook.assert_not_called()

    def test_success_leaves_no_work_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            confirm(root, get_token(root))
            self.assertFalse(workdir(root).exists())
            self.assertEqual(staging_residue(root), [])
            self.assertTrue(item(root, NEW).is_dir())


# ---------------------------------------------------------------------------
# O: remediation-3 — the confirm token binds every scanned markdown
# scan-input (regular UTF-8 / non-UTF-8 / malformed-frontmatter files by
# exact bytes, and symlinked .md bound WITHOUT following the link), and
# execution re-verifies the exact global markdown scan-input manifest at
# every boundary: at _execute entry (before any mutation/staging), after
# the item is hidden in the work directory, immediately before
# finalization, and after the final move (closing the finalize window).
# Any late add/delete/edit/chmod/type/symlink change to the global
# markdown candidate set is a read-only ItemConflict; the external state
# keeps its exact path/bytes/mode/inode; the old item is restored
# byte/type/mode/tree-identically; hook 0, staging/work residue 0, the
# final new item absent.
# ---------------------------------------------------------------------------


def inject_at_execute_entry(root, fn):
    """Fire ``fn`` at the true entry of ``_execute``: the hidden
    work-directory path is computed there, before any mutation or
    staging, so the entry global scan-input guard runs after it."""
    real_work = citations._work_directory

    def racer_work(old_dir):
        fn()
        return real_work(old_dir)

    return mock.patch("paper_notes.citations._work_directory", side_effect=racer_work)


def inject_at_finalize(root, fn):
    """Fire ``fn`` immediately before the pre-finalization untouched-
    subtree recheck (the finalization boundary); the global scan-input
    guard runs right after it, before the work->new rename."""
    real_vus = citations._verify_untouched_subtree

    def racer_vus(plan, work_dir, old_dir, new_dir):
        fn()
        return real_vus(plan, work_dir, old_dir, new_dir)

    return mock.patch(
        "paper_notes.citations._verify_untouched_subtree", side_effect=racer_vus
    )


class GlobalScanRaceTest(unittest.TestCase):
    def assert_conflict_clean(self, root, hook):
        self.assertFalse(item(root, NEW).exists())
        hook.assert_not_called()
        self.assertEqual(staging_residue(root), [])
        self.assertFalse(workdir(root).exists())
        self.assertFalse((root / ".paper-notes" / "write.lock").exists())

    def assert_external_diff(self, root, before, added=(), removed=(), changed=()):
        """Every manifest entry except the racer's own must be exact
        (path/type/bytes-or-link/mode); the racer's entry is the only
        difference."""
        after = vault_manifest(root)
        a, r, c = manifest_diff(before, after)
        self.assertEqual(a, sorted(added))
        self.assertEqual(r, sorted(removed))
        self.assertEqual(c, sorted(changed))

    def test_entry_late_md_add_conflicts_before_any_mutation(self):
        # the manager's repro #1: notes/late.md with a stale citation
        # appears at _execute entry — the operation must conflict before
        # any mutation or staging (begin_operation never runs), the
        # external file keeps its exact path/bytes/mode, the old item is
        # retained untouched, hook 0, residue 0, final new item absent.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            late = root / "notes" / "late.md"
            before = vault_manifest(root)

            def inject():
                late.write_text(f"late [@{OLD}]\n", encoding="utf-8")

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation"
            ) as begin_mock, inject_at_execute_entry(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            begin_mock.assert_not_called()  # conflicted before any staging
            self.assertEqual(late.read_text(encoding="utf-8"), f"late [@{OLD}]\n")
            self.assert_external_diff(root, before, added=["notes/late.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_clean_md_edit_conflicts(self):
        # the manager's repro #2: the already-scanned notes/clean.md is
        # edited at _execute entry — conflict with the external bytes
        # preserved at the exact path, the item never moved.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)
            external = "EXTERNAL EDIT\n"

            def inject():
                clean.write_text(external, encoding="utf-8")

            with inject_at_execute_entry(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(clean.read_text(encoding="utf-8"), external)
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_clean_md_chmod_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            def inject():
                clean.chmod(0o600)

            with inject_at_execute_entry(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(stat.S_IMODE(clean.lstat().st_mode), 0o600)
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_clean_md_delete_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            with inject_at_execute_entry(root, clean.unlink):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertFalse(clean.exists())
            self.assert_external_diff(root, before, removed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_clean_md_type_change_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            def to_dir():
                clean.unlink()
                clean.mkdir()

            with inject_at_execute_entry(root, to_dir):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(clean.is_dir())
            self.assertEqual(sorted(clean.iterdir()), [])
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_clean_md_symlink_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)
            target = root / "notes" / "reading-notes.md"

            def to_link():
                clean.unlink()
                clean.symlink_to(target)

            with inject_at_execute_entry(root, to_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(clean.is_symlink())
            self.assertEqual(os.readlink(clean), str(target))
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_nonutf8_md_change_conflicts(self):
        # a non-UTF-8 markdown file is bound to the token by its exact
        # bytes; a byte change at _execute entry conflicts
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            binary = root / "notes" / "binary.md"
            binary.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
            before = vault_manifest(root)
            external = b"\xfe\xff other bytes"

            def inject():
                binary.write_bytes(external)

            with inject_at_execute_entry(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(binary.read_bytes(), external)
            self.assert_external_diff(root, before, changed=["notes/binary.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_malformed_md_change_conflicts(self):
        # a malformed-frontmatter markdown file is bound by its exact
        # bytes; a change at _execute entry conflicts
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            broken = root / "notes" / "broken-fm.md"
            broken.write_text("---\ntitle: x\n", encoding="utf-8")
            before = vault_manifest(root)
            external = "---\ntitle: x\nchanged\n"

            def inject():
                broken.write_text(external, encoding="utf-8")

            with inject_at_execute_entry(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(broken.read_text(encoding="utf-8"), external)
            self.assert_external_diff(root, before, changed=["notes/broken-fm.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_symlink_md_add_conflicts(self):
        # a symlinked .md that did not exist at the preview is an
        # addition to the scan-input set; it must conflict without the
        # link target ever being read
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            link = root / "notes" / "link.md"
            before = vault_manifest(root)
            target = root / "notes" / "clean.md"

            def add_link():
                link.symlink_to(target)

            with inject_at_execute_entry(root, add_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(target))
            self.assert_external_diff(root, before, added=["notes/link.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_symlink_md_retarget_conflicts(self):
        # a symlinked .md present at the preview is bound by its link
        # identity (raw target hash, never followed); retargeting at
        # _execute entry conflicts with the new link preserved
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            link = root / "notes" / "link.md"
            link.symlink_to(root / "notes" / "clean.md")
            before = vault_manifest(root)
            new_target = root / "notes" / "reading-notes.md"

            def retarget():
                link.unlink()
                link.symlink_to(new_target)

            with inject_at_execute_entry(root, retarget):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(new_target))
            self.assert_external_diff(root, before, changed=["notes/link.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_symlink_md_type_change_conflicts(self):
        # symlink -> regular-file type change at _execute entry
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            link = root / "notes" / "link.md"
            link.symlink_to(root / "notes" / "clean.md")
            before = vault_manifest(root)
            external = "NOW A REGULAR FILE\n"

            def to_file():
                link.unlink()
                link.write_text(external, encoding="utf-8")

            with inject_at_execute_entry(root, to_file):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(link.is_file() and not link.is_symlink())
            self.assertEqual(link.read_text(encoding="utf-8"), external)
            self.assert_external_diff(root, before, changed=["notes/link.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_dir_symlink_md_add_conflicts(self):
        # the manager's repro: notes/linked.md — a symlink whose target
        # is a DIRECTORY (os.walk lists it under dirnames, not
        # filenames) — is added at _execute entry after a fresh plan:
        # the entry global scan-input guard must conflict before any
        # mutation or staging, preserving the link's exact path / raw
        # target / lstat inode+mode; old item untouched, hook 0,
        # residue 0, final new item absent.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            d1 = root / "notes" / "d1"
            d2 = root / "notes" / "d2"
            d1.mkdir()
            d2.mkdir()
            link = root / "notes" / "linked.md"
            before = vault_manifest(root)
            captured = {}

            def add_link():
                link.symlink_to(d1, target_is_directory=True)
                st = link.lstat()
                captured["ino"] = st.st_ino
                captured["mode"] = stat.S_IMODE(st.st_mode)

            with mock.patch(
                "paper_notes.citations.fsops.begin_operation"
            ) as begin_mock, inject_at_execute_entry(root, add_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            begin_mock.assert_not_called()
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(d1))
            st = link.lstat()
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertEqual(stat.S_IMODE(st.st_mode), captured["mode"])
            self.assert_external_diff(root, before, added=["notes/linked.md"])
            self.assert_conflict_clean(root, hook)

    def test_entry_dir_symlink_md_retarget_conflicts(self):
        # the manager's repro: an existing directory-targeted .md
        # symlink present at the fresh plan is bound by its raw link
        # identity; retargeting it (d1 -> d2) at _execute entry
        # conflicts with the new link preserved exactly.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            d1 = root / "notes" / "d1"
            d2 = root / "notes" / "d2"
            d1.mkdir()
            d2.mkdir()
            link = root / "notes" / "linked.md"
            link.symlink_to(d1, target_is_directory=True)
            before = vault_manifest(root)
            captured = {}

            def retarget():
                link.unlink()
                link.symlink_to(d2, target_is_directory=True)
                st = link.lstat()
                captured["ino"] = st.st_ino
                captured["mode"] = stat.S_IMODE(st.st_mode)

            with inject_at_execute_entry(root, retarget):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(d2))
            st = link.lstat()
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertEqual(stat.S_IMODE(st.st_mode), captured["mode"])
            self.assert_external_diff(root, before, changed=["notes/linked.md"])
            self.assert_conflict_clean(root, hook)

    def test_preview_dir_symlink_retarget_changes_token(self):
        # a directory-targeted .md symlink is a plan-time scan-input:
        # retargeting it between previews changes the confirmation
        # token (the link identity is bound, never the target's
        # contents).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            d1 = root / "notes" / "d1"
            d2 = root / "notes" / "d2"
            d1.mkdir()
            d2.mkdir()
            link = root / "notes" / "linked.md"
            link.symlink_to(d1, target_is_directory=True)
            first = preview_token(root)
            token1 = first.confirmation_token
            self.assertEqual(first.status, "needs_confirmation")
            link.unlink()
            link.symlink_to(d2, target_is_directory=True)
            second = preview_token(root)
            self.assertEqual(second.status, "needs_confirmation")
            self.assertNotEqual(token1, second.confirmation_token)

    def test_preview_dir_symlink_binds_link_and_never_reads_target(self):
        # the plan-time warning is consistent with file symlinks and
        # the link itself is bound (type link, raw-target hash, lstat
        # mode); the target directory (reachable ONLY through the link,
        # outside the vault) is never traversed — its markdown never
        # appears in files or occurrences and is never modified.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            make_vault(root)
            target = Path(outside)
            (target / "secret.md").write_text(
                f"target [@{OLD}]\n", encoding="utf-8"
            )
            link = root / "notes" / "linked.md"
            link.symlink_to(target, target_is_directory=True)
            result = preview_token(root)
            messages = " ".join(result.warnings)
            self.assertIn("linked.md", messages)
            files = result.plan["files"]
            links = [f for f in files if str(f["path"]).endswith("linked.md")]
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0]["type"], "link")
            self.assertEqual(
                links[0]["sha256"],
                hashlib.sha256(os.fsencode(os.readlink(link))).hexdigest(),
            )
            self.assertEqual(links[0]["mode"], stat.S_IMODE(link.lstat().st_mode))
            self.assertFalse(
                [f for f in files if str(f["path"]).startswith(str(target))]
            )
            self.assertFalse(
                [o for o in result.occurrences if str(o.path).startswith(str(target))]
            )
            self.assertEqual(
                (target / "secret.md").read_text(encoding="utf-8"),
                f"target [@{OLD}]\n",
            )

    def test_prefinalize_late_md_add_conflicts(self):
        # the manager's repro #3: notes/late.md is created immediately
        # before the finalization boundary (_verify_untouched_subtree);
        # the pre-finalize global guard conflicts, the external file
        # stays exact, the old item is restored byte/type/mode/tree-
        # identically, hook 0, staging/work residue 0, new item absent.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            late = root / "notes" / "late.md"
            before = vault_manifest(root)

            def inject():
                late.write_text(f"late [@{OLD}]\n", encoding="utf-8")

            with inject_at_finalize(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(late.read_text(encoding="utf-8"), f"late [@{OLD}]\n")
            self.assert_external_diff(root, before, added=["notes/late.md"])
            self.assert_conflict_clean(root, hook)

    def test_during_clean_md_edit_conflicts_and_preserves_inode(self):
        # an external edit to a scanned (but not occurrence) markdown
        # file during the managed writes conflicts at the pre-finalize
        # guard; the external bytes/mode/inode survive the rollback
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)
            external = "EXTERNAL EDIT DURING WRITES\n"

            def inject():
                clean.write_text(external, encoding="utf-8")

            with inject_late_in_work(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            st = clean.stat()
            self.assertEqual(clean.read_text(encoding="utf-8"), external)
            self.assertEqual(stat.S_IMODE(st.st_mode), stat.S_IMODE(clean.stat().st_mode))
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_during_clean_md_delete_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            with inject_late_in_work(root, clean.unlink):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertFalse(clean.exists())
            self.assert_external_diff(root, before, removed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_during_clean_md_chmod_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            def inject():
                clean.chmod(0o600)

            with inject_late_in_work(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(stat.S_IMODE(clean.lstat().st_mode), 0o600)
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_during_clean_md_type_change_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)

            def to_dir():
                clean.unlink()
                clean.mkdir()

            with inject_late_in_work(root, to_dir):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(clean.is_dir())
            self.assertEqual(sorted(clean.iterdir()), [])
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_during_clean_md_symlink_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            clean = root / "notes" / "clean.md"
            before = vault_manifest(root)
            target = root / "notes" / "reading-notes.md"

            def to_link():
                clean.unlink()
                clean.symlink_to(target)

            with inject_late_in_work(root, to_link):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertTrue(clean.is_symlink())
            self.assertEqual(os.readlink(clean), str(target))
            self.assert_external_diff(root, before, changed=["notes/clean.md"])
            self.assert_conflict_clean(root, hook)

    def test_final_window_late_md_add_conflicts(self):
        # notes/late.md appears during the atomic work->new rename (the
        # last window before post-verify): the after-final-move guard
        # conflicts, the external file stays exact at its path/bytes/
        # mode/inode, the old item is restored, hook 0, residue 0.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_vault(root)
            hook = mock.Mock()
            late = root / "notes" / "late.md"
            before = vault_manifest(root)
            captured = {}

            def inject():
                late.write_text(f"late [@{OLD}]\n", encoding="utf-8")
                st = late.lstat()
                captured["ino"] = st.st_ino
                captured["mode"] = stat.S_IMODE(st.st_mode)

            with self.final_window_racer(root, inject):
                with self.assertRaises(items.ItemConflict):
                    confirm(root, get_token(root), hook=hook)
            self.assertEqual(late.read_text(encoding="utf-8"), f"late [@{OLD}]\n")
            st = late.lstat()
            self.assertEqual(st.st_ino, captured["ino"])
            self.assertEqual(stat.S_IMODE(st.st_mode), captured["mode"])
            self.assert_external_diff(root, before, added=["notes/late.md"])
            self.assert_conflict_clean(root, hook)

    def final_window_racer(self, root, fn):
        real_rename = citations._rename_dir_noreplace
        work, new_dir = workdir(root), item(root, NEW)

        def racer_rename(src, dst):
            if src == work and dst == new_dir:
                fn()
            return real_rename(src, dst)

        return mock.patch(
            "paper_notes.citations._rename_dir_noreplace", side_effect=racer_rename
        )


if __name__ == "__main__":
    unittest.main()
