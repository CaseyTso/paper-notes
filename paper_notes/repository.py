"""Read-only repository index over the fixture vault.

``build_index`` scans one level of ``05 Literature/<key>/`` directories,
validates each main note against the canonical schema, and produces a
:class:`RepositoryIndex` of valid records plus :class:`InvalidRecord`
diagnostics for everything that does not index cleanly.

Indexing semantics:

- Spec-named derived notes (``minerUmd_<key>.md``,
  ``Figure解读_<key>.md``) are never parsed as main-note candidates.
- The canonical main note ``<key>.md`` is always indexed before any
  extra Markdown file in the directory; without it the directory never
  enters the valid index (extra files are diagnostic objects only).
- ``by_key`` / ``by_id`` hold schema-valid, key-consistent, unique
  records only (first one wins; duplicates are reported, not indexed).
- Alias resolution is a second stage after every current key is known,
  so results do not depend on scan order: an alias that collides with
  any current key is rejected, and an alias declared by more than one
  item is a collision for all declarers (no winner).
- ``invalid`` carries one :class:`InvalidRecord` per problem; a single
  item may appear with several records (e.g. an alias collision plus a
  PDF status mismatch). PDF-status mismatches are reported separately
  from schema failures; such items stay indexed because their metadata
  is valid. Schema failures include the failing field paths.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from .frontmatter import FrontmatterError, load_paper_note
from .models import Paper
from .paths import literature_root

DERIVED_NOTE_PREFIXES = ("minerUmd_", "Figure解读_")


@dataclass(frozen=True)
class PaperRecord:
    """A schema-valid main note."""

    path: Path
    paper: Paper


@dataclass(frozen=True)
class InvalidRecord:
    """One discoverable problem with a note or directory."""

    path: Path
    code: str
    message: str


@dataclass(frozen=True)
class RepositoryIndex:
    """Read-only view of the fixture vault."""

    by_key: dict[str, PaperRecord]
    aliases: dict[str, str]
    by_id: dict[UUID, PaperRecord]
    invalid: list[InvalidRecord]


def build_index(vault_root: Path) -> RepositoryIndex:
    """Scan ``vault_root`` and return a full repository index."""
    by_key: dict[str, PaperRecord] = {}
    aliases: dict[str, str] = {}
    by_id: dict[UUID, PaperRecord] = {}
    invalid: list[InvalidRecord] = []
    candidates: list[tuple[PaperRecord, list[str]]] = []

    root = literature_root(vault_root)
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            if directory.is_dir() and not directory.name.startswith("."):
                _index_directory(directory, by_key, by_id, invalid, candidates)

    # Stage 2: global alias resolution against every current key. Runs
    # after all keys are known, so the outcome is independent of the
    # scan order.
    current_keys = set(by_key)
    declared: dict[str, list[PaperRecord]] = {}
    for record, aliases_of in candidates:
        for alias in aliases_of:
            declared.setdefault(alias, []).append(record)
    for record, aliases_of in candidates:
        for alias in aliases_of:
            if alias in current_keys:
                invalid.append(
                    InvalidRecord(
                        record.path,
                        "alias_collision",
                        f"alias {alias!r} collides with an existing citation key",
                    )
                )
            elif len(declared[alias]) > 1:
                invalid.append(
                    InvalidRecord(
                        record.path,
                        "alias_collision",
                        f"alias {alias!r} is declared by multiple items",
                    )
                )
            else:
                aliases[alias] = record.paper.citation_key

    return RepositoryIndex(by_key, aliases, by_id, invalid)


def _is_derived_note(name: str) -> bool:
    return name.startswith(DERIVED_NOTE_PREFIXES)


def _index_directory(
    directory: Path,
    by_key: dict[str, PaperRecord],
    by_id: dict[UUID, PaperRecord],
    invalid: list[InvalidRecord],
    candidates: list[tuple[PaperRecord, list[str]]],
) -> None:
    key = directory.name
    main = directory / f"{key}.md"
    if not main.is_file():
        invalid.append(
            InvalidRecord(
                directory,
                "missing_main_note",
                f"no main note {main.name} in paper directory",
            )
        )
        # Extra Markdown can never substitute the canonical main note.
        return
    notes = sorted(
        note for note in directory.glob("*.md") if not _is_derived_note(note.name)
    )
    # The canonical main note is always processed first, before any
    # alphabetically earlier extra file.
    notes = [main] + [note for note in notes if note != main]
    for note in notes:
        _index_note(note, key, by_key, by_id, invalid, candidates)


def _validation_summary(exc: ValidationError) -> str:
    """Structured one-line summary with field paths and error messages."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = str(err.get("msg", ""))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _index_note(
    note: Path,
    key: str,
    by_key: dict[str, PaperRecord],
    by_id: dict[UUID, PaperRecord],
    invalid: list[InvalidRecord],
    candidates: list[tuple[PaperRecord, list[str]]],
) -> None:
    try:
        paper, _ = load_paper_note(note)
    except FrontmatterError as exc:
        invalid.append(InvalidRecord(note, "schema_failure", exc.message))
        return
    except ValidationError as exc:
        invalid.append(InvalidRecord(note, "schema_failure", _validation_summary(exc)))
        return

    if paper.citation_key != key:
        invalid.append(
            InvalidRecord(
                note,
                "key_mismatch",
                f"frontmatter citation_key {paper.citation_key!r} does not match "
                f"directory name {key!r}",
            )
        )
        return

    if paper.citation_key in by_key:
        invalid.append(
            InvalidRecord(
                note,
                "duplicate_key",
                f"citation_key {key!r} already indexed at {by_key[key].path}",
            )
        )
        return

    if paper.paper_id in by_id:
        invalid.append(
            InvalidRecord(
                note,
                "duplicate_uuid",
                f"paper_id {paper.paper_id} already indexed at {by_id[paper.paper_id].path}",
            )
        )
        return

    record = PaperRecord(path=note, paper=paper)

    pdf = note.parent / f"{paper.citation_key}.pdf"
    if paper.pdf_status == "available" and not pdf.is_file():
        invalid.append(
            InvalidRecord(
                note,
                "pdf_status_mismatch",
                f"pdf_status is available but {pdf.name} is missing",
            )
        )
    elif paper.pdf_status == "missing" and pdf.is_file():
        invalid.append(
            InvalidRecord(
                note,
                "pdf_status_mismatch",
                f"pdf_status is missing but {pdf.name} is present",
            )
        )

    by_key[paper.citation_key] = record
    by_id[paper.paper_id] = record
    candidates.append((record, list(paper.citation_key_aliases)))
