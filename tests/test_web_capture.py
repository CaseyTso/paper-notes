"""Web Capture core boundary tests (Task 3).

The Browser Connector is an untrusted candidate source: these tests pin
strict validation, normalization through canonical helpers, and the
web_* source priority before AI and after authoritative adapters.
"""

import unittest

from pydantic import ValidationError

from paper_notes.metadata import SourceRecord, merge_records
from paper_notes.web_capture import (
    MAX_CAPTURE_AUTHORS,
    MAX_CAPTURE_RECORDS,
    MAX_CAPTURE_STRING_LENGTH,
    WEB_CAPTURE_SCHEMA_VERSION,
    WebCaptureRequest,
)


def valid_payload(**overrides):
    payload = {
        "schema_version": 1,
        "capture_id": "7b0c7b5d-1d2e-4a6f-9f2e-8e9c0d1a2b3c",
        "page_url": "https://example.org/articles/10.1000/abc",
        "records": [
            {
                "source": "highwire",
                "values": {
                    "item_type": "article-journal",
                    "title": "A test paper",
                    "authors": [{"family": "Doe", "given": "Jane"}],
                    "publication_date": "2024-05-01",
                    "year": 2024,
                    "doi": "10.1000/abc",
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


class WebCaptureValidationTest(unittest.TestCase):
    def test_accepts_valid_request(self):
        request = WebCaptureRequest.model_validate(valid_payload())
        self.assertEqual(request.schema_version, WEB_CAPTURE_SCHEMA_VERSION)
        self.assertEqual(len(request.records), 1)

    def test_rejects_unknown_top_level_fields(self):
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(valid_payload(command="rm -rf /"))

    def test_rejects_unknown_source(self):
        payload = valid_payload()
        payload["records"][0]["source"] = "zotero"
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_unknown_value_field(self):
        payload = valid_payload()
        payload["records"][0]["values"]["tags"] = ["x"]
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_non_https_page_url(self):
        for url in ("http://example.org", "file:///etc/passwd", "/vault/path"):
            with self.assertRaises(ValidationError):
                WebCaptureRequest.model_validate(valid_payload(page_url=url))

    def test_rejects_loopback_page_url(self):
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(
                valid_payload(page_url="https://127.0.0.1/paper")
            )
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(
                valid_payload(page_url="https://localhost/paper")
            )

    def test_rejects_page_url_with_credentials(self):
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(
                valid_payload(page_url="https://user:pass@example.org/paper")
            )

    def test_rejects_empty_capture_id(self):
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(valid_payload(capture_id=""))

    def test_rejects_oversized_string(self):
        payload = valid_payload()
        payload["records"][0]["values"]["title"] = "x" * (MAX_CAPTURE_STRING_LENGTH + 1)
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_oversized_author_count(self):
        payload = valid_payload()
        payload["records"][0]["values"]["authors"] = [
            {"family": f"F{i}"} for i in range(MAX_CAPTURE_AUTHORS + 1)
        ]
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_oversized_record_count(self):
        payload = valid_payload()
        payload["records"] = [
            {"source": "doi_scan", "values": {"doi": f"10.1000/{i}"}}
            for i in range(MAX_CAPTURE_RECORDS + 1)
        ]
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_malformed_types(self):
        payload = valid_payload()
        payload["records"][0]["values"]["year"] = "2024"
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_invalid_dates_and_years(self):
        payload = valid_payload()
        payload["records"][0]["values"]["publication_date"] = "2024-13-99"
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

        payload = valid_payload()
        payload["records"][0]["values"]["year"] = 99
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_author_without_family_or_literal(self):
        payload = valid_payload()
        payload["records"][0]["values"]["authors"] = [{"given": "Only"}]
        with self.assertRaises(ValidationError):
            WebCaptureRequest.model_validate(payload)

    def test_rejects_invalid_strong_identifier(self):
        payload = valid_payload()
        payload["records"][0]["values"]["doi"] = "not-a-doi"
        request = WebCaptureRequest.model_validate(payload)
        with self.assertRaises(ValueError):
            request.to_source_records()


class WebCaptureNormalizationTest(unittest.TestCase):
    def test_maps_sources_to_web_metadata_names(self):
        payload = valid_payload(
            records=[
                {"source": "highwire", "values": {"title": "H"}},
                {"source": "json_ld", "values": {"title": "J"}},
                {"source": "dublin_core", "values": {"title": "D"}},
                {"source": "open_graph", "values": {"title": "O"}},
                {"source": "doi_scan", "values": {"doi": "10.1000/scan"}},
            ]
        )
        request = WebCaptureRequest.model_validate(payload)
        records = request.to_source_records()
        self.assertEqual(
            [record.source for record in records],
            [
                "web_highwire",
                "web_json_ld",
                "web_dublin_core",
                "web_open_graph",
                "web_doi_scan",
            ],
        )
        for record in records:
            self.assertIsInstance(record, SourceRecord)

    def test_normalizes_dates_year_issn_and_strong_ids(self):
        payload = valid_payload(
            records=[
                {
                    "source": "highwire",
                    "values": {
                        "title": "Normalize me",
                        "publication_date": "2024-05-01",
                        "year": 2024,
                        "doi": "HTTPS://DOI.ORG/10.1000/ABC",
                        "pmcid": "pmc1234567",
                        "arxiv": "arXiv:2401.00001v2",
                        "issn": ["1234-5678", " 8765-4321 "],
                        "authors": [
                            {"family": "Doe", "given": "Jane"},
                            {"literal": "The Test Consortium"},
                        ],
                    },
                }
            ]
        )
        request = WebCaptureRequest.model_validate(payload)
        record = request.to_source_records()[0]
        self.assertEqual(record.values["doi"], "10.1000/abc")
        self.assertEqual(record.values["pmcid"], "PMC1234567")
        self.assertEqual(record.values["arxiv"], "2401.00001v2")
        self.assertEqual(record.values["issn"], ["1234-5678", "8765-4321"])
        self.assertEqual(record.values["year"], 2024)
        self.assertEqual(
            record.values["authors"],
            [
                {"family": "Doe", "given": "Jane"},
                {"literal": "The Test Consortium"},
            ],
        )

    def test_year_derived_from_date_when_absent(self):
        payload = valid_payload()
        payload["records"][0]["values"].pop("year")
        request = WebCaptureRequest.model_validate(payload)
        record = request.to_source_records()[0]
        self.assertEqual(record.values["year"], 2024)

    def test_mismatched_year_rejected(self):
        payload = valid_payload()
        payload["records"][0]["values"]["year"] = 2023
        request = WebCaptureRequest.model_validate(payload)
        with self.assertRaises(ValueError):
            request.to_source_records()

    def test_strong_ids_deduplicated(self):
        payload = valid_payload(
            records=[
                {"source": "highwire", "values": {"doi": "10.1000/abc"}},
                {"source": "json_ld", "values": {"doi": "https://doi.org/10.1000/ABC"}},
            ]
        )
        request = WebCaptureRequest.model_validate(payload)
        ids = request.strong_ids()
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids[0].kind, "doi")
        self.assertEqual(ids[0].value, "10.1000/abc")


class WebCaptureMergePriorityTest(unittest.TestCase):
    def test_authoritative_source_wins_over_web(self):
        result = merge_records(
            [
                SourceRecord(
                    source="web_highwire",
                    values={
                        "title": "Web title",
                        "authors": [{"family": "Web"}],
                        "year": 2024,
                    },
                ),
                SourceRecord(
                    source="crossref",
                    values={
                        "title": "Official title",
                        "authors": [{"family": "Official"}],
                        "year": 2024,
                    },
                ),
            ]
        )
        self.assertEqual(result.values["title"], "Official title")
        self.assertEqual(result.field_provenance["title"], "crossref")
        self.assertEqual(result.confidence, "needs_confirmation")
        self.assertEqual(result.conflicts[0].field, "title")

    def test_web_source_wins_over_ai(self):
        result = merge_records(
            [
                SourceRecord(
                    source="ai",
                    values={"title": "AI title"},
                ),
                SourceRecord(
                    source="web_doi_scan",
                    values={"doi": "10.1000/scan", "title": "Web title"},
                ),
            ]
        )
        self.assertEqual(result.values["title"], "Web title")
        self.assertEqual(result.field_provenance["title"], "web_doi_scan")
        # AI fills only gaps; here web provided title, so no AI field.
        self.assertNotIn("title", {k: v for k, v in result.field_provenance.items() if v == "ai"})

    def test_web_only_complete_record_still_needs_confirmation(self):
        result = merge_records(
            [
                SourceRecord(
                    source="web_highwire",
                    values={
                        "title": "A complete web record",
                        "authors": [{"family": "Doe"}],
                        "year": 2024,
                    },
                )
            ]
        )
        # Complete critical fields but no authoritative/user source: the
        # merge layer marks it needs_confirmation because web evidence is
        # never trusted for auto-create.
        self.assertEqual(result.confidence, "needs_confirmation")

    def test_web_conflict_order_keeps_authoritative_first(self):
        result = merge_records(
            [
                SourceRecord(
                    source="web_highwire",
                    values={"title": "Web"},
                ),
                SourceRecord(
                    source="crossref",
                    values={"title": "Official"},
                ),
            ]
        )
        self.assertEqual(result.conflicts[0].values[0], ("crossref", "Official"))
        self.assertEqual(result.conflicts[0].values[1], ("web_highwire", "Web"))


if __name__ == "__main__":
    unittest.main()
