"""Identifier parsing tests (Task 9).

Frozen after the first red run; do not weaken or delete assertions.
Parsing is pure and deterministic — no network, no filesystem.

``original`` in :class:`ParsedIdentifier` is the exact input string that
produced the identifier; the helper below defaults it to the normalized
value for bare-value inputs and tests pass the real input where it
differs (prefixes, URLs, case changes).
"""

import unittest

from paper_notes.identifiers import (
    ParsedIdentifier,
    extract_identifiers,
    parse_arxiv,
    parse_doi,
    parse_identifier,
    parse_pmcid,
    parse_pmid,
)


def di(kind, value, original=None):
    return ParsedIdentifier(
        kind=kind, value=value, original=value if original is None else original
    )


class DoiTest(unittest.TestCase):
    def test_bare_doi(self):
        self.assertEqual(parse_doi("10.1000/xyz123"), di("doi", "10.1000/xyz123"))

    def test_doi_with_doi_prefix(self):
        self.assertEqual(
            parse_doi("doi:10.1000/xyz123"),
            di("doi", "10.1000/xyz123", "doi:10.1000/xyz123"),
        )

    def test_doi_prefix_with_space(self):
        self.assertEqual(
            parse_doi("DOI: 10.1000/xyz123"),
            di("doi", "10.1000/xyz123", "DOI: 10.1000/xyz123"),
        )

    def test_doi_org_url(self):
        self.assertEqual(
            parse_doi("https://doi.org/10.1000/xyz123"),
            di("doi", "10.1000/xyz123", "https://doi.org/10.1000/xyz123"),
        )

    def test_dx_doi_url(self):
        self.assertEqual(
            parse_doi("http://dx.doi.org/10.1000/xyz123"),
            di("doi", "10.1000/xyz123", "http://dx.doi.org/10.1000/xyz123"),
        )

    def test_doi_url_query_stripped(self):
        self.assertEqual(
            parse_doi("https://doi.org/10.1000/xyz123?redirect=1"),
            di("doi", "10.1000/xyz123", "https://doi.org/10.1000/xyz123?redirect=1"),
        )

    def test_doi_url_percent_encoded_decoded(self):
        self.assertEqual(
            parse_doi("https://doi.org/10.1000/xyz%28abc%29"),
            di("doi", "10.1000/xyz(abc)", "https://doi.org/10.1000/xyz%28abc%29"),
        )

    def test_doi_trailing_sentence_punctuation_stripped(self):
        self.assertEqual(
            parse_doi("10.1000/xyz123."),
            di("doi", "10.1000/xyz123", "10.1000/xyz123."),
        )
        self.assertEqual(
            parse_doi("10.1000/xyz123),"),
            di("doi", "10.1000/xyz123", "10.1000/xyz123),"),
        )

    def test_doi_case_normalized_lowercase(self):
        self.assertEqual(
            parse_doi("10.1000/XYZ123"),
            di("doi", "10.1000/xyz123", "10.1000/XYZ123"),
        )

    def test_doi_with_suffix_parts_kept(self):
        # hyphens, parens, dots inside a DOI are significant and kept
        self.assertEqual(
            parse_doi("10.1038/s41586-024-00000-0"),
            di("doi", "10.1038/s41586-024-00000-0"),
        )

    def test_doi_invalid(self):
        self.assertIsNone(parse_doi(""))
        self.assertIsNone(parse_doi("not a doi"))
        self.assertIsNone(parse_doi("https://example.com/page"))
        self.assertIsNone(parse_doi("10.123/abc"))  # prefix too short
        self.assertIsNone(parse_doi("11.1234/abc"))  # wrong first segment


