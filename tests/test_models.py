"""Canonical paper schema tests (Task 3).

Frozen after the first red run; do not weaken or delete assertions.
"""

import unittest
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML

from paper_notes.models import Author, Paper, SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "items"

VALID_ID = "550e8400-e29b-41d4-a716-446655440000"


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return YAML(typ="safe").load(fh)


def base_kwargs(**overrides) -> dict:
    kwargs = {
        "paper_id": VALID_ID,
        "citation_key": "shiauSpatiallyResolvedAnalysis2024",
        "item_type": "article-journal",
        "title": "A paper title",
        "authors": [{"family": "Shiau", "given": "Chia-Yu"}],
        "publication_date": "2024-01-01",
        "year": 2024,
        "pdf_status": "available",
        "reading_status": "unread",
    }
    kwargs.update(overrides)
    return kwargs


class PaperIdTest(unittest.TestCase):
    def test_valid_uuid_accepted(self):
        paper = Paper(**base_kwargs())
        self.assertEqual(str(paper.paper_id), VALID_ID)

    def test_invalid_uuid_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(paper_id="not-a-uuid"))

    def test_missing_paper_id_rejected(self):
        kwargs = base_kwargs()
        del kwargs["paper_id"]
        with self.assertRaises(ValidationError):
            Paper(**kwargs)


class CitationKeyTest(unittest.TestCase):
    def test_valid_bbt_style_key_accepted(self):
        paper = Paper(**base_kwargs(citation_key="shiauSpatiallyResolvedAnalysis2024"))
        self.assertEqual(paper.citation_key, "shiauSpatiallyResolvedAnalysis2024")

    def test_empty_key_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key=""))

    def test_key_with_slash_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key="a/b"))

    def test_key_with_space_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key="smith example2026"))

    def test_key_starting_with_digit_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key="2024Smith"))

    def test_key_with_dotdot_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key="..smith2026"))

    def test_duplicate_aliases_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(
                **base_kwargs(
                    citation_key_aliases=["oldKey2020", "oldKey2020"]
                )
            )

    def test_current_key_in_aliases_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(
                **base_kwargs(
                    citation_key_aliases=["shiauSpatiallyResolvedAnalysis2024"]
                )
            )

    def test_alias_with_slash_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(citation_key_aliases=["old/key"]))


class ItemTypeTest(unittest.TestCase):
    def test_article_journal_default(self):
        kwargs = base_kwargs()
        del kwargs["item_type"]
        paper = Paper(**kwargs)
        self.assertEqual(paper.item_type, "article-journal")

    def test_preprint_accepted(self):
        paper = Paper(**base_kwargs(item_type="preprint"))
        self.assertEqual(paper.item_type, "preprint")

    def test_unknown_item_type_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(item_type="thesis"))


class AuthorTest(unittest.TestCase):
    def test_personal_author(self):
        paper = Paper(
            **base_kwargs(authors=[{"family": "Shiau", "given": "Chia-Yu"}])
        )
        self.assertEqual(paper.authors[0].family, "Shiau")
        self.assertEqual(paper.authors[0].given, "Chia-Yu")
        self.assertIsNone(paper.authors[0].literal)

    def test_literal_group_author(self):
        paper = Paper(
            **base_kwargs(
                authors=[{"literal": "National Lung Cancer Consortium"}]
            )
        )
        self.assertEqual(paper.authors[0].literal, "National Lung Cancer Consortium")
        self.assertIsNone(paper.authors[0].family)

    def test_empty_author_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(authors=[{}]))


class PublicationDateTest(unittest.TestCase):
    def test_full_date_accepted(self):
        paper = Paper(**base_kwargs(publication_date="2024-01-01"))
        self.assertEqual(paper.publication_date, "2024-01-01")

    def test_year_month_accepted(self):
        paper = Paper(**base_kwargs(publication_date="2024-03"))
        self.assertEqual(paper.publication_date, "2024-03")

    def test_year_only_accepted(self):
        paper = Paper(**base_kwargs(publication_date="2024"))
        self.assertEqual(paper.publication_date, "2024")

    def test_impossible_month_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(publication_date="2024-13-01"))

    def test_impossible_day_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(publication_date="2024-02-30"))

    def test_unpadded_date_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(publication_date="2024-1-1"))

    def test_year_derived_from_date_when_missing(self):
        kwargs = base_kwargs()
        del kwargs["year"]
        paper = Paper(**kwargs)
        self.assertEqual(paper.year, 2024)

    def test_year_mismatch_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(publication_date="2024-01-01", year=2025))

    def test_year_only_integer_normalized(self):
        paper = Paper(**base_kwargs(publication_date=2026, year=2026))
        self.assertEqual(paper.publication_date, "2026")
        self.assertEqual(paper.year, 2026)

    def test_non_year_integer_rejected(self):
        for bad in (20261, 261, 0, -5):
            with self.assertRaises(ValidationError):
                Paper(**base_kwargs(publication_date=bad))

    def test_year_without_date_accepted(self):
        kwargs = base_kwargs(publication_date=None)
        paper = Paper(**kwargs)
        self.assertEqual(paper.year, 2024)


