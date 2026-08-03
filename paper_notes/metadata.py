"""Deterministic structured metadata merge (no AI in the v0.1 core).

Adapters produce :class:`SourceRecord` dicts in the canonical field
vocabulary; :func:`merge_records` folds them into one
:class:`MetadataCandidate`:

- Values are compared field-wise after normalization; agreeing values
  from several sources stay a single value whose provenance is the
  highest-priority source.
- Disagreeing values become a :class:`FieldConflict`; the priority
  source's value remains the candidate and confidence drops to
  ``needs_confirmation``.
- User-confirmed values always win and clear the conflicting field.
- AI is an optional, untrusted suggestion input: it only fills fields
  with no sourced value, never overrides a remote source, and any
  AI-sourced field forces ``needs_confirmation`` until a user confirms.
- Missing critical fields (title / non-empty authors / year or
  publication_date) mean the record must not be auto-created.

Source priority for provenance and conflict ordering: crossref, pubmed,
arxiv, ai. ``publication_date`` comparison is precision-aware
(``2024-05-01`` and ``2024-05`` agree); the more precise value wins.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from paper_notes.adapters import AdapterError
from paper_notes.identifiers import ParsedIdentifier

Confidence = Literal["high", "needs_confirmation"]

SOURCE_PRIORITY = ("crossref", "pubmed", "arxiv", "ai")

# Fields without which a record must never be created automatically.
CRITICAL_FIELDS = ("title", "authors", "year")

_SOURCE_FOR_KIND = {
    "doi": "crossref",
    "pmid": "pubmed",
    "pmcid": "pubmed",
    "arxiv": "arxiv",
}


class ResolutionError(Exception):
    """No metadata source responded for the given identifiers."""


@dataclass(frozen=True)
class SourceRecord:
    """One adapter result: ``source`` name plus canonical field values."""

    source: str
    values: dict[str, Any]


@dataclass(frozen=True)
class FieldConflict:
    """Disagreeing values for one field.

    ``values`` lists ``(source, value)`` pairs in source-priority order;
    the first pair is also what :attr:`MetadataCandidate.values` keeps as
    the candidate value.
    """

    field: str
    values: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class MetadataCandidate:
    """Merged result with per-field provenance and conflict review data."""

    values: dict[str, Any]
    field_provenance: dict[str, str]
    confidence: Confidence
    conflicts: list[FieldConflict] = field(default_factory=list)


def _priority(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _date_key(value: str) -> tuple[int | None, int | None, int | None]:
    parts = value.split("-")
    year = int(parts[0])
    month = int(parts[1]) if len(parts) > 1 else None
    day = int(parts[2]) if len(parts) > 2 else None
    return year, month, day


def _dates_equiv(a: tuple[int | None, int | None, int | None],
                 b: tuple[int | None, int | None, int | None]) -> bool:
    """Two dates agree at every precision both express."""
    return (
        a[0] == b[0]
        and (a[1] is None or b[1] is None or a[1] == b[1])
        and (a[2] is None or b[2] is None or a[2] == b[2])
    )


def _norm_author(author: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (str(key), str(value).strip().lower())
            for key, value in author.items()
            if value
        )
    )


def _norm_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "publication_date":
        return _date_key(str(value))
    if field == "year":
        return int(value)
    if field == "authors":
        # author ORDER is significant (first author, citation keys);
        # compare as an ordered sequence, never as a set
        return tuple(_norm_author(a) for a in value)
    if field == "issn":
        return tuple(sorted(str(i).strip().lower() for i in value))
    if isinstance(value, str):
        return " ".join(value.split()).lower()
    if isinstance(value, list):
        return tuple(value)
    return value


def _valid_publication_date(value: str) -> bool:
    """``YYYY[-MM[-DD]]`` with a real calendar date; impossible dates like
    2024-13-99 and out-of-range year-only dates like ``0000``/``0999``
    are rejected before they can reach the candidate values. The year
    component shares the canonical 1000-9999 range check.
    """
    text = str(value)
    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        return False
    year = int(text[:4])
    if not (1000 <= year <= 9999):
        return False
    try:
        if len(text) == 10:
            datetime.datetime.strptime(text, "%Y-%m-%d")
        elif len(text) == 7:
            datetime.datetime.strptime(f"{text}-01", "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _normalize_year(value: Any) -> int | None:
    """Validate and normalize a year to a four-digit integer.

    Only a non-bool ``int`` or a string matching ``^\\d{4}$`` is
    accepted; floats (including ``inf``/``nan``), Decimals, scientific
    notation, and leading-zero strings are rejected outright — never
    truncated, never crashed on. Returns ``None`` for anything invalid
    so source/confirmed/AI values are skipped silently.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        year_int = value
    elif isinstance(value, str):
        if not re.fullmatch(r"\d{4}", value):
            return None
        try:
            year_int = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        # float (incl. inf/nan), Decimal, and anything else: reject —
        # truncating 2024.5 to 2024 would silently corrupt the year
        return None
    if not (1000 <= year_int <= 9999):
        return None
    return year_int


