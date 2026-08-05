"""Migration apply / verify / rollback tests (Task 18).

Frozen after the first red run; do not weaken or delete assertions.

Covers the plan's Task 18 matrix:

- External backup created before staging (outside the vault).
- Staging built on the same filesystem as the vault.
- Main item generated with a new UUID / citation-key directory.
- Old active Zotero fields removed from frontmatter.
- MinerU / Figure / card content preserved byte-for-byte outside
  frontmatter changes.
- Primary PDF hash matches; secondary PDFs enter ``attachments/``.
- ``figures/`` receives the existing hash assets and both notes retain
  the same embeds.
- Successful switch removes the old empty source directory.
- Verify catches every deliberately corrupted artifact.
- Rollback restores the exact source tree.
- Reapplying an identical migration is idempotent.
- A non-identical target stops without overwrite.

All fixtures are synthetic copies of ``tests/fixtures/legacy_vault``;
nothing touches a real vault or Zotero.
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

from paper_notes.adapters.zotero import ZoteroAdapter
from paper_notes.frontmatter import load_paper_note
from paper_notes.migration import MigrationPlan, build_migration_plan
from paper_notes.migration import transaction

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "legacy_vault"

STANDARD = "Standard Single PDF 2024"
DERMATO = "Dermatomyositis - JAK1 (2025)"
XIA = "Xia Large-scale Single-cell Analysis"
HASH_FIG = "Hash Figures Paper"
CONFLICT = "Old Conflict Paper"
NO_KEY = "No Key Paper"
MISSING = "Missing PDF Paper"
R1_NO_MAIN = "R1 No Main Note"
R1_ZOTERO_PDF = "R1 Zotero PDF Paper"

SHIAU = "shiauSpatiallyResolvedAnalysis2024"
OSBORNE = "osborneDermatomyositisCharacterizedJAK1mediated2025"
XIA_KEY = "xiaLargescaleSinglecellAnalysis2026"
HASH_KEY = "hashFiguresPaper2026"
AMBIGUOUS_KEY = "ambiguousMainPdf2026"
R1_NO_MAIN_KEY = "r1NoMainNote2026"
R1_CARDS_KEY = "r1CardsType2026"
R1_ZOTERO_KEY = "r1ZoteroPdf2026"


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


def _frontmatter_dict(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    fm, _body, _nl = transaction._load_rt(text)
    return dict(fm)


def _body_of(path: Path) -> str:
    _fm, body, _nl = transaction._load_rt(path.read_text(encoding="utf-8"))
    return body


class MigrationTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = _copy_fixture(self.tmp)
        self.state = self.tmp / "state"
        self._plan = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def plan(self) -> MigrationPlan:
        if self._plan is None:
            self._plan = build_migration_plan(
                self.vault, state_root=self.state
            )
        return self._plan

    def apply(self, **kwargs):
        plan = self.plan()
        return transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
            **kwargs,
        )

    def verify(self, **kwargs):
        return transaction.verify_migration(
            self.plan().run_id, vault_root=self.vault, state_root=self.state, **kwargs
        )

    def rollback(self, **kwargs):
        return transaction.rollback_migration(
            self.plan().run_id, vault_root=self.vault, state_root=self.state, **kwargs
        )

    def target_dir(self, key: str) -> Path:
        return self.vault / "05 Literature" / key

    def source_dir(self, name: str) -> Path:
        return self.vault / "05 Literature" / name

    def backup_file(self, name: str, rel: str) -> Path:
        """Path of a preserved source file inside the external backup."""
        journal = transaction._load_journal(self.state, self.plan().run_id)
        jitem = next(
            j
            for j in journal["items"]
            if j["source_dir"] == f"05 Literature/{name}"
        )
        return self.state / self.plan().run_id / jitem["backup_dir"] / rel

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def test_apply_migrates_expected_items_and_skips_conflicts(self):
        result = self.apply()
        self.assertEqual(result.status, "applied")
        migrated = {m["citation_key"] for m in result.migrated}
        self.assertEqual(
            migrated,
            {
                SHIAU,
                OSBORNE,
                XIA_KEY,
                HASH_KEY,
                AMBIGUOUS_KEY,
                "unicodePaper2026",
                R1_NO_MAIN_KEY,
                R1_CARDS_KEY,
                R1_ZOTERO_KEY,
            },
        )
        skipped = {s["source_dir"]: s["reason"] for s in result.skipped}
        self.assertEqual(
            skipped[f"05 Literature/{CONFLICT}"], "target_conflict"
        )
        self.assertEqual(skipped[f"05 Literature/{NO_KEY}"], "missing_citation_key")
        self.assertEqual(
            skipped[f"05 Literature/{MISSING}"], "missing_citation_key"
        )

    def test_main_item_generated_with_new_uuid_and_key(self):
        result = self.apply()
        ids = [m["paper_id"] for m in result.migrated]
        self.assertEqual(len(ids), len(set(ids)))
        for pid in ids:
            UUID(pid)  # raises on invalid
        by_key = {m["citation_key"]: m["paper_id"] for m in result.migrated}
        target = self.target_dir(SHIAU)
        self.assertTrue(target.is_dir())
        main = target / f"{SHIAU}.md"
        self.assertTrue(main.is_file())
        paper, _doc = load_paper_note(main)
        self.assertEqual(paper.citation_key, SHIAU)
        self.assertEqual(str(paper.paper_id), by_key[SHIAU])
        self.assertEqual(paper.schema_version, 1)

    def test_migrated_main_note_raw_frontmatter_has_schema_version(self):
        # raw YAML must carry schema_version: 1 (plugin index relies on it);
        # asserting only paper.schema_version would pass via the Pydantic
        # default even when the frontmatter omits the field.
        result = self.apply()
        for m in result.migrated:
            main = self.target_dir(m["citation_key"]) / f"{m['citation_key']}.md"
            text = main.read_text(encoding="utf-8")
            self.assertIn("schema_version: 1", text)

    def test_old_active_zotero_fields_removed(self):
        self.apply()
        fm = _frontmatter_dict(self.target_dir(SHIAU) / f"{SHIAU}.md")
        normalized = {transaction._normalize_field(str(k)) for k in fm}
        self.assertNotIn("zotero", normalized)
        self.assertNotIn("zoterolink", normalized)
        self.assertNotIn("状态", normalized)
        self.assertNotIn("pdf", normalized)
        blob = json.dumps(fm, ensure_ascii=False)
        self.assertNotIn("zotero://", blob)
        self.assertNotIn("zotero.org", blob)
        self.assertEqual(fm["citation_key"], SHIAU)
        self.assertEqual(fm["pdf_status"], "available")
        self.assertEqual(fm["reading_status"], "read")

    def test_reading_status_mapped_from_legacy(self):
        self.apply()
        self.assertEqual(
            _frontmatter_dict(self.target_dir(OSBORNE) / f"{OSBORNE}.md")[
                "reading_status"
            ],
            "reading",
        )
        self.assertEqual(
            _frontmatter_dict(self.target_dir(XIA_KEY) / f"{XIA_KEY}.md")[
                "reading_status"
            ],
            "unread",
        )

    def test_main_note_body_preserved_byte_for_byte(self):
        self.apply()
        backup_main = self.backup_file(STANDARD, "Standard Single PDF 2024.md")
        target = self.target_dir(SHIAU) / f"{SHIAU}.md"
        self.assertEqual(_body_of(target), _body_of(backup_main))

    def test_derived_notes_transformed_and_body_preserved(self):
        self.apply()
        journal = transaction._load_journal(self.state, self.plan().run_id)
        jitems = {j["citation_key"]: j for j in journal["items"]}
        paper_id = jitems[SHIAU]["paper_id"]
        target_mineru = self.target_dir(SHIAU) / f"minerUmd_{SHIAU}.md"
        backup_mineru = self.backup_file(
            STANDARD, "minerUmd_shiauSpatiallyResolvedAnalysis2024.md"
        )
        self.assertTrue(target_mineru.is_file())
        self.assertEqual(_body_of(target_mineru), _body_of(backup_mineru))
        fm = _frontmatter_dict(target_mineru)
        self.assertEqual(fm["citation_key"], SHIAU)
        self.assertEqual(fm["paper_id"], paper_id)
        normalized = {transaction._normalize_field(str(k)) for k in fm}
        self.assertNotIn("zotero", normalized)
        self.assertNotIn("citationkey", {k for k in fm if k != "citation_key"})
        target_fig = self.target_dir(XIA_KEY) / f"Figure解读_{XIA_KEY}.md"
        backup_fig = self.backup_file(XIA, f"Figure解读_{XIA_KEY}.md")
        self.assertTrue(target_fig.is_file())
        self.assertEqual(_body_of(target_fig), _body_of(backup_fig))

    def test_cards_preserved_byte_for_byte(self):
        self.apply()
        target_card = self.target_dir(OSBORNE) / "cards" / "card-1.md"
        backup_card = self.backup_file(DERMATO, "cards/card-1.md")
        self.assertEqual(target_card.read_bytes(), backup_card.read_bytes())

    def test_primary_pdf_hash_matches(self):
        self.apply()
        target = self.target_dir(SHIAU) / f"{SHIAU}.pdf"
        backup_pdf = self.backup_file(STANDARD, "paper.pdf")
        self.assertEqual(
            hashlib.sha256(target.read_bytes()).hexdigest(),
            hashlib.sha256(backup_pdf.read_bytes()).hexdigest(),
        )

    def test_secondary_pdf_enters_attachments(self):
        self.apply()
        target = self.target_dir(XIA_KEY)
        self.assertEqual(
            hashlib.sha256(
                (target / f"{XIA_KEY}.pdf").read_bytes()
            ).hexdigest(),
            hashlib.sha256(
                self.backup_file(XIA, "primary.pdf").read_bytes()
            ).hexdigest(),
        )
        secondary = target / "attachments" / "supplementary.pdf"
        self.assertTrue(secondary.is_file())
        self.assertEqual(
            hashlib.sha256(secondary.read_bytes()).hexdigest(),
            hashlib.sha256(
                self.backup_file(XIA, "supplementary.pdf").read_bytes()
            ).hexdigest(),
        )

    def test_figures_receive_hash_assets_and_embeds_retained(self):
        item = self.vault / "05 Literature" / "Embedded Figures 2026"
        (item / "figures").mkdir(parents=True)
        png = hashlib.sha256(b"png-bytes").hexdigest() + ".png"
        (item / "figures" / png).write_bytes(b"png-bytes")
        (item / "Embedded Figures 2026.md").write_text(
            "---\ntitle: Embedded Figures\ncitation key: embeddedFigures2026\n"
            "---\n\nMain embeds ![[figures/" + png + "]]\n",
            encoding="utf-8",
        )
        (item / f"Figure解读_embeddedFigures2026.md").write_text(
            "---\ncitation key: embeddedFigures2026\n---\n\n"
            "# Figure interpretation\n\n![[figures/" + png + "]]\n",
            encoding="utf-8",
        )
        result = self.apply()
        self.assertEqual(result.status, "applied")
        target = self.target_dir("embeddedFigures2026")
        target_png = target / "figures" / png
        self.assertTrue(target_png.is_file())
        self.assertEqual(target_png.read_bytes(), b"png-bytes")
        self.assertIn(
            f"![[figures/{png}]]",
            (target / "embeddedFigures2026.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            f"![[figures/{png}]]",
            (target / f"Figure解读_embeddedFigures2026.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_backup_created_externally_before_switch(self):
        before = {
            str(p.relative_to(self.vault)): _tree_manifest(p)
            for p in sorted((self.vault / "05 Literature").iterdir())
            if p.is_dir()
        }
        result = self.apply()
        journal = transaction._load_journal(self.state, self.plan().run_id)
        self.assertEqual(journal["status"], "applied")
        for jitem in journal["items"]:
            if jitem.get("skipped"):
                continue
            backup = self.state / self.plan().run_id / jitem["backup_dir"]
            self.assertTrue(backup.is_dir())
            rel = jitem["source_dir"]
            self.assertEqual(_tree_manifest(backup), before[rel])

    def test_staging_built_on_same_filesystem(self):
        seen = []
        real = transaction.no_replace_move
        resolved_vault = self.vault.resolve()
        vault_dev = resolved_vault.stat().st_dev

        def spy(source, target, *, vault_root, source_kind="any"):
            seen.append((source, source.parent.stat().st_dev))
            return real(source, target, vault_root=vault_root, source_kind=source_kind)

        with mock.patch.object(transaction, "no_replace_move", side_effect=spy):
            result = self.apply()
        self.assertEqual(result.status, "applied")
        self.assertTrue(seen)
        for stage, dev in seen:
            self.assertTrue(stage.is_relative_to(resolved_vault))
            self.assertEqual(dev, vault_dev)

    def test_successful_switch_removes_old_source_directory(self):
        self.apply()
        self.assertFalse(self.source_dir(STANDARD).exists())
        self.assertFalse(self.source_dir(XIA).exists())
        self.assertFalse(self.source_dir(HASH_FIG).exists())

    def test_no_staging_residue_after_apply(self):
        self.apply()
        leftovers = [
            p.name
            for p in (self.vault / "05 Literature").iterdir()
            if p.name.startswith(transaction.STAGING_PREFIX)
        ]
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def test_verify_ok_after_apply(self):
        self.apply()
        report = self.verify()
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.problems, 0)
        self.assertEqual(report.applied, 9)
        self.assertEqual(report.skipped, 3)

    def test_verify_pending_before_apply(self):
        report = self.verify()
        self.assertEqual(report.status, "pending")
        self.assertEqual(report.pending, 12)
        self.assertEqual(report.problems, 0)

    def test_verify_catches_corrupted_primary_pdf(self):
        self.apply()
        pdf = self.target_dir(SHIAU) / f"{SHIAU}.pdf"
        pdf.write_bytes(b"corrupted bytes")
        report = self.verify()
        self.assertEqual(report.status, "problems")
        codes = {
            problem["code"]
            for item in report.items
            for problem in item["problems"]
        }
        self.assertIn("pdf_hash_mismatch", codes)

    def test_verify_catches_deleted_figure(self):
        self.apply()
        figure = (
            self.target_dir(HASH_KEY)
            / "figures"
            / "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e.png"
        )
        figure.unlink()
        report = self.verify()
        self.assertEqual(report.status, "problems")
        codes = {
            problem["code"]
            for item in report.items
            for problem in item["problems"]
        }
        self.assertIn("file_missing", codes)

    def test_verify_catches_deleted_card(self):
        self.apply()
        (self.target_dir(OSBORNE) / "cards" / "card-1.md").unlink()
        report = self.verify()
        self.assertEqual(report.status, "problems")
        codes = {
            problem["code"]
            for item in report.items
            for problem in item["problems"]
        }
        self.assertIn("file_missing", codes)

    def test_verify_catches_missing_paper_id(self):
        self.apply()
        main = self.target_dir(SHIAU) / f"{SHIAU}.md"
        text = main.read_text(encoding="utf-8")
        main.write_text(
            text.replace("paper_id: ", "removed: "), encoding="utf-8"
        )
        report = self.verify()
        self.assertEqual(report.status, "problems")
        codes = {
            problem["code"]
            for item in report.items
            for problem in item["problems"]
        }
        self.assertIn("paper_id_mismatch", codes)

    def test_verify_catches_edited_main_note_body(self):
        self.apply()
        main = self.target_dir(SHIAU) / f"{SHIAU}.md"
        main.write_text(
            main.read_text(encoding="utf-8") + "\nInjected prose.\n",
            encoding="utf-8",
        )
        report = self.verify()
        self.assertEqual(report.status, "problems")
        codes = {
            problem["code"]
            for item in report.items
            for problem in item["problems"]
        }
        self.assertIn("body_changed", codes)

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def test_rollback_restores_exact_source_tree(self):
        before = _tree_manifest(self.vault)
        self.apply()
        result = self.rollback()
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(_tree_manifest(self.vault), before)
        self.assertTrue(self.source_dir(STANDARD).is_dir())
        self.assertFalse(self.target_dir(SHIAU).exists())

    def test_rollback_twice_is_noop(self):
        self.apply()
        self.rollback()
        result = self.rollback()
        self.assertEqual(result.status, "nothing_to_roll_back")

    def test_rollback_stops_on_modified_target(self):
        self.apply()
        main = self.target_dir(SHIAU) / f"{SHIAU}.md"
        original = main.read_bytes()
        main.write_text("---\n---\nchanged\n", encoding="utf-8")
        with self.assertRaises(transaction.MigrationConflict):
            self.rollback()
        self.assertEqual(main.read_bytes(), b"---\n---\nchanged\n")

    def test_verify_after_rollback_reports_rolled_back(self):
        self.apply()
        self.rollback()
        report = self.verify()
        self.assertEqual(report.status, "pending")
        self.assertEqual(report.applied, 0)

    # ------------------------------------------------------------------
    # Idempotence and non-identical targets
    # ------------------------------------------------------------------

    def test_reapply_identical_migration_is_idempotent(self):
        self.apply()
        before = _tree_manifest(self.vault)
        result = self.apply()
        self.assertEqual(result.status, "already_applied")
        self.assertEqual(_tree_manifest(self.vault), before)

    def test_reapply_stops_on_non_identical_target(self):
        self.apply()
        main = self.target_dir(SHIAU) / f"{SHIAU}.md"
        main.write_text("---\n---\nchanged\n", encoding="utf-8")
        with self.assertRaises(transaction.MigrationConflict):
            self.apply()
        self.assertEqual(main.read_bytes(), b"---\n---\nchanged\n")

    def test_fresh_apply_stops_on_preexisting_target_and_restores(self):
        self.plan()  # build the manifest before the racer target appears
        target = self.target_dir(SHIAU)
        target.mkdir(parents=True)
        occupied = target / f"{SHIAU}.md"
        occupied.write_text("occupied\n", encoding="utf-8")
        with self.assertRaises(transaction.MigrationConflict):
            self.apply()
        self.assertEqual(occupied.read_text(encoding="utf-8"), "occupied\n")
        # Auto-rollback restored the items switched before the conflict.
        self.assertTrue(self.source_dir(STANDARD).is_dir())
        self.assertFalse(self.target_dir(XIA_KEY).exists())

    def test_wrong_token_rejected_without_writes(self):
        plan = self.plan()
        before = _tree_manifest(self.vault)
        with self.assertRaises(transaction.MigrationError):
            transaction.apply_migration(
                plan.run_id,
                "0" * 64,
                vault_root=self.vault,
                state_root=self.state,
            )
        self.assertEqual(_tree_manifest(self.vault), before)

    def test_apply_after_rollback_succeeds(self):
        self.apply()
        self.rollback()
        result = self.apply()
        self.assertEqual(result.status, "applied")
        self.assertEqual(self.verify().status, "ok")

    def test_unknown_run_id_rejected(self):
        with self.assertRaises(transaction.MigrationError):
            transaction.apply_migration(
                "deadbeefdeadbeefdeadbeefdeadbeef",
                "0" * 64,
                vault_root=self.vault,
                state_root=self.state,
            )


def build_r1_zotero_fixture(root: Path) -> Path:
    """Zotero snapshot fixture for the R1 apply tests (never committed).

    Identical to the discovery-test fixture: citekeys ``r1NoMainNote2026``
    and ``r1ZoteroPdf2026`` with storage PDFs under
    ``<root>/storage/<attachmentKey>/``.
    """
    db = root / "zotero.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY,
            parentItemID INTEGER, linkMode INTEGER, path TEXT, contentType TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);

        INSERT INTO itemTypes (itemTypeID, typeName) VALUES
            (1, 'journalArticle'), (2, 'attachment'), (3, 'note'), (4, 'annotation');
        INSERT INTO items (itemID, key, itemTypeID) VALUES
            (1, 'R1PAR1', 1), (2, 'R1ATT1', 2), (3, 'R1PAR2', 1),
            (4, 'R1ATT2', 2), (5, 'R1ATT3', 2);
        INSERT INTO fields (fieldID, fieldName) VALUES
            (1, 'title'), (2, 'date'), (110, 'citationKey'), (36, 'DOI'), (37, 'publicationTitle');
        INSERT INTO itemDataValues (valueID, value) VALUES
            (1001, 'R1 No Main Note Zotero Title'),
            (1002, '2026-03-01'),
            (1003, 'r1NoMainNote2026'),
            (1004, '10.1000/r1nomain'),
            (1005, 'Nature Methods'),
            (1006, 'R1 Zotero PDF Zotero Title'),
            (1007, '2026-05-15'),
            (1008, 'r1ZoteroPdf2026'),
            (1009, '10.1000/r1zotero'),
            (1010, 'Cell');
        INSERT INTO itemData (itemID, fieldID, valueID) VALUES
            (1, 1, 1001), (1, 2, 1002), (1, 110, 1003), (1, 36, 1004), (1, 37, 1005),
            (3, 1, 1006), (3, 2, 1007), (3, 110, 1008), (3, 36, 1009), (3, 37, 1010);
        INSERT INTO itemAttachments (itemID, parentItemID, linkMode, path, contentType) VALUES
            (2, 1, 0, 'storage:r1nomain-main.pdf', 'application/pdf'),
            (4, 3, 0, 'storage:r1zotero-main.pdf', 'application/pdf'),
            (5, 3, 0, 'storage:NMF.pdf', 'application/pdf');
        INSERT INTO creators (creatorID, firstName, lastName, fieldMode) VALUES
            (1, 'First', 'R1Author', 0), (2, 'Min', 'Zhao', 0);
        INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (1, 'author');
        INSERT INTO itemCreators (itemID, creatorID, creatorTypeID, orderIndex) VALUES
            (1, 1, 1, 0), (3, 2, 1, 0);
        """
    )
    conn.commit()
    conn.close()

    storage = root / "storage"
    (storage / "R1ATT1").mkdir(parents=True)
    (storage / "R1ATT1" / "r1nomain-main.pdf").write_bytes(
        b"%PDF-1.4 r1 no-main-note pdf fixture.%%EOF"
    )
    (storage / "R1ATT2").mkdir()
    (storage / "R1ATT2" / "r1zotero-main.pdf").write_bytes(
        b"%PDF-1.4 r1 zotero main text fixture.%%EOF"
    )
    (storage / "R1ATT3").mkdir()
    (storage / "R1ATT3" / "NMF.pdf").write_bytes(
        b"%PDF-1.4 r1 zotero NMF supplementary fixture.%%EOF"
    )
    return db


