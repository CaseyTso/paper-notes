"""Local PDF identifier extraction and attachment hashing.

Deterministic, read-only, no OCR, no AI:

- :func:`sha256_stream` fingerprints a file with SHA-256 over fixed
  :data:`HASH_CHUNK_SIZE` (1 MiB) chunks.
- :func:`extract_pdf_identifiers` opens the PDF read-only with PyMuPDF,
  collects candidate identifiers from the XMP packet (only elements
  whose local name is ``identifier`` or ``doi`` — any namespace prefix,
  case-insensitive — plus the ``rdf:li`` / ``rdf:value`` /
  ``rdf:resource`` values inside ``identifier``) and from the text
  layer of the first :data:`SCAN_PAGE_RANGE` pages (via
  :func:`~paper_notes.identifiers.extract_identifiers`, which also
  catches DOIs embedded in publisher URLs), and returns a
  :class:`PdfIdentifierResult` carrying the attachment SHA-256.

Contract:

- Channels are scanned in order ``("xmp", "first_pages")`` (XMP is
  authoritative metadata); results are deduplicated by
  ``(kind, value)`` in first-seen order and ``sources`` lists, in
  channel order, every channel that contributed at least one
  identifier.
- In PDF context a PMID is accepted only when explicitly labelled
  (``PMID: 28845751``, ``PMID 28845751``, whitespace/newline between
  label and digits allowed) or via a real PubMed URL; a bare 6-9 digit
  run (a manuscript number, a year, ...) is never reported. DOI /
  PMCID / arXiv keep their explicit forms.
- Any strong identifier found ⇒ ``confidence="high"``; none found ⇒ a
  low-confidence candidate whose identifiers are empty — metadata is
  never fabricated.
- Unreadable, non-PDF, corrupt, or directory input raises
  :class:`PdfError` with ``path`` and ``reason`` attributes; bare
  ``OSError`` subclasses never leak. PyMuPDF is a multi-format reader,
  so a file that opens fine but is not a PDF (an image or SVG renamed
  ``.pdf``) is rejected via ``doc.is_pdf`` and a password-protected
  PDF is rejected via ``doc.needs_pass`` before any content access;
  raw fitz exceptions (``FileDataError`` / ``RuntimeError`` /
  ``ValueError``) never leak either.
- The digest and the parsed identifiers always come from the same
  bytes: the file is re-hashed after parsing and a change between the
  two reads raises :class:`PdfError` instead of mixing results.
- The input file is only ever read: bytes, mode, mtime, and inode are
  unchanged on every success and failure path.
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import fitz

from paper_notes.identifiers import (
    ParsedIdentifier,
    extract_identifiers,
    parse_identifier,
)

# Deterministic text-scan window: pages 0..SCAN_PAGE_RANGE-1
# (0-indexed) are the only pages whose text layer is searched for
# identifiers. Fixed by contract — never derive it from the document.
SCAN_PAGE_RANGE = 3

# Streaming hash chunk size (1 MiB). Fixed constant.
HASH_CHUNK_SIZE = 1 << 20

# In PDF context a PMID is accepted only when explicitly labelled
# (``PMID: 28845751``, ``PMID 28845751`` — whitespace, including a line
# break, between label and digits is fine) or via a real PubMed URL.
# A bare 6-9 digit run is a manuscript number, a year, ... and must
# never be reported.
_PMID_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:PMID|PubMed\s+ID)\s*:?\s*(\d{6,9})(?!\d)",
    re.IGNORECASE,
)

Confidence = Literal["high", "low"]

# Channel scan order; also the order of ``sources`` in results.
CHANNEL_ORDER = ("xmp", "first_pages")


class PdfError(Exception):
    """Structured error: the input is not a readable PDF.

    ``path`` is the offending path and ``reason`` a short description
    (``"no such file"``, ``"not a readable PDF: ..."``, ``"not a
    PDF: ..."``, ``"encrypted PDF: ..."``, ``"corrupt PDF: ..."``).
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"cannot read PDF {path}: {reason}")


