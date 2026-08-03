"""Canonical v1 paper schema (Pydantic models).

Validation is pure — no filesystem access. The model is the frontmatter
contract for ``<citation_key>.md`` main notes:

- ``paper_id`` is a stable UUID, never derived from mutable metadata.
- ``citation_key`` is the vault-wide identity; aliases are reserved and
  unique, and the current key may not also be an alias.
- ``item_type`` is ``article-journal`` (primary) or ``preprint``
  (compatible fallback).
- ``publication_date`` accepts ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``;
  ``year`` is validated as a denormalized field equal to the date year.
- Volatile metric fields (``if``/``jci``/``jcr``/``cas``/``easyscholar``)
  are rejected at the top level, case-insensitively.
- Unknown non-reserved user fields are preserved (``extra="allow"``) for
  round-trip compatibility.
"""

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

# Better-BibTeX-style semantic keys: letter start, then letters/digits
# plus BBT-compatible separators; never a path-traversal sequence.
CITATION_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_.-]*$")
PUBLICATION_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

# Volatile metric fields forbidden at the top level.
#
# Matching is case-insensitive and tolerant of common naming forms
# (snake_case, kebab-case, spaces, camelCase, no separator):
# - short roots ("if", "jci", "jcr", "cas") match as whole words only, so
#   unrelated fields like "castle" or "case_study" stay valid;
# - long roots ("easyscholar", "impactfactor", "caspartition") match as
#   normalized substrings (they are distinctive enough not to collide);
# - the paired words {"impact", "factor"} reject impact-factor fields
#   while leaving single-word fields like "growth_factor" untouched;
# - EasyScholar's own raw field names are rejected as exact normalized
#   field names (case-insensitive; separator variants like "SCI_UP" or
#   "sci-base" also normalize to a forbidden name).
FORBIDDEN_METRIC_WORDS = frozenset({"if", "jci", "jcr", "cas"})
FORBIDDEN_METRIC_SUBSTRINGS = ("easyscholar", "impactfactor", "caspartition")
FORBIDDEN_METRIC_PAIRS = (frozenset({"impact", "factor"}),)
FORBIDDEN_METRIC_FIELD_NAMES = frozenset(
    {"sciif", "sciif5", "sci", "scibase", "sciup", "sciupsmall", "sciuptop"}
)

_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")


def _key_words(key: str) -> frozenset[str]:
    """Split a field name into lowercase words (separators + camel humps)."""
    words: set[str] = set()
    for part in _SEPARATOR_RE.split(key):
        words.update(word.lower() for word in _WORD_RE.findall(part))
    return frozenset(words)


def _is_forbidden_metric_key(key: str) -> bool:
    words = _key_words(key)
    if words & FORBIDDEN_METRIC_WORDS:
        return True
    if any(pair <= words for pair in FORBIDDEN_METRIC_PAIRS):
        return True
    normalized = _SEPARATOR_RE.sub("", key.lower())
    if normalized in FORBIDDEN_METRIC_FIELD_NAMES:
        return True
    return any(root in normalized for root in FORBIDDEN_METRIC_SUBSTRINGS)


ITEM_TYPES = ("article-journal", "preprint")
READING_STATUSES = ("unread", "reading", "read")
PDF_STATUSES = ("missing", "available")


class Author(BaseModel):
    """Structured author; ``literal`` covers group/consortium authors."""

    family: str | None = None
    given: str | None = None
    literal: str | None = None

    @model_validator(mode="after")
    def _require_family_or_literal(self) -> "Author":
        if self.family is None and self.literal is None:
            raise ValueError("author must have family or literal")
        return self


class Paper(BaseModel):
    """Canonical v1 main-item frontmatter model."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = SCHEMA_VERSION
    paper_id: UUID
    citation_key: str
    citation_key_aliases: list[str] = Field(default_factory=list)
    item_type: Literal["article-journal", "preprint"] = "article-journal"
    title: str
    authors: list[Author] = Field(default_factory=list)
    publication_date: str | None = None
    year: int | None = None
    pdf_status: Literal["missing", "available"] = "missing"
    reading_status: Literal["unread", "reading", "read"] = "unread"
    metadata_sources: list[str] = Field(default_factory=list)
    field_provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_metric_fields(cls, data: object) -> object:
        if isinstance(data, dict):
            for key in data:
                if _is_forbidden_metric_key(str(key)):
                    raise ValueError(f"forbidden volatile metric field: {key}")
        return data

    @field_validator("citation_key", "citation_key_aliases")
    @classmethod
    def _validate_citation_key(cls, value: object) -> object:
        keys = value if isinstance(value, list) else [value]
        for key in keys:
            if (
                not isinstance(key, str)
                or not CITATION_KEY_RE.match(key)
                or ".." in key
            ):
                raise ValueError(f"invalid citation key: {key!r}")
        return value

    @field_validator("publication_date", mode="before")
    @classmethod
    def _validate_publication_date(cls, value: object) -> str | None:
        if value is None:
            return None
        # YAML loaders parse plain dates as datetime.date and a bare
        # four-digit year as an integer; normalize both to text.
        if isinstance(value, datetime):
            value = value.date().isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, int) and not isinstance(value, bool):
            if 1000 <= value <= 9999:
                value = str(value)
        if not isinstance(value, str) or not PUBLICATION_DATE_RE.match(value):
            raise ValueError(f"invalid publication_date precision: {value!r}")
        text = value if len(value) == 10 else f"{value}-01"
        text = text if len(text) == 10 else f"{text}-01"
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"invalid publication_date: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _sync_and_check_year(self) -> "Paper":
        if self.publication_date is not None:
            date_year = int(self.publication_date[:4])
            if self.year is None:
                self.year = date_year
            elif self.year != date_year:
                raise ValueError("year must equal the publication_date year")
        return self

    @model_validator(mode="after")
    def _check_aliases(self) -> "Paper":
        seen: set[str] = set()
        for alias in self.citation_key_aliases:
            if alias in seen:
                raise ValueError(f"duplicate alias: {alias!r}")
            seen.add(alias)
            if alias == self.citation_key:
                raise ValueError("current citation_key cannot be an alias")
        return self

    @model_validator(mode="after")
    def _check_provenance_covered(self) -> "Paper":
        for field, source in self.field_provenance.items():
            if source not in self.metadata_sources:
                raise ValueError(
                    f"field_provenance[{field!r}] source {source!r} "
                    "not in metadata_sources"
                )
        return self
