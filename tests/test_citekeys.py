"""Citation-key allocation and alias reservation tests (Task 7).

Frozen after the first red run; do not weaken or delete assertions.
"""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_notes import citekeys

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "citekeys" / "regression.json"


class RegressionFixtureTest(unittest.TestCase):
    """Every fixture entry must reproduce the real BBT citation key."""

    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_all_fixture_entries_generate_expected_key(self):
        checked = 0
        for e in self.entries:
            if e.get("skip_generation"):
                continue
            with self.subTest(key=e["citation_key"]):
                occupied = frozenset(e.get("occupied", []))
                got = citekeys.allocate_citation_key(
                    e["title"], e["year"], e["creators"], occupied
                )
                self.assertEqual(got, e["citation_key"])
            checked += 1
        self.assertGreaterEqual(checked, 45)  # regression breadth


class TransliterationTest(unittest.TestCase):
    def test_german_umlauts(self):
        self.assertEqual(citekeys.transliterate("Bücklein"), "Buecklein")
        self.assertEqual(citekeys.transliterate("Müller"), "Mueller")
        self.assertEqual(citekeys.transliterate("Grüne Welle"), "Gruene Welle")
        self.assertEqual(citekeys.transliterate("Straße"), "Strasse")

    def test_other_accents_stripped(self):
        self.assertEqual(citekeys.transliterate("Associação"), "Associacao")
        self.assertEqual(citekeys.transliterate("François"), "Francois")

    def test_cjk_transliterated(self):
        self.assertEqual(citekeys.transliterate("胰腺外分泌功能检测方法的临床应用"),
                         "YiXianWaiFenMiGongNengJianCeFangFaDeLinChuangYingYong")
        self.assertEqual(citekeys.transliterate("左林"), "ZuoLin")

    def test_cjk_author_slug_whole_name_camelcase(self):
        # BBT transliterates the whole CJK name (single-field lastName),
        # every pinyin syllable capitalized (fixture: ZuoLinYiXian...)
        self.assertEqual(citekeys.author_slug("左林", ""), "ZuoLin")


class GenerationRuleTest(unittest.TestCase):
    def test_author_slug_particles_joined_lowercase(self):
        self.assertEqual(citekeys.author_slug("de Boer", "Max"), "deboer")
        self.assertEqual(citekeys.author_slug("van der Waals", "Johannes"), "vanderwaals")
        self.assertEqual(citekeys.author_slug("Del Bufalo", "Franco"), "delbufalo")
        self.assertEqual(citekeys.author_slug("De Matteis", "Anna"), "dematteis")
        self.assertEqual(citekeys.author_slug("Rente Lavastida", "Carla"), "rentelavastida")
        self.assertEqual(citekeys.author_slug("Bücklein", "Veit"), "buecklein")

    def test_organization_author_full_name_lowercase(self):
        self.assertEqual(
            citekeys.author_slug("American Heart Association", ""),
            "americanheartassociation",
        )

    def test_single_character_words_skipped_in_title(self):
        # "CAR T cell-induced..." -> T (len 1) is skipped
        self.assertEqual(
            citekeys.base_key(
                "CAR T cell-induced cytokine release syndrome is mediated by...",
                "2018",
                [{"family": "Giavridis", "given": "Theo"}],
            ),
            "giavridisCARCellinducedCytokine2018",
        )

    def test_no_creators_omits_author_segment(self):
        self.assertEqual(
            citekeys.base_key(
                "A Multi-Omic Single-Cell Landscape of Cytokine Release Syndrome",
                "2023",
                [],
            ),
            "MultiOmicSingleCellLandscape2023",
        )

    def test_short_title_keeps_casing_and_joins_hyphenated_tokens(self):
        self.assertEqual(
            citekeys.base_key(
                "T-cell recruiting immunotherapies in B-cell lymphoma",
                "2024",
                [{"family": "Bücklein", "given": "Veit"}],
            ),
            "bueckleinTcellRecruitingImmunotherapies2024",
        )
        self.assertEqual(
            citekeys.base_key(
                "CAR T-Cells for the Treatment of B-Cell Acute Lymphoblastic Leukemia",
                "2023",
                [{"family": "Saleh", "given": "Kris"}],
            ),
            "salehCARTCellsTreatment2023",
        )

    def test_missing_year_omits_year_segment(self):
        self.assertEqual(
            citekeys.base_key(
                "Clinical Characteristics of Adolescents and Youth",
                "",
                [],
            ),
            "ClinicalCharacteristicsAdolescents",
        )

    def test_year_parsed_from_full_date(self):
        self.assertEqual(
            citekeys.base_key(
                "Alpha Title",
                "2023-11-02",
                [{"family": "Wu", "given": "X"}],
            ),
            "wuAlphaTitle2023",
        )

    def test_stopwords_removed_case_insensitive(self):
        self.assertEqual(
            citekeys.base_key(
                "Imaging the Side Effects of CAR T Cell Therapy: A Primer",
                "2023",
                [{"family": "Huang", "given": "X"}],
            ),
            "huangImagingSideEffects2023",
        )
        # "vs" is not a stopword; "down" is
        self.assertEqual(
            citekeys.base_key(
                "Primary vs. pre-emptive anti-seizure medication prophylaxis",
                "2024",
                [{"family": "Pensato", "given": "U"}],
            ),
            "pensatoPrimaryVsPreemptive2024",
        )
        self.assertEqual(
            citekeys.base_key(
                "[Batten down the hatches: CAR T-cells - immuno-oncology",
                "2019",
                [{"family": "Borrega", "given": "R"}],
            ),
            "borregaBattenHatchesCAR2019",
        )


