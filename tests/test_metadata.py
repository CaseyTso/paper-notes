"""Structured metadata adapters and deterministic merge tests (Task 9).

Frozen after the first red run; do not weaken or delete assertions.
All network access is mocked (unittest.mock) — no live network in unit
tests. AI is only an optional untrusted suggestion input; no AI call is
implemented in the v0.1 core.
"""

import unittest
from unittest import mock

from paper_notes.adapters.arxiv import ArxivAdapter
from paper_notes.adapters.crossref import CrossrefAdapter
from paper_notes.adapters.pubmed import PubMedAdapter
from paper_notes.identifiers import ParsedIdentifier
from paper_notes.metadata import (
    FieldConflict,
    MetadataCandidate,
    ResolutionError,
    SourceRecord,
    merge_records,
    resolve,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


CROSSREF_MESSAGE = {
    "message": {
        "title": ["Spatially resolved analysis of lung adenocarcinoma"],
        "author": [
            {"given": "Chia-Yu", "family": "Shiau", "sequence": "first"},
            {"given": "John", "family": "Smith", "sequence": "additional"},
        ],
        "issued": {"date-parts": [[2024, 5, 1]]},
        "container-title": ["Nature Medicine"],
        "short-container-title": ["Nat Med"],
        "volume": "30",
        "issue": "5",
        "page": "1234-1245",
        "DOI": "10.1038/s41591-024-00000-0",
        "ISSN": ["1546-170X"],
        "URL": "https://doi.org/10.1038/s41591-024-00000-0",
        "abstract": "<jats:p>Background text.</jats:p>",
        "publisher": "Springer Nature",
    }
}


def ident(kind, value):
    return ParsedIdentifier(kind=kind, value=value, original=value)


class CrossrefAdapterTest(unittest.TestCase):
    def test_maps_message_to_canonical_dict(self):
        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(payload=CROSSREF_MESSAGE),
        ) as get:
            record = CrossrefAdapter().fetch(ident("doi", "10.1038/s41591-024-00000-0"))
        get.assert_called_once()
        url = get.call_args.args[0]
        self.assertIn("api.crossref.org/works/", url)
        self.assertEqual(
            record,
            {
                "title": "Spatially resolved analysis of lung adenocarcinoma",
                "authors": [
                    {"given": "Chia-Yu", "family": "Shiau"},
                    {"given": "John", "family": "Smith"},
                ],
                "publication_date": "2024-05-01",
                "year": 2024,
                "journal": "Nature Medicine",
                "journal_abbreviation": "Nat Med",
                "volume": "30",
                "issue": "5",
                "pages": "1234-1245",
                "doi": "10.1038/s41591-024-00000-0",
                "issn": ["1546-170X"],
                "url": "https://doi.org/10.1038/s41591-024-00000-0",
                "abstract": "Background text.",
                "publisher": "Springer Nature",
            },
        )

    def test_abstract_jats_tags_stripped(self):
        msg = dict(CROSSREF_MESSAGE)
        msg["message"] = dict(CROSSREF_MESSAGE["message"])
        msg["message"]["abstract"] = "<jats:p>Text <jats:italic>with</jats:italic> tags.</jats:p>"
        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(payload=msg),
        ):
            record = CrossrefAdapter().fetch(ident("doi", "10.1038/x"))
        self.assertEqual(record["abstract"], "Text with tags.")

    def test_http_error_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(status_code=404, payload={}),
        ):
            with self.assertRaises(AdapterError):
                CrossrefAdapter().fetch(ident("doi", "10.1000/missing"))

    def test_network_error_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            side_effect=__import__("requests").ConnectionError("down"),
        ):
            with self.assertRaises(AdapterError):
                CrossrefAdapter().fetch(ident("doi", "10.1000/xyz"))

    def test_invalid_json_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(payload=None),
        ):
            with self.assertRaises(AdapterError):
                CrossrefAdapter().fetch(ident("doi", "10.1000/xyz"))

    def test_missing_message_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(payload={"status": "failed"}),
        ):
            with self.assertRaises(AdapterError):
                CrossrefAdapter().fetch(ident("doi", "10.1000/xyz"))

    def test_impossible_date_parts_dropped(self):
        msg = dict(CROSSREF_MESSAGE)
        msg["message"] = dict(CROSSREF_MESSAGE["message"])
        msg["message"]["issued"] = {"date-parts": [[2024, 13, 99]]}
        with mock.patch(
            "paper_notes.adapters.crossref.requests.get",
            return_value=FakeResponse(payload=msg),
        ):
            record = CrossrefAdapter().fetch(ident("doi", "10.1038/x"))
        self.assertNotIn("publication_date", record)
        # the year component itself is still valid
        self.assertEqual(record["year"], 2024)