class PmidTest(unittest.TestCase):
    def test_bare_digits(self):
        self.assertEqual(parse_pmid("28845751"), di("pmid", "28845751"))

    def test_short_bare_digits_rejected(self):
        # bare numbers below six digits are ambiguous, not PMIDs
        self.assertIsNone(parse_pmid("12345"))

    def test_pmid_prefix(self):
        self.assertEqual(
            parse_pmid("PMID: 28845751"),
            di("pmid", "28845751", "PMID: 28845751"),
        )

    def test_pmid_url(self):
        self.assertEqual(
            parse_pmid("https://pubmed.ncbi.nlm.nih.gov/28845751/"),
            di("pmid", "28845751", "https://pubmed.ncbi.nlm.nih.gov/28845751/"),
        )

    def test_pmid_url_with_query(self):
        self.assertEqual(
            parse_pmid("https://pubmed.ncbi.nlm.nih.gov/28845751/?from_term=x"),
            di(
                "pmid",
                "28845751",
                "https://pubmed.ncbi.nlm.nih.gov/28845751/?from_term=x",
            ),
        )

    def test_pmid_legacy_url(self):
        self.assertEqual(
            parse_pmid("https://www.ncbi.nlm.nih.gov/pubmed/28845751"),
            di("pmid", "28845751", "https://www.ncbi.nlm.nih.gov/pubmed/28845751"),
        )

    def test_pmid_invalid(self):
        self.assertIsNone(parse_pmid("abc"))
        self.assertIsNone(parse_pmid(""))
        self.assertIsNone(parse_pmid("12 345 678"))


class PmcidTest(unittest.TestCase):
    def test_bare_pmcid(self):
        self.assertEqual(parse_pmcid("PMC1234567"), di("pmcid", "PMC1234567"))

    def test_pmcid_lowercase_prefix_normalized(self):
        self.assertEqual(
            parse_pmcid("pmc1234567"),
            di("pmcid", "PMC1234567", "pmc1234567"),
        )

    def test_pmcid_colon_prefix(self):
        self.assertEqual(
            parse_pmcid("PMCID: PMC1234567"),
            di("pmcid", "PMC1234567", "PMCID: PMC1234567"),
        )

    def test_pmcid_versioned(self):
        self.assertEqual(parse_pmcid("PMC1234567.1"), di("pmcid", "PMC1234567.1"))

    def test_pmcid_new_url(self):
        self.assertEqual(
            parse_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"),
            di("pmcid", "PMC1234567", "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"),
        )

    def test_pmcid_legacy_url(self):
        self.assertEqual(
            parse_pmcid("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/"),
            di(
                "pmcid",
                "PMC1234567",
                "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/",
            ),
        )

    def test_pmcid_invalid(self):
        self.assertIsNone(parse_pmcid("1234567"))  # digits without PMC
        self.assertIsNone(parse_pmcid("PMCabc"))
        self.assertIsNone(parse_pmcid(""))


