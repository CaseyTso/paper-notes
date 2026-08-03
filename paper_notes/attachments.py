"""Primary / supplementary PDF attachment and reconcile operations.

Implements spec §5/§7.5/§8.1 and §17.1-§17.4 for attaching files to an
existing canonical item and for repairing ``pdf_status`` /
``pdf_sha256`` metadata against the actual primary PDF:

- **Primary attach** (§7.5): a supplied PDF is copied to
  ``<key>.pdf`` and SHA-256 verified; the source file is never moved,
  deleted, or modified; the note is updated atomically
  (``pdf_status=available``, ``pdf_sha256`` = the verified hash).
  An identical attachment already in place with matching metadata is a
  strict no-op (zero writes, hook 0); a matching file with mismatched
  metadata is never silently corrected — it requires confirmation and
  applies the same metadata-only fix as ``item reconcile``. A
  *differing* existing primary is never overwritten: preview returns a
  ``needs_confirmation`` contract, and only the matching
  ``confirm_token`` executes the replacement (still copy-verified,
  hook exactly once, full rollback on failure).
- **Supplementary attach**: any regular file enters
  ``<item>/attachments/`` under a safe basename. Directories, symlink
  sources, symlinked target paths, and out-of-bounds targets are
  refused. Same content already present (under any name) is idempotent
  (hook 0, zero writes); same name with different content never
  overwrites — a deterministic hash suffix is allocated. Existing
  bytes are never changed.
- **Reconcile** (§17.2): preview only *observes* the actual canonical
  PDF (presence / type / hash) against the YAML fields and returns a
  deterministic token plus machine-readable ``before`` / ``after`` /
  ``plan`` — zero writes, zero placeholders, zero downloads, hook 0.
  Only the correct ``confirm_token`` updates the YAML
  (present ⇒ ``available`` + actual hash, absent ⇒ ``missing`` and the
  hash is cleared); the actual PDF is never modified or deleted. An
  already-consistent item returns ``no_changes`` with no mutation.
- **Confirmation tokens** bind ``paper_id``, the canonical citation
  key, the source hash, the current target hash *and type*, and the
  note bytes hash. Tokens are recomputed under the lock at confirm
  time; any change to the source, target, or note makes the token
  stale and turns the confirm into a read-only
  :class:`~paper_notes.items.ItemConflict` (rc 3).

Every authoritative decision (index, resolution, target preflight,
idempotency, token validation) happens *after* the write lock is
acquired (spec §17.4). Held locks map to :class:`ItemConflict`,
stale locks to :class:`ItemError`. Low-level exception text never
leaks into the JSON envelope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import fsops
from .items import (
    ItemConflict,
    ItemError,
    _acquire,
    _apply_pdf_fields,
    _assert_repository_consistent,
    _is_real_regular_file,
    _noop_rebuild,
    _note_only_transaction,
    _primary_pdf_transaction,
    _resolve_record,
    _stage_file_copy,
)
from .paths import attachments_directory, paper_directory, pdf_attachment
from .pdf import PdfError, extract_pdf_identifiers, sha256_stream
from .repository import build_index

# Attach-pdf results share one shape with ``needs_confirmation``
# outcomes; ``status`` distinguishes the two.


@dataclass(frozen=True)
class AttachResult:
    """Outcome of :func:`attach_pdf`.

    ``status`` is ``attached`` or ``needs_confirmation``; ``action``
    names the concrete step (``attached_primary``,
    ``already_attached``, ``replaced_primary``, ``reconciled_metadata``,
    ``replace_primary``, ``reconcile_metadata``, ``attached_supplement``,
    ``already_present``). For ``needs_confirmation``,
    ``confirmation_token`` and ``plan`` carry the machine-readable
    contract. ``sha256`` is the verified source/content hash and
    ``target`` the file path involved (written or already present).
    """

    status: str
    action: str
    citation_key: str
    paper_id: str
    path: str
    sha256: str | None = None
    target: str | None = None
    confirmation_token: str | None = None
    plan: dict | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of :func:`reconcile`.

    ``status`` is ``no_changes``, ``needs_confirmation``, or
    ``reconciled``; ``before`` / ``after`` are machine-readable
    ``{"pdf_status": ..., "pdf_sha256": ...}`` pairs; ``plan`` and
    ``confirmation_token`` exist only on the confirmation path.
    """

    status: str
    action: str
    citation_key: str
    paper_id: str
    path: str
    before: dict
    after: dict
    confirmation_token: str | None = None
    plan: dict | None = None


