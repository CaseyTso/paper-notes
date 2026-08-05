"""Migration discovery tests (Task 17).

Frozen after the first red run; do not weaken or delete assertions.

Covers the fixture matrix required by the plan:

- Standard single PDF / no card.
- One card.
- Multiple PDFs / multiple cards.
- Legacy ``citation key``, ``zotero``, ``zotero link``, and ``状态``
  fields.
- Existing hash-named figure directory.
- Missing PDF.
- Ambiguous main PDF.
- Target conflict.
- A legacy path with spaces, punctuation, and Unicode.

Dry-run assertions: ``run_id``, source/target inventory, hashes/counts/
bytes, proposed YAML transformations, primary-PDF confirmation
requirement, backlink/conflict/disk-space diagnostics, and zero
fixture-vault writes.
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

from paper_notes.adapters.zotero import ZoteroAdapter
from paper_notes.migration import (
    RUN_ID_RE,
    build_migration_plan,
    new_run_id,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "legacy_vault"

STANDARD = "Standard Single PDF 2024"
DERMATO = "Dermatomyositis - JAK1 (2025)"
XIA = "Xia Large-scale Single-cell Analysis"
HASH_FIG = "Hash Figures Paper"
MISSING = "Missing PDF Paper"
AMBIGUOUS = "Ambiguous Main PDF"
CONFLICT = "Old Conflict Paper"
NO_KEY = "No Key Paper"
UNICODE = "Unicode 论文, 带 标点 & Ünïcode 2026"
R1_NO_MAIN = "R1 No Main Note"
R1_CARDS_TYPE = "R1 Cards Type Paper"
R1_ZOTERO_PDF = "R1 Zotero PDF Paper"

ALL_LEGACY = (
    STANDARD,
    DERMATO,
    XIA,
    HASH_FIG,
    MISSING,
    AMBIGUOUS,
    CONFLICT,
    NO_KEY,
    UNICODE,
    R1_NO_MAIN,
    R1_CARDS_TYPE,
    R1_ZOTERO_PDF,
)

R1_NO_MAIN_KEY = "r1NoMainNote2026"
R1_CARDS_KEY = "r1CardsType2026"
R1_ZOTERO_KEY = "r1ZoteroPdf2026"


def _pdf_sha256(rel: str) -> str:
    return hashlib.sha256((FIXTURE / rel).read_bytes()).hexdigest()


def _item(plan, source_dir: str) -> dict:
    for item in plan["items"]:
        if item["source_dir"].endswith(source_dir):
            return item
    raise AssertionError(f"item not found in plan: {source_dir!r}; got {[i['source_dir'] for i in plan['items']]}")


def _tree_manifest(root: Path) -> set[tuple]:
    """(relative path, size, sha256, mtime_ns) for every file under root."""
    out = set()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out.add(
                (
                    str(path.relative_to(root)),
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
            )
    return out


class RunIdTest(unittest.TestCase):
    def test_run_id_format(self):
        run_id = new_run_id()
        self.assertRegex(run_id, RUN_ID_RE)
        self.assertEqual(len(run_id), 32)

    def test_run_ids_unique(self):
        self.assertNotEqual(new_run_id(), new_run_id())


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as state:
            cls.plan = build_migration_plan(
                FIXTURE, state_root=Path(state)
            ).to_json()

    def test_inventory_counts_all_legacy_items(self):
        dirs = sorted(i["source_dir"] for i in self.plan["items"])
        self.assertEqual(
            dirs, sorted(f"05 Literature/{name}" for name in ALL_LEGACY)
        )

    def test_canonical_directory_not_discovered(self):
        # The canonical directory is skipped as a legacy item but listed
        # as an existing canonical key (the source of target conflicts).
        self.assertIn("smithExample2026", self.plan["diagnostics"]["canonical_items"])
        self.assertEqual(
            self.plan["diagnostics"]["canonical_items"], ["smithExample2026"]
        )
        self.assertNotIn(
            "05 Literature/smithExample2026",
            [i["source_dir"] for i in self.plan["items"]],
        )

    def test_single_pdf_no_card(self):
        item = _item(self.plan, STANDARD)
        self.assertEqual(item["pdf_count"], 1)
        self.assertEqual(item["card_count"], 0)
        self.assertEqual(item["derived_count"], 1)  # minerUmd note
        self.assertEqual(item["main_note"].endswith("Standard Single PDF 2024.md"), True)
        pdf = item["pdfs"][0]
        self.assertEqual(pdf["sha256"], _pdf_sha256(f"05 Literature/{STANDARD}/paper.pdf"))
        self.assertEqual(pdf["bytes"], (FIXTURE / f"05 Literature/{STANDARD}/paper.pdf").stat().st_size)
        self.assertEqual(item["citation_key"], "shiauSpatiallyResolvedAnalysis2024")

    def test_one_card(self):
        item = _item(self.plan, DERMATO)
        self.assertEqual(item["card_count"], 1)
        self.assertEqual(item["pdf_count"], 1)
        self.assertEqual(item["citation_key"], "osborneDermatomyositisCharacterizedJAK1mediated2025")

    def test_multiple_pdfs_and_cards(self):
        item = _item(self.plan, XIA)
        self.assertEqual(item["pdf_count"], 2)
        self.assertEqual(item["card_count"], 2)
        self.assertEqual(item["derived_count"], 2)  # minerUmd + Figure解读
        rel = f"05 Literature/{XIA}"
        expected = sum(
            p.stat().st_size
            for p in (FIXTURE / rel).rglob("*")
            if p.is_file()
        )
        self.assertEqual(item["total_bytes"], expected)

    def test_legacy_fields_detected_and_transformations(self):
        item = _item(self.plan, STANDARD)
        self.assertEqual(
            sorted(item["zotero_fields"]), ["zotero", "zotero link"]
        )
        self.assertEqual(item["status_field"], "状态")
        tf = item["transformations"][0]
        self.assertEqual(
            sorted(tf["remove"]),
            ["citation key", "zotero", "zotero link", "状态"],
        )
        self.assertEqual(tf["set"]["citation_key"], "shiauSpatiallyResolvedAnalysis2024")
        self.assertEqual(tf["set"]["pdf_status"], "available")
        self.assertEqual(tf["set"]["reading_status"], "read")
        self.assertIn("paper_id", tf["add"])
        # the canonical main note must carry schema_version in the plan
        # itself, not only via Pydantic defaults at parse time
        self.assertEqual(tf["add"]["schema_version"], 1)

    def test_reading_status_mapping(self):
        self.assertEqual(
            _item(self.plan, DERMATO)["transformations"][0]["set"]["reading_status"],
            "reading",
        )
        self.assertEqual(
            _item(self.plan, XIA)["transformations"][0]["set"]["reading_status"],
            "unread",
        )

    def test_hash_named_figure_directory(self):
        item = _item(self.plan, HASH_FIG)
        self.assertEqual(item["has_figure_dir"], True)
        self.assertEqual(item["figure_count"], 1)
        self.assertEqual(item["derived_count"], 1)

    def test_missing_pdf_diagnostic(self):
        item = _item(self.plan, MISSING)
        self.assertIn("missing_pdf", item["diagnostics"])
        self.assertEqual(item["pdf_count"], 0)
        reasons = [c["reason"] for c in item["confirmation_required"]]
        self.assertIn("missing_pdf", reasons)
        self.assertEqual(
            item["transformations"][0]["set"]["pdf_status"], "missing"
        )

    def test_ambiguous_main_pdf_requires_confirmation(self):
        item = _item(self.plan, AMBIGUOUS)
        reasons = [c["reason"] for c in item["confirmation_required"]]
        self.assertIn("multiple_pdfs", reasons)
        requirement = next(
            c for c in item["confirmation_required"] if c["reason"] == "multiple_pdfs"
        )
        self.assertEqual(
            [Path(p).name for p in requirement["candidates"]],
            ["paper-a.pdf", "paper-b.pdf"],
        )

    def test_target_conflict_diagnostic(self):
        item = _item(self.plan, CONFLICT)
        self.assertEqual(item["citation_key"], "smithExample2026")
        self.assertIn("target_conflict", item["diagnostics"])
        self.assertIn("smithExample2026", self.plan["diagnostics"]["target_conflicts"])

    def test_missing_citation_key_requires_confirmation(self):
        item = _item(self.plan, NO_KEY)
        self.assertIsNone(item["citation_key"])
        reasons = [c["reason"] for c in item["confirmation_required"]]
        self.assertIn("missing_citation_key", reasons)

    def test_unicode_path_item(self):
        item = _item(self.plan, UNICODE)
        self.assertEqual(item["citation_key"], "unicodePaper2026")
        self.assertEqual(item["card_count"], 1)
        self.assertEqual(item["pdf_count"], 1)

    def test_backlink_diagnostics(self):
        bl = self.plan["diagnostics"]["backlinks"]
        self.assertEqual(bl[f"05 Literature/{STANDARD}"], 1)
        self.assertEqual(bl[f"05 Literature/{UNICODE}"], 1)
        self.assertEqual(bl[f"05 Literature/{XIA}"], 0)

    def test_disk_space_diagnostics(self):
        ds = self.plan["diagnostics"]["disk_space"]
        self.assertIn("free_bytes", ds)
        self.assertIn("needed_bytes", ds)
        self.assertIsInstance(ds["sufficient"], bool)
        self.assertEqual(ds["needed_bytes"] >= 0, True)

    def test_inventory_diagnostics(self):
        inv = self.plan["diagnostics"]["inventory"]
        self.assertEqual(inv["items"], 12)
        self.assertEqual(inv["pdfs"], 11)
        self.assertEqual(inv["cards"], 6)
        self.assertGreater(inv["total_bytes"], 0)

    def test_zero_fixture_vault_writes(self):
        before = _tree_manifest(FIXTURE)
        with tempfile.TemporaryDirectory() as state:
            build_migration_plan(FIXTURE, state_root=Path(state))
        after = _tree_manifest(FIXTURE)
        self.assertEqual(before, after)

    def test_manifest_written_under_state_root(self):
        with tempfile.TemporaryDirectory() as state:
            state_root = Path(state)
            plan = build_migration_plan(FIXTURE, state_root=state_root)
            manifest_path = state_root / plan.run_id / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data["run_id"], plan.run_id)
            self.assertEqual(data["confirmation_token"], plan.confirmation_token)
            self.assertEqual(len(data["items"]), 12)
            self.assertNotIn(
                "05 Literature",
                str(manifest_path.relative_to(state_root)),
            )
            self.assertNotIn("legacy_vault", str(manifest_path.relative_to(state_root)))

    def test_run_id_and_token_determinism(self):
        with tempfile.TemporaryDirectory() as state:
            state_root = Path(state)
            first = build_migration_plan(FIXTURE, state_root=state_root)
            second = build_migration_plan(FIXTURE, state_root=state_root)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.confirmation_token, second.confirmation_token)

    def test_keys_filter(self):
        with tempfile.TemporaryDirectory() as state:
            plan = build_migration_plan(
                FIXTURE,
                keys=["xiaLargescaleSinglecellAnalysis2026"],
                state_root=Path(state),
            ).to_json()
            self.assertEqual(len(plan["items"]), 1)
            self.assertEqual(
                plan["items"][0]["citation_key"],
                "xiaLargescaleSinglecellAnalysis2026",
            )

    def test_keys_filter_matches_directory_name(self):
        with tempfile.TemporaryDirectory() as state:
            plan = build_migration_plan(
                FIXTURE,
                keys=[UNICODE],
                state_root=Path(state),
            ).to_json()
            self.assertEqual(len(plan["items"]), 1)
            self.assertEqual(plan["items"][0]["citation_key"], "unicodePaper2026")

    def test_duplicate_target_keys_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            root = vault / "05 Literature"
            (root / "Alpha Dir").mkdir(parents=True)
            (root / "Beta Dir").mkdir()
            (root / "Alpha Dir" / "Alpha Dir.md").write_text(
                "---\ncitation key: dupKey2026\n---\n", encoding="utf-8"
            )
            (root / "Beta Dir" / "Beta Dir.md").write_text(
                "---\ncitation key: dupKey2026\n---\n", encoding="utf-8"
            )
            plan = build_migration_plan(vault, state_root=Path(tmp) / "state")
            self.assertIn("dupKey2026", plan.duplicate_targets)


def build_r1_zotero_fixture(root: Path) -> Path:
    """Zotero snapshot fixture for the R1 integration tests (never committed).

    Mirrors the real schema: INTEGER linkMode, ``storage/<attachmentKey>/``
    layout, creator fieldMode 0/1, DOI/publicationTitle fields, and BBT
    ``citationKey`` values equal to the R1 vault fixture keys.
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


