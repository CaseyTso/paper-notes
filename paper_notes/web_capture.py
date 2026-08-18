"""Web Capture validation and conversion (Browser Connector V1).

The browser extension is an untrusted candidate source. This module owns
the core-side boundary:

- accepts only schema v1 and the allowlisted field/source vocabulary;
- enforces size caps, strict types, HTTPS page URLs, and rejects
  unknown fields / path / command-shaped input before it can reach the
  metadata merge layer;
- normalizes dates, ordered authors, ISSNs, and strong identifiers
  through the same canonical helpers used by confirmed/remote metadata;
- maps web sources onto ``web_*`` :class:`SourceRecord` names so the
  merge layer can keep authoritative adapters above web evidence.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identifiers import (
    ParsedIdentifier,
    parse_arxiv,
    parse_doi,
    parse_pmcid,
    parse_pmid,
)
from .metadata import SourceRecord, _normalize_year, _valid_publication_date

WEB_CAPTURE_SCHEMA_VERSION = 1
MAX_CAPTURE_STRING_LENGTH = 20000
MAX_CAPTURE_AUTHORS = 200
MAX_CAPTURE_RECORDS = 16
MAX_CAPTURE_ISSNS = 20

WEB_CAPTURE_SOURCES = ("highwire", "json_ld", "dublin_core", "open_graph", "doi_scan")

# Browser source name -> metadata SourceRecord source name. The metadata
# layer's SOURCE_PRIORITY places all web_* sources after crossref/pubmed/
# arxiv and before ai.
WEB_SOURCE_TO_METADATA = {
    "highwire": "web_highwire",
    "json_ld": "web_json_ld",
    "dublin_core": "web_dublin_core",
    "open_graph": "web_open_graph",
    "doi_scan": "web_doi_scan",
}

_STRONG_PARSERS = {
    "doi": parse_doi,
    "pmid": parse_pmid,
    "pmcid": parse_pmcid,
    "arxiv": parse_arxiv,
}

_HTTPS_RE = re.compile(r"^https://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CAPTURE_STRING_LENGTH:
        raise ValueError("string exceeds Web Capture size cap")
    return value


class CapturedAuthor(BaseModel):
    """One structured web author; order is significant."""

    model_config = ConfigDict(extra="forbid")

    family: str | None = None
    given: str | None = None
    literal: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _cap(cls, value: Any) -> Any:
        return _bounded(value)

    @model_validator(mode="after")
    def _require_family_or_literal(self) -> "CapturedAuthor":
        if self.family is None and self.literal is None:
            raise ValueError("author must have family or literal")
        return self


class CapturedBibliography(BaseModel):
    """Allowlisted candidate bibliographic fields from one web source."""

    model_config = ConfigDict(extra="forbid", strict=True)

    item_type: Literal["article-journal", "preprint"] | None = None
    title: str | None = None
    authors: list[CapturedAuthor] | None = None
    journal: str | None = None
    journal_abbreviation: str | None = None
    publication_date: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv: str | None = None
    url: str | None = None
    issn: list[str] | None = None
    language: str | None = None
    abstract: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _cap(cls, value: Any) -> Any:
        return _bounded(value)

    @field_validator("authors")
    @classmethod
    def _cap_authors(cls, value: list[CapturedAuthor] | None) -> list[CapturedAuthor] | None:
        if value is not None and not (1 <= len(value) <= MAX_CAPTURE_AUTHORS):
            raise ValueError("author count out of range")
        return value

    @field_validator("issn")
    @classmethod
    def _cap_issn(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not (1 <= len(value) <= MAX_CAPTURE_ISSNS):
            raise ValueError("ISSN count out of range")
        return value

    @field_validator("publication_date")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _valid_publication_date(value):
            raise ValueError("invalid publication_date")
        return value

    @field_validator("year")
    @classmethod
    def _validate_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        normalized = _normalize_year(value)
        if normalized is None:
            raise ValueError("invalid year")
        return normalized

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HTTPS_RE.match(value):
            raise ValueError("url must be an HTTPS URL")
        return value


class WebCaptureRecord(BaseModel):
    """One preserved source record; conflicts are not flattened away."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["highwire", "json_ld", "dublin_core", "open_graph", "doi_scan"]
    values: CapturedBibliography

    def to_source_record(self) -> SourceRecord:
        data = self.values.model_dump(exclude_none=True)

        # Canonicalize strong identifiers; an unparseable strong field is
        # rejected instead of being stored as an extra/unknown string.
        for field, parser in _STRONG_PARSERS.items():
            if field not in data:
                continue
            parsed = parser(str(data[field]))
            if parsed is None:
                raise ValueError(f"invalid {field} value in web capture")
            data[field] = parsed.value

        # Canonicalize dates and keep year consistent.
        if "publication_date" in data:
            date_text = str(data["publication_date"])
            date_year = int(date_text[:4])
            if "year" in data and int(data["year"]) != date_year:
                raise ValueError("year must match publication_date year")
            data["year"] = date_year

        if "issn" in data:
            seen: set[str] = set()
            normalized_issns: list[str] = []
            for issn in data["issn"]:
                text = str(issn).strip().upper()
                if text and text not in seen:
                    seen.add(text)
                    normalized_issns.append(text)
            data["issn"] = normalized_issns

        return SourceRecord(
            source=WEB_SOURCE_TO_METADATA[self.source],
            values=data,
        )


class WebCaptureRequest(BaseModel):
    """Validated request envelope from the Browser Connector."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = WEB_CAPTURE_SCHEMA_VERSION
    capture_id: str = Field(min_length=1, max_length=128)
    page_url: str
    records: list[WebCaptureRecord] = Field(
        min_length=1, max_length=MAX_CAPTURE_RECORDS
    )

    @field_validator("page_url")
    @classmethod
    def _validate_page_url(cls, value: str) -> str:
        if not _HTTPS_RE.match(value):
            raise ValueError("page_url must be an HTTPS URL")
        parts = urllib.parse.urlsplit(value)
        if not parts.hostname or parts.hostname == "127.0.0.1" or parts.hostname == "localhost":
            raise ValueError("page_url must be a remote HTTPS page")
        if parts.username is not None or parts.password is not None:
            raise ValueError("page_url must not contain credentials")
        return value

    def to_source_records(self) -> list[SourceRecord]:
        return [record.to_source_record() for record in self.records]

    def strong_ids(self) -> list[ParsedIdentifier]:
        """Canonical strong identifiers from all records, deduplicated."""
        seen: set[tuple[str, str]] = set()
        out: list[ParsedIdentifier] = []
        for record in self.records:
            data = record.values.model_dump(exclude_none=True)
            for field, parser in _STRONG_PARSERS.items():
                if field not in data:
                    continue
                parsed = parser(str(data[field]))
                if parsed is None:
                    raise ValueError(f"invalid {field} value in web capture")
                key = (parsed.kind, parsed.value)
                if key not in seen:
                    seen.add(key)
                    out.append(parsed)
        return out
