"""Canonical item create / show / update operations.

Implements spec §6-§8.1 and §17.3-§17.4 on top of the read-only
repository index, the deterministic metadata merge, the citation-key
allocator, the PDF identifier extractor, and the staged filesystem
transaction layer:

- **Deduplication** (§7.4): an exact strong identifier (DOI / PMID /
  PMCID / arXiv) or an exact PDF SHA-256 matches an existing item ⇒ the
  request becomes an update/attach on that item, never a second record.
  All owners hit by the input strong identifiers + PDF hash are
  collected; 0/1 owners proceed, more than 1 distinct owner is an
  :class:`ItemConflict` (never a silent pick). Fuzzy title/author/year
  similarity only ever returns confirmation candidates
  (``needs_confirmation``); fuzzy-auto-merge never happens.
- **PDF ownership** (§7.5): a supplied PDF is copied into the vault and
  verified by SHA-256; the source file is never moved, deleted, or
  modified; a differing existing primary PDF is never overwritten
  silently (it requires confirmation); a symlink at the primary PDF
  target is never accepted as ``already_attached``.
- **Managed writes** (§8.1, §17.3, §17.4): the merged frontmatter is
  validated against the canonical schema *before* any mutation, writes
  happen under a short-lived workspace write lock through a
  :class:`StagedOperation` with atomic replacement, and every successful
  mutation calls the rebuild hook exactly once. The authoritative
  decision (index rebuild, dedup, fuzzy, key allocation, target
  preflight, identity-consistency check) always happens *after* the
  lock is acquired — never on a stale pre-lock index. Repository
  identity conflicts (duplicate current keys, aliases, or UUIDs) make
  every mutation fail read-only with :class:`ItemConflict`. A failing
  rebuild hook rolls the whole staged operation back (zero partial
  items); no-op outcomes (``duplicate_exists`` / ``already_attached``)
  are not bibliographic mutations and call the hook zero times.
- **Key stability** (§8.2 note): ``item update`` never changes the
  citation key; key renames are a separate command. The explicit
  ``citation_key_for`` call below is the documented contract that an
  existing key is returned unchanged, and is the injection point used by
  acceptance to prove key stability. Identity fields
  (``schema_version`` / ``paper_id`` / ``citation_key`` /
  ``citation_key_aliases``) are immutable through ``item update``.
- **Error boundaries** (§17): lock, staging, hook, and target-type
  failures map onto :class:`ItemError` (user/validation, rc 2) and
  :class:`ItemConflict` (conflicting state, rc 3); arbitrary low-level
  exception text never reaches the JSON envelope.

All operations are pure library functions: they raise :class:`ItemError`
(user/validation problems) and :class:`ItemConflict` (conflicting target
state); the CLI layer maps those onto protocol envelopes.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from . import fsops
from . import pdf as pdf_mod
from .citekeys import AllocationError, allocate_citation_key, citation_key_for
from .frontmatter import FrontmatterError, load_paper_note
from .identifiers import (
    ParsedIdentifier,
    parse_arxiv,
    parse_doi,
    parse_pmid,
    parse_pmcid,
)
from .locking import (
    LockConflict,
    StaleLockError,
    acquire_lock,
    lock_path,
    release_lock,
)
from .metadata import ResolutionError, merge_records, resolve
from .models import Paper
from .paths import is_valid_key, main_note, pdf_attachment
from .pdf import PdfError, extract_pdf_identifiers, sha256_stream
from .repository import PaperRecord, RepositoryIndex, build_index

_RT = YAML(typ="rt")
_RT.width = 4096  # same line width as the frontmatter codec

# Fuzzy similarity gate: a candidate at or above this score forces
# needs_confirmation instead of automatic creation.
_FUZZY_THRESHOLD = 0.5

# Identity fields are immutable through `item update`; only a dedicated
# rename-key operation may change the citation key / aliases.
_FROZEN_UPDATE_FIELDS = (
    "schema_version",
    "paper_id",
    "citation_key",
    "citation_key_aliases",
)

# Lock-operation token for `item rename-key` (paper_notes/citations.py).
# locking.OPERATIONS is a code-defined whitelist in locking.py; the
# rename reuses the closest existing item-operation token. Every item
# mutation happens under the same exclusive workspace write lock, so the
# metadata label is informational only.
RENAME_KEY_LOCK_OPERATION = "update_item"

# Repository-level identity conflicts (spec §17.3): any of these in the
# index forces a read-only error state for every mutation.
_IDENTITY_CONFLICT_CODES = frozenset(
    {"duplicate_key", "duplicate_uuid", "alias_collision"}
)

# CJK / kana runs are tokenized into character bigrams so Unicode titles
# participate in fuzzy similarity (an empty [a-z0-9] token set would
# silently auto-create duplicates).
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]+"
)

# Strong identity fields (spec §7.4): (frontmatter field, identifier
# kind). Every value is canonicalized through the identifiers parsers —
# resolver URLs, ``DOI:``/``PMID:`` prefixes and case differences are
# all equivalent to the bare canonical value.
_STRONG_FIELDS = (
    ("doi", "doi"),
    ("pmid", "pmid"),
    ("pmcid", "pmcid"),
    ("arxiv", "arxiv"),
)
_STRONG_FIELD_NAMES = tuple(field for field, _ in _STRONG_FIELDS)
_FIELD_KIND = dict(_STRONG_FIELDS)
_PARSER_FOR_KIND = {
    "doi": parse_doi,
    "pmid": parse_pmid,
    "pmcid": parse_pmcid,
    "arxiv": parse_arxiv,
}

# PDF fields are derived artifacts of the real attachment, managed only
# by attach/reconcile (Task 12) — never settable through create's
# confirmed/candidate values or through `item update`.
_PDF_MANAGED_FIELDS = ("pdf_status", "pdf_sha256")

# Field order for freshly created notes (spec §6 illustrative order);
# any remaining (extra) fields follow in insertion order.
_FIELD_ORDER = (
    "schema_version",
    "paper_id",
    "citation_key",
    "citation_key_aliases",
    "item_type",
    "title",
    "aliases",
    "authors",
    "journal",
    "journal_abbreviation",
    "publication_date",
    "year",
    "volume",
    "issue",
    "pages",
    "doi",
    "pmid",
    "pmcid",
    "arxiv",
    "url",
    "issn",
    "language",
    "abstract",
    "pdf_status",
    "pdf_sha256",
    "reading_status",
    "archived",
    "created_at",
    "updated_at",
    "metadata_sources",
    "field_provenance",
)


class ItemError(Exception):
    """User/config/validation error; maps to exit code 2."""


class ItemConflict(Exception):
    """Conflicting target state; maps to exit code 3."""


def _noop_rebuild() -> None:
    """Default rebuild hook: generated citation indexes are not part of
    the v0.1 core yet, so the default hook is a no-op that is still
    *called* once on every success (contract stays testable)."""


@dataclass(frozen=True)
class CreateResult:
    """Outcome of :func:`create_item`.

    ``status`` is ``created``, ``attached`` (strong-identifier or
    PDF-hash duplicate handled in place), or ``needs_confirmation``.
    ``action`` names the concrete step taken (``created``,
    ``duplicate_exists``, ``attached_pdf``, ``already_attached``,
    ``replace_primary``, ``confirm_candidates``). For
    ``needs_confirmation``, ``confirmation_token`` and ``plan`` carry
    the machine-readable confirmation contract; ``candidates`` lists
    fuzzy duplicates when present.
    """

    status: str
    action: str
    citation_key: str | None = None
    paper_id: str | None = None
    path: str | None = None
    pdf_sha256: str | None = None
    confirmation_token: str | None = None
    plan: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ShowResult:
    """Outcome of :func:`show_item`; resolves current keys and aliases."""

    citation_key: str
    requested_key: str
    resolved_as: str  # "key" | "alias"
    path: Path
    frontmatter: dict[str, Any]


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of :func:`update_item`; the citation key never changes."""

    status: str
    citation_key: str
    path: str
    updated_fields: list[str]
    frontmatter: dict[str, Any]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def create_item(
    vault_root: str | Path,
    *,
    identifiers: Sequence[ParsedIdentifier] = (),
    pdf: str | Path | None = None,
    confirmed: Mapping[str, Any] | None = None,
    ai: Mapping[str, Any] | None = None,
    adapters: Mapping[str, Any] | None = None,
    rebuild_hook: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> CreateResult:
    """Create a canonical item, or update/attach when an exact duplicate
    of a strong identifier or of the PDF hash already exists.

    ``identifiers`` are parsed strong identifiers (DOI/PMID/PMCID/
    arXiv); ``pdf`` is a local PDF whose hash and embedded identifiers
    participate in deduplication and resolution; ``confirmed`` carries
    user-confirmed values that outrank remote sources. ``confirmed`` is
    validated before any owner fast path: it must be a JSON object,
    pdf_status/pdf_sha256 cannot be fabricated without a real PDF, and
    its strong identifiers are canonicalized and merged with the
    explicit/PDF identifiers in every owner set (spec §7.4) — a
    confirmed cross-ID that belongs to a different item is an
    :class:`ItemConflict`, never a silent duplicate. ``adapters`` maps
    identifier kinds to duck-typed metadata adapters (network is mocked
    in tests). ``rebuild_hook`` is called exactly once on a successful
    mutation (created / attached_pdf), zero times on no-op outcomes and
    failures. The authoritative dedup / fuzzy / key / target preflight
    always runs *after* the write lock is acquired (spec §17.4); the
    pre-lock index is only a resolution fast path.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    now_iso = (now if now is not None else datetime.now().astimezone()).isoformat()

    pdf_sha, pdf_identifiers = _ingest_pdf(pdf)
    # Confirmed metadata is validated BEFORE any owner fast path: a
    # non-object confirmed is a structured error, pdf_status/pdf_sha256
    # cannot be fabricated without a real PDF, and strong values that do
    # not parse are rejected instead of being silently ignored when the
    # fast path would otherwise return duplicate_exists. Legal confirmed
    # strong identifiers join the explicit/PDF identifiers in every
    # owner set (pre-lock fast path and the authoritative in-lock set).
    confirmed_ids = _validate_confirmed(confirmed, pdf is not None)
    explicit_ids = _canonical_identifiers([*identifiers, *pdf_identifiers])
    ids = _canonical_identifiers([*explicit_ids, *confirmed_ids])

    # Pre-lock fast path (hint only): an exact explicit strong
    # identifier, confirmed strong identifier, or PDF-hash owner already
    # exists, so resolution (and the lock) can be skipped — pure
    # duplicates stay offline-safe. The authoritative owner set is
    # re-derived under the lock.
    pre_owners = _strong_owners(build_index(root), ids, pdf_sha)
    if pre_owners:
        return _attach_duplicate(root, ids, pdf_sha, pdf, hook, now_iso)

    # No owner was hit by the locally given identifiers/PDF hash, so the
    # candidate is resolved. Resolution only consults remote adapters
    # for the explicit/PDF identifiers — confirmed strong ids are
    # already locally authoritative and never trigger network calls.
    # Adapter candidate values join the owner set (spec §7.4: every
    # identity source is an owner hit; a candidate cross-ID that belongs
    # to an existing item is dedup/attach, never a second item).
    candidate = _resolve_candidate(explicit_ids, confirmed, ai, adapters, pdf is not None)
    candidate_ids = _strong_ids_from_values(candidate.values)
    all_ids = _canonical_identifiers([*ids, *candidate_ids])

    lock = _acquire(root, "create_item")
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        owners = _strong_owners(index, all_ids, pdf_sha)
        if owners:
            if len(owners) > 1:
                raise ItemConflict(
                    "strong identifiers and PDF hash resolve to multiple "
                    "distinct items; refusing to pick one silently"
                )
            return _attach_locked(root, owners[0], pdf, pdf_sha, hook, now_iso)

        fuzzy = _fuzzy_candidates(candidate.values, index)
        if fuzzy:
            return _needs_confirmation(
                token=_confirmation_token(ids, pdf_sha, confirmed),
                plan={
                    "action": "confirm_candidates",
                    "message": "fuzzy duplicates require confirmation; "
                    "never auto-merged",
                },
                candidates=fuzzy,
                action="confirm_candidates",
            )

        if candidate.confidence == "needs_confirmation":
            return _needs_confirmation(
                token=_confirmation_token(ids, pdf_sha, confirmed),
                plan={
                    "action": "create_with_confirmation",
                    "values": _json_safe(candidate.values),
                    "conflicts": [
                        {"field": c.field, "values": [list(p) for p in c.values]}
                        for c in candidate.conflicts
                    ],
                    "message": "missing critical fields, conflicting values, or "
                    "AI-suggested facts require confirmation",
                },
                candidates=[],
                action="confirm_candidates",
            )

        key = _choose_key(confirmed, candidate.values, index)
        _preflight_create_target(root, key, index, pdf is not None)
        paper = _build_paper(candidate, key, pdf_sha, now_iso)

        _commit_new_item(root, key, paper, pdf, pdf_sha, hook)
        return CreateResult(
            status="created",
            action="created",
            citation_key=key,
            paper_id=str(paper.paper_id),
            path=str(main_note(root, key)),
            pdf_sha256=pdf_sha,
        )
    finally:
        release_lock(lock)


def show_item(vault_root: str | Path, *, key: str) -> ShowResult:
    """Resolve ``key`` (current key or alias) and return the item's
    stored frontmatter. Read-only: no lock, no staged writes, no hook.
    """
    root = Path(vault_root)
    index = build_index(root)
    record, resolved_as = _resolve_record(index, key)
    try:
        paper, doc = load_paper_note(record.path)
    except (FrontmatterError, ValidationError) as exc:
        raise ItemError(f"item {key!r} is no longer readable: {exc}") from exc
    return ShowResult(
        citation_key=paper.citation_key,
        requested_key=key,
        resolved_as=resolved_as,
        path=record.path,
        frontmatter=_json_safe(dict(doc.frontmatter)),
    )


def update_item(
    vault_root: str | Path,
    *,
    key: str,
    patch: Mapping[str, Any],
    rebuild_hook: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> UpdateResult:
    """Apply ``patch`` fields to an item's frontmatter.

    The citation key never changes here (renames are a separate
    command): identity fields (``schema_version`` / ``paper_id`` /
    ``citation_key`` / ``citation_key_aliases``) are rejected up front.
    The Markdown body, comments, and unknown user fields survive
    byte-for-byte because the round-trip document is only re-serialized
    from its ruamel ``CommentedMap``. The item is re-resolved and
    re-loaded *after* the write lock is acquired (spec §17.4), so a
    concurrent metadata change is merged onto the latest on-disk state
    instead of being lost; writes are staged and rolled back on failure.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    now_iso = (now if now is not None else datetime.now().astimezone()).isoformat()

    if not isinstance(patch, Mapping) or not patch:
        raise ItemError("patch must be a non-empty JSON object")
    for field_name in _FROZEN_UPDATE_FIELDS:
        if field_name in patch:
            raise ItemError(
                f"{field_name} cannot be changed via item update; use item rename-key"
            )
    for field_name in _PDF_MANAGED_FIELDS:
        if field_name in patch:
            raise ItemError(
                f"{field_name} is managed by PDF attach/reconcile; "
                "cannot be set via item update"
            )

    lock = _acquire(root, "update_item")
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        record, _ = _resolve_record(index, key)
        note = record.path
        try:
            _, doc = load_paper_note(note)
        except (FrontmatterError, ValidationError) as exc:
            raise ItemError(f"item {key!r} is no longer readable: {exc}") from exc

        for field_name, value in patch.items():
            if value is None:
                doc.frontmatter.pop(field_name, None)
            else:
                doc.frontmatter[field_name] = value

        # Strong fields changed by the patch are canonicalized and, when
        # any changed, the item's full canonical strong-id set is checked
        # against every OTHER item (spec §7.4): a collision is a
        # read-only ItemConflict — bytes untouched, hook never called.
        if any(field in patch for field in _STRONG_FIELD_NAMES):
            wanted = _canonicalize_patched_strong_fields(doc.frontmatter, patch)
            self_id = record.paper.paper_id
            for other_key in sorted(index.by_key):
                other = index.by_key[other_key]
                if other.paper.paper_id == self_id:
                    continue
                if wanted & _paper_strong_ids(other.paper):
                    raise ItemConflict(
                        "updated strong identifier is already owned by a "
                        "different item; strong identifiers are unique "
                        "across the repository"
                    )

        # Key stability: an existing citation key is always returned
        # unchanged by citation_key_for; this call is the documented
        # injection point that acceptance uses to prove update never
        # recalculates the key.
        occupied = set(index.by_key) | set(index.aliases)
        preserved = citation_key_for(
            record.paper.citation_key,
            str(doc.frontmatter.get("title") or ""),
            str(doc.frontmatter.get("year") or ""),
            list(doc.frontmatter.get("authors") or []),
            occupied,
        )
        doc.frontmatter["citation_key"] = preserved
        doc.frontmatter["updated_at"] = now_iso

        try:
            validated = Paper(**dict(doc.frontmatter))
        except ValidationError as exc:
            raise ItemError(
                f"invalid updated metadata: {_validation_summary(exc)}"
            ) from exc

        op: fsops.StagedOperation | None = None
        committed = False
        try:
            op = fsops.begin_operation(root, uuid.uuid4().hex)
            fsops.stage_target(op, note)
            fsops.write_target(
                op, note, _serialize_content(doc.frontmatter, doc.body, doc.newline)
            )
            hook()
            conflicts = fsops.commit(op)
            committed = True
            if conflicts:
                raise ItemConflict(
                    "concurrent change detected while updating item: "
                    + _conflict_summary(conflicts)
                )
        except fsops.OperationConflict:
            if op is not None and not committed:
                fsops.rollback(op)
            raise ItemConflict(
                "concurrent change detected while updating item"
            ) from None
        except ItemError:
            if op is not None and not committed:
                fsops.rollback(op)
            raise
        except ItemConflict:
            if op is not None and not committed:
                fsops.rollback(op)
            raise
        except Exception:
            if op is not None and not committed:
                fsops.rollback(op)
            raise ItemError("item update failed") from None
        except BaseException:
            if op is not None and not committed:
                fsops.rollback(op)
            raise

        return UpdateResult(
            status="updated",
            citation_key=validated.citation_key,
            path=str(note),
            updated_fields=list(patch),
            frontmatter=_json_safe(dict(doc.frontmatter)),
        )
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# lock / index / identity helpers
# ---------------------------------------------------------------------------


def _acquire(root: Path, operation: str) -> Path:
    """Acquire the workspace write lock, mapping lock failures onto the
    structured error hierarchy (spec §17.4, §17 error boundaries)."""
    try:
        return acquire_lock(root, operation)
    except LockConflict as exc:
        raise ItemConflict(f"write lock at {lock_path(root)} is held by another process") from exc
    except StaleLockError as exc:
        raise ItemError(str(exc)) from exc


def _assert_repository_consistent(index: RepositoryIndex) -> None:
    """Spec §17.3: duplicate current keys, aliases, or UUIDs force a
    read-only error state for every mutation."""
    for record in index.invalid:
        if record.code in _IDENTITY_CONFLICT_CODES:
            raise ItemConflict(f"repository identity conflict: {record.message}")


def _conflict_summary(conflicts: Sequence[Path]) -> str:
    return ", ".join(str(path) for path in conflicts)


# ---------------------------------------------------------------------------
# duplicate detection and attach
# ---------------------------------------------------------------------------


def _canonical_identifiers(
    identifiers: Sequence[ParsedIdentifier],
) -> list[ParsedIdentifier]:
    """Re-parse and dedupe identifiers into the canonical vocabulary.

    Every create identity source (explicit ParsedIdentifier, PDF
    extraction, confirmed metadata, adapter candidate values) passes
    through the same identifiers parsers, so resolver URLs, prefixes and
    case differences compare equal. A value that does not parse for its
    declared kind is an :class:`ItemError` — junk never reaches the
    owner set or the written YAML.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ParsedIdentifier] = []
    for identifier in identifiers:
        parser = _PARSER_FOR_KIND.get(identifier.kind)
        if parser is None:
            raise ItemError(f"unknown identifier kind {identifier.kind!r}")
        parsed = parser(identifier.value)
        if parsed is None:
            raise ItemError(
                f"invalid {identifier.kind} identifier {identifier.value!r}"
            )
        key = (parsed.kind, parsed.value)
        if key not in seen:
            seen.add(key)
            out.append(parsed)
    return out


def _strong_ids_from_values(values: Mapping[str, Any]) -> list[ParsedIdentifier]:
    """Parse the strong fields of a confirmed/candidate values mapping.

    A non-empty value that does not parse for its kind is an
    :class:`ItemError`: invalid strong IDs must never be stored as plain
    extra fields. ``None``/empty values contribute nothing.
    """
    out: list[ParsedIdentifier] = []
    for field, kind in _STRONG_FIELDS:
        value = values.get(field)
        if value is None or value == "":
            continue
        parsed = _PARSER_FOR_KIND[kind](str(value))
        if parsed is None:
            raise ItemError(f"invalid {kind} value {value!r} in item metadata")
        out.append(parsed)
    return out


def _validate_confirmed(
    confirmed: Mapping[str, Any] | None, pdf_was_given: bool
) -> list[ParsedIdentifier]:
    """Validate the confirmed mapping BEFORE any owner fast path.

    ``confirmed`` must be a JSON-object-like :class:`Mapping` (anything
    else is a structured :class:`ItemError`, never a raw ``dict()``
    crash). ``pdf_status``/``pdf_sha256`` are derived artifacts of the
    real attachment: without a real PDF they are rejected up front so a
    fast-path duplicate can never silently swallow fabricated PDF
    fields. Strong fields are parsed through the canonical parsers
    (``_strong_ids_from_values``) so junk never reaches the pre-lock
    owner set; the returned canonical identifiers join the explicit/PDF
    identifiers in every owner set.
    """
    if confirmed is None:
        return []
    if not isinstance(confirmed, Mapping):
        raise ItemError("confirmed metadata must be a JSON object")
    if not pdf_was_given:
        for field_name in _PDF_MANAGED_FIELDS:
            if field_name in confirmed:
                raise ItemError(
                    f"{field_name} cannot be set without a real PDF; "
                    "it is managed by PDF attach/reconcile"
                )
    return _strong_ids_from_values(confirmed)


def _paper_strong_ids(paper: Paper) -> set[tuple[str, str]]:
    """Canonical strong ids declared by an indexed item's frontmatter.

    Existing YAML values go through the same parsers as input ids, so a
    URL-form or prefixed DOI/PMID stored by an older writer still
    matches its canonical input form. Values that do not parse are
    skipped (they cannot participate in identity).
    """
    have: set[tuple[str, str]] = set()
    for field, kind in _STRONG_FIELDS:
        value = getattr(paper, field, None)
        if not value:
            continue
        parsed = _PARSER_FOR_KIND[kind](str(value))
        if parsed is not None:
            have.add((parsed.kind, parsed.value))
    return have


def _canonicalize_patched_strong_fields(
    frontmatter: CommentedMap, patch: Mapping[str, Any]
) -> set[tuple[str, str]]:
    """Canonicalize strong fields touched by an update patch and return
    the item's full canonical strong-id set after the patch.

    ``None``/empty removes the field; a non-empty value that does not
    parse for its kind is an :class:`ItemError` (never stored raw).
    """
    for field in _STRONG_FIELD_NAMES:
        if field not in patch:
            continue
        value = frontmatter.get(field)
        if value is None or value == "":
            frontmatter.pop(field, None)
            continue
        parsed = _PARSER_FOR_KIND[_FIELD_KIND[field]](str(value))
        if parsed is None:
            raise ItemError(
                f"invalid {_FIELD_KIND[field]} value {value!r} in updated metadata"
            )
        frontmatter[field] = parsed.value
    wanted: set[tuple[str, str]] = set()
    for field, kind in _STRONG_FIELDS:
        value = frontmatter.get(field)
        if value:
            parsed = _PARSER_FOR_KIND[kind](str(value))
            if parsed is not None:
                wanted.add((parsed.kind, parsed.value))
    return wanted


def _strong_owners(
    index: RepositoryIndex, ids: Sequence[ParsedIdentifier], pdf_sha: str | None
) -> list[PaperRecord]:
    """Every distinct item hit by any input strong identifier or by the
    PDF hash. More than one owner means a split-brain identity; callers
    must treat that as a conflict (never pick one silently)."""
    wanted = {(i.kind, i.value) for i in ids}
    owners: dict[str, PaperRecord] = {}
    for key in sorted(index.by_key):
        record = index.by_key[key]
        if wanted & _paper_strong_ids(record.paper):
            owners[str(record.paper.paper_id)] = record
    if pdf_sha:
        for key in sorted(index.by_key):
            record = index.by_key[key]
            if getattr(record.paper, "pdf_sha256", None) == pdf_sha:
                owners[str(record.paper.paper_id)] = record
    return list(owners.values())


def _attach_duplicate(
    root: Path,
    ids: Sequence[ParsedIdentifier],
    pdf_sha: str | None,
    pdf: str | Path | None,
    hook: Callable[[], None],
    now_iso: str,
) -> CreateResult:
    """Pre-lock entry point for an exact duplicate: re-derive the owner
    set under the lock and attach to it — never a second item."""
    lock = _acquire(root, "create_item")
    try:
        index = build_index(root)
        _assert_repository_consistent(index)
        owners = _strong_owners(index, ids, pdf_sha)
        if len(owners) > 1:
            raise ItemConflict(
                "strong identifiers and PDF hash resolve to multiple "
                "distinct items; refusing to pick one silently"
            )
        if not owners:
            raise ItemConflict(
                "the duplicate item disappeared while attaching; retry"
            )
        return _attach_locked(root, owners[0], pdf, pdf_sha, hook, now_iso)
    finally:
        release_lock(lock)


def _attach_locked(
    root: Path,
    record: PaperRecord,
    pdf: str | Path | None,
    pdf_sha: str | None,
    hook: Callable[[], None],
    now_iso: str,
) -> CreateResult:
    """Attach to an existing item (caller holds the write lock).

    ``duplicate_exists`` (no PDF) and ``already_attached`` (identical
    attachment already in place) are *not* bibliographic mutations: they
    never write and never call the hook. Attaching a new PDF is a real
    mutation (hook exactly once)."""
    key = record.paper.citation_key
    base = {
        "citation_key": key,
        "paper_id": str(record.paper.paper_id),
        "path": str(record.path),
        "pdf_sha256": getattr(record.paper, "pdf_sha256", None),
    }
    if pdf is None:
        return CreateResult(status="attached", action="duplicate_exists", **base)

    target = pdf_attachment(root, key)
    # A symlink (or directory) at the primary PDF target is never
    # accepted as an attachment — read-only conflict, nothing touched.
    if target.is_symlink():
        raise ItemConflict(
            f"primary PDF target {target} is a symlink; refusing to touch it"
        )
    if target.exists() and not _is_real_regular_file(target):
        raise ItemConflict(
            f"primary PDF target {target} exists but is not a regular file"
        )

    # Idempotent fast path: identical attachment already in place.
    if (
        getattr(record.paper, "pdf_sha256", None) == pdf_sha
        and _is_real_regular_file(target)
        and sha256_stream(target) == pdf_sha
    ):
        return CreateResult(status="attached", action="already_attached", **base)

    # A differing existing primary PDF is never overwritten silently.
    if _is_real_regular_file(target) and sha256_stream(target) != pdf_sha:
        return CreateResult(
            status="needs_confirmation",
            action="replace_primary",
            confirmation_token=_confirmation_token([], pdf_sha, {}),
            plan={
                "action": "attach_pdf",
                "citation_key": key,
                "message": "existing primary PDF differs from the supplied "
                "file; replacement requires confirmation",
            },
            **base,
        )

    # The copy + verify + note update + hook + commit transaction is
    # shared with `item attach-pdf` (Task 12): one primary-attachment
    # semantic lives in _primary_pdf_transaction. The decision-time note
    # bytes and target state are passed in so the transaction verifies
    # them at entry (check-to-use guard) instead of accepting whatever
    # the filesystem holds when it runs.
    assert pdf_sha is not None  # a real PDF was ingested above
    _primary_pdf_transaction(
        root,
        key,
        Path(pdf),
        pdf_sha,
        hook,
        now_iso,
        expected_note_sha=_file_sha256(record.path),
        expected_target=_target_state(target),
    )

    return CreateResult(
        status="attached",
        action="attached_pdf",
        citation_key=key,
        paper_id=str(record.paper.paper_id),
        path=str(record.path),
        pdf_sha256=pdf_sha,
    )


def _commit_new_item(
    root: Path,
    key: str,
    paper: Paper,
    pdf: str | Path | None,
    pdf_sha: str | None,
    hook: Callable[[], None],
) -> None:
    """Stage and commit a brand-new item (caller holds the write lock).

    The rebuild hook runs inside the staged operation's failure
    boundary: a failing hook rolls everything back (zero partial items)
    and is called exactly once. A commit that reports conflicts has
    already finished the operation — it surfaces as :class:`ItemConflict`
    and is never rolled back (no ``operation already committed`` leak).
    """
    op: fsops.StagedOperation | None = None
    committed = False
    try:
        op = fsops.begin_operation(root, uuid.uuid4().hex)
        note = main_note(root, key)
        fsops.stage_target(op, note)
        fsops.write_target(op, note, _serialize_new_note(paper))
        if pdf is not None:
            target = pdf_attachment(root, key)
            # Copy + verify shared with `item attach-pdf` (Task 12).
            assert pdf_sha is not None  # a real PDF was ingested above
            _stage_file_copy(op, target, Path(pdf), pdf_sha)
        hook()
        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise ItemConflict(
                "concurrent change detected while creating item: "
                + _conflict_summary(conflicts)
            )
    except fsops.OperationConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemConflict(
            "concurrent change detected while creating item"
        ) from None
    except ItemError:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except ItemConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except Exception:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemError("item creation failed") from None
    except BaseException:
        if op is not None and not committed:
            fsops.rollback(op)
        raise


# ---------------------------------------------------------------------------
# shared attachment transaction primitives (Task 12)
#
# `item attach-pdf` (paper_notes/attachments.py) and the create/duplicate
# PDF paths below share ONE primary-attachment semantic: stage the target,
# copy the source bytes, verify the SHA-256, update the note, fire the
# rebuild hook exactly once, commit — and roll everything back
# byte-identically on any failure.
# ---------------------------------------------------------------------------


def _target_state(path: Path) -> dict:
    """Actual lstat-based state of ``path``: presence, type, content hash.

    Returns ``{"present": bool, "type": "file"|"other"|None, "sha256":
    str|None}``. Symlinks, directories, and other non-regular types are
    ``type="other"`` and are never followed. The content hash is read
    through the ``pdf`` module binding so test injection of the
    attachment-layer ``sha256_stream`` (copy-verification failure) never
    corrupts state observation. Low-level read errors become a
    sanitized :class:`ItemError` — exception text never leaks.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"present": False, "type": None, "sha256": None}
    if stat.S_ISLNK(st.st_mode):
        return {"present": True, "type": "other", "sha256": None}
    if stat.S_ISREG(st.st_mode):
        try:
            return {
                "present": True,
                "type": "file",
                "sha256": pdf_mod.sha256_stream(path),
            }
        except OSError:
            raise ItemError(f"cannot read {path}") from None
    return {"present": True, "type": "other", "sha256": None}