def attach_pdf(
    vault_root: str | Path,
    *,
    key: str,
    file: str | Path,
    supplementary: bool = False,
    confirm_token: str | None = None,
    rebuild_hook=None,
    now: datetime | None = None,
) -> AttachResult:
    """Attach ``file`` to the item resolved by ``key`` (key or alias).

    ``supplementary=False`` (primary): the file must be a real, valid
    PDF; it is copied to ``<key>.pdf`` and verified, and the note's
    ``pdf_status`` / ``pdf_sha256`` are updated atomically. A differing
    existing primary (or a matching file with mismatched metadata) is
    never changed without ``confirm_token``.
    ``supplementary=True``: any real regular file is copied into
    ``<item>/attachments/``; the note is never touched.
    ``rebuild_hook`` is called exactly once on a successful mutation,
    zero times on no-ops, previews, and failures.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    now_iso = (now if now is not None else datetime.now().astimezone()).isoformat()

    # Supplementary attach has no confirmation flow: any token is an
    # explicit user error (rc 2), rejected before any read or write.
    if supplementary and confirm_token is not None:
        raise ItemError(
            "supplementary attach has no confirmation flow; "
            "--confirm-token is not accepted for --supplementary"
        )

    source = Path(file)
    if source.is_symlink():
        raise ItemError(
            f"source {source} is a symlink; only a real regular file can be attached"
        )
    if not source.is_file():
        raise ItemError(f"source {source} is not a regular file")

    if supplementary:
        try:
            source_sha = sha256_stream(source)
        except OSError:
            # low-level exception text never enters the user-facing error
            raise ItemError(f"cannot read source {source}") from None
    else:
        # Primary attaches accept only valid PDFs; the extraction also
        # provides the attachment digest (and is only ever read).
        try:
            source_sha = extract_pdf_identifiers(source).sha256
        except PdfError:
            raise ItemError(f"source {source} is not a valid PDF") from None

    lock = _acquire(root, "attach_pdf")
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        record, _ = _resolve_record(index, key)
        if supplementary:
            return _attach_supplement_locked(
                root, record, source, source_sha, hook, now_iso
            )
        return _attach_primary_locked(
            root, record, source, source_sha, hook, now_iso, confirm_token
        )
    finally:
        from .locking import release_lock

        release_lock(lock)


def reconcile(
    vault_root: str | Path,
    *,
    key: str,
    confirm_token: str | None = None,
    rebuild_hook=None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Observe the actual primary PDF against the YAML fields (preview),
    or apply the proposed correction when ``confirm_token`` matches.

    Preview is read-only: zero writes, zero placeholders, zero
    downloads, hook 0. The confirmation path updates *only* the note
    (``pdf_status`` / ``pdf_sha256`` / ``updated_at``) under a staged
    transaction with the rebuild hook exactly once; the actual PDF is
    never modified or deleted. A stale token — note bytes or the actual
    target state changed since the preview — is a read-only
    :class:`ItemConflict`.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    now_iso = (now if now is not None else datetime.now().astimezone()).isoformat()

    lock = _acquire(root, "reconcile")
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        record, _ = _resolve_record(index, key)
        paper = record.paper
        canonical = paper.citation_key
        note = record.path
        target = pdf_attachment(root, canonical)
        actual = _observe_target(target)
        before = {
            "pdf_status": paper.pdf_status,
            "pdf_sha256": getattr(paper, "pdf_sha256", None),
        }
        after, warning = _propose_after(actual)

        if after == before:
            # Already consistent: a confirm token arriving here is by
            # definition stale (the preview that issued it observed a
            # mismatch); never mutate, never conflict silently.
            if confirm_token is not None:
                raise ItemConflict(
                    "confirmation token is stale: the note or the primary PDF "
                    "changed since the preview; re-run item reconcile"
                )
            return ReconcileResult(
                status="no_changes",
                action="consistent",
                citation_key=canonical,
                paper_id=str(paper.paper_id),
                path=str(note),
                before=before,
                after=after,
            )

        note_sha = _file_sha256(note)
        token = _reconcile_token(paper, note_sha, actual, before, after)
        plan: dict = {
            "action": "reconcile_pdf_status",
            "citation_key": canonical,
            "message": "pdf_status/pdf_sha256 disagree with the actual primary "
            "PDF; confirmation updates metadata only",
            "actual": actual,
        }
        if warning:
            plan["warning"] = warning

        if confirm_token is None:
            return ReconcileResult(
                status="needs_confirmation",
                action="reconcile_pdf_status",
                citation_key=canonical,
                paper_id=str(paper.paper_id),
                path=str(note),
                before=before,
                after=after,
                confirmation_token=token,
                plan=plan,
            )

        if not hmac.compare_digest(str(confirm_token), token):
            raise ItemConflict(
                "confirmation token is stale: the note or the primary PDF "
                "changed since the preview; re-run item reconcile"
            )
        _note_only_transaction(
            root,
            canonical,
            lambda doc: _apply_reconcile_fields(doc, after, now_iso),
            hook,
            expected_note_sha=note_sha,
            expected_target=actual,
        )
        return ReconcileResult(
            status="reconciled",
            action="reconciled",
            citation_key=canonical,
            paper_id=str(paper.paper_id),
            path=str(note),
            before=before,
            after=after,
        )
    finally:
        from .locking import release_lock

        release_lock(lock)


# ---------------------------------------------------------------------------
# primary attach
# ---------------------------------------------------------------------------


def _attach_primary_locked(
    root: Path,
    record,
    source: Path,
    source_sha: str,
    hook,
    now_iso: str,
    confirm_token: str | None,
) -> AttachResult:
    """Primary attach under the write lock (authoritative decisions).

    Token-first: when ``confirm_token`` is given, the CURRENT
    authoritative note/source/target state is validated against the
    token before any branch is chosen. A state that needs no
    confirmation — an already-attached file or a fresh absent target —
    can never match an issued token (tokens are only issued for states
    that require confirmation), so the confirm is a stale read-only
    :class:`ItemConflict` with zero writes and hook 0. Only the exact
    state the token was issued for proceeds, executing precisely the
    transition the token authorizes.
    """
    paper = record.paper
    key = paper.citation_key
    target = pdf_attachment(root, key)
    base = {
        "citation_key": key,
        "paper_id": str(paper.paper_id),
        "path": str(record.path),
        "sha256": source_sha,
        "target": str(target),
    }

    _preflight_primary_target(root, key, target)

    actual = _observe_target(target)
    target_sha = actual["sha256"] if actual["type"] == "file" else None
    note_sha = _file_sha256(record.path)
    yaml_matches = paper.pdf_status == "available" and getattr(
        paper, "pdf_sha256", None
    ) == source_sha

    if confirm_token is not None:
        # Token-first: validate the token against the current state
        # before selecting the ordinary / idempotent / fresh branch.
        if target_sha == source_sha:
            if yaml_matches:
                # already attached: this state needs no confirmation, so
                # any token arriving here is stale by construction
                raise ItemConflict(_STALE_TOKEN_MESSAGE)
            op = "reconcile_metadata"
        elif target_sha is not None:
            op = "replace_primary"
        else:
            # fresh absent target: needs no confirmation, so any token
            # arriving here is stale by construction
            raise ItemConflict(_STALE_TOKEN_MESSAGE)
        expected = _replace_token(paper, source_sha, actual, note_sha, op)
        if not hmac.compare_digest(str(confirm_token), expected):
            raise ItemConflict(_STALE_TOKEN_MESSAGE)
        # the token authorizes exactly this transition
        if op == "reconcile_metadata":
            _note_only_transaction(
                root,
                key,
                lambda doc: _apply_pdf_fields(doc, source_sha, now_iso),
                hook,
                expected_note_sha=note_sha,
                expected_target=actual,
            )
            return AttachResult(
                status="attached", action="reconciled_metadata", **base
            )
        _primary_pdf_transaction(
            root,
            key,
            source,
            source_sha,
            hook,
            now_iso,
            expected_note_sha=note_sha,
            expected_target=actual,
        )
        return AttachResult(status="attached", action="attached_primary", **base)

    if target_sha == source_sha:
        # The file already matches the supplied source.
        if yaml_matches:
            return AttachResult(status="attached", action="already_attached", **base)
        # Metadata mismatch: never corrected silently. The confirmation
        # applies the same metadata-only fix as `item reconcile`.
        return AttachResult(
            status="needs_confirmation",
            action="reconcile_metadata",
            confirmation_token=_replace_token(
                paper, source_sha, actual, note_sha, "reconcile_metadata"
            ),
            plan={
                "action": "reconcile_metadata",
                "message": "primary PDF already matches the supplied file but "
                "pdf_status/pdf_sha256 disagree; confirmation applies the same "
                "metadata fix as item reconcile (the file is untouched)",
                "target": str(target),
            },
            **base,
        )

    if target_sha is not None:
        # A differing existing primary is never overwritten silently.
        return AttachResult(
            status="needs_confirmation",
            action="replace_primary",
            confirmation_token=_replace_token(
                paper, source_sha, actual, note_sha, "replace_primary"
            ),
            plan={
                "action": "attach_pdf",
                "citation_key": key,
                "message": "existing primary PDF differs from the supplied "
                "file; replacement requires confirmation",
                "existing_sha256": target_sha,
                "incoming_sha256": source_sha,
                "target": str(target),
            },
            **base,
        )

    # Fresh attach: the target does not exist yet.
    _primary_pdf_transaction(
        root,
        key,
        source,
        source_sha,
        hook,
        now_iso,
        expected_note_sha=note_sha,
        expected_target=actual,
    )
    return AttachResult(status="attached", action="attached_primary", **base)


def _preflight_primary_target(root: Path, key: str, target: Path) -> None:
    """Read-only target preflight (spec §17.2/§17.3): symlinks and
    non-regular files at the primary target or the item directory are
    conflicts — nothing is staged or written."""
    item_dir = paper_directory(root, key)
    if item_dir.is_symlink() or not item_dir.is_dir():
        raise ItemConflict(
            f"item directory {item_dir} is not a real directory; refusing to touch it"
        )
    if target.is_symlink():
        raise ItemConflict(
            f"primary PDF target {target} is a symlink; refusing to touch it"
        )
    if target.exists() and not _is_real_regular_file(target):
        raise ItemConflict(
            f"primary PDF target {target} exists but is not a regular file"
        )


# ---------------------------------------------------------------------------
# supplementary attach
# ---------------------------------------------------------------------------


def _attach_supplement_locked(
    root: Path, record, source: Path, source_sha: str, hook, now_iso: str
) -> AttachResult:
    """Supplementary attach under the write lock."""
    key = record.paper.citation_key
    item_dir = paper_directory(root, key)
    if item_dir.is_symlink() or not item_dir.is_dir():
        raise ItemConflict(
            f"item directory {item_dir} is not a real directory; refusing to touch it"
        )
    att_dir = attachments_directory(root, key)
    if att_dir.is_symlink():
        raise ItemConflict(
            f"attachments directory {att_dir} is a symlink; refusing to touch it"
        )
    if att_dir.exists() and not att_dir.is_dir():
        raise ItemConflict(
            f"attachments directory {att_dir} exists but is not a directory"
        )

    name = source.name
    if not _safe_basename(name):
        raise ItemError(f"unsafe attachment filename {name!r}")

    base = {
        "citation_key": key,
        "paper_id": str(record.paper.paper_id),
        "path": str(record.path),
        "sha256": source_sha,
    }

    # Same content already present under ANY name is idempotent (hook 0,
    # zero writes) — a duplicate copy is never created.
    existing = _find_existing_content(att_dir, source_sha)
    if existing is not None:
        return AttachResult(
            status="attached", action="already_present", target=str(existing), **base
        )

    target = att_dir / name
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not _is_real_regular_file(target):
            raise ItemConflict(
                f"attachment target {target} exists but is not a regular file"
            )
        # Same name, different content: never overwrite — allocate a
        # deterministic content-hash suffix.
        stem, dot, ext = name.rpartition(".")
        if dot:
            suffixed_name = f"{stem}.{source_sha[:16]}{dot}{ext}"
        else:
            suffixed_name = f"{name}.{source_sha[:16]}"
        target = att_dir / suffixed_name
        if target.exists() or target.is_symlink():
            if (
                target.is_symlink()
                or not _is_real_regular_file(target)
                or sha256_stream(target) != source_sha
            ):
                raise ItemConflict(
                    f"attachment target {target} is occupied; refusing to overwrite"
                )
            return AttachResult(
                status="attached", action="already_present", target=str(target), **base
            )

    op: fsops.StagedOperation | None = None
    committed = False
    try:
        op = fsops.begin_operation(root, uuid.uuid4().hex)
        # Snapshot the decided target first, then re-decide on a FRESH
        # authoritative scan: a file that appeared since the pre-scan
        # must never become the transaction's overwritable baseline.
        fsops.stage_target(op, target)
        fresh = _find_existing_content(att_dir, source_sha)
        if fresh is not None:
            # identical content arrived under this or another name: no
            # duplicate copy is created — idempotent, zero writes, hook 0
            fsops.rollback(op)
            op = None
            return AttachResult(
                status="attached",
                action="already_present",
                target=str(fresh),
                **base,
            )
        if target.exists() or target.is_symlink():
            # the decided target is now occupied by DIFFERENT content
            # (identical content would have matched the scan above):
            # preserve the external file, read-only conflict, zero writes
            fsops.rollback(op)
            op = None
            raise ItemConflict(
                f"attachment target {target} appeared with different content; "
                "refusing to overwrite"
            )
        _stage_file_copy(op, target, source, source_sha)
        hook()
        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise ItemConflict(
                "concurrent change detected while attaching file: "
                + ", ".join(str(path) for path in conflicts)
            )
    except fsops.OperationConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise ItemConflict(
            "concurrent change detected while attaching file"
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
        raise ItemError("attachment copy failed") from None
    except BaseException:
        if op is not None and not committed:
            fsops.rollback(op)
        raise

    return AttachResult(
        status="attached", action="attached_supplement", target=str(target), **base
    )


def _find_existing_content(att_dir: Path, source_sha: str) -> Path | None:
    """The first real regular file in ``att_dir`` with content hash
    ``source_sha``, or None. Symlinks and non-regular files are never
    considered present."""
    if not att_dir.is_dir():
        return None
    for child in sorted(att_dir.iterdir()):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            if sha256_stream(child) == source_sha:
                return child
        except OSError:
            continue
    return None


def _safe_basename(name: str) -> bool:
    """True for a single safe path component (no traversal, separators,
    or NUL)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return True