class CollisionTest(unittest.TestCase):
    def test_suffix_ladder_a_b_c(self):
        base = "smithAlphaBaseTitle2020"
        occ = {base}
        self.assertEqual(
            citekeys.allocate_citation_key(
                "Alpha Base Title", "2020", [{"family": "Smith", "given": "A"}], occ
            ),
            f"{base}a",
        )
        occ.add(f"{base}a")
        self.assertEqual(
            citekeys.allocate_citation_key(
                "Alpha Base Title", "2020", [{"family": "Smith", "given": "A"}], occ
            ),
            f"{base}b",
        )

    def test_suffix_ladder_z_then_aa(self):
        base = "smithTitleExampleZeta2020"
        occ = {base} | {f"{base}{chr(ord('a') + i)}" for i in range(25)}  # base, a..y
        self.assertEqual(
            citekeys.allocate_citation_key(
                "Title Example Zeta", "2020", [{"family": "Smith", "given": "A"}], occ
            ),
            f"{base}z",
        )
        occ.add(f"{base}z")
        self.assertEqual(
            citekeys.allocate_citation_key(
                "Title Example Zeta", "2020", [{"family": "Smith", "given": "A"}], occ
            ),
            f"{base}aa",
        )

    def test_current_keys_and_aliases_both_block(self):
        occupied = {"liInductionChemotherapyUnresectable2020", "aliasOldKey"}
        self.assertEqual(
            citekeys.allocate_citation_key(
                "Induction chemotherapy for unresectable Stage III",
                "2020",
                [{"family": "Li", "given": "J"}],
                occupied,
            ),
            "liInductionChemotherapyUnresectable2020a",
        )


class MigrationTest(unittest.TestCase):
    def test_existing_key_returned_unchanged(self):
        # a migrated/refresh path passes the existing key: never re-allocate
        self.assertEqual(
            citekeys.citation_key_for(
                existing="wuCongenitalMultipleEventrations2014",
                title="Congenital multiple eventrations of the right diaphragm",
                year="2014",
                creators=[{"family": "Wu", "given": "X"}],
                occupied=frozenset(),
            ),
            "wuCongenitalMultipleEventrations2014",
        )

    def test_existing_key_unchanged_even_when_occupied_conflicts(self):
        key = "liInductionChemotherapyUnresectable2020a"
        self.assertEqual(
            citekeys.citation_key_for(
                existing=key,
                title="Induction chemotherapy for unresectable Stage III",
                year="2020",
                creators=[{"family": "Li", "given": "J"}],
                occupied=frozenset({key, "liInductionChemotherapyUnresectable2020"}),
            ),
            key,
        )

    def test_no_existing_key_allocates(self):
        got = citekeys.citation_key_for(
            existing="",
            title="Congenital multiple eventrations of the right diaphragm",
            year="2014",
            creators=[{"family": "Wu", "given": "X"}],
            occupied=frozenset(),
        )
        self.assertEqual(got, "wuCongenitalMultipleEventrations2014")