EFETCH_XML = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMed 2.0//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_200101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">28845751</PMID>
      <Article PubModel="Print">
        <Journal>
          <ISSN IssnType="Electronic">1527-7755</ISSN>
          <JournalIssue CitedMedium="Internet">
            <Volume>35</Volume>
            <Issue>6</Issue>
            <PubDate>
              <Year>2017</Year>
              <Month>Nov</Month>
            </PubDate>
          </JournalIssue>
          <Title>Journal of Clinical Oncology</Title>
          <ISOAbbreviation>J Clin Oncol</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Spatially resolved analysis of lung adenocarcinoma.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Background text.</AbstractText>
          <AbstractText Label="METHODS">Methods text.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author>
            <LastName>Shiau</LastName>
            <ForeName>Chia-Yu</ForeName>
          </Author>
          <Author>
            <LastName>Smith</LastName>
            <ForeName>John A</ForeName>
          </Author>
        </AuthorList>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""

EFETCH_EMPTY = """<?xml version="1.0" ?>
<PubmedArticleSet></PubmedArticleSet>
"""

PMC_ESUMMARY = {
    "header": {"type": "esummary", "version": "0.3"},
    "result": {
        "13428259": {
            "title": "Spatially resolved analysis of lung adenocarcinoma",
            "authors": [{"name": "Shiau CY"}, {"name": "Smith JA"}],
            "fulljournalname": "Journal of Clinical Oncology",
            "pubdate": "2017 Nov",
            "volume": "35",
            "issue": "6",
            "pages": "3001-3010",
            "articleids": [
                {"idtype": "pmcid", "idautn": 1, "value": "pmc-id: PMC13428259;"},
                {"idtype": "pubmed", "idautn": 1, "value": "28845751"},
            ],
        }
    },
}


EFETCH_INLINE = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">28845752</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <Volume>1</Volume>
            <PubDate><Year>2024</Year></PubDate>
          </JournalIssue>
          <Title>Test Journal</Title>
        </Journal>
        <ArticleTitle>Alpha <i>beta</i> gamma</ArticleTitle>
        <Abstract>
          <AbstractText>One <b>two</b> three</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""

EFETCH_BAD_DATE = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">28845753</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2024</Year><Month>13</Month><Day>99</Day></PubDate>
          </JournalIssue>
          <Title>Test Journal</Title>
        </Journal>
        <ArticleTitle>A title</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


