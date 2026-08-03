"""Identifier parsing and normalization (DOI / PMID / PMCID / arXiv / URL).

Parsing is pure and deterministic — no network, no filesystem access.
Every parser returns a :class:`ParsedIdentifier` or ``None``; nothing here
ever fabricates an identifier from ambiguous input.

Normalization rules:

- DOI: ``doi:`` prefix, ``doi.org`` / ``dx.doi.org`` resolver URLs and
  bare values are accepted; output is lowercased (DOIs are
  case-insensitive) with trailing sentence punctuation stripped.
- PMID: ``PMID:`` prefix, PubMed URLs and bare 6-9 digit runs.
- PMCID: ``PMC`` prefix (case-insensitive, normalized to uppercase),
  optional version suffix (``.N``), and legacy/new PMC article URLs.
- arXiv: modern ``NNNN.NNNNN`` ids (optionally ``vN``), legacy
  ``category/NNNNNNN`` ids, ``arXiv:`` prefix, and ``arxiv.org``
  abs/pdf URLs.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Literal

IdentifierKind = Literal["doi", "pmid", "pmcid", "arxiv"]

# A DOI is a 10. followed by 4-9 registrant digits, a slash, then any
# non-space characters that are not markup delimiters.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+")

_DOI_URL_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org", "www.dx.doi.org"})
_PUBMED_HOSTS = frozenset({"pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"})
_PMC_HOSTS = frozenset({"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"})
_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org"})

# Trailing characters that are sentence/quoting artifacts, never part of a
# normalized identifier. Closing brackets are stripped only when they have
# no matching opener inside (a percent-decoded DOI like
# ``10.1000/xyz(abc)`` keeps its balanced closing paren).
_TRAILING_JUNK = ".,;:!?"


@dataclass(frozen=True)
class ParsedIdentifier:
    """A recognized strong identifier.

    ``value`` is the normalized form (e.g. ``10.1000/xyz``,
    ``28845751``, ``PMC1234567``, ``2401.00001v2``); ``original`` is the
    exact input string that produced it.
    """

    kind: IdentifierKind
    value: str
    original: str


def _strip_trailing_junk(text: str) -> str:
    result = text
    while result:
        char = result[-1]
        if char in _TRAILING_JUNK:
            result = result[:-1]
            continue
        if char in ")]}":
            opener = {")": "(", "]": "[", "}": "{"}[char]
            if opener not in result[:-1]:
                result = result[:-1]
                continue
        break
    return result


def _unwrap(text: str) -> str:
    """Strip one pair of enclosing brackets/parens (``[hep-th/9901001]``)."""
    if len(text) >= 2 and text[0] in "[(" and text[-1] in "])":
        return text[1:-1]
    return text


# Scheme-less URL prefixes that prose commonly omits the scheme for.
_BARE_DOMAIN_PREFIXES = (
    "arxiv.org/",
    "www.arxiv.org/",
    "doi.org/",
    "dx.doi.org/",
    "pubmed.ncbi.nlm.nih.gov/",
    "pmc.ncbi.nlm.nih.gov/",
    "www.ncbi.nlm.nih.gov/",
)


def _url_parts(value: str) -> tuple[str, str] | None:
    """Return (host, path) for an http(s) URL, else None."""
    if value.lower().startswith(_BARE_DOMAIN_PREFIXES):
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower().rstrip(".")
    return host, parsed.path


def parse_doi(value: str) -> ParsedIdentifier | None:
    """Parse a DOI from a bare value, ``doi:`` prefix, or resolver URL."""
    text = _unwrap(value.strip())
    if not text:
        return None
    prefix = re.match(r"^doi\s*:\s*(.+)$", text, re.IGNORECASE)
    if prefix:
        text = prefix.group(1).strip()
    if not text:
        return None

    url_parts = _url_parts(text)
    if url_parts is not None:
        host, path = url_parts
        if host not in _DOI_URL_HOSTS:
            return None
        candidate = path.lstrip("/").split("?", 1)[0].split("#", 1)[0]
    else:
        candidate = text.split("?", 1)[0].split("#", 1)[0]

    candidate = urllib.parse.unquote(candidate)
    if _DOI_RE.fullmatch(candidate) is None:
        return None
    normalized = _strip_trailing_junk(candidate).lower()
    return ParsedIdentifier(kind="doi", value=normalized, original=value)


def parse_pmid(value: str) -> ParsedIdentifier | None:
    """Parse a PMID from ``PMID:`` prefix, PubMed URL, or bare digits."""
    text = value.strip()
    if not text:
        return None
    prefix = re.match(r"^pmid\s*:\s*(\d+)$", text, re.IGNORECASE)
    if prefix:
        return ParsedIdentifier(kind="pmid", value=prefix.group(1), original=value)

    url_parts = _url_parts(text)
    if url_parts is not None:
        host, path = url_parts
        if host not in _PUBMED_HOSTS:
            return None
        segments = [s for s in path.split("/") if s]
        if not segments:
            return None
        if host == "pubmed.ncbi.nlm.nih.gov" and segments[0].isdigit():
            return ParsedIdentifier(kind="pmid", value=segments[0], original=value)
        # legacy /pubmed/<id> path
        if segments[0] == "pubmed" and len(segments) > 1 and segments[1].isdigit():
            return ParsedIdentifier(kind="pmid", value=segments[1], original=value)
        return None

    # bare digits: ambiguous below six digits (a year, a count, ...)
    if re.fullmatch(r"\d{6,9}", text) is not None:
        return ParsedIdentifier(kind="pmid", value=text, original=value)
    return None


def parse_pmcid(value: str) -> ParsedIdentifier | None:
    """Parse a PMCID from bare ``PMC...``, ``PMCID:`` prefix, or article URL."""
    text = _unwrap(value.strip())
    if not text:
        return None
    prefix = re.match(r"^pmcid\s*:\s*(.+)$", text, re.IGNORECASE)
    if prefix:
        text = prefix.group(1).strip()
    bare = re.fullmatch(r"pmc(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if bare:
        return ParsedIdentifier(
            kind="pmcid", value=f"PMC{bare.group(1)}", original=value
        )

    url_parts = _url_parts(text)
    if url_parts is not None:
        host, path = url_parts
        if host not in _PMC_HOSTS:
            return None
        segments = [s for s in path.split("/") if s]
        if "articles" in segments:
            position = segments.index("articles")
            if position + 1 < len(segments):
                match = re.fullmatch(r"(PMC\d+(?:\.\d+)?)", segments[position + 1], re.IGNORECASE)
                if match:
                    return ParsedIdentifier(
                        kind="pmcid", value=match.group(1).upper(), original=value
                    )
    return None


_MODERN_ARXIV_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")
_LEGACY_ARXIV_RE = re.compile(r"[a-z][a-z0-9\-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?")


def parse_arxiv(value: str) -> ParsedIdentifier | None:
    """Parse an arXiv id from bare value, ``arXiv:`` prefix, or abs/pdf URL."""
    text = _unwrap(value.strip())
    if not text:
        return None
    prefix = re.match(r"^arxiv\s*:\s*(.+)$", text, re.IGNORECASE)
    if prefix:
        text = prefix.group(1).strip()

    url_parts = _url_parts(text)
    if url_parts is not None:
        host, path = url_parts
        if host not in _ARXIV_HOSTS:
            return None
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 2 and segments[0] in ("abs", "pdf"):
            # legacy ids span two segments ("/pdf/hep-th/9901001"); PDF
            # URLs may also carry a trailing ".pdf" suffix
            candidate = "/".join(segments[1:])
            if candidate.endswith(".pdf"):
                candidate = candidate[:-4]
        else:
            return None
    else:
        candidate = text

    if _MODERN_ARXIV_RE.fullmatch(candidate) or _LEGACY_ARXIV_RE.fullmatch(candidate):
        return ParsedIdentifier(kind="arxiv", value=candidate, original=value)
    return None


def parse_identifier(value: str) -> ParsedIdentifier | None:
    """Dispatch a bare value or URL to its identifier kind, or None."""
    for parser in (parse_doi, parse_pmcid, parse_pmid, parse_arxiv):
        parsed = parser(value)
        if parsed is not None:
            return parsed
    return None


def extract_identifiers(value: str) -> list[ParsedIdentifier]:
    """Extract every known identifier from a URL or free-text string.

    The input is split on whitespace and each token is parsed against the
    known identifier forms; a DOI-pattern scan additionally catches
    publisher URLs (e.g. Wiley ``/doi/10.1002/...``). ``original`` on
    each result is the exact input fragment that produced it. Results
    are deduplicated by ``(kind, value)`` and returned in first-seen
    order.
    """
    results: list[ParsedIdentifier] = []
    seen: set[tuple[str, str]] = set()

    def add(identifier: ParsedIdentifier | None) -> None:
        if identifier is None:
            return
        key = (identifier.kind, identifier.value)
        if key not in seen:
            seen.add(key)
            results.append(identifier)

    for token in re.split(r"\s+", value.strip()):
        add(parse_identifier(token))
    for match in _DOI_RE.finditer(value):
        add(parse_doi(match.group(0)))
    return results