@dataclass(frozen=True)
class PdfIdentifierResult:
    """Identifiers found in a local PDF plus its attachment identity.

    ``sha256`` is the hex digest of the raw PDF bytes (streaming
    SHA-256); ``identifiers`` are the strong identifiers found, empty
    for a low-confidence candidate; ``sources`` names the channels
    that contributed (subset of :data:`CHANNEL_ORDER`).
    """

    sha256: str
    identifiers: tuple[ParsedIdentifier, ...]
    confidence: Confidence
    sources: tuple[str, ...]


def sha256_stream(path: Path) -> str:
    """Return the lowercase hex SHA-256 of ``path``, read in fixed chunks.

    Generic file hashing primitive — only reads; never modifies the
    file. A missing file raises ``FileNotFoundError``.
    """
    hasher = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _local_name(tag: str) -> str:
    """Return the local name of an ElementTree tag (strip any namespace)."""
    return tag.rsplit("}", 1)[-1]


def _parse_candidate(candidate: str) -> ParsedIdentifier | None:
    """Parse an identifier candidate in PDF context.

    PMIDs are accepted only when explicitly labelled (``PMID: 28845751``,
    ``PMID 28845751``) or via a real PubMed URL; a bare 6-9 digit run
    (a manuscript number, a year, ...) is never reported. DOI / PMCID /
    arXiv keep their explicit forms.
    """
    label = _PMID_LABEL_RE.fullmatch(candidate)
    if label:
        return ParsedIdentifier(kind="pmid", value=label.group(1), original=candidate)
    parsed = parse_identifier(candidate)
    if parsed is not None and parsed.kind == "pmid" and re.fullmatch(r"\d{6,9}", candidate.strip()):
        return None
    return parsed