def _date_precision(key: tuple[int | None, int | None, int | None]) -> int:
    return sum(1 for part in key if part is not None)


def _pick_winner(field: str, group: list[tuple[str, Any]]) -> tuple[str, Any]:
    """Winner (source, value) inside a group of agreeing values."""
    if field == "publication_date":
        return max(
            group,
            key=lambda pair: (_date_precision(_date_key(str(pair[1]))), -_priority(pair[0])),
        )
    return min(group, key=lambda pair: _priority(pair[0]))


def merge_records(
    records: Sequence[SourceRecord],
    *,
    confirmed: Mapping[str, Any] | None = None,
    ai: Mapping[str, Any] | None = None,
) -> MetadataCandidate:
    """Fold source records into one candidate with provenance and conflicts.

    ``confirmed`` values (user decisions) override every source; ``ai``
    values only fill fields no source provided.
    """
    confirmed = confirmed or {}
    ai = ai or {}

    # 1. collect per-field (source, value) pairs in source-priority order;
    #    impossible dates and invalid years are dropped before they can
    #    reach candidates
    field_pairs: dict[str, list[tuple[str, Any]]] = {}
    for record in sorted(records, key=lambda rec: _priority(rec.source)):
        for field_name, value in record.values.items():
            if value is None:
                continue
            if field_name == "publication_date":
                if not _valid_publication_date(value):
                    continue
            elif field_name == "year":
                normalized_year = _normalize_year(value)
                if normalized_year is None:
                    continue
                value = normalized_year
            field_pairs.setdefault(field_name, []).append((record.source, value))

    # 2. group agreeing values per field; differing groups are conflicts.
    #    date equivalence is not transitive (2024 ~ 2024-05 and 2024 ~
    #    2024-06 but 2024-05 !~ 2024-06), so a value only joins a group
    #    when it is compatible with EVERY member already in it.
    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    conflicts: list[FieldConflict] = []
    for field_name, pairs in field_pairs.items():
        if field_name == "publication_date":
            groups: list[list[tuple[str, Any]]] = []
            for pair in pairs:
                for group in groups:
                    if all(
                        _dates_equiv(_date_key(str(pair[1])), _date_key(str(member[1])))
                        for member in group
                    ):
                        group.append(pair)
                        break
                else:
                    groups.append([pair])
        else:
            by_norm: dict[Any, list[tuple[str, Any]]] = {}
            for pair in pairs:
                by_norm.setdefault(_norm_value(field_name, pair[1]), []).append(pair)
            groups = list(by_norm.values())

        if len(groups) == 1:
            source, value = _pick_winner(field_name, groups[0])
            values[field_name] = value
            provenance[field_name] = source
        else:
            ordered = sorted(pairs, key=lambda pair: _priority(pair[0]))
            # on conflict the highest-priority source's value remains the
            # candidate — exactly the first conflict pair (date precision
            # only arbitrates inside an agreeing group)
            source, value = ordered[0]
            values[field_name] = value
            provenance[field_name] = source
            conflicts.append(FieldConflict(field=field_name, values=tuple(ordered)))

    # 3. user-confirmed values win and clear their field's conflict
    for field_name, value in confirmed.items():
        if value is None:
            continue
        if field_name == "publication_date":
            if not _valid_publication_date(value):
                continue
        elif field_name == "year":
            normalized_year = _normalize_year(value)
            if normalized_year is None:
                continue
            value = normalized_year
        values[field_name] = value
        provenance[field_name] = "user"
        conflicts = [c for c in conflicts if c.field != field_name]

    # 4. AI suggestions only fill gaps, never override sourced values
    for field_name, value in ai.items():
        if value is None or field_name in values:
            continue
        if field_name == "publication_date":
            if not _valid_publication_date(value):
                continue
        elif field_name == "year":
            normalized_year = _normalize_year(value)
            if normalized_year is None:
                continue
            value = normalized_year
        values[field_name] = value
        provenance[field_name] = "ai"

    # 5. canonical consistency is re-verified AFTER confirmed/AI were
    #    applied: year and publication_date must agree, otherwise the
    #    candidate is not high-confidence
    pub_date = values.get("publication_date")
    year = values.get("year")
    if pub_date is not None:
        pub_year = int(str(pub_date)[:4])
        if year is None:
            values["year"] = pub_year
            provenance["year"] = provenance.get("publication_date", "unknown")
        elif int(year) != pub_year:
            conflicts.append(
                FieldConflict(
                    field="publication_date",
                    values=(
                        (provenance.get("publication_date", "unknown"), pub_date),
                        (provenance.get("year", "unknown"), year),
                    ),
                )
            )

    # 6. confidence: conflicts, AI-sourced facts, or missing critical
    #    fields all block automatic creation
    has_ai = any(source == "ai" for source in provenance.values())
    title = values.get("title")
    authors = values.get("authors")
    critical_ok = (
        title not in (None, "")
        and isinstance(authors, list)
        and len(authors) > 0
        and values.get("year") is not None
    )
    confidence: Confidence = (
        "high" if (not conflicts and critical_ok and not has_ai) else "needs_confirmation"
    )

    return MetadataCandidate(
        values=values,
        field_provenance=provenance,
        confidence=confidence,
        conflicts=conflicts,
    )