def _file_sha256(path: Path) -> str:
    """Content hash of ``path``; low-level read errors become a
    sanitized :class:`ItemError` (exception text never leaks)."""
    try:
        return pdf_mod.sha256_stream(path)
    except OSError:
        raise ItemError(f"cannot read {path}") from None


def _verify_decision_state(
    root: Path,
    key: str,
    expected_note_sha: str,
    expected_target: dict,
    *,
    note: bool = True,
) -> None:
    """Check-to-use guard: the current note bytes and primary-PDF state
    must still match the state the caller decided / the token authorized.

    Runs before anything is staged; a mismatch is a read-only
    :class:`ItemConflict` with zero writes. The expected values are
    passed in explicitly so a change between the decision and the
    transaction can never be mistaken for the new baseline.
    """
    if _target_state(pdf_attachment(root, key)) != expected_target:
        raise ItemConflict(
            "concurrent change detected: the primary PDF changed since the "
            "decision; nothing was written"
        )
    if note and _file_sha256(main_note(root, key)) != expected_note_sha:
        raise ItemConflict(
            "concurrent change detected: the note changed since the decision; "
            "nothing was written"
        )


def _verify_op_state(
    op: fsops.StagedOperation,
    root: Path,
    key: str,
    *,
    note: bool = True,
    target: bool = True,
) -> None:
    """In-transaction guard: the staged targets must still match the
    transaction's expected (staged or written) fingerprints.

    Raises :class:`fsops.OperationConflict` on mismatch, which the
    transaction handlers convert into an :class:`ItemConflict` after
    rolling back the unfinished operation (the read-only guard is
    re-checked before the hook and before commit).
    """
    if target:
        t = pdf_attachment(root, key)
        if t in op.targets:
            fsops._verify_expected_state(op, t, op.targets[t])
    if note:
        n = main_note(root, key)
        if n in op.targets:
            fsops._verify_expected_state(op, n, op.targets[n])


