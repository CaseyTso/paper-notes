"""Read-only PubMed metadata adapter.

Two query paths:

- PMID: ``efetch.fcgi?db=pubmed&retmode=xml`` — the structured PubMed
  XML record (LastName/ForeName authors, journal title, pub date).
- PMCID: ``esummary.fcgi?db=pmc&retmode=json`` — the compact PMC
  summary (author names arrive as ``"Family Given"`` strings; the last
  token is treated as the family name).

Transport failures, non-200 responses, malformed payloads, and missing
records are reported as :class:`~paper_notes.adapters.AdapterError`.
"""

from __future__ import annotations

import datetime
import re
import xml.etree.ElementTree as ET
from typing import Any

import requests

from paper_notes.adapters import AdapterError
from paper_notes.identifiers import ParsedIdentifier

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _parse_pub_date(year: str, month: str | None = None, day: str | None = None) -> str:
    """Build ``YYYY[-MM[-DD]]``; return ``""`` for impossible dates.

    Never raises: malformed input simply drops the unusable components.
    """
    try:
        year_int = int(year.strip())
    except (ValueError, AttributeError):
        return ""
    if not (1000 <= year_int <= 9999):
        return ""
    parts = [f"{year_int:04d}"]
    if month:
        month_num = _MONTHS.get(month.strip().lower()[:3])
        if month_num is None and month.strip().isdigit():
            month_num = int(month.strip())
        if month_num is None or not (1 <= month_num <= 12):
            # a provided-but-impossible month invalidates the whole date
            return ""
        parts.append(f"{month_num:02d}")
        if day and day.strip().isdigit():
            day_int = int(day.strip())
            if not (1 <= day_int <= 31):
                return ""
            parts.append(f"{day_int:02d}")
        elif day:
            # a provided-but-malformed day invalidates the whole date
            return ""
    value = "-".join(parts)
    if len(parts) == 2:
        try:
            datetime.datetime.strptime(f"{value}-01", "%Y-%m-%d")
        except ValueError:
            return ""
    elif len(parts) == 3:
        try:
            datetime.datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return ""
    return value


def _parse_pubdate_string(text: str) -> str:
    """Parse esummary ``pubdate`` forms like ``2017 Nov`` / ``2017 Nov 1``."""
    tokens = text.strip().split()
    if not tokens or not tokens[0].isdigit():
        return ""
    year = tokens[0]
    month = None
    day = None
    if len(tokens) > 1:
        month = tokens[1]
    if len(tokens) > 2:
        day = tokens[2]
    return _parse_pub_date(year, month, day)


def _local(elem: ET.Element, name: str) -> ET.Element | None:
    """First descendant with the given local tag name (namespace-agnostic)."""
    for child in elem.iter():
        if child.tag.rsplit("}", 1)[-1] == name:
            return child
    return None


def _locals(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem.iter() if c.tag.rsplit("}", 1)[-1] == name]


def _text(elem: ET.Element | None) -> str | None:
    """All text inside the element, including inline markup children.

    ``<ArticleTitle>Alpha <i>beta</i> gamma</ArticleTitle>`` yields
    ``"Alpha beta gamma"`` — using ``itertext`` instead of ``elem.text``
    so structured PubMed records are never truncated.
    """
    if elem is None:
        return None
    text = " ".join("".join(elem.itertext()).split())
    return text or None