# ---------------------------------------------------------------------------
# confirmation tokens
# ---------------------------------------------------------------------------


_STALE_TOKEN_MESSAGE = (
    "confirmation token is stale: the source file, primary PDF, or note "
    "changed since the preview; re-run item attach-pdf"
)


def _replace_token(
    paper, source_sha: str, target_state: dict, note_sha: str, op: str
) -> str:
    """Deterministic token binding paper_id, canonical key, source hash,
    the current target state (presence, type, hash), and note bytes.

    ``target_state`` is the :func:`_observe_target` shape — including
    explicit absence — so confirm-time recomputation against the CURRENT
    state always yields a comparable token: any change to the source,
    target, or note makes it stale.
    """
    payload = {
        "op": op,
        "paper_id": str(paper.paper_id),
        "citation_key": paper.citation_key,
        "source_sha256": source_sha,
        "target": target_state,
        "note_sha256": note_sha,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _reconcile_token(paper, note_sha: str, actual: dict, before: dict, after: dict) -> str:
    """Deterministic token binding paper_id, canonical key, note bytes,
    the actual target state, and the proposed before/after pair."""
    payload = {
        "op": "reconcile",
        "paper_id": str(paper.paper_id),
        "citation_key": paper.citation_key,
        "note_sha256": note_sha,
        "actual": actual,
        "before": before,
        "after": after,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return sha256_stream(path)
    except OSError:
        # low-level exception text never enters the user-facing error
        raise ItemError(f"cannot read {path}") from None


# ---------------------------------------------------------------------------
# reconcile observation helpers
# ---------------------------------------------------------------------------


def _observe_target(target: Path) -> dict:
    """Actual state of the canonical primary PDF: presence, type, hash.

    Symlinks and directories are ``type="other"`` — never treated as a
    usable PDF and never touched by reconcile.
    """
    try:
        st = target.lstat()
    except FileNotFoundError:
        return {"present": False, "type": None, "sha256": None}
    if stat.S_ISLNK(st.st_mode):
        return {"present": True, "type": "other", "sha256": None}
    if stat.S_ISREG(st.st_mode):
        try:
            return {"present": True, "type": "file", "sha256": sha256_stream(target)}
        except OSError:
            # low-level exception text never enters the user-facing error
            raise ItemError(f"cannot read primary PDF {target}") from None
    return {"present": True, "type": "other", "sha256": None}


def _propose_after(actual: dict) -> tuple[dict, str | None]:
    """The metadata correction the actual state implies, plus an
    optional warning. ``after == before`` means already consistent."""
    if actual["present"] and actual["type"] == "file":
        return {"pdf_status": "available", "pdf_sha256": actual["sha256"]}, None
    if actual["present"]:
        return (
            {"pdf_status": "missing", "pdf_sha256": None},
            "primary PDF path is occupied by a non-regular file; reconcile "
            "updates metadata only and never touches that file",
        )
    return {"pdf_status": "missing", "pdf_sha256": None}, None


def _apply_reconcile_fields(doc, after: dict, now_iso: str) -> None:
    """Apply a reconcile proposal to a fresh round-trip doc."""
    if after["pdf_status"] == "available":
        doc.frontmatter["pdf_status"] = "available"
        doc.frontmatter["pdf_sha256"] = after["pdf_sha256"]
    else:
        doc.frontmatter["pdf_status"] = "missing"
        doc.frontmatter.pop("pdf_sha256", None)
    doc.frontmatter["updated_at"] = now_iso