def _stage_file_copy(
    op: fsops.StagedOperation,
    target: Path,
    source: Path,
    source_sha: str,
    post_stage_guard: Callable[[], None] | None = None,
) -> None:
    """Stage ``target``, copy ``source`` bytes into it, verify the copy.

    The caller's staged operation owns rollback: a hash mismatch raises
    :class:`ItemError` and the transaction removes the copy, so a failed
    copy leaves zero residue. ``post_stage_guard`` (when given) runs
    after the target is staged and immediately before the write —
    closing the decision→stage→write window so an external file that
    appeared since the decision is never overwritten. The source is
    re-verified as a real regular file (never a symlink or directory)
    at the copy entry — topology changes after the initial check fail
    closed; the copy hash verification is kept as the content backstop.
    """
    fsops.stage_target(op, target)
    if post_stage_guard is not None:
        post_stage_guard()
    if not _is_real_regular_file(source):
        raise ItemError(f"source {source} is no longer a real regular file")
    fsops.write_target(op, target, source.read_bytes())
    if sha256_stream(target) != source_sha:
        raise ItemError(
            "PDF copy verification failed: copied file hash does not "
            "match the source"
        )


def _apply_pdf_fields(doc: Any, pdf_sha: str, now_iso: str) -> None:
    """Set the derived PDF fields on a round-trip document."""
    doc.frontmatter["pdf_status"] = "available"
    doc.frontmatter["pdf_sha256"] = pdf_sha
    doc.frontmatter["updated_at"] = now_iso


