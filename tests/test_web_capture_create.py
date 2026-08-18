"""Web Capture create/review/confirm tests (Task 4).

These tests exercise `item create --web-capture` through the pure item
operation with mocked official adapters. They pin Verified Capture
auto-create, review plans, opaque token staleness, idempotent retry,
cross-owner safety, and zero-write failures.
"""

import tempfile
import unittest
from pathlib import Path

from paper_notes import items
from paper_notes.adapters import AdapterError
from paper_notes.frontmatter import load_paper_note
from paper_notes.web_capture import WebCaptureRecord, WebCaptureRequest


def make_capture(**overrides):
    payload = {
        "schema_version": 1,
        "capture_id": "7b0c7b5d-1d2e-4a6f-9f2e-8e9c0d1a2b3c",
        "page_url": "https://example.org/articles/10.1000/abc",
        "records": [
            {
                "source": "highwire",
                "values": {
                    "item_type": "article-journal",
                    "title": "A web capture paper",
                    "authors": [{"family": "Doe", "given": "Jane"}],
                    "publication_date": "2024-05-01",
                    "year": 2024,
                    "doi": "10.1000/abc",
                },
            }
        ],
    }
    payload.update(overrides)
    return WebCaptureRequest.model_validate(payload)


class FakeAdapter:
    def __init__(self, values):
        self._values = dict(values)

    def fetch(self, identifier):
        return dict(self._values)


class FailingAdapter:
    def fetch(self, identifier):
        raise AdapterError("down")


OFFICIAL = {
    "title": "A web capture paper",
    "authors": [{"family": "Doe", "given": "Jane"}],
    "publication_date": "2024-05-01",
    "year": 2024,
    "doi": "10.1000/abc",
}


def write_existing_item(root, key, **kwargs):
    d = root / "05 Literature" / key
    d.mkdir(parents=True, exist_ok=True)
    values = {
        "paper_id": "550e8400-e29b-41d4-a716-446655440000",
        "title": kwargs.get("title", "Existing paper"),
        "authors": kwargs.get("authors", [{"family": "Smith", "given": "John"}]),
        "year": kwargs.get("year", 2024),
        "publication_date": kwargs.get("publication_date", "2024-01-01"),
        "doi": kwargs.get("doi"),
        "journal": kwargs.get("journal"),
        "abstract": kwargs.get("abstract"),
    }
    lines = [
        "schema_version: 1",
        f"paper_id: {values['paper_id']}",
        f"citation_key: {key}",
        "item_type: article-journal",
        f"title: {values['title']}",
        "authors:",
    ]
    for author in values["authors"]:
        if "literal" in author:
            lines.append(f"- literal: {author['literal']}")
        else:
            lines.append(f"- family: {author['family']}")
            if author.get("given"):
                lines.append(f"  given: {author['given']}")
    lines.append(f"publication_date: {values['publication_date']}")
    lines.append(f"year: {values['year']}")
    if values["doi"]:
        lines.append(f"doi: {values['doi']}")
    if values["journal"]:
        lines.append(f"journal: {values['journal']}")
    if values["abstract"]:
        lines.append(f"abstract: {values['abstract']}")
    lines.append("pdf_status: missing")
    lines.append("reading_status: unread")
    (d / f"{key}.md").write_text(f"---\n{chr(10).join(lines)}\n---\n# body\n", encoding="utf-8")


def count_paper_dirs(root):
    lit = root / "05 Literature"
    if not lit.is_dir():
        return 0
    return sum(1 for p in lit.iterdir() if p.is_dir())