class PubMedAdapterTest(unittest.TestCase):
    def test_pmid_efetch_xml_maps_to_canonical_dict(self):
        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            return_value=FakeResponse(text=EFETCH_XML),
        ) as get:
            record = PubMedAdapter().fetch(ident("pmid", "28845751"))
        url = get.call_args.args[0]
        self.assertIn("efetch.fcgi", url)
        self.assertEqual(
            record,
            {
                "title": "Spatially resolved analysis of lung adenocarcinoma.",
                "authors": [
                    {"family": "Shiau", "given": "Chia-Yu"},
                    {"family": "Smith", "given": "John A"},
                ],
                "publication_date": "2017-11",
                "year": 2017,
                "journal": "Journal of Clinical Oncology",
                "journal_abbreviation": "J Clin Oncol",
                "volume": "35",
                "issue": "6",
                "pmid": "28845751",
                "abstract": "Background text. Methods text.",
                "issn": ["1527-7755"],
                "language": "eng",
            },
        )

    def test_pmid_efetch_missing_article_raises(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            return_value=FakeResponse(text=EFETCH_EMPTY),
        ):
            with self.assertRaises(AdapterError):
                PubMedAdapter().fetch(ident("pmid", "99999999"))

    def test_pmcid_esummary_maps_to_canonical_dict(self):
        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            return_value=FakeResponse(payload=PMC_ESUMMARY),
        ) as get:
            record = PubMedAdapter().fetch(ident("pmcid", "PMC13428259"))
        url = get.call_args.args[0]
        self.assertIn("esummary.fcgi", url)
        # real NCBI contract: db=pmc takes the bare numeric uid
        self.assertEqual(get.call_args.kwargs["params"]["db"], "pmc")
        self.assertEqual(get.call_args.kwargs["params"]["id"], "13428259")
        self.assertEqual(
            record,
            {
                "title": "Spatially resolved analysis of lung adenocarcinoma",
                "authors": [
                    {"family": "Shiau", "given": "CY"},
                    {"family": "Smith", "given": "JA"},
                ],
                "publication_date": "2017-11",
                "year": 2017,
                "journal": "Journal of Clinical Oncology",
                "volume": "35",
                "issue": "6",
                "pages": "3001-3010",
                "pmcid": "PMC13428259",
                "pmid": "28845751",
            },
        )

    def test_pmid_efetch_inline_markup_preserved(self):
        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            return_value=FakeResponse(text=EFETCH_INLINE),
        ):
            record = PubMedAdapter().fetch(ident("pmid", "28845752"))
        self.assertEqual(record["title"], "Alpha beta gamma")
        self.assertEqual(record["abstract"], "One two three")

    def test_pmid_efetch_bad_date_does_not_raise_and_drops_date(self):
        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            return_value=FakeResponse(text=EFETCH_BAD_DATE),
        ):
            record = PubMedAdapter().fetch(ident("pmid", "28845753"))
        self.assertNotIn("publication_date", record)
        self.assertNotIn("year", record)
        self.assertEqual(record["title"], "A title")

    def test_network_error_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.pubmed.requests.get",
            side_effect=__import__("requests").Timeout("slow"),
        ):
            with self.assertRaises(AdapterError):
                PubMedAdapter().fetch(ident("pmid", "28845751"))


ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <title>Spatially Resolved &lt;i&gt;Analysis&lt;/i&gt; of Lung Cancer</title>
    <summary>We present a new method for spatial analysis.</summary>
    <author><name>Shiau, Chia-Yu</name></author>
    <author><name>Plain Name</name></author>
    <published>2024-01-02T18:00:00Z</published>
    <arxiv:doi>10.1101/2024.01.01.000001</arxiv:doi>
  </entry>
</feed>
"""

ARXIV_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>ArXiv Query</title></feed>
"""


class ArxivAdapterTest(unittest.TestCase):
    def test_atom_feed_maps_to_canonical_dict(self):
        with mock.patch(
            "paper_notes.adapters.arxiv.requests.get",
            return_value=FakeResponse(text=ARXIV_ATOM),
        ) as get:
            record = ArxivAdapter().fetch(ident("arxiv", "2401.00001v2"))
        url = get.call_args.args[0]
        self.assertIn("export.arxiv.org", url)
        self.assertEqual(
            record,
            {
                "title": "Spatially Resolved Analysis of Lung Cancer",
                "authors": [
                    {"family": "Shiau", "given": "Chia-Yu"},
                    {"literal": "Plain Name"},
                ],
                "publication_date": "2024-01-02",
                "year": 2024,
                "arxiv": "2401.00001v2",
                "abstract": "We present a new method for spatial analysis.",
                "doi": "10.1101/2024.01.01.000001",
            },
        )

    def test_empty_feed_raises(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.arxiv.requests.get",
            return_value=FakeResponse(text=ARXIV_EMPTY),
        ):
            with self.assertRaises(AdapterError):
                ArxivAdapter().fetch(ident("arxiv", "2401.00002"))

    def test_network_error_raises_adapter_error(self):
        from paper_notes.adapters import AdapterError

        with mock.patch(
            "paper_notes.adapters.arxiv.requests.get",
            side_effect=__import__("requests").ConnectionError("down"),
        ):
            with self.assertRaises(AdapterError):
                ArxivAdapter().fetch(ident("arxiv", "2401.00001"))