def _primary_pdf_transaction(
    root: Path,
    key: str,
    source: Path,
    source_sha: str,
    hook: Callable[[], None],
    now_iso: str,
    *,
    expected_note_sha: str,
    expected_target: dict,
) -> None:
    """Copy ``source`` onto ``<key>.pdf`` and update the note atomically.

    Caller holds the write lock. Target and note are staged in ONE
    operation: the copy is verified, the note is re-loaded fresh under
    the lock, patched (``pdf_status=available``, ``pdf_sha256``,
    ``updated_at``), schema-checked before writing, the rebuild hook
    fires exactly once, then the operation commits.

    ``expected_note_sha`` / ``expected_target`` are the note bytes and
    primary-PDF state captured at decision/token-validation time. The
    transaction verifies them at entry (before anything is staged), and
    re-checks the transaction's own expected fingerprints after staging,
    before the hook, and before commit — a change at any point is a
    read-only :class:`ItemConflict` with the unfinished operation rolled
    back. A commit that reports conflicts has already finished and is
    never rolled back.
    """
    target = pdf_attachment(root, key)
    note = main_note(root, key)
    op: fsops.StagedOperation | None = None
    committed = False
    try:
        # entry guard: current state must still be the decided/authorized
        # state (closes the decision → transaction check-to-use window)
        _verify_decision_state(root, key, expected_note_sha, expected_target)
        op = fsops.begin_operation(root, uuid.uuid4().hex)
        _stage_file_copy(
            op,
            target,
            source,
            source_sha,
            post_stage_guard=lambda: _verify_decision_state(
                root, key, expected_note_sha, expected_target
            ),
        )
        fsops.stage_target(op, note)
        _verify_op_state(op, root, key)
        _, doc = load_paper_note(note)
        _apply_pdf_fields(doc, source_sha, now_iso)
        Paper(**dict(doc.frontmatter))  # schema pre-validation before write
        fsops.write_target(
            op, note, _serialize_content(doc.frontmatter, doc.body, doc.newline)
        )
        # read-only guard re-check before the hook: an external change
        # since the write must fail the transaction without firing it
        _verify_op_state(op, root, key)
        hook()
        # read-only guard re-check before commit: an external change must
        # roll back here instead of letting commit finish and surface it
        _verify_op_state(op, root, key)
        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise ItemConflict(
                "concurrent change detected while attaching PDF: "
                + _conflict_summary(conflicts)
            )
    except fsops.OperationConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemConflict(
            "concurrent change detected while attaching PDF"
        ) from None
    except ItemError:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except ItemConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except Exception:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemError("PDF attach failed") from None
    except BaseException:
        if op is not None and not committed:
            fsops.rollback(op)
        raise


