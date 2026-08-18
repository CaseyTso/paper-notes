"""CSL-JSON and citation alias indexes (Task 15).

Rebuilds deterministic Pandoc inputs solely from canonical paper notes:

- ``.paper-notes/library.json`` — whole-library CSL-JSON. Only current
  citation keys appear; aliases live in the alias map.
- ``.paper-notes/citation-aliases.json`` — old-to-current key mapping.

Both files are regenerated deterministically (sorted keys, stable byte
output) and published through :func:`paper_notes.fsops.atomic_replace`.
``validate_manuscript`` parses a manuscript with real Pandoc, walks the
AST (never the raw text) to find citations, and reports unknown keys
with their source line/column while ignoring code blocks and inline code.
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fsops import atomic_replace
from .models import Paper
from .repository import InvalidRecord, RepositoryIndex, build_index

PAPER_NOTES_DIR = ".paper-notes"
LIBRARY_JSON = "library.json"
ALIASES_JSON = "citation-aliases.json"


class PandocMissingError(Exception):
    """Pandoc is not installed; manuscript validation cannot run."""


class ManuscriptError(Exception):
    """The manuscript could not be parsed (missing file or Pandoc failure)."""


@dataclass(frozen=True)
class UnknownCitation:
    """An unknown citation key and its first source location."""

    key: str
    line: int
    column: int


@dataclass(frozen=True)
class ManuscriptReport:
    """AST-derived citation validation for one manuscript."""

    citations: list[str]
    unknown: list[UnknownCitation]


@dataclass(frozen=True)
class RebuildResult:
    """Outcome of an index rebuild."""

    library_path: Path
    aliases_path: Path
    papers: int
    aliases: int
    invalid: tuple[InvalidRecord, ...]

    @property
    def invalid_count(self) -> int:
        return len(self.invalid)


def paper_to_csl(paper: Paper) -> dict[str, Any]:
    """Map a canonical :class:`Paper` to one CSL-JSON item.

    Internal bookkeeping fields (``paper_id``, ``metadata_sources``,
    ``field_provenance``, aliases) never leak into the bibliography.
    """
    entry: dict[str, Any] = {
        "id": paper.citation_key,
        "type": paper.item_type,
        "title": paper.title,
    }
    if paper.authors:
        authors: list[dict[str, str]] = []
        for author in paper.authors:
            if author.literal is not None:
                authors.append({"literal": author.literal})
            else:
                mapped: dict[str, str] = {}
                if author.family is not None:
                    mapped["family"] = author.family
                if author.given is not None:
                    mapped["given"] = author.given
                authors.append(mapped)
        entry["author"] = authors
    if paper.publication_date is not None:
        parts = [int(part) for part in paper.publication_date.split("-")]
        entry["issued"] = {"date-parts": [parts]}
    journal = getattr(paper, "journal", None)
    if journal:
        entry["container-title"] = journal
    journal_abbreviation = getattr(paper, "journal_abbreviation", None)
    if journal_abbreviation:
        entry["container-title-short"] = journal_abbreviation
    volume = getattr(paper, "volume", None)
    if volume:
        entry["volume"] = volume
    issue = getattr(paper, "issue", None)
    if issue:
        entry["issue"] = issue
    pages = getattr(paper, "pages", None)
    if pages:
        entry["page"] = pages
    doi = getattr(paper, "doi", None)
    if doi:
        entry["DOI"] = doi
    url = getattr(paper, "url", None)
    if url:
        entry["URL"] = url
    issn = getattr(paper, "issn", None)
    if issn:
        entry["ISSN"] = list(issn)
    language = getattr(paper, "language", None)
    if language:
        entry["language"] = language
    abstract = getattr(paper, "abstract", None)
    if abstract:
        entry["abstract"] = abstract
    # Useful identifier extensions; not part of the core CSL-JSON spec but
    # harmless for downstream consumers and never internal bookkeeping.
    pmid = getattr(paper, "pmid", None)
    if pmid:
        entry["PMID"] = pmid
    pmcid = getattr(paper, "pmcid", None)
    if pmcid:
        entry["PMCID"] = pmcid
    arxiv = getattr(paper, "arxiv", None)
    if arxiv:
        entry["arXiv"] = arxiv
    return entry


def render_library(index: RepositoryIndex) -> str:
    """Deterministic CSL-JSON text for the whole library."""
    entries = [paper_to_csl(index.by_key[key].paper) for key in sorted(index.by_key)]
    return json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_alias_map(index: RepositoryIndex) -> str:
    """Deterministic alias JSON text: old key -> current key."""
    data = {alias: index.aliases[alias] for alias in sorted(index.aliases)}
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def rebuild_indexes(vault_root: Path) -> RebuildResult:
    """Regenerate ``library.json`` and ``citation-aliases.json`` atomically."""
    index = build_index(vault_root)
    notes_dir = vault_root / PAPER_NOTES_DIR
    library = notes_dir / LIBRARY_JSON
    aliases = notes_dir / ALIASES_JSON
    atomic_replace(library, render_library(index))
    atomic_replace(aliases, render_alias_map(index))
    return RebuildResult(
        library_path=library,
        aliases_path=aliases,
        papers=len(index.by_key),
        aliases=len(index.aliases),
        invalid=tuple(index.invalid),
    )


# ---------------------------------------------------------------------------
# Manuscript validation through the Pandoc AST
# ---------------------------------------------------------------------------

# Match "[@key", "; @key" (also ";" without space) citation markers.
def _key_marker_pattern(key: str) -> re.Pattern[str]:
    return re.compile(r"\[\s*@" + re.escape(key) + r"|;\s*@" + re.escape(key))


def _collect_citation_ids(ast: dict[str, Any]) -> list[str]:
    """All citation ids in document order from a Pandoc JSON AST."""
    ids: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("t") == "Cite":
                citations = node.get("c", [[], []])[0]
                for citation in citations:
                    cid = citation.get("citationId")
                    if isinstance(cid, str):
                        ids.append(cid)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ast)
    return ids


def _mask_inline_code(line: str) -> str:
    """Replace backtick code spans with spaces, preserving column offsets.

    Pandoc never emits ``Cite`` elements for ``[@key]`` inside inline
    code, so location reporting must not attribute such occurrences to
    the unknown key. The mask keeps every column position intact so the
    reported column still matches the original text.
    """

    chars = list(line)
    i = 0
    while i < len(chars):
        if chars[i] != "`":
            i += 1
            continue
        open_run = 1
        while i + open_run < len(chars) and chars[i + open_run] == "`":
            open_run += 1
        j = i + open_run
        close_at: int | None = None
        while j < len(chars):
            if chars[j] == "`":
                close_run = 1
                while j + close_run < len(chars) and chars[j + close_run] == "`":
                    close_run += 1
                if close_run >= open_run:
                    close_at = j + close_run
                    break
                j += close_run
            else:
                j += 1
        end = close_at if close_at is not None else len(chars)
        for m in range(i, end):
            chars[m] = " "
        i = end
    return "".join(chars)


def _find_in_line(line: str, key: str) -> int | None:
    """1-based column of the first ``[@key`` / ``; @key`` outside backticks."""
    pattern = _key_marker_pattern(key)
    found = pattern.search(_mask_inline_code(line))
    if found:
        return found.start() + 1
    return None


def _locate_unknown(text: str, key: str) -> tuple[int, int] | None:
    """First (line, column) of a citation marker outside code blocks/spans."""
    lines = text.splitlines()
    in_fence: str | None = None
    for lineno, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if in_fence is not None:
            if stripped.startswith(in_fence * 3):
                in_fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence_char = stripped[0]
            in_fence = fence_char
            continue
        column = _find_in_line(line, key)
        if column is not None:
            return lineno, column
    return None


def validate_manuscript(vault_root: Path, input_path: Path) -> ManuscriptReport:
    """Parse ``input_path`` with Pandoc and validate every AST citation.

    Citations resolve against current keys and aliases; unknown keys are
    reported with their first source location and must block export.
    Code blocks and inline code are ignored because they never produce
    ``Cite`` elements in the AST.
    """
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise PandocMissingError(
            "pandoc is required to validate a manuscript; install it first"
        )
    if not input_path.is_file():
        raise ManuscriptError(f"manuscript file not found: {input_path}")
    index = build_index(vault_root)
    result = subprocess.run(
        [pandoc, "-f", "markdown", "-t", "json", str(input_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ManuscriptError(
            f"pandoc failed on {input_path}: {result.stderr.strip()}"
        )
    ast = json.loads(result.stdout)
    text = input_path.read_text(encoding="utf-8")
    citations: list[str] = []
    unknown: list[UnknownCitation] = []
    for cid in _collect_citation_ids(ast):
        if cid in index.by_key:
            citations.append(cid)
        elif cid in index.aliases:
            citations.append(index.aliases[cid])
        else:
            location = _locate_unknown(text, cid) or (0, 0)
            unknown.append(UnknownCitation(cid, location[0], location[1]))
    return ManuscriptReport(citations=citations, unknown=unknown)