def _default_adapters() -> dict[str, Any]:
    from paper_notes.adapters.arxiv import ArxivAdapter
    from paper_notes.adapters.crossref import CrossrefAdapter
    from paper_notes.adapters.pubmed import PubMedAdapter

    return {
        "doi": CrossrefAdapter(),
        "pmid": PubMedAdapter(),
        "pmcid": PubMedAdapter(),
        "arxiv": ArxivAdapter(),
    }


def resolve(
    identifiers: Sequence[ParsedIdentifier],
    *,
    adapters: Mapping[str, Any] | None = None,
    confirmed: Mapping[str, Any] | None = None,
    ai: Mapping[str, Any] | None = None,
) -> MetadataCandidate:
    """Resolve identifiers through adapters and merge the results.

    Identical ``(kind, value)`` pairs are queried once. An adapter that
    raises :class:`~paper_notes.adapters.AdapterError` is skipped (its
    identifiers simply contribute nothing); when no source responds at
    all, :class:`ResolutionError` is raised. No AI service is ever
    called — ``ai`` is plain input data passed to :func:`merge_records`.
    """
    adapter_map = adapters if adapters is not None else _default_adapters()
    records: list[SourceRecord] = []
    seen: set[tuple[str, str]] = set()
    for identifier in identifiers:
        key = (identifier.kind, identifier.value)
        if key in seen:
            continue
        seen.add(key)
        adapter = adapter_map.get(identifier.kind)
        source = _SOURCE_FOR_KIND.get(identifier.kind)
        if adapter is None or source is None:
            continue
        try:
            records.append(
                SourceRecord(source=source, values=adapter.fetch(identifier))
            )
        except AdapterError:
            continue
    if not records:
        raise ResolutionError(
            "no metadata source responded for the given identifiers"
        )
    return merge_records(records, confirmed=confirmed, ai=ai)