class ArxivTest(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(parse_arxiv("1234.5678"), di("arxiv", "1234.5678"))

    def test_versioned_id(self):
        self.assertEqual(parse_arxiv("1234.5678v2"), di("arxiv", "1234.5678v2"))

    def test_arxiv_prefix(self):
        self.assertEqual(
            parse_arxiv("arXiv:1234.5678"),
            di("arxiv", "1234.5678", "arXiv:1234.5678"),
        )

    def test_abs_url(self):
        self.assertEqual(
            parse_arxiv("https://arxiv.org/abs/2401.00001v2"),
            di("arxiv", "2401.00001v2", "https://arxiv.org/abs/2401.00001v2"),
        )

    def test_pdf_url(self):
        self.assertEqual(
            parse_arxiv("https://arxiv.org/pdf/2401.00001v2"),
            di("arxiv", "2401.00001v2", "https://arxiv.org/pdf/2401.00001v2"),
        )

    def test_bare_domain_pdf_url(self):
        # scheme-less URLs like "arxiv.org/pdf/..." are common in prose
        self.assertEqual(
            parse_arxiv("arxiv.org/pdf/2401.00001"),
            di("arxiv", "2401.00001", "arxiv.org/pdf/2401.00001"),
        )

    def test_legacy_pdf_url(self):
        # legacy ids span two path segments after /pdf/
        self.assertEqual(
            parse_arxiv("arxiv.org/pdf/hep-th/9901001"),
            di("arxiv", "hep-th/9901001", "arxiv.org/pdf/hep-th/9901001"),
        )

    def test_pdf_suffix_stripped(self):
        self.assertEqual(
            parse_arxiv("https://arxiv.org/pdf/2401.00001.pdf"),
            di("arxiv", "2401.00001", "https://arxiv.org/pdf/2401.00001.pdf"),
        )

    def test_bracketed_legacy_id(self):
        self.assertEqual(
            parse_arxiv("[hep-th/9901001]"),
            di("arxiv", "hep-th/9901001", "[hep-th/9901001]"),
        )

    def test_legacy_id(self):
        self.assertEqual(parse_arxiv("hep-th/9901001"), di("arxiv", "hep-th/9901001"))

    def test_invalid(self):
        self.assertIsNone(parse_arxiv("1234.567"))  # malformed modern id
        self.assertIsNone(parse_arxiv("12.1234"))  # wrong prefix width
        self.assertIsNone(parse_arxiv("arxiv.org"))
        self.assertIsNone(parse_arxiv(""))


class ParseIdentifierTest(unittest.TestCase):
    def test_doi_url_dispatch(self):
        self.assertEqual(
            parse_identifier("https://doi.org/10.1000/xyz"),
            di("doi", "10.1000/xyz", "https://doi.org/10.1000/xyz"),
        )

    def test_pmid_url_dispatch(self):
        self.assertEqual(
            parse_identifier("https://pubmed.ncbi.nlm.nih.gov/28845751/"),
            di("pmid", "28845751", "https://pubmed.ncbi.nlm.nih.gov/28845751/"),
        )

    def test_pmcid_url_dispatch(self):
        self.assertEqual(
            parse_identifier("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"),
            di(
                "pmcid",
                "PMC1234567",
                "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
            ),
        )

    def test_arxiv_url_dispatch(self):
        self.assertEqual(
            parse_identifier("https://arxiv.org/abs/2401.00001"),
            di("arxiv", "2401.00001", "https://arxiv.org/abs/2401.00001"),
        )

    def test_bare_doi_dispatch(self):
        self.assertEqual(parse_identifier("10.1000/xyz"), di("doi", "10.1000/xyz"))

    def test_bare_digits_dispatch(self):
        self.assertEqual(parse_identifier("28845751"), di("pmid", "28845751"))

    def test_garbage_dispatch(self):
        self.assertIsNone(parse_identifier(""))
        self.assertIsNone(parse_identifier("hello world"))
        self.assertIsNone(parse_identifier("https://example.com/page"))


class ExtractIdentifiersTest(unittest.TestCase):
    def test_publisher_url_with_embedded_doi(self):
        self.assertEqual(
            extract_identifiers("https://onlinelibrary.wiley.com/doi/10.1002/anie.202400001"),
            [di("doi", "10.1002/anie.202400001", "10.1002/anie.202400001")],
        )

    def test_free_text_with_doi(self):
        self.assertEqual(
            extract_identifiers("see doi:10.1000/xyz123 for details"),
            [di("doi", "10.1000/xyz123", "doi:10.1000/xyz123")],
        )

    def test_multiple_identifiers_deduped(self):
        self.assertEqual(
            extract_identifiers("10.1000/xyz https://doi.org/10.1000/xyz"),
            [di("doi", "10.1000/xyz", "10.1000/xyz")],
        )

    def test_pmid_and_doi_together(self):
        self.assertEqual(
            extract_identifiers("PMID:28845751 and doi:10.1000/xyz"),
            [
                di("pmid", "28845751", "PMID:28845751"),
                di("doi", "10.1000/xyz", "doi:10.1000/xyz"),
            ],
        )

    def test_free_text_legacy_arxiv_url(self):
        # realistic prose: a bracketed legacy id plus a scheme-less URL;
        # both forms must parse independently (no dedup masking)
        self.assertEqual(
            extract_identifiers(
                "arXiv e-print repository [hep-th/9901001] "
                "String Junctions and Their Duals in Heterotic String Theory "
                "arxiv.org/pdf/hep-th/9901001"
            ),
            [di("arxiv", "hep-th/9901001", "[hep-th/9901001]")],
        )

    def test_legacy_pdf_url_alone(self):
        # URL-only input: no bracketed bare id to mask a URL parse failure
        self.assertEqual(
            extract_identifiers("arxiv.org/pdf/hep-th/9901001"),
            [di("arxiv", "hep-th/9901001", "arxiv.org/pdf/hep-th/9901001")],
        )

    def test_unknown_url_yields_nothing(self):
        self.assertEqual(extract_identifiers("https://example.com/page"), [])

    def test_empty_input(self):
        self.assertEqual(extract_identifiers(""), [])


if __name__ == "__main__":
    unittest.main()