def rec(source, **values):
    return SourceRecord(source=source, values=values)


FULL_CROSSREF = dict(
    title="Spatially resolved analysis of lung adenocarcinoma",
    authors=[{"family": "Shiau", "given": "Chia-Yu"}],
    publication_date="2024-05-01",
    year=2024,
    journal="Nature Medicine",
    doi="10.1038/s41591-024-00000-0",
)

FULL_PUBMED = dict(
    title="Spatially resolved analysis of lung adenocarcinoma",
    authors=[{"family": "Shiau", "given": "Chia-Yu"}],
    publication_date="2024-05",
    year=2024,
    journal="Nature Medicine",
    doi="10.1038/s41591-024-00000-0",
)


class MergeTest(unittest.TestCase):
    def test_single_source_high_confidence(self):
        result = merge_records([rec("crossref", **FULL_CROSSREF)])
        self.assertIsInstance(result, MetadataCandidate)
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.values["title"], FULL_CROSSREF["title"])
        self.assertEqual(result.field_provenance["title"], "crossref")
        self.assertEqual(result.field_provenance["authors"], "crossref")
        self.assertEqual(result.field_provenance["doi"], "crossref")

    def test_agreeing_sources_high_confidence_priority_provenance(self):
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **FULL_PUBMED)])
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.conflicts, [])
        # agreeing fields keep the highest-priority source's provenance
        self.assertEqual(result.field_provenance["title"], "crossref")
        self.assertEqual(result.values["journal"], "Nature Medicine")

    def test_date_precision_does_not_false_conflict(self):
        # crossref day precision vs pubmed month precision agree at month level
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **FULL_PUBMED)])
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.values["publication_date"], "2024-05-01")
        self.assertEqual(result.field_provenance["publication_date"], "crossref")

    def test_date_more_precise_wins_even_from_lower_priority(self):
        pubmed_full = dict(FULL_PUBMED)
        pubmed_full["publication_date"] = "2024-05-03"
        pubmed_full.pop("journal")
        result = merge_records([rec("crossref", publication_date="2024-05"), rec("pubmed", **pubmed_full)])
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.values["publication_date"], "2024-05-03")
        self.assertEqual(result.field_provenance["publication_date"], "pubmed")

    def test_date_equivalence_is_transitive_conflict_detected(self):
        # 2024 and 2024-05 agree; 2024 and 2024-06 agree; but 2024-05 and
        # 2024-06 disagree — the whole group must be pairwise compatible.
        result = merge_records(
            [
                rec("crossref", publication_date="2024"),
                rec("pubmed", publication_date="2024-05"),
                rec("arxiv", publication_date="2024-06"),
            ]
        )
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "publication_date")
        self.assertEqual(
            result.conflicts[0].values,
            (
                ("crossref", "2024"),
                ("pubmed", "2024-05"),
                ("arxiv", "2024-06"),
            ),
        )
        # on conflict the highest-priority source's value stays the
        # candidate, matching the first conflict pair exactly
        self.assertEqual(result.values["publication_date"], "2024")
        self.assertEqual(result.field_provenance["publication_date"], "crossref")
        self.assertEqual(
            (result.field_provenance["publication_date"], result.values["publication_date"]),
            result.conflicts[0].values[0],
        )

    def test_author_order_conflict(self):
        # author order determines first author and citation keys; reversed
        # order must not be silently merged as an unordered set
        result = merge_records(
            [
                rec("crossref", authors=[{"family": "Alpha"}, {"family": "Beta"}]),
                rec("pubmed", authors=[{"family": "Beta"}, {"family": "Alpha"}]),
            ]
        )
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "authors")

    def test_author_agreement_keeps_order_high(self):
        base = {"title": "A title", "year": 2024}
        result = merge_records(
            [
                rec("crossref", **base, authors=[{"family": "Alpha"}, {"family": "Beta"}]),
                rec("pubmed", **base, authors=[{"family": "Alpha"}, {"family": "Beta"}]),
            ]
        )
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.conflicts, [])

    def test_title_conflict_produces_needs_confirmation(self):
        pubmed = dict(FULL_PUBMED)
        pubmed["title"] = "A different title"
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **pubmed)])
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(
            result.conflicts,
            [
                FieldConflict(
                    field="title",
                    values=(
                        ("crossref", FULL_CROSSREF["title"]),
                        ("pubmed", "A different title"),
                    ),
                )
            ],
        )
        # the priority source's value remains the candidate
        self.assertEqual(result.values["title"], FULL_CROSSREF["title"])
        self.assertEqual(result.field_provenance["title"], "crossref")

    def test_year_conflict_produces_needs_confirmation(self):
        pubmed = dict(FULL_PUBMED)
        pubmed["year"] = 2023
        pubmed.pop("publication_date")
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **pubmed)])
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "year")

    def test_invalid_source_year_does_not_crash(self):
        result = merge_records(
            [rec("crossref", title="A title", year="not-a-year")]
        )
        self.assertNotIn("year", result.values)
        self.assertEqual(result.confidence, "needs_confirmation")

    def test_invalid_confirmed_year_not_applied_not_high(self):
        result = merge_records(
            [rec("crossref", title="A title")],
            confirmed={"year": "not-a-year"},
        )
        self.assertNotIn("year", result.values)
        self.assertNotEqual(result.field_provenance.get("year"), "user")
        self.assertNotEqual(result.confidence, "high")

    def test_zero_confirmed_year_not_high(self):
        result = merge_records(
            [rec("crossref", title="A title")],
            confirmed={"year": 0},
        )
        self.assertNotIn("year", result.values)
        self.assertNotEqual(result.confidence, "high")

    def test_string_confirmed_year_normalized_to_int(self):
        result = merge_records(
            [rec("crossref", title="A title", authors=[{"family": "Shiau"}])],
            confirmed={"year": "2024"},
        )
        self.assertEqual(result.values["year"], 2024)
        self.assertIsInstance(result.values["year"], int)
        self.assertEqual(result.confidence, "high")

    def test_float_confirmed_year_rejected(self):
        result = merge_records(
            [rec("crossref", title="A title")],
            confirmed={"year": 2024.5},
        )
        self.assertNotIn("year", result.values)
        self.assertNotEqual(result.confidence, "high")

    def test_float_source_year_rejected(self):
        result = merge_records(
            [rec("crossref", title="A title", year=2024.9)]
        )
        self.assertNotIn("year", result.values)
        self.assertNotEqual(result.confidence, "high")

    def test_float_ai_year_rejected(self):
        result = merge_records(
            [rec("crossref", title="A title")],
            ai={"year": 2024.5},
        )
        self.assertNotIn("year", result.values)

    def test_inf_year_never_crashes(self):
        # float("inf") must not raise OverflowError from int() conversion
        for source in ("source", "confirmed", "ai"):
            with self.subTest(entry=source):
                kwargs: dict = {"title": "A title"}
                if source == "source":
                    kwargs["year"] = float("inf")
                    result = merge_records([rec("crossref", **kwargs)])
                elif source == "confirmed":
                    result = merge_records(
                        [rec("crossref", **kwargs)], confirmed={"year": float("inf")}
                    )
                else:
                    result = merge_records(
                        [rec("crossref", **kwargs)], ai={"year": float("inf")}
                    )
                self.assertNotIn("year", result.values)
                self.assertNotEqual(result.confidence, "high")

    def test_leading_zero_string_year_rejected(self):
        result = merge_records(
            [rec("crossref", title="A title")],
            confirmed={"year": "02024"},
        )
        self.assertNotIn("year", result.values)
        self.assertNotEqual(result.confidence, "high")

    def test_year_only_date_out_of_range_rejected(self):
        for bad_date in ("0000", "0001", "0999"):
            with self.subTest(publication_date=bad_date):
                result = merge_records(
                    [rec("crossref", title="A title", publication_date=bad_date)]
                )
                self.assertNotIn("publication_date", result.values)
                self.assertNotIn("year", result.values)
                self.assertNotEqual(result.confidence, "high")

    def test_year_only_date_lower_boundary_accepted(self):
        result = merge_records(
            [rec("crossref", title="A title", authors=[{"family": "Shiau"}],
                 publication_date="1000")]
        )
        self.assertEqual(result.values["publication_date"], "1000")
        self.assertEqual(result.values["year"], 1000)
        self.assertEqual(result.confidence, "high")

    def test_author_variant_conflict(self):
        pubmed = dict(FULL_PUBMED)
        pubmed["authors"] = [{"family": "Shiau", "given": "C-Y"}]
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **pubmed)])
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "authors")

    def test_user_confirmed_value_wins(self):
        pubmed = dict(FULL_PUBMED)
        pubmed["title"] = "A different title"
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF), rec("pubmed", **pubmed)],
            confirmed={"title": "User corrected title"},
        )
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.values["title"], "User corrected title")
        self.assertEqual(result.field_provenance["title"], "user")

    def test_user_confirmed_fills_missing_field(self):
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            confirmed={"abstract": "User supplied abstract."},
        )
        self.assertEqual(result.values["abstract"], "User supplied abstract.")
        self.assertEqual(result.field_provenance["abstract"], "user")

    def test_missing_title_not_auto_created(self):
        no_title = dict(FULL_CROSSREF)
        del no_title["title"]
        result = merge_records([rec("crossref", **no_title)])
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertNotIn("title", result.values)

    def test_missing_authors_not_auto_created(self):
        no_authors = dict(FULL_CROSSREF)
        no_authors["authors"] = []
        result = merge_records([rec("crossref", **no_authors)])
        self.assertEqual(result.confidence, "needs_confirmation")

    def test_missing_year_and_date_not_auto_created(self):
        no_year = dict(FULL_CROSSREF)
        del no_year["year"]
        del no_year["publication_date"]
        result = merge_records([rec("crossref", **no_year)])
        self.assertEqual(result.confidence, "needs_confirmation")

    def test_year_derived_from_publication_date(self):
        rec_in = dict(FULL_CROSSREF)
        del rec_in["year"]
        result = merge_records([rec("crossref", **rec_in)])
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.values["year"], 2024)
        self.assertEqual(result.field_provenance["year"], "crossref")

    def test_year_date_mismatch_is_conflict(self):
        pubmed = dict(FULL_PUBMED)
        pubmed["publication_date"] = "2023-06-01"
        result = merge_records([rec("crossref", **FULL_CROSSREF), rec("pubmed", **pubmed)])
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "publication_date")

    def test_confirmed_year_mismatching_date_is_conflict(self):
        # canonical consistency must be re-verified AFTER user confirmation
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            confirmed={"year": 2023},
        )
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "publication_date")

    def test_confirmed_date_mismatching_year_is_conflict(self):
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            confirmed={"publication_date": "2023-06-01"},
        )
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "publication_date")

    def test_invalid_date_from_source_is_dropped(self):
        result = merge_records(
            [rec("crossref", **dict(FULL_CROSSREF, publication_date="2024-13-99"))]
        )
        self.assertNotIn("publication_date", result.values)

    def test_invalid_confirmed_date_is_not_applied(self):
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            confirmed={"publication_date": "2024-13-99"},
        )
        # the invalid confirmation is skipped; the sourced date is untouched
        self.assertEqual(result.values["publication_date"], "2024-05-01")
        self.assertEqual(result.field_provenance["publication_date"], "crossref")

    def test_ai_fills_gap_only_and_blocks_auto_create(self):
        crossref = dict(FULL_CROSSREF)
        result = merge_records(
            [rec("crossref", **crossref)],
            ai={"abstract": "AI suggested abstract."},
        )
        self.assertEqual(result.values["abstract"], "AI suggested abstract.")
        self.assertEqual(result.field_provenance["abstract"], "ai")
        # AI-sourced facts require user confirmation before writing
        self.assertEqual(result.confidence, "needs_confirmation")

    def test_ai_never_overrides_remote_values(self):
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            ai={"title": "AI wrong title", "abstract": "AI abstract."},
        )
        self.assertEqual(result.values["title"], FULL_CROSSREF["title"])
        self.assertEqual(result.field_provenance["title"], "crossref")
        self.assertEqual(result.values["abstract"], "AI abstract.")

    def test_user_confirmation_of_ai_value_restores_high(self):
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            ai={"abstract": "AI suggested abstract."},
            confirmed={"abstract": "AI suggested abstract."},
        )
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.field_provenance["abstract"], "user")

    def test_ai_is_optional(self):
        result = merge_records([rec("crossref", **FULL_CROSSREF)])
        self.assertEqual(result.confidence, "high")

    def test_ai_input_is_plain_dict_not_a_source(self):
        # the v0.1 core never calls an AI service: ai is pure input data
        result = merge_records(
            [rec("crossref", **FULL_CROSSREF)],
            ai={"pages": "1-10"},
        )
        self.assertEqual(result.values["pages"], "1-10")
        self.assertEqual(result.field_provenance["pages"], "ai")