# RDF namespace URI used for ``rdf:li`` / ``rdf:value`` /
# ``rdf:resource`` inside XMP identifier containers.
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _xmp_identifier_candidates(xml: str) -> list[str]:
    """Collect candidate identifier strings from an XMP packet.

    Structured parse: only elements whose local name (case-insensitive)
    is ``identifier`` or ``doi`` contribute (any legal namespace
    prefix). Values come from the element's own text, its own
    ``rdf:resource`` attribute, and, inside ``identifier`` containers,
    the ``rdf:li`` / ``rdf:value`` items and any ``rdf:resource``
    attribute. ``li`` / ``value`` / ``resource`` of other elements
    (``dc:creator``, ``dc:title``, ``dc:subject``, ...) are never
    scanned. A malformed packet simply yields no candidates — the PDF
    itself is still valid input.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    candidates: list[str] = []

    def add(value: str) -> None:
        stripped = value.strip()
        if stripped:
            candidates.append(html.unescape(stripped))

    for element in root.iter():
        if _local_name(element.tag).lower() not in ("identifier", "doi"):
            continue
        resource = element.attrib.get(f"{{{_RDF_NS}}}resource")
        if resource:
            add(resource)
        add(element.text or "")
        for descendant in element.iter():
            if descendant is element:
                continue
            if _local_name(descendant.tag).lower() in ("li", "value"):
                add(descendant.text or "")
            resource = descendant.attrib.get(f"{{{_RDF_NS}}}resource")
            if resource:
                add(resource)
    return candidates


def _scan_xmp(doc: fitz.Document) -> list[ParsedIdentifier]:
    xml = doc.get_xml_metadata()
    if not xml:
        return []
    found: list[ParsedIdentifier] = []
    seen: set[tuple[str, str]] = set()
    for candidate in _xmp_identifier_candidates(xml):
        parsed = _parse_candidate(candidate)
        if parsed is None:
            continue
        key = (parsed.kind, parsed.value)
        if key not in seen:
            seen.add(key)
            found.append(parsed)
    return found


def _scan_first_pages(doc: fitz.Document) -> list[ParsedIdentifier]:
    limit = min(SCAN_PAGE_RANGE, doc.page_count)
    pages = []
    for index in range(limit):
        pages.append(doc.load_page(index).get_text())
    text = "\n".join(pages)
    found: list[ParsedIdentifier] = []
    seen: set[tuple[str, str]] = set()

    def add(identifier: ParsedIdentifier) -> None:
        key = (identifier.kind, identifier.value)
        if key not in seen:
            seen.add(key)
            found.append(identifier)

    # Explicit PMID labels first — they may span whitespace including a
    # line break between the label and the digits.
    for match in _PMID_LABEL_RE.finditer(text):
        add(ParsedIdentifier(kind="pmid", value=match.group(1), original=match.group(0)))
    # extract_identifiers additionally catches DOI patterns embedded in
    # publisher URLs (Wiley /doi/<doi>, ...). Its bare-digit PMIDs are
    # dropped here — a bare 6-9 digit run is a manuscript number, a
    # year, ... — while labelled PMIDs and PubMed URLs are already
    # covered above or keep a non-bare ``original``.
    for identifier in extract_identifiers(text):
        if identifier.kind == "pmid" and re.fullmatch(
            r"\d{6,9}", identifier.original.strip()
        ):
            continue
        add(identifier)
    return found


def _collect(doc: fitz.Document) -> tuple[list[ParsedIdentifier], tuple[str, ...]]:
    identifiers: list[ParsedIdentifier] = []
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    for channel, scan in (("xmp", _scan_xmp), ("first_pages", _scan_first_pages)):
        contributed = False
        for identifier in scan(doc):
            key = (identifier.kind, identifier.value)
            if key in seen:
                continue
            seen.add(key)
            identifiers.append(identifier)
            contributed = True
        if contributed:
            sources.append(channel)
    return identifiers, tuple(sources)


def extract_pdf_identifiers(path: Path) -> PdfIdentifierResult:
    """Extract identifiers and the SHA-256 from a local PDF.

    Raises :class:`PdfError` for a missing file, a directory, an
    unreadable file (permissions, I/O errors), a non-PDF (PyMuPDF is
    a multi-format reader — an image or SVG renamed ``.pdf`` opens
    fine and is rejected via ``doc.is_pdf``), a password-protected
    PDF (rejected via ``doc.needs_pass`` before any content access), a
    corrupt PDF, or a file that changed between the initial hash and
    parsing (the digest of the first bytes is never mixed with
    identifiers of the second bytes). Raw fitz exceptions
    (``FileDataError`` / ``RuntimeError`` / ``ValueError``) never leak
    and a :class:`PdfError` raised by this function's own guards
    propagates as-is. The input is only ever read and the document is
    closed on every path.
    """
    path = Path(path)

    def structured_read_error(exc: OSError, phase: str) -> PdfError:
        if isinstance(exc, FileNotFoundError):
            return PdfError(path, "no such file")
        if isinstance(exc, IsADirectoryError):
            return PdfError(path, "is a directory")
        if isinstance(exc, PermissionError):
            return PdfError(path, "permission denied")
        return PdfError(path, f"{phase}: {exc}")

    try:
        digest = sha256_stream(path)
    except OSError as exc:
        raise structured_read_error(exc, "cannot read") from exc

    try:
        doc = fitz.open(str(path))
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise PdfError(path, f"not a readable PDF: {exc}") from exc
    except OSError as exc:
        raise PdfError(path, f"cannot open: {exc}") from exc
    try:
        # PyMuPDF opens many formats; only a real PDF may be scanned.
        # Anything else (image, SVG, ... renamed ``.pdf``) is rejected
        # here — never scanned, never a low-confidence candidate.
        if not doc.is_pdf:
            raise PdfError(
                path, "not a PDF: PyMuPDF opened the file as a different format"
            )
        # A password-protected PDF opens without the password but its
        # content is inaccessible — refuse before any XMP/page access
        # (which would otherwise raise "document closed or encrypted").
        if doc.needs_pass:
            raise PdfError(path, "encrypted PDF: password required")
        identifiers, sources = _collect(doc)
    except PdfError:
        raise  # own structured error: never re-wrapped as "corrupt"
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise PdfError(path, f"corrupt PDF: {exc}") from exc
    finally:
        doc.close()

    # TOCTOU guard: the file may have been swapped between the initial
    # hash and the parse above. Re-hash after parsing and refuse to mix
    # the digest of the first bytes with identifiers of the second.
    try:
        digest_after = sha256_stream(path)
    except OSError as exc:
        raise structured_read_error(exc, "cannot re-read after extraction") from exc
    if digest_after != digest:
        raise PdfError(path, "file changed during extraction")

    confidence: Confidence = "high" if identifiers else "low"
    return PdfIdentifierResult(
        sha256=digest,
        identifiers=tuple(identifiers),
        confidence=confidence,
        sources=sources,
    )