class AllocationSafetyTest(unittest.TestCase):
    """The allocator must never return a key the canonical schema rejects."""

    def _alloc(self, title, year, creators, occupied=frozenset()):
        return citekeys.allocate_citation_key(title, year, creators, occupied)

    def test_unmapped_cjk_raises(self):
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("肺癌精准治疗的新进展", "2026", [{"family": "陈", "given": ""}])

    def test_mixed_cjk_latin_raises(self):
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("AI在肺癌中的应用", "2026", [{"family": "张", "given": ""}])

    def test_cjk_punctuation_raises(self):
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("肺癌（精准）治疗：新进展", "2026", [{"family": "Zhang", "given": "X"}])

    def test_empty_title_and_no_creators_raises(self):
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("", "2026", [])

    def test_year_only_raises(self):
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("", "2024", [])

    def test_digit_leading_without_author_raises(self):
        # no author segment -> the key would start with a digit
        with self.assertRaises(citekeys.AllocationError):
            self._alloc("8th Lung Cancer Staging", "2018", [])

    def test_valid_keys_still_allocate(self):
        got = self._alloc(
            "Congenital multiple eventrations of the right diaphragm",
            "2014",
            [{"family": "Wu", "given": "X"}],
        )
        self.assertEqual(got, "wuCongenitalMultipleEventrations2014")


class GroupAuthorTest(unittest.TestCase):
    def test_literal_group_author_slug(self):
        # canonical Author.literal form, not a fake family/given pair
        self.assertEqual(
            citekeys.author_slug("World Health Organization", ""),
            "worldhealthorganization",
        )

    def test_literal_group_author_key(self):
        got = citekeys.allocate_citation_key(
            "Global Tuberculosis Report",
            "2024",
            [{"literal": "World Health Organization"}],
            frozenset(),
        )
        self.assertEqual(
            got, "worldhealthorganizationGlobalTuberculosisReport2024"
        )


class GetCitekeyScriptTest(unittest.TestCase):
    """Migration-compatibility mode of scripts/get_citekey.py."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "get_citekey", REPO / "scripts" / "get_citekey.py"
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _make_db(self):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "zotero.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
            CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO fields (fieldID, fieldName) VALUES (110, 'citationKey'),
                (1, 'title'), (2, 'date');
            INSERT INTO items (itemID, key) VALUES (1, 'HASKEY1'), (2, 'NOKEY1');
            INSERT INTO itemDataValues (valueID, value) VALUES
                (1001, 'wuCongenitalMultipleEventrations2014'), (1002, 'A Title');
            INSERT INTO itemData (itemID, fieldID, valueID) VALUES
                (1, 110, 1001), (2, 1, 1002);
            """
        )
        conn.commit()
        conn.close()
        return td, db

    def test_existing_key_returned(self):
        td, db = self._make_db()
        try:
            self.assertEqual(
                self.mod.get_citekey("HASKEY1", db_path=str(db)),
                "wuCongenitalMultipleEventrations2014",
            )
        finally:
            td.cleanup()

    def test_missing_key_returns_none_no_fallback(self):
        td, db = self._make_db()
        try:
            # no title+year fallback remains: missing BBT key -> None
            self.assertIsNone(self.mod.get_citekey("NOKEY1", db_path=str(db)))
        finally:
            td.cleanup()

    def test_batch_marks_requires_allocation(self):
        td, db = self._make_db()
        try:
            results = self.mod.get_citekeys_batch(
                ["HASKEY1", "NOKEY1"], db_path=str(db)
            )
            self.assertEqual(
                results["HASKEY1"]["citation_key"],
                "wuCongenitalMultipleEventrations2014",
            )
            self.assertFalse(results["HASKEY1"]["requires_allocation"])
            self.assertTrue(results["NOKEY1"]["requires_allocation"])
            self.assertIsNone(results["NOKEY1"]["citation_key"])
        finally:
            td.cleanup()

    def test_missing_item_reports_not_found(self):
        import io
        import contextlib

        td, db = self._make_db()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.mod.main(["GHOSTKEY", "--db-path", str(db)])
            payload = json.loads(buf.getvalue().strip())
            self.assertEqual(payload["status"], "not_found")
        finally:
            td.cleanup()

    def test_batch_distinguishes_not_found_from_missing_key(self):
        td, db = self._make_db()
        try:
            results = self.mod.get_citekeys_batch(
                ["HASKEY1", "NOKEY1", "GHOSTKEY"], db_path=str(db)
            )
            self.assertEqual(
                results["HASKEY1"],
                {"citation_key": "wuCongenitalMultipleEventrations2014",
                 "requires_allocation": False, "not_found": False},
            )
            self.assertEqual(
                results["NOKEY1"],
                {"citation_key": None, "requires_allocation": True,
                 "not_found": False},
            )
            self.assertEqual(
                results["GHOSTKEY"],
                {"citation_key": None, "requires_allocation": False,
                 "not_found": True},
            )
        finally:
            td.cleanup()

    def test_missing_key_structured_cli_output(self):
        import io
        import contextlib

        td, db = self._make_db()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.mod.main(["NOKEY1", "--db-path", str(db)])
            out = buf.getvalue().strip()
            payload = json.loads(out)
            self.assertEqual(payload["status"], "requires_core_allocation")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
