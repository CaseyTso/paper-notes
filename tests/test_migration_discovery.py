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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

ALL_LEGACY = (STANDARD, DERMATO, XIA, HASH_FIG, MISSING, AMBIGUOUS, CONFLICT, NO_KEY, UNICODE)


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
        self.assertEqual(inv["items"], 9)
        self.assertEqual(inv["pdfs"], 10)
        self.assertEqual(inv["cards"], 4)
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
            self.assertEqual(len(data["items"]), 9)
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
            self.assertEqual(len(data["items"]), 9)
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