def _note_only_transaction(
    root: Path,
    key: str,
    mutator: Callable[[Any], None],
    hook: Callable[[], None],
    *,
    expected_note_sha: str,
    expected_target: dict,
) -> None:
    """Apply ``mutator(doc)`` to a fresh round-trip note and commit.

    Used by attach/reconcile confirmation: the primary PDF file is never
    part of this transaction — only the note's frontmatter changes, and
    only after the caller validated the confirmation token. The note is
    re-loaded fresh under the lock, schema-checked before writing, the
    rebuild hook fires exactly once, and any failure rolls back
    byte-identically.

    ``expected_note_sha`` is the note bytes the token was validated
    against and ``expected_target`` the observed primary-PDF state: both
    are verified at entry and after staging, and the read-only target
    guard is re-checked before the hook and before commit. A stale note
    or a moved target is a read-only :class:`ItemConflict` with zero
    writes; a commit that reports conflicts is never rolled back.
    """
    note = main_note(root, key)
    op: fsops.StagedOperation | None = None
    committed = False
    try:
        _verify_decision_state(root, key, expected_note_sha, expected_target)
        op = fsops.begin_operation(root, uuid.uuid4().hex)
        fsops.stage_target(op, note)
        _verify_decision_state(root, key, expected_note_sha, expected_target)
        _, doc = load_paper_note(note)
        mutator(doc)
        Paper(**dict(doc.frontmatter))  # schema pre-validation before write
        fsops.write_target(
            op, note, _serialize_content(doc.frontmatter, doc.body, doc.newline)
        )
        # read-only target guard + note fingerprint before the hook
        _verify_op_state(op, root, key)
        _verify_decision_state(
            root, key, expected_note_sha, expected_target, note=False
        )
        hook()
        # read-only target guard + note fingerprint before commit
        _verify_op_state(op, root, key)
        _verify_decision_state(
            root, key, expected_note_sha, expected_target, note=False
        )
        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise ItemConflict(
                "concurrent change detected while updating item: "
                + _conflict_summary(conflicts)
            )
    except fsops.OperationConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemConflict(
            "concurrent change detected while updating item"
        ) from None
    except ItemError:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except ItemConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except Exception:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemError("item update failed") from None
    except BaseException:
        if op is not None and not committed:
            fsops.rollback(op)
        raise