class R1ApplyTest(unittest.TestCase):
    """R1 apply: generate main notes and copy Zotero storage PDFs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = _copy_fixture(self.tmp)
        self.state = self.tmp / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def _zotero_root(self):
        self.zroot = self.tmp / "zotero"
        self.zroot.mkdir()
        return self.zroot

    def test_generate_main_note_item_applied_not_skipped(self):
        plan = build_migration_plan(self.vault, state_root=self.state)
        result = transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
        )
        keys = {m["citation_key"] for m in result.migrated}
        self.assertIn(R1_NO_MAIN_KEY, keys)
        target = self.vault / "05 Literature" / R1_NO_MAIN_KEY
        main = target / f"{R1_NO_MAIN_KEY}.md"
        self.assertTrue(main.is_file())
        paper, _doc = load_paper_note(main)
        self.assertEqual(paper.citation_key, R1_NO_MAIN_KEY)
        self.assertEqual(paper.schema_version, 1)
        self.assertEqual(str(paper.paper_id), str(UUID(str(paper.paper_id))))
        text = main.read_text(encoding="utf-8")
        self.assertIn("schema_version: 1", text)
        # derived notes preserved with identity fields
        fig = target / f"Figure解读_{R1_NO_MAIN_KEY}.md"
        self.assertTrue(fig.is_file())
        fm = _frontmatter_dict(fig)
        self.assertEqual(fm["citation_key"], R1_NO_MAIN_KEY)
        report = transaction.verify_migration(
            plan.run_id, vault_root=self.vault, state_root=self.state
        )
        self.assertEqual(report.status, "ok")

    def test_generate_main_note_uses_zotero_identity(self):
        zroot = self._zotero_root()
        db = build_r1_zotero_fixture(zroot)
        with ZoteroAdapter(db_path=db, data_dir=zroot) as ad:
            plan = build_migration_plan(
                self.vault, state_root=self.state, zotero=ad
            )
        transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
        )
        main = (
            self.vault
            / "05 Literature"
            / R1_NO_MAIN_KEY
            / f"{R1_NO_MAIN_KEY}.md"
        )
        paper, _doc = load_paper_note(main)
        self.assertEqual(paper.title, "R1 No Main Note Zotero Title")
        self.assertEqual(paper.year, 2026)
        self.assertEqual(len(paper.authors), 1)
        self.assertEqual(paper.authors[0].family, "R1Author")
        self.assertEqual(paper.authors[0].given, "First")
        fm = _frontmatter_dict(main)
        self.assertEqual(fm["journal"], "Nature Methods")
        self.assertEqual(fm["doi"], "10.1000/r1nomain")
        blob = json.dumps(fm, ensure_ascii=False)
        self.assertNotIn("R1PAR1", blob)
        self.assertNotIn("zotero://", blob)

    def test_apply_copies_primary_pdf_from_zotero_storage(self):
        zroot = self._zotero_root()
        db = build_r1_zotero_fixture(zroot)
        with ZoteroAdapter(db_path=db, data_dir=zroot) as ad:
            plan = build_migration_plan(
                self.vault, state_root=self.state, zotero=ad
            )
        result = transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
        )
        migrated = {m["citation_key"]: m for m in result.migrated}
        self.assertIn(R1_ZOTERO_KEY, migrated)
        target = self.vault / "05 Literature" / R1_ZOTERO_KEY
        primary = target / f"{R1_ZOTERO_KEY}.pdf"
        self.assertTrue(primary.is_file())
        expected = hashlib.sha256(
            (zroot / "storage" / "R1ATT2" / "r1zotero-main.pdf").read_bytes()
        ).hexdigest()
        self.assertEqual(
            hashlib.sha256(primary.read_bytes()).hexdigest(), expected
        )
        # secondary (NMF.pdf) lands in attachments/
        nmf = target / "attachments" / "NMF.pdf"
        self.assertTrue(nmf.is_file())
        self.assertEqual(
            hashlib.sha256(nmf.read_bytes()).hexdigest(),
            hashlib.sha256(
                (zroot / "storage" / "R1ATT3" / "NMF.pdf").read_bytes()
            ).hexdigest(),
        )
        # Zotero storage files are never modified
        self.assertEqual(
            (zroot / "storage" / "R1ATT2" / "r1zotero-main.pdf").read_bytes(),
            b"%PDF-1.4 r1 zotero main text fixture.%%EOF",
        )
        report = transaction.verify_migration(
            plan.run_id, vault_root=self.vault, state_root=self.state
        )
        self.assertEqual(report.status, "ok")

    def test_rollback_restores_no_main_note_folder(self):
        plan = build_migration_plan(self.vault, state_root=self.state)
        transaction.apply_migration(
            plan.run_id,
            plan.confirmation_token,
            vault_root=self.vault,
            state_root=self.state,
        )
        transaction.rollback_migration(
            plan.run_id, vault_root=self.vault, state_root=self.state
        )
        source = self.vault / "05 Literature" / R1_NO_MAIN
        self.assertTrue((source / "Figure解读_r1NoMainNote2026.md").is_file())
        self.assertTrue((source / "key-points.md").is_file())
        self.assertFalse(
            (self.vault / "05 Literature" / R1_NO_MAIN_KEY).exists()
        )


class CliMigrationTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = _copy_fixture(self.tmp)
        self.state = self.tmp / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def _dry_run(self):
        result = self._run(
            "migrate",
            "legacy-obsidian",
            "--dry-run",
            "--vault",
            str(self.vault),
            "--state-root",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["data"]
        return data["run_id"], data["confirmation_token"]

    def test_cli_apply_verify_rollback_roundtrip_without_vault_flag(self):
        run_id, token = self._dry_run()
        result = self._run(
            "migrate",
            "legacy-obsidian",
            "--apply",
            run_id,
            "--confirm-token",
            token,
            "--state-root",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"]["action"], "applied")
        self.assertEqual(len(payload["data"]["migrated"]), 9)

        result = self._run(
            "migrate", "verify", run_id, "--state-root", str(self.state)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["status"], "ok")

        result = self._run(
            "migrate", "rollback", run_id, "--state-root", str(self.state)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["action"], "rolled_back")
        self.assertTrue(
            (self.vault / "05 Literature" / "Standard Single PDF 2024").is_dir()
        )

    def test_cli_apply_requires_confirm_token(self):
        run_id, _token = self._dry_run()
        result = self._run(
            "migrate", "legacy-obsidian", "--apply", run_id
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errors"][0]["code"], "user_error")

    def test_cli_apply_wrong_token(self):
        run_id, _token = self._dry_run()
        result = self._run(
            "migrate",
            "legacy-obsidian",
            "--apply",
            run_id,
            "--confirm-token",
            "0" * 64,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_cli_verify_missing_run(self):
        result = self._run("migrate", "verify", "0" * 32)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_cli_verify_reports_corruption(self):
        run_id, token = self._dry_run()
        result = self._run(
            "migrate",
            "legacy-obsidian",
            "--apply",
            run_id,
            "--confirm-token",
            token,
            "--state-root",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pdf = (
            self.vault
            / "05 Literature"
            / "shiauSpatiallyResolvedAnalysis2024"
            / "shiauSpatiallyResolvedAnalysis2024.pdf"
        )
        pdf.write_bytes(b"corrupted")
        result = self._run(
            "migrate", "verify", run_id, "--state-root", str(self.state)
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        codes = [issue["code"] for issue in payload["errors"]]
        self.assertIn("verify_problem", codes)

    def test_cli_rollback_conflict_exit_code(self):
        run_id, token = self._dry_run()
        result = self._run(
            "migrate",
            "legacy-obsidian",
            "--apply",
            run_id,
            "--confirm-token",
            token,
            "--state-root",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        main = (
            self.vault
            / "05 Literature"
            / "shiauSpatiallyResolvedAnalysis2024"
            / "shiauSpatiallyResolvedAnalysis2024.md"
        )
        main.write_text("---\n---\nchanged\n", encoding="utf-8")
        result = self._run(
            "migrate", "rollback", run_id, "--state-root", str(self.state)
        )
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