class R1DiscoveryTest(unittest.TestCase):
    """R1 semantics (plan 2026-08-05): generate main note, type:cards, and
    keys filtering by Figure-filename key."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as state:
            cls.plan = build_migration_plan(
                FIXTURE, state_root=Path(state)
            ).to_json()

    def test_no_main_note_yields_generate_plan(self):
        item = _item(self.plan, R1_NO_MAIN)
        self.assertIsNone(item["main_note"])
        self.assertEqual(item["main_note_action"], "generate")
        self.assertEqual(item["citation_key"], R1_NO_MAIN_KEY)
        self.assertEqual(item["identity_source"], "figure_filename")
        self.assertEqual(item["identity_fields"], {"citation_key": R1_NO_MAIN_KEY})
        self.assertIn("main_note_generate", item["diagnostics"])
        self.assertEqual(item["derived_count"], 3)  # Figure + minerUmd + cards
        self.assertEqual(item["card_count"], 1)
        self.assertIsNotNone(item["target"])
        self.assertEqual(item["target"]["citation_key"], R1_NO_MAIN_KEY)
        reasons = [c["reason"] for c in item["confirmation_required"]]
        self.assertNotIn("missing_citation_key", reasons)
        self.assertIn("missing_pdf", reasons)

    def test_type_cards_frontmatter_not_main_candidate(self):
        item = _item(self.plan, R1_CARDS_TYPE)
        self.assertEqual(
            item["main_note"],
            f"05 Literature/{R1_CARDS_TYPE}/R1 Cards Type Paper.md",
        )
        self.assertEqual(item["main_note_action"], "transform")
        self.assertEqual(item["card_count"], 1)
        self.assertEqual(item["citation_key"], R1_CARDS_KEY)

    def test_no_main_note_folder_never_uses_cards_as_main(self):
        item = _item(self.plan, R1_NO_MAIN)
        self.assertIsNone(item["main_note"])
        self.assertNotIn("key-points.md", str(item["main_note"]))

    def test_keys_filter_matches_figure_filename_key(self):
        # No frontmatter citation key; only the Figure解读 filename carries
        # r1NoMainNote2026. --keys must still select the item.
        with tempfile.TemporaryDirectory() as state:
            plan = build_migration_plan(
                FIXTURE,
                keys=[R1_NO_MAIN_KEY],
                state_root=Path(state),
            ).to_json()
            self.assertEqual(len(plan["items"]), 1)
            self.assertEqual(plan["items"][0]["citation_key"], R1_NO_MAIN_KEY)
            self.assertIn("R1 No Main Note", plan["items"][0]["source_dir"])


class R1ZoteroIntegrationTest(unittest.TestCase):
    """Dry-run consuming the Zotero storage snapshot (R1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.zroot = self.tmp / "zotero"
        self.zroot.mkdir()
        self.zdb = build_r1_zotero_fixture(self.zroot)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, keys=None):
        with tempfile.TemporaryDirectory() as state:
            with ZoteroAdapter(db_path=self.zdb, data_dir=self.zroot) as ad:
                return build_migration_plan(
                    FIXTURE, keys=keys, state_root=Path(state), zotero=ad
                ).to_json()

    def test_generate_item_identity_from_zotero(self):
        plan = self._plan()
        item = _item(plan, R1_NO_MAIN)
        self.assertEqual(item["main_note_action"], "generate")
        self.assertEqual(item["identity_source"], "zotero_item")
        fields = item["identity_fields"]
        self.assertEqual(fields["citation_key"], R1_NO_MAIN_KEY)
        self.assertEqual(fields["title"], "R1 No Main Note Zotero Title")
        self.assertEqual(fields["journal"], "Nature Methods")
        self.assertEqual(fields["year"], "2026")
        self.assertEqual(fields["DOI"], "10.1000/r1nomain")
        self.assertEqual(
            fields["authors"], [{"family": "R1Author", "given": "First"}]
        )
        # Zotero source keys / zotero:// URLs must never be planned into the
        # generated frontmatter.
        blob = json.dumps(fields, ensure_ascii=False)
        self.assertNotIn("R1PAR1", blob)
        self.assertNotIn("zotero://", blob)
        self.assertNotIn("zotero.org", blob)

    def test_zero_vault_pdf_with_zotero_candidates(self):
        plan = self._plan()
        item = _item(plan, R1_ZOTERO_PDF)
        self.assertEqual(item["pdf_count"], 2)
        self.assertEqual({p["source"] for p in item["pdfs"]}, {"zotero"})
        reasons = [c["reason"] for c in item["confirmation_required"]]
        self.assertIn("multiple_pdfs", reasons)
        self.assertNotIn("missing_pdf", reasons)
        names = sorted(Path(p["path"]).name for p in item["pdfs"])
        self.assertEqual(names, ["NMF.pdf", "r1zotero-main.pdf"])
        for pdf in item["pdfs"]:
            self.assertTrue(Path(pdf["path"]).is_absolute())
            self.assertTrue(pdf["sha256"])
            self.assertGreater(pdf["bytes"], 0)
        main_pdf = next(
            p for p in item["pdfs"] if Path(p["path"]).name == "r1zotero-main.pdf"
        )
        self.assertEqual(
            main_pdf["sha256"],
            hashlib.sha256(
                (
                    self.zroot / "storage" / "R1ATT2" / "r1zotero-main.pdf"
                ).read_bytes()
            ).hexdigest(),
        )
        # recommended primary = main text; NMF.pdf stays a candidate
        req = next(
            c
            for c in item["confirmation_required"]
            if c["reason"] == "multiple_pdfs"
        )
        self.assertEqual(Path(req["recommended_primary"]).name, "r1zotero-main.pdf")
        self.assertIn(
            "NMF.pdf", [Path(c).name for c in req["candidates"]]
        )

    def test_transform_item_keeps_existing_main_note(self):
        plan = self._plan()
        item = _item(plan, R1_ZOTERO_PDF)
        self.assertEqual(item["main_note_action"], "transform")
        self.assertEqual(item["main_note"], f"05 Literature/{R1_ZOTERO_PDF}/R1 Zotero PDF Paper.md")
        self.assertIsNone(item.get("identity_fields"))


class CliMigrationTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "paper_notes", "--json", "migrate", "legacy-obsidian", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_cli_dry_run_json_envelope(self):
        with tempfile.TemporaryDirectory() as state:
            result = self._run(
                "--dry-run", "--vault", str(FIXTURE), "--state-root", state
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "needs_confirmation")
            data = payload["data"]
            self.assertIn("run_id", data)
            self.assertRegex(data["run_id"], RUN_ID_RE)
            self.assertIn("confirmation_token", data)
            self.assertEqual(data["writes"]["vault_writes"], 0)
            self.assertEqual(len(data["items"]), 12)
            self.assertTrue(Path(data["manifest_path"]).is_file())

    def test_cli_keys_filter(self):
        with tempfile.TemporaryDirectory() as state:
            result = self._run(
                "--dry-run",
                "--vault", str(FIXTURE),
                "--keys", "xiaLargescaleSinglecellAnalysis2026",
                "--state-root", state,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["data"]["items"]), 1)

    def test_cli_keys_file(self):
        with tempfile.TemporaryDirectory() as state:
            keys_file = Path(state) / "keys.txt"
            keys_file.write_text(
                "xiaLargescaleSinglecellAnalysis2026\nambiguousMainPdf2026\n",
                encoding="utf-8",
            )
            result = self._run(
                "--dry-run",
                "--vault", str(FIXTURE),
                "--keys-file", str(keys_file),
                "--state-root", state,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["data"]["items"]), 2)

    def test_cli_requires_dry_run(self):
        with tempfile.TemporaryDirectory() as state:
            result = self._run("--vault", str(FIXTURE), "--state-root", state)
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["errors"][0]["code"], "user_error")

    def test_cli_dry_run_consumes_zotero_snapshot(self):
        # R1: --zotero-db/--zotero-data-dir feed the plan builder; the
        # Zotero-storage PDFs appear as primary-PDF candidates.
        with tempfile.TemporaryDirectory() as state:
            zroot = Path(state) / "zotero"
            zroot.mkdir()
            zdb = build_r1_zotero_fixture(zroot)
            result = self._run(
                "--dry-run",
                "--vault",
                str(FIXTURE),
                "--zotero-db",
                str(zdb),
                "--zotero-data-dir",
                str(zroot),
                "--state-root",
                str(Path(state) / "state"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            items = {
                i["source_dir"].split("/")[-1]: i for i in payload["data"]["items"]
            }
            item = items[R1_ZOTERO_PDF]
            self.assertEqual(item["pdf_count"], 2)
            self.assertEqual({p["source"] for p in item["pdfs"]}, {"zotero"})
            reasons = [c["reason"] for c in item["confirmation_required"]]
            self.assertIn("multiple_pdfs", reasons)
            self.assertNotIn("missing_pdf", reasons)

    def test_cli_zero_fixture_vault_writes(self):
        before = _tree_manifest(FIXTURE)
        with tempfile.TemporaryDirectory() as state:
            result = self._run(
                "--dry-run", "--vault", str(FIXTURE), "--state-root", state
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        after = _tree_manifest(FIXTURE)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
