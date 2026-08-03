"""Read-only Crossref metadata adapter.

Queries ``api.crossref.org/works/<doi>`` and maps the ``message`` object
onto the canonical field vocabulary used by the merge layer. Transport
failures, non-200 responses, invalid JSON, and records without a
``message`` are reported as :class:`~paper_notes.adapters.AdapterError`;
no identifier is ever fabricated.
"""

from __future__ import annotations

import datetime
import html
import re
import urllib.parse
from typing import Any

import requests

from paper_notes.adapters import AdapterError
from paper_notes.identifiers import ParsedIdentifier

API_URL = "https://api.crossref.org/works/"
USER_AGENT = "paper-notes/0.1 (https://github.com/CaseyTso/paper-notes)"


def _strip_jats(value: str) -> str:
    """Remove JATS XML tags and unescape entities in an abstract."""
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _valid_date_parts(parts: list[str]) -> bool:
    """``YYYY[-MM[-DD]]`` with a real calendar date; impossible dates
    like 2024-13-99 are rejected."""
    try:
        if len(parts) == 1:
            return 1000 <= int(parts[0]) <= 9999
        if len(parts) == 2:
            return bool(datetime.datetime.strptime(f"{'-'.join(parts)}-01", "%Y-%m-%d"))
        if len(parts) == 3:
            return bool(datetime.datetime.strptime("-".join(parts), "%Y-%m-%d"))
    except ValueError:
        return False
    return False


class CrossrefAdapter:
    """Minimal read-only Crossref client.

    ``fetch`` accepts a :class:`ParsedIdentifier` of kind ``doi`` or a
    bare DOI string.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def fetch(self, identifier: ParsedIdentifier | str) -> dict[str, Any]:
        doi = identifier.value if isinstance(identifier, ParsedIdentifier) else identifier
        url = API_URL + urllib.parse.quote(doi, safe="")
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdapterError(f"crossref request failed for {doi!r}") from exc
        if response.status_code != 200:
            raise AdapterError(
                f"crossref returned HTTP {response.status_code} for {doi!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError(f"crossref returned invalid JSON for {doi!r}") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise AdapterError(f"crossref response missing message for {doi!r}")
        return _map_message(message)


def _map_message(message: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {}

    title = message.get("title") or message.get("original-title")
    if isinstance(title, list):
        title = title[0] if title else None
    if title:
        record["title"] = title.strip()

    authors: list[dict[str, str]] = []
    for author in message.get("author") or []:
        if not isinstance(author, dict):
            continue
        family = author.get("family")
        given = author.get("given")
        if family:
            entry: dict[str, str] = {"family": family}
            if given:
                entry["given"] = given
            authors.append(entry)
        elif author.get("name"):
            authors.append({"literal": author["name"]})
    if authors:
        record["authors"] = authors

    date_parts = (message.get("issued") or {}).get("date-parts")
    if date_parts and isinstance(date_parts, list) and date_parts[0]:
        raw = [str(p) for p in date_parts[0]]
        parts = [raw[0]] + [p.zfill(2) for p in raw[1:]]
        if _valid_date_parts(parts):
            record["publication_date"] = "-".join(parts)
            record["year"] = int(parts[0])
        elif raw[0].isdigit():
            # impossible full date but a usable year component
            record["year"] = int(raw[0])

    container = message.get("container-title")
    if isinstance(container, list):
        container = container[0] if container else None
    if container:
        record["journal"] = container.strip()

    short = message.get("short-container-title")
    if isinstance(short, list):
        short = short[0] if short else None
    if short:
        record["journal_abbreviation"] = short.strip()

    for key, out in (
        ("volume", "volume"),
        ("issue", "issue"),
        ("page", "pages"),
        ("DOI", "doi"),
        ("URL", "url"),
        ("publisher", "publisher"),
    ):
        value = message.get(key)
        if value:
            record[out] = value

    issn = message.get("ISSN")
    if isinstance(issn, list) and issn:
        record["issn"] = [str(i) for i in issn]

    abstract = message.get("abstract")
    if abstract:
        cleaned = _strip_jats(abstract)
        if cleaned:
            record["abstract"] = cleaned

    return record