class WebCaptureCreateTest(unittest.TestCase):
    def test_verified_capture_creates_automatically(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            result = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(result.action, "created")
            self.assertEqual(count_paper_dirs(vault), 1)
            note = vault / "05 Literature" / result.citation_key / f"{result.citation_key}.md"
            paper, _ = load_paper_note(note)
            self.assertEqual(paper.title, "A web capture paper")
            self.assertEqual(paper.doi, "10.1000/abc")

    def test_verified_capture_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            first = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(first.status, "created")
            second = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(second.status, "attached")
            self.assertEqual(second.action, "duplicate_exists")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_api_failure_requires_review(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            result = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FailingAdapter()},
            )
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.plan["action"], "create_with_confirmation")
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_official_web_conflict_requires_review(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            conflicting_official = dict(OFFICIAL)
            conflicting_official["title"] = "Official title"
            result = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FakeAdapter(conflicting_official)},
            )
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.plan["action"], "create_with_confirmation")
            self.assertTrue(result.plan["conflicts"])
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_exact_duplicate_noop(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_existing_item(
                vault,
                "doeWebCapturePaper2024",
                title="A web capture paper",
                authors=[{"family": "Doe", "given": "Jane"}],
                year=2024,
                publication_date="2024-05-01",
                doi="10.1000/abc",
            )
            result = items.create_item_from_web_capture(
                vault,
                capture=make_capture(),
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(result.status, "attached")
            self.assertEqual(result.action, "duplicate_exists")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_exact_duplicate_blank_field_proposes_update(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_existing_item(
                vault,
                "doeWebCapturePaper2024",
                title="A web capture paper",
                authors=[{"family": "Doe", "given": "Jane"}],
                year=2024,
                publication_date="2024-05-01",
                doi="10.1000/abc",
            )
            capture = make_capture()
            capture.records[0].values.abstract = "New abstract"
            result = items.create_item_from_web_capture(
                vault,
                capture=capture,
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.plan["action"], "update_existing")
            self.assertEqual(result.plan["proposed_values"]["abstract"], "New abstract")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_fuzzy_duplicate_returns_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_existing_item(
                vault,
                "smithExisting2024",
                title="A web capture paper",
                authors=[{"family": "Doe", "given": "Jane"}],
                year=2024,
            )
            capture = make_capture()
            capture.records[0].values.doi = None
            result = items.create_item_from_web_capture(vault, capture=capture)
            self.assertEqual(result.status, "needs_confirmation")
            self.assertEqual(result.plan["action"], "confirm_candidates")
            self.assertTrue(result.candidates)
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_id_less_approval_creates_with_token_and_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            capture = make_capture()
            capture.records[0].values.doi = None
            plan = items.create_item_from_web_capture(vault, capture=capture)
            self.assertEqual(plan.status, "needs_confirmation")
            self.assertEqual(plan.plan["action"], "create_with_confirmation")
            confirmed = {
                "title": "A web capture paper",
                "authors": [{"family": "Doe", "given": "Jane"}],
                "year": 2024,
            }
            result = items.create_item_from_web_capture(
                vault,
                capture=capture,
                confirmed=confirmed,
                confirm_token=plan.confirmation_token,
            )
            self.assertEqual(result.status, "created")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_confirmed_without_token_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            capture = make_capture()
            capture.records[0].values.doi = None
            with self.assertRaises(items.ItemError):
                items.create_item_from_web_capture(
                    vault,
                    capture=capture,
                    confirmed={"title": "x", "authors": [{"family": "A"}], "year": 2024},
                )

    def test_stale_token_conflict_zero_write(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            capture = make_capture()
            capture.records[0].values.doi = None
            plan = items.create_item_from_web_capture(vault, capture=capture)
            confirmed = {
                "title": "A web capture paper",
                "authors": [{"family": "Doe", "given": "Jane"}],
                "year": 2024,
            }
            with self.assertRaises(items.ItemConflict):
                items.create_item_from_web_capture(
                    vault,
                    capture=capture,
                    confirmed=confirmed,
                    confirm_token="wrong-token",
                )
            self.assertEqual(count_paper_dirs(vault), 0)

    def test_replayed_token_after_create_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            capture = make_capture()
            capture.records[0].values.doi = None
            plan = items.create_item_from_web_capture(vault, capture=capture)
            confirmed = {
                "title": "A web capture paper",
                "authors": [{"family": "Doe", "given": "Jane"}],
                "year": 2024,
            }
            first = items.create_item_from_web_capture(
                vault,
                capture=capture,
                confirmed=confirmed,
                confirm_token=plan.confirmation_token,
            )
            self.assertEqual(first.status, "created")
            # Replaying the same create token now hits the fuzzy-duplicate
            # guard: no second item is written, no token bypass is allowed.
            replay = items.create_item_from_web_capture(
                vault,
                capture=capture,
                confirmed=confirmed,
                confirm_token=plan.confirmation_token,
            )
            self.assertEqual(replay.status, "needs_confirmation")
            self.assertEqual(replay.plan["action"], "confirm_candidates")
            self.assertEqual(count_paper_dirs(vault), 1)

    def test_cross_owner_identifiers_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_existing_item(
                vault,
                "itemOne2024",
                title="One",
                doi="10.1000/one",
            )
            write_existing_item(
                vault,
                "itemTwo2024",
                title="Two",
                doi="10.1000/two",
            )
            capture = make_capture()
            capture.records[0].values.doi = "10.1000/one"
            capture.records.append(
                WebCaptureRecord.model_validate(
                    {
                        "source": "doi_scan",
                        "values": {"doi": "10.1000/two"},
                    }
                )
            )
            with self.assertRaises(items.ItemConflict):
                items.create_item_from_web_capture(vault, capture=capture)
            self.assertEqual(count_paper_dirs(vault), 2)

    def test_concurrent_change_makes_update_token_stale(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            write_existing_item(
                vault,
                "doeWebCapturePaper2024",
                title="A web capture paper",
                authors=[{"family": "Doe", "given": "Jane"}],
                year=2024,
                publication_date="2024-05-01",
                doi="10.1000/abc",
            )
            capture = make_capture()
            capture.records[0].values.abstract = "New abstract"
            plan = items.create_item_from_web_capture(
                vault,
                capture=capture,
                adapters={"doi": FakeAdapter(OFFICIAL)},
            )
            self.assertEqual(plan.status, "needs_confirmation")
            # Mutate the note after the plan was issued.
            note = vault / "05 Literature" / "doeWebCapturePaper2024" / "doeWebCapturePaper2024.md"
            text = note.read_text(encoding="utf-8")
            note.write_text(text + "\nchanged\n", encoding="utf-8")
            confirmed = {"abstract": "New abstract"}
            with self.assertRaises(items.ItemConflict):
                items.create_item_from_web_capture(
                    vault,
                    capture=capture,
                    confirmed=confirmed,
                    confirm_token=plan.confirmation_token,
                    adapters={"doi": FakeAdapter(OFFICIAL)},
                )


if __name__ == "__main__":
    unittest.main()