class PubMedAdapter:
    """Minimal read-only PubMed/PMC client."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def fetch(self, identifier: ParsedIdentifier) -> dict[str, Any]:
        if identifier.kind == "pmcid":
            return self._fetch_pmcid(identifier.value)
        if identifier.kind == "pmid":
            return self._fetch_pmid(identifier.value)
        raise AdapterError(f"pubmed adapter does not handle {identifier.kind!r}")

    def _get(self, url: str, params: dict[str, str]) -> requests.Response:
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AdapterError(f"pubmed request failed for {params.get('id', '')!r}") from exc
        if response.status_code != 200:
            raise AdapterError(
                f"pubmed returned HTTP {response.status_code} for {params.get('id', '')!r}"
            )
        return response

    def _fetch_pmid(self, pmid: str) -> dict[str, Any]:
        response = self._get(EFETCH_URL, {"db": "pubmed", "id": pmid, "retmode": "xml"})
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise AdapterError(f"pubmed returned invalid XML for {pmid!r}") from exc
        article = _local(root, "PubmedArticle")
        if article is None:
            raise AdapterError(f"pubmed returned no record for {pmid!r}")

        record: dict[str, Any] = {}
        title = _text(_local(article, "ArticleTitle"))
        if title:
            record["title"] = title
        pmid_value = _text(_local(article, "PMID"))
        if pmid_value:
            record["pmid"] = pmid_value

        authors: list[dict[str, str]] = []
        for author in _locals(article, "Author"):
            family = _text(_local(author, "LastName"))
            given = _text(_local(author, "ForeName"))
            collective = _text(_local(author, "CollectiveName"))
            if family:
                entry: dict[str, str] = {"family": family}
                if given:
                    entry["given"] = given
                authors.append(entry)
            elif collective:
                authors.append({"literal": collective})
        if authors:
            record["authors"] = authors

        journal = _text(_local(article, "Title"))
        if journal:
            record["journal"] = journal
        iso = _text(_local(article, "ISOAbbreviation"))
        if iso:
            record["journal_abbreviation"] = iso
        volume = _text(_local(article, "Volume"))
        if volume:
            record["volume"] = volume
        issue = _text(_local(article, "Issue"))
        if issue:
            record["issue"] = issue

        year = _text(_local(article, "Year"))
        month = _text(_local(article, "Month"))
        day = _text(_local(article, "Day"))
        pub_date = _parse_pub_date(year, month, day) if year else ""
        if pub_date:
            record["publication_date"] = pub_date
            record["year"] = int(pub_date[:4])

        abstract_parts = []
        for abstract_text in _locals(article, "AbstractText"):
            part = _text(abstract_text)
            if part:
                abstract_parts.append(part)
        if abstract_parts:
            record["abstract"] = " ".join(abstract_parts)

        issn = _text(_local(article, "ISSN"))
        if issn:
            record["issn"] = [issn]
        language = _text(_local(article, "Language"))
        if language:
            record["language"] = language

        if not record:
            raise AdapterError(f"pubmed record empty for {pmid!r}")
        return record

    def _fetch_pmcid(self, pmcid: str) -> dict[str, Any]:
        # real NCBI contract: db=pmc expects the bare numeric uid, and the
        # JSON result is keyed by that same numeric uid (not "PMC...").
        uid = pmcid[3:] if pmcid.upper().startswith("PMC") else pmcid
        response = self._get(ESUMMARY_URL, {"db": "pmc", "id": uid, "retmode": "json"})
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError(f"pubmed returned invalid JSON for {pmcid!r}") from exc
        result = None
        for key, value in (payload.get("result") or {}).items():
            if key.lower().rstrip(";") in (uid.lower(), pmcid.lower()):
                result = value
                break
        if not isinstance(result, dict):
            raise AdapterError(f"pubmed returned no result for {pmcid!r}")

        record: dict[str, Any] = {}
        title = result.get("title")
        if title:
            record["title"] = title.strip()

        authors: list[dict[str, str]] = []
        for author in result.get("authors") or []:
            name = (author or {}).get("name")
            if not name:
                continue
            tokens = name.split()
            if len(tokens) == 1:
                authors.append({"family": tokens[0]})
            else:
                authors.append({"family": tokens[0], "given": " ".join(tokens[1:])})
        if authors:
            record["authors"] = authors

        journal = result.get("fulljournalname")
        if journal:
            record["journal"] = journal.strip()
        pub_date = _parse_pubdate_string(result.get("pubdate") or "")
        if pub_date:
            record["publication_date"] = pub_date
            record["year"] = int(pub_date[:4])
        for key in ("volume", "issue", "pages"):
            if result.get(key):
                record[key] = result[key]

        for article_id in result.get("articleids") or []:
            id_type = (article_id or {}).get("idtype")
            value = (article_id or {}).get("value")
            if id_type == "pmcid" and value:
                # real values may arrive as "pmc-id: PMC7094943;"
                cleaned = str(value).removeprefix("pmc-id:").strip().rstrip(";").strip()
                record["pmcid"] = cleaned
            elif id_type == "pubmed" and value:
                record["pmid"] = value

        if not record:
            raise AdapterError(f"pubmed record empty for {pmcid!r}")
        return record
