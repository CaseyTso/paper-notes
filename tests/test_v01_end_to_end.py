"""Task 30A: v0.1 isolated vertical slice (core).

Proves the whole core pipeline against a synthetic fixture vault copied
out of ``tests/fixtures/v01_vault`` — no network, no real vault, no real
Zotero, no credentials. Mirrors plan Task 30 step 2 (core portion):

1. Isolated fixture: a standard one-PDF item, a one-card item, a
   multi-PDF/two-card item, plus a disposable fourth item.
2. create (core API with a synthetic PDF) and migrate (legacy layout
   discovery + apply) produce canonical items whose schema, layout and
   hashes are verified; the citation index is rebuilt.
3. A mocked EasyScholar query leaves every Markdown hash unchanged and
   never writes a metrics file (volatile UI data, spec §10).
4. rename-key updates layout, managed citations and derived notes, and
   the old key resolves as a reserved alias.
5. Deletion is exercised only on the disposable fourth fixture.
6. A deliberately failed migration auto-rolls back the items already
   switched, and a later apply/verify/rollback round-trip restores the
   exact source tree.
7. No output produced by the slice ever contains the real vault path.

All product code is read-only; this file only adds tests + fixture data.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

import fitz

from paper_notes import citations as rename_mod
from paper_notes import deletion
from paper_notes import items
from paper_notes.adapters import easyscholar
from paper_notes.csl import rebuild_indexes
from paper_notes.frontmatter import load_paper_note
from paper_notes.migration import build_migration_plan
from paper_notes.migration import transaction
from paper_notes.repository import build_index

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "v01_vault"
LIT = "05 Literature"

# Synthetic citation keys — deliberately unrelated to the real pilot keys.
SMITH = "smithStandardOnePdf2024"
JONES = "jonesCardPaper2025"
LEE = "leeMultiPdfTwoCards2026"
DOE = "doeDisposablePaper2027"
SMITH_NEW = "smithStandardPdfRenamed2026"
CREATED = "chenSyntheticSlice2026"

SMITH_SRC = "Standard Single PDF 2024"
JONES_SRC = "Jones Concept Study 2025"
LEE_SRC = "Lee Multi Study 2026"
DOE_SRC = "Disposable Paper 2027"

_SRC_TO_KEY = {SMITH_SRC: SMITH, JONES_SRC: JONES, LEE_SRC: LEE, DOE_SRC: DOE}
_READING = {SMITH_SRC: "read", JONES_SRC: "reading", LEE_SRC: "unread", DOE_SRC: "unread"}

# The user's real vault path. Fixture files and every generated output
# must never contain it (read from the environment when configured,
# else the known default; the assertion only needs the string absent).
REAL_VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or (
    "/Users/juicewrld/Downloads/obsidian/知识库"
)

# Mocked EasyScholar open-info payload (shape mirrors the real API).
EASY_PAYLOAD = {
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


def _copy_fixture(tmp: Path) -> Path:
    vault = tmp / "vault"
    shutil.copytree(FIXTURE, vault)
    return vault


def _tree_manifest(root: Path) -> set[tuple]:
    """(relative path, size, sha256) for every file under root."""
    out = set()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out.add(
                (
                    str(path.relative_to(root)),
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return out


def _md_hashes(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*.md")):
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _make_pdf(path: Path, text: str) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def _json_response(payload: dict, status: int = 200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V01FixtureTest(unittest.TestCase):
    """The committed fixture itself must be well-formed and isolated."""

    def test_fixture_committed_layout(self):
        for src in (SMITH_SRC, JONES_SRC, LEE_SRC, DOE_SRC):
            directory = FIXTURE / LIT / src
            self.assertTrue(directory.is_dir(), src)
            self.assertTrue((directory / f"{src}.md").is_file(), src)
            text = (directory / f"{src}.md").read_text(encoding="utf-8")
            # legacy layout: title-folder with legacy frontmatter fields
            self.assertIn("citation key:", text)
            self.assertIn("zotero:", text)
            self.assertIn("状态:", text)
        # item-specific assets
        self.assertTrue((FIXTURE / LIT / SMITH_SRC / "paper.pdf").is_file())
        self.assertTrue(
            (FIXTURE / LIT / SMITH_SRC / f"minerUmd_{SMITH}.md").is_file()
        )
        self.assertTrue((FIXTURE / LIT / JONES_SRC / "main.pdf").is_file())
        self.assertTrue((FIXTURE / LIT / JONES_SRC / "cards" / "card-1.md").is_file())
        self.assertTrue((FIXTURE / LIT / LEE_SRC / "main.pdf").is_file())
        self.assertTrue((FIXTURE / LIT / LEE_SRC / "supplementary.pdf").is_file())
        self.assertTrue((FIXTURE / LIT / LEE_SRC / "cards" / "card-a.md").is_file())
        self.assertTrue((FIXTURE / LIT / LEE_SRC / "cards" / "card-b.md").is_file())
        self.assertTrue((FIXTURE / LIT / DOE_SRC / "main.pdf").is_file())
        self.assertTrue((FIXTURE / "notes" / "reading-notes.md").is_file())
        # the five fixture PDFs are distinct attachment bytes
        pdfs = [
            FIXTURE / LIT / SMITH_SRC / "paper.pdf",
            FIXTURE / LIT / JONES_SRC / "main.pdf",
            FIXTURE / LIT / LEE_SRC / "main.pdf",
            FIXTURE / LIT / LEE_SRC / "supplementary.pdf",
            FIXTURE / LIT / DOE_SRC / "main.pdf",
        ]
        digests = [_sha(path) for path in pdfs]
        self.assertEqual(len(set(digests)), len(digests))

    def test_fixture_files_never_contain_real_vault_path(self):
        hits = []
        for path in sorted(FIXTURE.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if REAL_VAULT in text:
                hits.append(str(path))
        self.assertEqual(hits, [])


class V01VerticalSliceTest(unittest.TestCase):
    """The full core pipeline against an isolated fixture copy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = _copy_fixture(self.tmp)
        self.state = self.tmp / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _plan(self):
        return build_migration_plan(self.vault, state_root=self.state)

    def _apply(self, plan):
        return transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
        )

    def _assert_no_real_vault_path(self, root: Path) -> None:
        hits = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if REAL_VAULT in text:
                hits.append(str(path))
        self.assertEqual(hits, [], f"real vault path leaked into {root}")

    def _assert_canonical_item(self, key: str, *, reading_status: str) -> None:
        directory = self.vault / LIT / key
        note = directory / f"{key}.md"
        self.assertTrue(note.is_file(), key)
        paper, _ = load_paper_note(note)
        self.assertEqual(paper.schema_version, 1)
        self.assertEqual(str(paper.paper_id), str(UUID(str(paper.paper_id))))
        self.assertEqual(paper.citation_key, key)
        self.assertEqual(paper.citation_key_aliases, [])
        self.assertEqual(paper.reading_status, reading_status)
        pdf = directory / f"{key}.pdf"
        self.assertTrue(pdf.is_file(), key)
        self.assertEqual(paper.pdf_status, "available")
        # legacy fields are gone from the canonical note
        text = note.read_text(encoding="utf-8")
        # raw frontmatter must declare the schema explicitly (the plugin
        # index keys on it); paper.schema_version alone is a Pydantic
        # default and would hide a missing field.
        self.assertIn("schema_version: 1", text)
        self.assertNotIn("zotero", text)
        self.assertNotIn("状态", text)
        self.assertNotIn("citation key:", text)
        return paper

    # ------------------------------------------------------------------
    # phase tests
    # ------------------------------------------------------------------

    def test_create_item_via_core_api(self):
        hook = mock.Mock()
        pdf = _make_pdf(self.tmp / "new-paper.pdf", "A synthetic vertical slice paper.")
        source_sha = _sha(pdf)
        confirmed = {
            "citation_key": CREATED,
            "title": "A synthetic vertical slice study",
            "authors": [{"family": "Chen", "given": "Wei"}],
            "publication_date": "2026-06-01",
            "year": 2026,
        }
        result = items.create_item(
            self.vault, pdf=pdf, confirmed=confirmed, rebuild_hook=hook
        )
        self.assertEqual(result.status, "created")
        self.assertEqual(result.action, "created")
        self.assertEqual(result.citation_key, CREATED)
        self.assertEqual(result.pdf_sha256, source_sha)

        directory = self.vault / LIT / CREATED
        note = directory / f"{CREATED}.md"
        copied = directory / f"{CREATED}.pdf"
        self.assertTrue(note.is_file())
        self.assertTrue(copied.is_file())
        self.assertEqual(_sha(copied), source_sha)

        paper, _ = load_paper_note(note)
        self.assertEqual(paper.schema_version, 1)
        self.assertEqual(paper.citation_key, CREATED)
        self.assertEqual(paper.paper_id, UUID(result.paper_id))
        self.assertEqual(paper.title, "A synthetic vertical slice study")
        self.assertEqual(paper.pdf_status, "available")
        self.assertEqual(paper.pdf_sha256, source_sha)
        # the source PDF is never moved or modified
        self.assertEqual(_sha(pdf), source_sha)
        hook.assert_called_once_with()

    def test_migrate_fixture_items_to_canonical_layout(self):
        plan = self._plan()
        result = self._apply(plan)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(result.migrated), 4)
        self.assertEqual(len(result.skipped), 0)

        for src, key in _SRC_TO_KEY.items():
            self._assert_canonical_item(key, reading_status=_READING[src])
            # primary PDF bytes match the source PDF
            source_pdf = FIXTURE / LIT / src
            candidates = sorted(source_pdf.glob("*.pdf"))
            self.assertEqual(_sha(self.vault / LIT / key / f"{key}.pdf"), _sha(candidates[0]))

        # standard item: derived MinerU note transformed in place
        mineru = self.vault / LIT / SMITH / f"minerUmd_{SMITH}.md"
        text = mineru.read_text(encoding="utf-8")
        self.assertIn(f"citation_key: {SMITH}", text)
        self.assertIn("paper_id:", text)
        self.assertNotIn("zotero", text)
        self.assertIn("# MinerU extraction", text)  # body preserved

        # one-card item: card bytes preserved byte-for-byte
        card_dst = self.vault / LIT / JONES / "cards" / "card-1.md"
        card_src = FIXTURE / LIT / JONES_SRC / "cards" / "card-1.md"
        self.assertEqual(card_dst.read_bytes(), card_src.read_bytes())

        # multi-PDF/two-card item: secondary PDF lands in attachments/
        supp_dst = self.vault / LIT / LEE / "attachments" / "supplementary.pdf"
        supp_src = FIXTURE / LIT / LEE_SRC / "supplementary.pdf"
        self.assertTrue(supp_dst.is_file())
        self.assertEqual(supp_dst.read_bytes(), supp_src.read_bytes())
        for card in ("card-a.md", "card-b.md"):
            self.assertTrue((self.vault / LIT / LEE / "cards" / card).is_file())

        # migration verify reports a clean applied state
        verify = transaction.verify_migration(
            plan.run_id, vault_root=self.vault, state_root=self.state
        )
        self.assertEqual(verify.status, "ok")

    def test_rebuild_citation_index_lists_current_keys_only(self):
        self._apply(self._plan())
        result = rebuild_indexes(self.vault)
        self.assertEqual(result.papers, 4)
        self.assertEqual(result.aliases, 0)
        self.assertEqual(result.invalid_count, 0)

        library = self.vault / ".paper-notes" / "library.json"
        aliases = self.vault / ".paper-notes" / "citation-aliases.json"
        self.assertTrue(library.is_file())
        self.assertTrue(aliases.is_file())
        entries = json.loads(library.read_text(encoding="utf-8"))
        self.assertEqual(sorted(entry["id"] for entry in entries), sorted(_SRC_TO_KEY.values()))
        self.assertEqual(json.loads(aliases.read_text(encoding="utf-8")), {})
        # deterministic byte output
        first = library.read_bytes()
        rebuild_indexes(self.vault)
        self.assertEqual(library.read_bytes(), first)

    def test_mocked_easyscholar_query_leaves_markdown_unchanged(self):
        self._apply(self._plan())
        rebuild_indexes(self.vault)
        md_before = _md_hashes(self.vault)
        tree_before = _tree_manifest(self.vault)

        secret = "sk-" + "synthetic-" + "marker-only"
        with mock.patch.object(
            easyscholar.requests, "get", return_value=_json_response(EASY_PAYLOAD)
        ) as get:
            result = easyscholar.EasyScholarAdapter(secret).query(journal="Nature Medicine")
        get.assert_called_once()
        self.assertEqual(result["journal"], "Nature Medicine")
        self.assertEqual(result["metrics"]["if"], 82.9)
        self.assertEqual(result["metrics"]["jcr_partition"], "Q1")

        # metrics are volatile UI data: every Markdown hash is unchanged
        # and no file was created or modified anywhere in the vault
        self.assertEqual(_md_hashes(self.vault), md_before)
        self.assertEqual(_tree_manifest(self.vault), tree_before)

    def test_rename_key_updates_layout_and_resolves_alias(self):
        self._apply(self._plan())
        notes_path = self.vault / "notes" / "reading-notes.md"
        self.assertIn(f"[@{SMITH}]", notes_path.read_text(encoding="utf-8"))

        hook = mock.Mock()
        preview = rename_mod.preview_rename_key(self.vault, key=SMITH, new_key=SMITH_NEW)
        self.assertEqual(preview.status, "needs_confirmation")
        self.assertTrue(
            any(
                str(occ.path).endswith("reading-notes.md")
                for occ in preview.occurrences
            )
        )
        result = rename_mod.confirm_rename_key(
            self.vault,
            key=SMITH,
            new_key=SMITH_NEW,
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "renamed")
        self.assertEqual(result.citation_key, SMITH_NEW)
        hook.assert_called_once_with()

        # layout moved and key-dependent files renamed
        new_dir = self.vault / LIT / SMITH_NEW
        self.assertTrue((new_dir / f"{SMITH_NEW}.md").is_file())
        self.assertTrue((new_dir / f"{SMITH_NEW}.pdf").is_file())
        self.assertTrue((new_dir / f"minerUmd_{SMITH_NEW}.md").is_file())
        self.assertFalse((self.vault / LIT / SMITH).exists())

        paper, _ = load_paper_note(new_dir / f"{SMITH_NEW}.md")
        self.assertEqual(paper.citation_key, SMITH_NEW)
        self.assertEqual(paper.citation_key_aliases, [SMITH])

        # managed global citations rewritten
        text = notes_path.read_text(encoding="utf-8")
        self.assertIn(f"[@{SMITH_NEW}]", text)
        self.assertNotIn(f"[@{SMITH}]", text)

        # current key + alias both resolve to the same paper
        index = build_index(self.vault)
        self.assertIn(SMITH_NEW, index.by_key)
        self.assertEqual(index.aliases.get(SMITH), SMITH_NEW)
        shown = items.show_item(self.vault, key=SMITH)
        self.assertEqual(shown.resolved_as, "alias")
        self.assertEqual(shown.citation_key, SMITH_NEW)

    def test_delete_only_disposable_fourth_item(self):
        self._apply(self._plan())
        hook = mock.Mock()
        preview = deletion.preview_delete(self.vault, key=DOE)
        self.assertEqual(preview.status, "needs_confirmation")
        self.assertEqual(preview.citation_key, DOE)
        result = deletion.confirm_delete(
            self.vault,
            key=DOE,
            confirm_key=DOE,
            confirm_token=preview.confirmation_token,
            rebuild_hook=hook,
        )
        self.assertEqual(result.status, "deleted")
        hook.assert_called_once_with()

        self.assertFalse((self.vault / LIT / DOE).exists())
        index = build_index(self.vault)
        self.assertNotIn(DOE, index.by_key)
        self.assertEqual(index.aliases, {})
        # the other three canonical items survive untouched
        self.assertEqual(sorted(index.by_key), [JONES, LEE, SMITH])
        for key in (JONES, LEE, SMITH):
            self.assertTrue((self.vault / LIT / key / f"{key}.md").is_file())

    def test_deliberately_failed_migration_rolls_back_then_recovers(self):
        before = _tree_manifest(self.vault)
        plan = self._plan()  # manifest is built before the racer appears
        # pre-seed the LAST-processed target (Standard is last alphabetically):
        # the first three items are switched, then this conflict fires.
        target = self.vault / LIT / SMITH
        target.mkdir(parents=True)
        occupied = target / f"{SMITH}.md"
        occupied.write_text("occupied\n", encoding="utf-8")

        with self.assertRaises(transaction.MigrationConflict):
            self._apply(plan)

        # auto-rollback restored the items switched before the conflict
        for src, key in _SRC_TO_KEY.items():
            if key == SMITH:
                continue
            self.assertTrue((self.vault / LIT / src).is_dir())
            self.assertFalse((self.vault / LIT / key).exists())
        self.assertEqual(occupied.read_text(encoding="utf-8"), "occupied\n")

        # once the racer is gone a fresh plan applies, verifies, rolls back
        shutil.rmtree(target)
        plan2 = self._plan()
        result = self._apply(plan2)
        self.assertEqual(result.status, "applied")
        verify = transaction.verify_migration(
            plan2.run_id, vault_root=self.vault, state_root=self.state
        )
        self.assertEqual(verify.status, "ok")
        rollback = transaction.rollback_migration(
            plan2.run_id, vault_root=self.vault, state_root=self.state
        )
        self.assertEqual(rollback.status, "rolled_back")
        self.assertEqual(_tree_manifest(self.vault), before)

    # ------------------------------------------------------------------
    # the whole chain, mirroring plan Task 30 step 2
    # ------------------------------------------------------------------

    def test_full_vertical_slice_flow(self):
        # 1. create: a brand-new canonical item through the core API
        pdf = _make_pdf(self.tmp / "slice.pdf", "Vertical slice create input.")
        confirmed = {
            "citation_key": CREATED,
            "title": "A synthetic vertical slice study",
            "authors": [{"family": "Chen", "given": "Wei"}],
            "publication_date": "2026-06-01",
            "year": 2026,
        }
        created = items.create_item(self.vault, pdf=pdf, confirmed=confirmed)
        self.assertEqual(created.status, "created")
        self.assertEqual(created.citation_key, CREATED)

        # 2. migrate the four legacy fixture items
        applied = self._apply(self._plan())
        self.assertEqual(applied.status, "applied")
        self.assertEqual(len(applied.migrated), 4)

        # 3. schema / layout / hash verification for all five items
        for src, key in _SRC_TO_KEY.items():
            self._assert_canonical_item(key, reading_status=_READING[src])
        self._assert_canonical_item(CREATED, reading_status="unread")
        supp_dst = self.vault / LIT / LEE / "attachments" / "supplementary.pdf"
        self.assertTrue(supp_dst.is_file())

        # 4. rebuild the citation index
        rebuilt = rebuild_indexes(self.vault)
        self.assertEqual(rebuilt.papers, 5)
        self.assertEqual(rebuilt.invalid_count, 0)
        library = json.loads(
            (self.vault / ".paper-notes" / "library.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            sorted(entry["id"] for entry in library),
            sorted([CREATED, JONES, LEE, SMITH, DOE]),
        )

        # 5. mocked EasyScholar query: all Markdown hashes unchanged
        md_before = _md_hashes(self.vault)
        tree_before = _tree_manifest(self.vault)
        secret = "sk-" + "synthetic-" + "marker-only"
        with mock.patch.object(
            easyscholar.requests, "get", return_value=_json_response(EASY_PAYLOAD)
        ):
            queried = easyscholar.EasyScholarAdapter(secret).query(journal="Nature Medicine")
        self.assertEqual(queried["metrics"]["if"], 82.9)
        self.assertEqual(_md_hashes(self.vault), md_before)
        self.assertEqual(_tree_manifest(self.vault), tree_before)

        # 6. rename key: layout + managed citations + alias resolution
        preview = rename_mod.preview_rename_key(self.vault, key=SMITH, new_key=SMITH_NEW)
        renamed = rename_mod.confirm_rename_key(
            self.vault,
            key=SMITH,
            new_key=SMITH_NEW,
            confirm_token=preview.confirmation_token,
        )
        self.assertEqual(renamed.status, "renamed")
        index = build_index(self.vault)
        self.assertEqual(index.aliases.get(SMITH), SMITH_NEW)
        self.assertEqual(items.show_item(self.vault, key=SMITH).resolved_as, "alias")

        # 7. delete only the disposable fourth item
        delete_preview = deletion.preview_delete(self.vault, key=DOE)
        deleted = deletion.confirm_delete(
            self.vault,
            key=DOE,
            confirm_key=DOE,
            confirm_token=delete_preview.confirmation_token,
        )
        self.assertEqual(deleted.status, "deleted")
        self.assertFalse((self.vault / LIT / DOE).exists())

        # 8. a deliberately failed migration rolls back on a second copy
        vault2 = self.tmp / "vault2"
        shutil.copytree(FIXTURE, vault2)
        state2 = self.tmp / "state2"
        before2 = _tree_manifest(vault2)
        plan2 = build_migration_plan(vault2, state_root=state2)
        racer = vault2 / LIT / SMITH
        racer.mkdir(parents=True)
        (racer / f"{SMITH}.md").write_text("occupied\n", encoding="utf-8")
        with self.assertRaises(transaction.MigrationConflict):
            transaction.apply_migration(
                plan2.run_id,
                plan2.confirmation_token,
                vault_root=vault2,
                state_root=state2,
            )
        for src in (JONES_SRC, DOE_SRC, LEE_SRC):
            self.assertTrue((vault2 / LIT / src).is_dir())
        shutil.rmtree(racer)
        plan3 = build_migration_plan(vault2, state_root=state2)
        applied2 = transaction.apply_migration(
            plan3.run_id,
            plan3.confirmation_token,
            vault_root=vault2,
            state_root=state2,
        )
        self.assertEqual(applied2.status, "applied")
        rolled = transaction.rollback_migration(
            plan3.run_id, vault_root=vault2, state_root=state2
        )
        self.assertEqual(rolled.status, "rolled_back")
        self.assertEqual(_tree_manifest(vault2), before2)

        # 9. nothing produced by the slice contains the real vault path
        self._assert_no_real_vault_path(self.vault)
        self._assert_no_real_vault_path(self.state)
        self._assert_no_real_vault_path(vault2)
        self._assert_no_real_vault_path(state2)