# ---------------------------------------------------------------------------
# resolution, keys, and confirmation
# ---------------------------------------------------------------------------


def _ingest_pdf(pdf: str | Path | None) -> tuple[str | None, list[ParsedIdentifier]]:
    if pdf is None:
        return None, []
    pdf_path = Path(pdf)
    try:
        pdf_result = extract_pdf_identifiers(pdf_path)
    except PdfError:
        # low-level PdfError text never enters the ItemError message
        raise ItemError(f"cannot read PDF {pdf_path}") from None
    return pdf_result.sha256, list(pdf_result.identifiers)


def _resolve_candidate(
    ids: Sequence[ParsedIdentifier],
    confirmed: Mapping[str, Any] | None,
    ai: Mapping[str, Any] | None,
    adapters: Mapping[str, Any] | None,
    pdf_was_given: bool,
) -> Any:
    confirmed = dict(confirmed or {})
    if not ids and not confirmed and not pdf_was_given:
        raise ItemError(
            "no identifiers, PDF, or confirmed metadata available to describe the item"
        )
    try:
        if ids:
            return resolve(ids, adapters=adapters, confirmed=confirmed, ai=ai)
        return merge_records([], confirmed=confirmed, ai=ai)
    except ResolutionError as exc:
        raise ItemError(str(exc)) from exc