class ResolveTest(unittest.TestCase):
    def test_orchestrates_adapters_by_kind(self):
        crossref = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_CROSSREF)))
        pubmed = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_PUBMED)))
        result = resolve(
            [ident("doi", "10.1038/s41591-024-00000-0"), ident("pmid", "28845751")],
            adapters={"doi": crossref, "pmid": pubmed},
        )
        crossref.fetch.assert_called_once_with(ident("doi", "10.1038/s41591-024-00000-0"))
        pubmed.fetch.assert_called_once_with(ident("pmid", "28845751"))
        self.assertEqual(result.confidence, "high")

    def test_deduplicates_identical_identifiers(self):
        crossref = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_CROSSREF)))
        result = resolve(
            [
                ident("doi", "10.1000/xyz"),
                ident("doi", "10.1000/xyz"),
                ident("doi", "10.1000/xyz"),
            ],
            adapters={"doi": crossref},
        )
        crossref.fetch.assert_called_once()

    def test_url_identifier_resolved_through_adapter(self):
        crossref = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_CROSSREF)))
        resolve(
            [ident("doi", "10.1000/xyz")],
            adapters={"doi": crossref},
        )
        crossref.fetch.assert_called_once_with(ident("doi", "10.1000/xyz"))

    def test_partial_adapter_failure_merges_remaining(self):
        from paper_notes.adapters import AdapterError

        crossref = mock.Mock(fetch=mock.Mock(side_effect=AdapterError("down")))
        pubmed = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_PUBMED)))
        result = resolve(
            [ident("doi", "10.1000/xyz"), ident("pmid", "28845751")],
            adapters={"doi": crossref, "pmid": pubmed},
        )
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.field_provenance["title"], "pubmed")

    def test_all_adapters_fail_raises_resolution_error(self):
        from paper_notes.adapters import AdapterError

        crossref = mock.Mock(fetch=mock.Mock(side_effect=AdapterError("down")))
        with self.assertRaises(ResolutionError):
            resolve(
                [ident("doi", "10.1000/xyz")],
                adapters={"doi": crossref},
            )

    def test_pmcid_routes_to_pubmed_adapter(self):
        pubmed = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_PUBMED)))
        resolve(
            [ident("pmcid", "PMC1234567")],
            adapters={"pmcid": pubmed},
        )
        pubmed.fetch.assert_called_once_with(ident("pmcid", "PMC1234567"))

    def test_ai_and_confirmed_pass_through_to_merge(self):
        crossref = mock.Mock(fetch=mock.Mock(return_value=dict(FULL_CROSSREF)))
        result = resolve(
            [ident("doi", "10.1000/xyz")],
            adapters={"doi": crossref},
            confirmed={"abstract": "User text."},
            ai={"pages": "1-10"},
        )
        self.assertEqual(result.values["abstract"], "User text.")
        self.assertEqual(result.field_provenance["abstract"], "user")
        self.assertEqual(result.field_provenance["pages"], "ai")


if __name__ == "__main__":
    unittest.main()