class StatusTest(unittest.TestCase):
    def test_reading_status_values(self):
        for status in ("unread", "reading", "read"):
            paper = Paper(**base_kwargs(reading_status=status))
            self.assertEqual(paper.reading_status, status)

    def test_reading_status_invalid_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(reading_status="done"))

    def test_reading_status_default_unread(self):
        kwargs = base_kwargs()
        del kwargs["reading_status"]
        paper = Paper(**kwargs)
        self.assertEqual(paper.reading_status, "unread")

    def test_pdf_status_values(self):
        for status in ("missing", "available"):
            paper = Paper(**base_kwargs(pdf_status=status))
            self.assertEqual(paper.pdf_status, status)

    def test_pdf_status_invalid_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(pdf_status="processing"))


class ProvenanceTest(unittest.TestCase):
    def test_provenance_sources_listed(self):
        paper = Paper(
            **base_kwargs(
                metadata_sources=["crossref", "pubmed"],
                field_provenance={"title": "crossref", "authors": "pubmed"},
            )
        )
        self.assertEqual(paper.metadata_sources, ["crossref", "pubmed"])
        self.assertEqual(paper.field_provenance["title"], "crossref")

    def test_provenance_source_not_listed_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(
                **base_kwargs(
                    metadata_sources=["pubmed"],
                    field_provenance={"title": "crossref"},
                )
            )

    def test_empty_provenance_with_empty_sources_accepted(self):
        paper = Paper(**base_kwargs())
        self.assertEqual(paper.metadata_sources, [])
        self.assertEqual(paper.field_provenance, {})


class ForbiddenMetricsTest(unittest.TestCase):
    def test_forbidden_fields_rejected_case_insensitive(self):
        for name in ("IF", "if", "If", "JCI", "jci", "JCR", "jcr", "CAS", "cas"):
            with self.assertRaises(ValidationError):
                Paper(**base_kwargs(**{name: 10.2}))

    def test_easyscholar_rejected(self):
        for name in ("EasyScholar", "easyscholar", "EASYSCHOLAR"):
            with self.assertRaises(ValidationError):
                Paper(**base_kwargs(**{name: {"impact_factor": 10.2}}))

    def test_metric_field_name_variants_rejected(self):
        for name in (
            "cas_partition",
            "CAS_partition",
            "casPartition",
            "caspartition",
            "easyscholar_cache",
            "easyscholarCache",
            "impact_factor",
            "ImpactFactor",
            "impactfactor",
        ):
            with self.assertRaises(ValidationError):
                Paper(**base_kwargs(**{name: "x"}))

    def test_unrelated_user_fields_not_rejected(self):
        for name in (
            "castle",
            "case_study",
            "growth_factor",
            "impact_study",
            "custom_note",
            "my_flag",
            "science",
            "sci_fi",
        ):
            paper = Paper(**base_kwargs(**{name: "ok"}))
            self.assertEqual(getattr(paper, name), "ok")

    def test_easyscholar_raw_fields_rejected(self):
        for name in (
            "sciif",
            "sciif5",
            "sci",
            "sciBase",
            "sciUp",
            "sciUpSmall",
            "sciUpTop",
            "SCIIF",
            "SciBase",
            "SCI_UP",
        ):
            with self.assertRaises(ValidationError):
                Paper(**base_kwargs(**{name: "12.3"}))


class RoundTripTest(unittest.TestCase):
    def test_unknown_user_fields_preserved(self):
        paper = Paper(**base_kwargs(custom_note="keep me", my_flag=True))
        self.assertEqual(paper.custom_note, "keep me")
        self.assertTrue(paper.my_flag)

    def test_schema_version_fixed(self):
        with self.assertRaises(ValidationError):
            Paper(**base_kwargs(schema_version=2))

    def test_schema_version_default(self):
        kwargs = base_kwargs()
        kwargs.pop("schema_version", None)
        paper = Paper(**kwargs)
        self.assertEqual(paper.schema_version, SCHEMA_VERSION)


class FixtureTest(unittest.TestCase):
    def test_valid_article_fixture(self):
        paper = Paper(**load_yaml(FIXTURES / "valid_article.yaml"))
        self.assertEqual(paper.item_type, "article-journal")
        self.assertEqual(paper.citation_key, "shiauSpatiallyResolvedAnalysis2024")
        self.assertEqual(paper.year, 2024)
        self.assertEqual(paper.pdf_status, "available")
        # non-reserved user field survives the round trip
        self.assertEqual(paper.custom_note, "preserved user field")

    def test_valid_preprint_fixture(self):
        paper = Paper(**load_yaml(FIXTURES / "valid_preprint.yaml"))
        self.assertEqual(paper.item_type, "preprint")
        self.assertEqual(paper.publication_date, "2025-03")
        self.assertEqual(paper.year, 2025)
        self.assertEqual(paper.pdf_status, "missing")
        self.assertEqual(paper.reading_status, "reading")
        self.assertEqual(paper.citation_key_aliases, ["liuDeepLearningPre2025"])

    def test_invalid_metrics_fixture_rejected(self):
        with self.assertRaises(ValidationError):
            Paper(**load_yaml(FIXTURES / "invalid_metrics.yaml"))


if __name__ == "__main__":
    unittest.main()