def _needs_confirmation(
    *,
    token: str,
    plan: dict[str, Any],
    candidates: list[dict[str, Any]],
    action: str,
) -> CreateResult:
    return CreateResult(
        status="needs_confirmation",
        action=action,
        confirmation_token=token,
        plan=plan,
        candidates=candidates,
    )


def _confirmation_token(
    ids: Sequence[ParsedIdentifier],
    pdf_sha: str | None,
    confirmed: Mapping[str, Any] | None,
) -> str:
    payload = {
        "identifiers": sorted((i.kind, i.value) for i in ids),
        "pdf_sha256": pdf_sha,
        "confirmed": _json_safe(dict(confirmed or {})),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _choose_key(
    confirmed: Mapping[str, Any] | None,
    values: Mapping[str, Any],
    index: RepositoryIndex,
) -> str:
    occupied = set(index.by_key) | set(index.aliases)
    explicit = (confirmed or {}).get("citation_key")
    if explicit:
        if explicit in occupied:
            raise ItemConflict(
                f"citation key {explicit!r} already belongs to a different item"
            )
        if not is_valid_key(str(explicit)):
            raise ItemError(f"invalid citation key {explicit!r}")
        return str(explicit)
    try:
        return allocate_citation_key(
            str(values.get("title") or ""),
            str(values.get("year") or ""),
            list(values.get("authors") or []),
            occupied,
        )
    except AllocationError as exc:
        raise ItemError(str(exc)) from exc


def _preflight_create_target(
    root: Path, key: str, index: RepositoryIndex, pdf_was_given: bool
) -> None:
    """Spec §17.4 / target preflight: any pre-existing different target
    for the chosen key (orphan directory, unindexed/corrupt note, a
    symlink or directory where a file belongs) is a read-only conflict —
    nothing is staged or written."""
    directory = root / "05 Literature" / key
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink() or not directory.is_dir():
            raise ItemConflict(
                f"item directory {directory} exists but is not a real directory"
            )
        if key not in index.by_key:
            raise ItemConflict(
                f"item directory {directory} already exists with unindexed "
                "or conflicting content"
            )
        raise ItemConflict(
            f"citation key {key!r} already belongs to an indexed item"
        )
    if pdf_was_given:
        target = pdf_attachment(root, key)
        if target.is_symlink() or (
            target.exists() and not _is_real_regular_file(target)
        ):
            raise ItemConflict(
                f"primary PDF target {target} exists but is not a regular file"
            )


# ---------------------------------------------------------------------------
# fuzzy candidates
# ---------------------------------------------------------------------------


def _title_tokens(text: str) -> set[str]:
    """Unicode-safe title tokenization.

    Latin/numeric words are kept as whole words; CJK / kana runs are
    split into character bigrams so Chinese, Japanese, and Korean titles
    participate in fuzzy similarity instead of silently matching nothing
    (which used to auto-create duplicates)."""
    text = str(text).lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", text):
        tokens.add(word)
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            tokens.add(f"cj:{run}")
        else:
            for index in range(len(run) - 1):
                tokens.add(f"cj:{run[index : index + 2]}")
    return tokens


def _author_surname(author: Any) -> str | None:
    if isinstance(author, dict):
        surname = author.get("family") or author.get("literal")
    else:
        surname = getattr(author, "family", None) or getattr(author, "literal", None)
    if not surname:
        return None
    return str(surname).strip().lower() or None


def _first_author_surname(authors: Any) -> str | None:
    if not authors:
        return None
    return _author_surname(authors[0])


def _fuzzy_score(
    tokens: set[str], year: Any, author: str | None, paper: Paper
) -> float:
    paper_tokens = _title_tokens(paper.title)
    if tokens and paper_tokens:
        score = len(tokens & paper_tokens) / len(tokens | paper_tokens)
    else:
        score = 0.0
    paper_author = _first_author_surname(paper.authors)
    if author and paper_author and author == paper_author:
        score += 0.25
    if year is not None and paper.year is not None:
        try:
            if int(year) == int(paper.year):
                score += 0.25
        except (TypeError, ValueError):
            pass
    return score


def _fuzzy_candidates(values: Mapping[str, Any], index: RepositoryIndex) -> list[dict[str, Any]]:
    title = values.get("title")
    if not title:
        return []
    tokens = _title_tokens(str(title))
    if not tokens:
        return []
    year = values.get("year")
    author = _first_author_surname(values.get("authors"))
    candidates: list[dict[str, Any]] = []
    for key in sorted(index.by_key):
        paper = index.by_key[key].paper
        score = _fuzzy_score(tokens, year, author, paper)
        if score >= _FUZZY_THRESHOLD:
            candidates.append(
                {
                    "citation_key": key,
                    "title": paper.title,
                    "year": paper.year,
                    "first_author": _first_author_surname(paper.authors),
                    "similarity": round(score, 3),
                }
            )
    return candidates


# ---------------------------------------------------------------------------
# schema pre-validation and note serialization
# ---------------------------------------------------------------------------


def _build_paper(
    candidate: Any, key: str, pdf_sha: str | None, now_iso: str
) -> Paper:
    values = dict(candidate.values)
    provenance = dict(candidate.field_provenance)
    values.update(
        {
            "schema_version": 1,
            "paper_id": uuid.uuid4(),
            "citation_key": key,
            "citation_key_aliases": [],
            "reading_status": "unread",
            "archived": False,
            "created_at": now_iso,
            "updated_at": now_iso,
            "metadata_sources": sorted(set(provenance.values())),
            "field_provenance": provenance,
        }
    )
    # A confirmed/sourced legal item_type is preserved (e.g. preprint);
    # only a missing one defaults to article-journal. Anything invalid is
    # rejected by the Paper Literal validation below.
    values.setdefault("item_type", "article-journal")
    # Strong fields are written in canonical form only (invalid non-empty
    # values were already rejected by _strong_ids_from_values).
    for parsed in _strong_ids_from_values(values):
        values[parsed.kind] = parsed.value
    # pdf_status/pdf_sha256 are derived from the real artifact. A
    # PDF-backed create is overwritten by the real extraction result; a
    # metadata-only create may never fabricate them.
    if pdf_sha:
        values["pdf_status"] = "available"
        values["pdf_sha256"] = pdf_sha
    else:
        for field_name in _PDF_MANAGED_FIELDS:
            if field_name in values:
                raise ItemError(
                    f"{field_name} cannot be set without a real PDF; "
                    "it is managed by PDF attach/reconcile"
                )
        values["pdf_status"] = "missing"
        values.pop("pdf_sha256", None)
    try:
        return Paper(**values)
    except ValidationError as exc:
        raise ItemError(f"invalid item metadata: {_validation_summary(exc)}") from exc


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = str(err.get("msg", ""))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _yaml_safe(value: Any) -> Any:
    """Convert non-YAML-native values (UUID, dates) to plain scalars."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _yaml_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_yaml_safe(v) for v in value]
    return value


def _json_safe(value: Any) -> Any:
    """Recursively normalize a value for JSON envelopes (human mode too)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _is_real_regular_file(path: Path) -> bool:
    """True only for a real regular file — symlinks are never followed."""
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _serialize_content(
    frontmatter: CommentedMap, body: str = "", newline: str = "\n"
) -> str:
    """Serialize a round-trip document the same way the frontmatter
    codec does: ruamel dump of the CommentedMap, exact body, restored
    newline style. The Markdown body is never parsed or reformatted."""
    buf = io.StringIO()
    _RT.dump(frontmatter, buf)
    content = f"---\n{buf.getvalue()}---\n{body}"
    if newline == "\r\n":
        content = content.replace("\n", "\r\n")
    return content


def _serialize_new_note(paper: Paper) -> str:
    """Serialize a freshly created main note in spec §6 field order."""
    data = _yaml_safe(paper.model_dump())
    ordered: CommentedMap = CommentedMap()
    for key in _FIELD_ORDER:
        if key in data:
            ordered[key] = data[key]
    for key in data:
        if key not in ordered:
            ordered[key] = data[key]
    return _serialize_content(ordered, body="", newline="\n")


def _resolve_record(index: RepositoryIndex, key: str) -> tuple[PaperRecord, str]:
    record = index.by_key.get(key)
    if record is not None:
        return record, "key"
    canonical = index.aliases.get(key)
    if canonical is not None:
        return index.by_key[canonical], "alias"
    raise ItemError(f"no item found for key or alias {key!r}")
