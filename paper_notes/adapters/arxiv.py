"""Read-only arXiv metadata adapter.

Queries the arXiv Atom export API (``export.arxiv.org/api/query``) and
maps the first ``entry`` onto the canonical field vocabulary. Author
names like ``"Shiau, Chia-Yu"`` split into family/given; names without a
comma are kept as ``literal`` (conservative — never guessed). Transport
failures, non-200 responses, and empty feeds are reported as
:class:`~paper_notes.adapters.AdapterError`.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

import requests

from paper_notes.adapters import AdapterError
from paper_notes.identifiers import ParsedIdentifier

API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _clean_arxiv_text(value: str) -> str:
    """Strip tags, unescape entities, and collapse whitespace."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())


class ArxivAdapter:
    """Minimal read-only arXiv API client."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def fetch(self, identifier: ParsedIdentifier) -> dict[str, Any]:
        arxiv_id = identifier.value
        try:
            response = requests.get(
                API_URL, params={"id_list": arxiv_id}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise AdapterError(f"arxiv request failed for {arxiv_id!r}") from exc
        if response.status_code != 200:
            raise AdapterError(
                f"arxiv returned HTTP {response.status_code} for {arxiv_id!r}"
            )
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise AdapterError(f"arxiv returned invalid XML for {arxiv_id!r}") from exc

        entry = root.find(f"{ATOM_NS}entry")
        if entry is None:
            raise AdapterError(f"arxiv returned no entry for {arxiv_id!r}")

        record: dict[str, Any] = {}
        title = entry.findtext(f"{ATOM_NS}title")
        if title:
            record["title"] = _clean_arxiv_text(title)

        authors: list[dict[str, str]] = []
        for author in entry.findall(f"{ATOM_NS}author"):
            name = author.findtext(f"{ATOM_NS}name")
            if not name:
                continue
            name = _clean_arxiv_text(name)
            if "," in name:
                family, given = (part.strip() for part in name.split(",", 1))
                entry_auth: dict[str, str] = {"family": family}
                if given:
                    entry_auth["given"] = given
                authors.append(entry_auth)
            else:
                authors.append({"literal": name})
        if authors:
            record["authors"] = authors

        published = entry.findtext(f"{ATOM_NS}published")
        if published and len(published) >= 10:
            record["publication_date"] = published[:10]
            record["year"] = int(published[:4])

        record["arxiv"] = arxiv_id

        summary = entry.findtext(f"{ATOM_NS}summary")
        if summary:
            cleaned = _clean_arxiv_text(summary)
            if cleaned:
                record["abstract"] = cleaned

        doi = entry.findtext(f"{ARXIV_NS}doi")
        if doi:
            record["doi"] = doi.strip()

        if not record:
            raise AdapterError(f"arxiv record empty for {arxiv_id!r}")
        return record
