"""Confirmed permanent item deletion (spec §8.3, plan Task 14).

``preview_delete`` is read-only: it resolves ``key`` (current key or
alias) to a canonical paper, validates every identity gate, scans the
whole vault with the parser-aware line scanner, manifests the whole
item subtree, and returns the deletion plan (file count / total bytes /
item-external Pandoc citation and wikilink backlinks / warnings) plus a
deterministic confirmation token. No lock, zero writes, hook 0.

``confirm_delete`` re-resolves and re-plans **under the workspace write
lock** (operation ``delete_item``) and recomputes the token: any
subtree add/delete/edit/chmod/type/symlink change, any empty-directory
change, or any global markdown candidate add/delete/edit/type change
since the preview makes the presented token stale and turns the confirm
into a read-only :class:`ItemConflict` with zero writes and hook 0.
The user must additionally pass the canonical current citation key
exactly (``confirm_key``, char-for-char: aliases, case differences and
surrounding whitespace are rejected as user errors, rc2, zero writes).

Execution is one all-or-nothing transaction on the same filesystem:

1. the exact global markdown scan-input manifest and the exact item
   subtree manifest are re-verified before any mutation;
2. the item directory ``05 Literature/<key>/`` is atomically renamed
   (no-replace) to a hidden same-filesystem work directory
   ``.<key>.delete-work`` so the canonical path never exposes a
   half-deleted item;
3. the mapped subtree manifest and the global markdown set are
   re-verified with the item hidden;
4. every subtree regular file is staged (snapshot into the staging
   area, expected state verified before and after the stage) and
   removed through the managed delete, so rollback restores exact
   bytes, permission modes, and topology;
5. every subtree directory (including empty and hidden ones) is
   removed deepest-first, ending with the work directory itself;
6. post-verify (fresh index: the key, its aliases and the paper_id no
   longer resolve, the canonical directory is gone) runs **before**
   the commit verdict: the real ``fsops.commit`` conflict detection
   runs against a proxy operation (shared targets, throwaway staging
   directory), so the recovery material survives the verdict and a
   racer that reappeared at a deleted path is a conflict — each racer
   is preserved untouched at a named
   ``.paper-notes/recovery/<operation>/`` location and the staged
   deletion rolls back completely (hook zero times);
6.5 every deleted path is re-checked right after the clean verdict and
   before the participant's finalize seam: a racer reappearing in that
   window is the same explicit conflict (hook zero times, item restored
   byte-for-byte, racer preserved). The recovery area is resolved with
   lstat boundary checks on every path component (never through a
   symlink, never outside the vault) and existing recovery material is
   never overwritten, deleted or rewritten — a racer colliding with
   pre-seeded recovery material is preserved at a fresh in-vault
   location instead;
6.75 index publication, the final authority and the success transition
   are merged into the transaction participant (``IndexParticipant``):
   the full expected-state authority runs again inside ``finalize`` —
   a hidden work-target racer injected at the finalize seam is the
   same explicit conflict with the hook zero times — then the legacy
   rebuild hook fires exactly once INSIDE the transaction, then a
   second full authority detects any edit/chmod/type swap the hook
   performed on a transaction target and rolls the item AND both index
   files back, and only then is the operation finished — the
   deletion's linearization point: after the last expected-state
   authority no writable callback runs and nothing can roll the
   effective deletion back;
7. the rebuild hook fires exactly once, and only when every authority
   is clean, so every rollback path fires the hook zero times (a
   hook-time divergence fires it exactly once — the deletion itself is
   never reported 'deleted');
8. the transaction is then finalized — only a real in-vault staging
   directory is removed (boundary re-check, never through a symlink) —
   and the staged operation finished. Cleanup after the linearization
   point is best-effort: a failure, a silent no-op or leftover residue
   is reported as the structured ``deleted_with_cleanup_required``
   status (with the desensitized operation id, the exact
   vault-relative residue paths and idempotent retry-cleanup guidance)
   — never a silent ``deleted`` — and the effective deletion is never
   rolled back. :func:`retry_cleanup` is the idempotent, vault-bound,
   symlink-safe retry seam for that residue.

On any failure the staged operation is rolled back, any removed
directories are recreated with their recorded modes, and the restored
item is moved back to the canonical path with no-replace semantics (a
concurrent directory that appeared there is preserved in place and
reported explicitly). A commit conflict is a failure too: the racer
never replaces the deletion baseline, never disappears, and is never
silently written over — it is moved to a named recovery location whose
path the conflict message reports, and the canonical item is restored
byte-for-byte.

The scanner reuses the parser-aware state machine from
:mod:`paper_notes.citations`: fenced code blocks, inline code spans,
indented code, HTML ``<pre>``/``<code>`` regions, backslash-escaped
literals, and every YAML frontmatter block are never reported as
backlinks. Only ``.md`` files outside the deleted item subtree are
scanned; hidden directories, the ``.paper-notes`` transaction area,
and symlinks are never followed; unreadable files are a structured
conflict.

Low-level exception text and the confirmation key never reach
:class:`ItemError` / :class:`ItemConflict` messages (the CLI JSON
envelope is therefore free of secrets).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import fsops
from .citations import (
    FileState,
    _file_state,
    _global_md_manifest,
    _link_state,
    _read_text,
    _scan_item_subtree,
    _stage_expected,
    _verify_subtree,
    scan_markdown,
    start_of_body,
)
from .items import (
    ItemConflict,
    ItemError,
    _acquire,
    _assert_repository_consistent,
    _noop_rebuild,
    _resolve_record,
)
from .locking import LOCK_DIR, release_lock, validate_operation_id
from .paths import paper_directory
from .repository import build_index

DELETE_LOCK_OPERATION = "delete_item"

_STALE_TOKEN_MESSAGE = (
    "confirmation token is stale: the vault changed since the preview; "
    "re-run item delete --dry-run"
)

_CLEANUP_RETRY_GUIDANCE = (
    "the item deletion and both index publications succeeded, but "
    "transaction material remains under .paper-notes/.staging/"
    "{operation_id}; the deleted item and published indexes are final "
    "and will not be rolled back — run "
    "paper_notes.deletion.retry_cleanup(vault, operation_id={operation_id!r}) "
    "(idempotent) or remove the listed residue manually after confirming "
    "no write is in progress"
)

_WORK_SUFFIX = ".delete-work"

_RECOVERY_SUBDIR = "recovery"


# ---------------------------------------------------------------------------
# backlink scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeleteOccurrence:
    path: Path
    kind: str  # "pandoc" | "wikilink"
    line: int  # 1-based file line
    column: int  # 1-based file column
    context: str


def _scan_backlinks(
    root: Path, item_dir: Path, key: str
) -> tuple[list[DeleteOccurrence], list[FileState], list[str]]:
    """Parser-aware scan of every markdown file OUTSIDE the item subtree.

    The item subtree is never scanned (its content is deleted with the
    item). Hidden directories/files, the ``.paper-notes`` area and
    symlinked directories (never descended) are skipped; symlinked
    ``.md`` files are never read but bound by their raw link identity
    so a later retarget/type/mode change makes the token stale.
    Non-UTF-8 / malformed-frontmatter files are skipped with a warning
    but bound by their exact bytes; unreadable files are a structured
    conflict.
    """
    occurrences: list[DeleteOccurrence] = []
    files: list[FileState] = []
    warnings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep: list[str] = []
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            if d.startswith("."):
                continue
            if p.is_relative_to(item_dir):
                continue
            try:
                st = p.lstat()
            except OSError:
                raise ItemConflict(f"cannot inspect {p}") from None
            if stat.S_ISLNK(st.st_mode):
                if d.endswith(".md"):
                    warnings.append(f"skipped symlink {p}")
                    files.append(_link_state(p))
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_relative_to(item_dir):
                continue
            if path.is_symlink():
                warnings.append(f"skipped symlink {path}")
                files.append(_link_state(path))
                continue
            try:
                st = path.stat()
            except OSError:
                raise ItemConflict(f"cannot inspect {path}") from None
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                text = _read_text(path)
            except UnicodeDecodeError:
                warnings.append(f"skipped non-UTF-8 markdown file {path}")
                files.append(_file_state(path))
                continue
            except OSError:
                raise ItemConflict(f"cannot read {path}") from None
            hits = scan_markdown(text, key)
            if start_of_body(text) == -1:
                warnings.append(f"skipped malformed frontmatter in {path}")
                files.append(_file_state(path))
                continue
            files.append(_file_state(path))
            if not hits:
                continue
            text_lines = text.split("\n")
            for hit in hits:
                occurrences.append(
                    DeleteOccurrence(
                        path=path,
                        kind=hit.kind,
                        line=hit.line + 1,
                        column=hit.column + 1,
                        context=text_lines[hit.line],
                    )
                )
    return occurrences, files, warnings


# ---------------------------------------------------------------------------
# plan data model
# ---------------------------------------------------------------------------


@dataclass
class DeletePlan:
    paper_id: str
    key: str  # canonical current key
    requested_key: str
    resolved_as: str  # "key" | "alias"
    file_count: int
    total_bytes: int
    occurrences: list[DeleteOccurrence]
    warnings: list[str]
    files: list[FileState]  # global markdown candidates (subtree excluded)
    subtree: list[FileState]  # whole item subtree (token binding)
    expected: dict[Path, tuple]  # subtree file fingerprints at plan time


@dataclass(frozen=True)
class DeletePreview:
    status: str
    action: str
    paper_id: str
    citation_key: str  # canonical current key
    requested_key: str
    resolved_as: str
    file_count: int
    total_bytes: int
    occurrences: list[DeleteOccurrence]
    warnings: list[str]
    confirmation_token: str
    plan: dict[str, Any]


@dataclass(frozen=True)
class DeleteResult:
    status: str
    action: str
    paper_id: str
    citation_key: str
    path: str
    file_count: int
    total_bytes: int
    occurrences: list[DeleteOccurrence]
    warnings: list[str]
    # R5: recovery metadata for the post-linearization cleanup. The
    # operation id is a generated, non-sensitive value; ``residue``
    # holds exact vault-relative (desensitized) paths; ``retry`` is
    # idempotent retry-cleanup guidance. All three are empty on a
    # fully cleaned ``deleted`` result.
    operation_id: str = ""
    residue: tuple[str, ...] = ()
    retry: str = ""


@dataclass(frozen=True)
class CleanupOutcome:
    """Outcome of one :func:`retry_cleanup` attempt."""

    operation_id: str
    residue: tuple[str, ...]  # still present after this attempt (vault-relative)
    cleaned: bool  # True when nothing remains


# ---------------------------------------------------------------------------
# plan construction (read-only)
# ---------------------------------------------------------------------------


def _build_plan(
    root: Path, record: Any, requested_key: str, resolved_as: str
) -> DeletePlan:
    """Construct the full deletion plan (read-only; every collision gate
    blocks here, before any write)."""
    key = record.paper.citation_key
    paper_id = str(record.paper.paper_id)
    item_dir = paper_directory(root, key)

    # canonical-directory topology: must be a real directory
    if item_dir.is_symlink() or not item_dir.is_dir():
        raise ItemConflict(
            f"item directory {item_dir} is not a real directory; refusing to touch it"
        )

    # the whole item subtree is bound to the token: every regular file
    # (type/hash/mode), every directory (type/mode, including empty and
    # hidden ones); symlinks / other types are a structured preview
    # conflict (never silently deleted)
    subtree = _scan_item_subtree(item_dir)

    file_count = 0
    total_bytes = 0
    expected: dict[Path, tuple] = {}
    for state in subtree:
        if state.type != "file":
            continue
        file_count += 1
        try:
            total_bytes += state.path.stat().st_size
        except OSError:
            raise ItemConflict(
                "concurrent change detected while planning; nothing was written"
            ) from None
        expected[state.path] = fsops._file_fingerprint(state.path)

    occurrences, files, warnings = _scan_backlinks(root, item_dir, key)
    occurrences = sorted(
        occurrences, key=lambda o: (str(o.path), o.line, o.column, o.kind)
    )
    files = sorted(files, key=lambda f: str(f.path))

    return DeletePlan(
        paper_id=paper_id,
        key=key,
        requested_key=requested_key,
        resolved_as=resolved_as,
        file_count=file_count,
        total_bytes=total_bytes,
        occurrences=occurrences,
        warnings=warnings,
        files=files,
        subtree=subtree,
        expected=expected,
    )


# ---------------------------------------------------------------------------
# token / preview / confirm
# ---------------------------------------------------------------------------


def _delete_token(plan: DeletePlan) -> str:
    payload = {
        "paper_id": plan.paper_id,
        "key": plan.key,
        "file_count": plan.file_count,
        "total_bytes": plan.total_bytes,
        "occurrences": sorted(
            (
                {"path": str(o.path), "kind": o.kind, "line": o.line, "column": o.column}
                for o in plan.occurrences
            ),
            key=lambda d: (d["path"], d["line"], d["column"], d["kind"]),
        ),
        "files": sorted(
            (
                {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
                for f in plan.files
            ),
            key=lambda d: d["path"],
        ),
        "subtree": sorted(
            (
                {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
                for f in plan.subtree
            ),
            key=lambda d: d["path"],
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _plan_json(plan: DeletePlan, token: str) -> dict[str, Any]:
    return {
        "action": "delete",
        "paper_id": plan.paper_id,
        "citation_key": plan.key,
        "requested_key": plan.requested_key,
        "resolved_as": plan.resolved_as,
        "file_count": plan.file_count,
        "total_bytes": plan.total_bytes,
        "occurrences": [
            {
                "path": str(o.path),
                "kind": o.kind,
                "line": o.line,
                "column": o.column,
                "context": o.context,
            }
            for o in plan.occurrences
        ],
        "warnings": list(plan.warnings),
        "files": [
            {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
            for f in plan.files
        ],
        "subtree": [
            {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
            for f in plan.subtree
        ],
        "confirmation_token": token,
    }


def preview_delete(vault_root: str | Path, *, key: str) -> DeletePreview:
    """Read-only dry-run: resolve, validate, scan, and return the plan
    plus a deterministic confirmation token. No lock, zero writes,
    hook 0."""
    root = Path(vault_root)
    index = build_index(root)
    _assert_repository_consistent(index)
    record, resolved_as = _resolve_record(index, key)
    plan = _build_plan(root, record, key, resolved_as)
    token = _delete_token(plan)
    return DeletePreview(
        status="needs_confirmation",
        action="delete",
        paper_id=plan.paper_id,
        citation_key=plan.key,
        requested_key=plan.requested_key,
        resolved_as=plan.resolved_as,
        file_count=plan.file_count,
        total_bytes=plan.total_bytes,
        occurrences=plan.occurrences,
        warnings=plan.warnings,
        confirmation_token=token,
        plan=_plan_json(plan, token),
    )


def confirm_delete(
    vault_root: str | Path,
    *,
    key: str,
    confirm_key: str,
    confirm_token: str,
    rebuild_hook: Callable[[], None] | None = None,
) -> DeleteResult:
    """Execute the token-authorized permanent deletion under the
    workspace write lock.

    The authoritative index, plan, and token are recomputed under the
    lock; any change since the preview makes the token stale (read-only
    :class:`ItemConflict`, zero writes, hook 0). ``confirm_key`` must
    equal the canonical current citation key char-for-char (aliases,
    case differences and surrounding whitespace are rejected as a user
    error, zero writes, hook 0). The rebuild hook fires exactly once on
    success and zero times on preview/failure; a commit conflict (an
    external racer reappearing at a deleted path) fires it zero times,
    restores the whole item byte-for-byte, and preserves the racer at a
    named ``.paper-notes/recovery/`` location reported by the conflict.

    Once the second final authority inside the participant passed (the
    deletion's linearization point) the item deletion and both index
    publications are effective and are never rolled back: a staging
    cleanup that fails, silently no-ops or leaves residue surfaces as
    the structured ``deleted_with_cleanup_required`` result (with the
    desensitized operation id, exact vault-relative residue paths and
    idempotent retry-cleanup guidance) instead of a silent
    ``deleted``; :func:`retry_cleanup` is the retry seam.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    lock = _acquire(root, DELETE_LOCK_OPERATION)
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        record, resolved_as = _resolve_record(index, key)
        if confirm_key != record.paper.citation_key:
            # never echo the confirmation text back
            raise ItemError(
                "confirmation key does not match the canonical citation key "
                "for this item; re-run item delete --dry-run"
            )
        plan = _build_plan(root, record, key, resolved_as)
        token = _delete_token(plan)
        if not hmac.compare_digest(str(confirm_token), token):
            raise ItemConflict(_STALE_TOKEN_MESSAGE)
        operation_id, residue = _execute(root, plan, hook)
        if residue:
            # The deletion and both index publications are effective
            # (the linearization point already passed) but leftover
            # transaction material remains: never report a silent
            # 'deleted' — surface the structured cleanup-required
            # status with the desensitized operation id, the exact
            # vault-relative residue paths and idempotent
            # retry-cleanup guidance.
            return DeleteResult(
                status="deleted_with_cleanup_required",
                action="delete",
                paper_id=plan.paper_id,
                citation_key=plan.key,
                path=str(paper_directory(root, plan.key)),
                file_count=plan.file_count,
                total_bytes=plan.total_bytes,
                occurrences=plan.occurrences,
                warnings=plan.warnings,
                operation_id=operation_id,
                residue=residue,
                retry=_CLEANUP_RETRY_GUIDANCE.format(operation_id=operation_id),
            )
        return DeleteResult(
            status="deleted",
            action="delete",
            paper_id=plan.paper_id,
            citation_key=plan.key,
            path=str(paper_directory(root, plan.key)),
            file_count=plan.file_count,
            total_bytes=plan.total_bytes,
            occurrences=plan.occurrences,
            warnings=plan.warnings,
            operation_id=operation_id,
        )
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# execution transaction
# ---------------------------------------------------------------------------


def _work_directory(item_dir: Path) -> Path:
    """Hidden, same-filesystem work directory the item is moved into
    before being removed, so the canonical path never exposes a
    half-deleted item. The name is deterministic per item and unique in
    practice because every deletion runs under the exclusive workspace
    write lock; a stale leftover fails the entry rename closed (EEXIST)
    instead of being reused."""
    return item_dir.parent / f".{item_dir.name}{_WORK_SUFFIX}"


def _verify_global_scan(plan: DeletePlan, root: Path, item_root: Path) -> None:
    """Execution-time guard: the live vault markdown scan-input set
    (complete candidate path set plus lstat type/hash/mode, symlink
    identity without following) must equal the plan-time snapshot in
    ``plan.files``. The item subtree is excluded from both sides (it is
    bound independently by ``plan.subtree`` and currently lives at
    ``item_root``). Any addition, deletion, edit, chmod, or
    file<->dir/symlink type change conflicts read-only with the
    external state preserved at its exact path."""
    expected = {state.path: state for state in plan.files}
    current = _global_md_manifest(root, item_root)
    for exp_path, state in expected.items():
        cur = current.pop(exp_path, None)
        if cur is None or (cur.type, cur.sha256, cur.mode) != (
            state.type,
            state.sha256,
            state.mode,
        ):
            raise ItemConflict(
                "concurrent change detected: a markdown file changed since "
                "the plan; nothing was written"
            )
    if current:
        raise ItemConflict(
            "concurrent change detected: a markdown file appeared since "
            "the plan; nothing was written"
        )


def _post_verify(root: Path, plan: DeletePlan) -> None:
    """Fresh-index verification of the deletion result; runs before the
    hook so a failed verify never fired the rebuild. Raises on any
    mismatch (the transaction rolls back)."""
    index = build_index(root)
    _assert_repository_consistent(index)
    for record in index.by_key.values():
        if str(record.paper.paper_id) == plan.paper_id:
            raise ItemError("post-verify failed: the deleted paper still resolves")
    if plan.key in index.by_key:
        raise ItemError("post-verify failed: the deleted citation key still resolves")
    if any(canonical == plan.key for canonical in index.aliases.values()):
        raise ItemError("post-verify failed: the deleted key is still a reserved alias")
    if paper_directory(root, plan.key).exists() or paper_directory(
        root, plan.key
    ).is_symlink():
        raise ItemError("post-verify failed: the item directory is still present")


def _abort(
    op: fsops.StagedOperation | None,
    committed: bool,
    phase: str,
    plan: DeletePlan,
    item_dir: Path,
    work_dir: Path,
    participant: "IndexParticipant | None" = None,
) -> None:
    """Roll back the staged operation and restore the item directory
    according to the phase, using no-replace renames only. Directories
    already removed by the deletion loop are recreated (shallowest
    first) with their recorded modes before the staged files are
    restored into them. The citation-index participant is aborted
    BEFORE the staged operation is rolled back: an index file edited
    externally is moved to the recovery area and restored from its
    staged backup first (``fsops.rollback`` alone would preserve it as
    an unrecoverable conflict). Nested rollback/restore failures are
    sanitized; a refused restore (racer at the original path) surfaces
    as its own ItemConflict.

    A committed operation is never rolled back: commit already
    finalized the deletion (the staged snapshots were consumed), so the
    item stays deleted and any racer at a deleted path stays preserved
    untouched — nothing is restored, and the caller's conflict is what
    surfaces.
    """
    if committed:
        return
    try:
        if phase == "work":
            for state in sorted(
                (s for s in plan.subtree if s.type == "dir"),
                key=lambda s: len(s.path.parts),
            ):
                p = work_dir / state.path.relative_to(item_dir)
                if not p.exists() and not p.is_symlink():
                    p.mkdir(parents=True, exist_ok=True)
                    # directory FileStates always carry their lstat mode
                    os.chmod(p, state.mode if state.mode is not None else 0o755)
        if op is not None and not committed:
            if participant is not None:
                participant.abort(op)
            fsops.rollback(op)
        if phase == "work":
            assert op is not None  # the work phase only runs after the op exists
            try:
                _rename_dir_noreplace(
                    work_dir, item_dir, vault_root=op.vault_root
                )
            except ItemConflict:
                raise ItemConflict(
                    "concurrent change detected while deleting item: "
                    f"a conflicting directory appeared at the original item "
                    f"path {item_dir}; it was preserved and the item remains "
                    "in the hidden work directory"
                ) from None
    except ItemConflict:
        raise
    except Exception:
        raise ItemError("item deletion failed while rolling back") from None


def _rename_dir_noreplace(source: Path, target: Path, *, vault_root: Path) -> None:
    """dirfd-anchored atomic no-replace directory move (R3 primitive).

    Delegates to :func:`paper_notes.fsops.no_replace_move` (dirfd
    anchored, ``renameatx_np RENAME_EXCL``): both parents are anchored
    as O_NOFOLLOW directory fds opened component by component from the
    vault root, so a parent path swapped for an outside symlink after
    the last path check can never redirect the rename outside the
    vault. An existing target (file, directory or symlink) is never
    replaced — :class:`MoveTargetExists` surfaces as an
    :class:`ItemConflict` so the caller can preserve the racer; a
    missing / wrong-type source or an unsupported platform surfaces as
    :class:`ItemError` via the generic handler.
    """
    try:
        fsops.no_replace_move(
            source, target, vault_root=vault_root, source_kind="dir"
        )
    except fsops.MoveTargetExists:
        raise ItemConflict(
            "concurrent change detected: target appeared before the rename"
        ) from None


def _commit_verdict(op: fsops.StagedOperation) -> list[Path]:
    """Run the real ``fsops.commit`` conflict verdict without losing the
    recovery material.

    ``fsops.commit`` both detects conflicts and destroys the staging
    directory, so a conflict discovered there could never be rolled
    back. The verdict therefore runs against a proxy operation that
    shares the real targets (the commit only reads fingerprints from
    them) but points at a throwaway staging directory that never
    exists: the real staging snapshots stay intact, the conflict list
    is authoritative (identical detection to a real commit), and the
    staged deletion can still be rolled back completely. A clean
    verdict is finalized by the caller (the recovery material is then
    discarded); a conflicted verdict rolls back with every pre-delete
    original file restored.
    """
    proxy = fsops.StagedOperation(
        vault_root=op.vault_root,
        operation_id=op.operation_id,
        directory=op.directory.parent / f"{op.operation_id}.verdict",
        targets=op.targets,
    )
    return fsops.commit(proxy)


def _real_dir_chain(p: Path, vault: Path) -> bool:
    """True when every existing component from ``vault`` down to ``p``
    is a real (lstat) directory.

    Symlinks and non-directories anywhere in the chain fail closed —
    the path is never resolved through them. Missing components below
    an existing real directory are fine (they are created one level at
    a time and re-verified by :func:`_ensure_real_dir_chain`)."""
    cur = p
    while True:
        if cur == vault:
            return True
        if cur == cur.parent:  # filesystem root, still not the vault
            return False
        try:
            st = cur.lstat()
        except FileNotFoundError:
            cur = cur.parent
            continue
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
        cur = cur.parent


def _ensure_real_dir_chain(p: Path, vault: Path) -> Path | None:
    """Ensure ``p`` is a real directory inside the vault, creating
    missing components one level at a time.

    Every existing component is lstat-verified (never a symlink, never
    a non-directory) and every created component is re-verified right
    after creation, so a symlink planted at any depth can neither
    redirect the subsequent write outside the vault nor be silently
    followed. Returns ``p`` when the whole chain is real, else None."""
    existing = p
    missing: list[Path] = []
    while True:
        if existing == vault:
            break
        if existing == existing.parent:
            return None  # outside the vault
        try:
            existing.lstat()
        except FileNotFoundError:
            missing.append(existing)
            existing = existing.parent
            continue
        except OSError:
            return None
        break
    if not _real_dir_chain(existing, vault):
        return None
    for comp in reversed(missing):
        try:
            os.mkdir(comp)
        except FileExistsError:
            pass  # appeared between the check and the create: re-verify
        except OSError:
            return None
        try:
            st = comp.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return None
    return p


def _recovery_candidates(op: fsops.StagedOperation, rel: Path):
    """Yield boundary-checked, no-replace recovery destinations.

    Tries the nominal ``.paper-notes/recovery/<operation>/<rel>`` and,
    when any existing component of that path is a symlink or a
    non-directory (vault escape) or the destination itself already
    exists (pre-seeded recovery material that must never be
    overwritten, deleted or rewritten), falls back to fresh real
    directories under the recovery area (then directly under
    ``.paper-notes``). Each candidate is re-verified right before it is
    returned; the caller re-checks on every retry (a destination that
    appeared after the last check conflicts and the next candidate is
    taken). The racer is therefore always preserved inside the vault
    and the outside directory receives zero writes.
    """
    vault = op.vault_root
    base = vault / LOCK_DIR / _RECOVERY_SUBDIR
    candidates: list[Path] = [base / op.operation_id]
    candidates += [
        base / f"{op.operation_id}.recovery-{i}" for i in range(1, 65)
    ]
    candidates += [
        vault / LOCK_DIR / f"{op.operation_id}.recovery-{i}" for i in range(1, 65)
    ]
    for root in candidates:
        dest = root / rel
        if dest.exists() or dest.is_symlink():
            continue  # never replace existing recovery material
        if _ensure_real_dir_chain(dest.parent, vault) is None:
            continue  # symlink / non-directory in the chain: never follow
        if dest.exists() or dest.is_symlink():
            continue  # re-verify immediately before use
        yield dest


def _remove_staging_dir(directory: Path, vault: Path) -> None:
    """Best-effort removal of a staging directory.

    Only a real directory chain from the vault root down is removed
    (lstat boundary re-check, never through a symlink, never outside
    the vault): a symlink or non-directory planted anywhere in the
    chain is left untouched and reported by :func:`_staging_residue`
    instead. Removal failures are swallowed — the residue check that
    follows is the source of truth (a failed deletion of transaction
    material must never fail an already-effective deletion)."""
    if _real_dir_chain(directory, vault):
        shutil.rmtree(directory, ignore_errors=True)


def _staging_residue(directory: Path, vault: Path) -> tuple[str, ...]:
    """Vault-relative, desensitized residue paths still present at the
    staging location after a best-effort cleanup attempt.

    ``()`` means the staging area is fully cleaned. A real directory,
    a symlink or any other type still sitting at the staging path is
    reported as residue; when the staging directory itself is gone but
    its ``.staging`` parent is no longer a real directory (e.g. a
    symlink planted there), the parent is reported instead — cleanup
    never follows or deletes through it. The operator (or
    :func:`retry_cleanup`) decides how to remove the residue; the
    deleted item and the published indexes are final and are never
    rolled back because of leftover transaction material."""
    if directory.exists() or directory.is_symlink():
        return (str(directory.relative_to(vault)),)
    parent = directory.parent
    if not _real_dir_chain(parent, vault) and (
        parent.exists() or parent.is_symlink()
    ):
        return (str(parent.relative_to(vault)),)
    return ()


def _remove_staging(op: fsops.StagedOperation) -> None:
    """Finalize the staging directory only when it is a real directory
    inside the vault (lstat boundary re-check, never through a
    symlink): a symlink planted at the staging path can neither make
    the finalize delete anything outside the vault nor make us write
    outside it. A compromised staging path is simply left untouched
    and is reported as residue by the check that follows."""
    _remove_staging_dir(op.directory, op.vault_root)


def retry_cleanup(vault_root: str | Path, *, operation_id: str) -> CleanupOutcome:
    """Idempotent, vault-bound, symlink-safe cleanup retry for a
    deletion reported as ``deleted_with_cleanup_required``.

    Removes the leftover staging directory
    ``.paper-notes/.staging/<operation_id>`` only when every path
    component from the vault root down is a real directory (lstat,
    never through a symlink, never outside the vault): a symlink or
    non-directory planted at the staging path or its ``.staging``
    parent is left untouched and reported as residue — nothing
    external is ever written, deleted or overwritten. Re-running is a
    safe no-op when nothing remains (the same ``cleaned`` outcome).
    The already-effective deletion and the published indexes are never
    rolled back or rewritten by this function.

    ``operation_id`` must be a validated safe path component
    (``[a-z0-9][a-z0-9_.-]{0,31}``, no ``..``); anything else raises
    :class:`ValueError` before any filesystem access. The real CLI
    command wiring for this seam is deferred to a later explicit task.
    """
    root = Path(vault_root)
    validate_operation_id(operation_id)
    directory = fsops.staging_directory(root, operation_id)
    try:
        _remove_staging_dir(directory, root)
    except Exception:
        pass  # best-effort: residue is reported, never raised
    residue = _staging_residue(directory, root)
    return CleanupOutcome(
        operation_id=operation_id, residue=residue, cleaned=not residue
    )


def _preserve_racers(
    op: fsops.StagedOperation, conflicts: list[Path], work_dir: Path
) -> list[tuple[Path, Path]]:
    """Move every external racer that reappeared at a deleted path to a
    named recovery location (``.paper-notes/recovery/<operation>/`` by
    default, same filesystem, atomic no-replace) so the staged deletion
    can roll back to the exact pre-delete item while the racer survives
    untouched with its exact bytes. Returns ``(racer_path, recovery)``
    pairs that the conflict message reports explicitly — the racer is
    never overwritten, deleted, or silently adopted as the deletion
    baseline.

    The move itself is the dirfd-anchored atomic no-replace primitive
    (``paper_notes.fsops.no_replace_move``): the destination is
    re-verified right before the rename and an existing destination —
    file, directory or symlink, including one that appeared after the
    last check — is never replaced; the next candidate recovery path is
    taken instead (seed bytes stay untouched, the racer lands at a
    fresh in-vault path). A recovery parent swapped to an outside
    symlink after the last check receives zero outside writes. Racer
    directories are moved whole; racer files are preserved
    deepest-first so a file keeps its flat named recovery path even
    when a containing directory reappeared too.
    """
    recovered: list[tuple[Path, Path]] = []
    for target in sorted(
        conflicts, key=lambda p: (len(p.parts), str(p)), reverse=True
    ):
        if not target.exists() and not target.is_symlink():
            continue  # vanished again (moved with a preserved directory)
        if target == work_dir:
            rel = Path(work_dir.name)
        elif work_dir in target.parents:
            rel = target.relative_to(work_dir)
        else:
            # an index target (e.g. .paper-notes/library.json): keep
            # the flat file name under the recovery area
            rel = Path(target.name)
        moved = False
        for dest in _recovery_candidates(op, rel):
            try:
                fsops.no_replace_move(
                    target, dest, vault_root=op.vault_root, source_kind="any"
                )
            except (fsops.MoveTargetExists, fsops.NoReplaceMoveError):
                continue  # destination appeared / parent compromised: next one
            recovered.append((target, dest))
            moved = True
            break
        if not moved:
            raise ItemConflict(
                "concurrent change detected while deleting item: the recovery "
                "area cannot be secured, so the racer was left in place"
            )
    return recovered


def _raise_conflicts(
    op: fsops.StagedOperation, conflicts: list[Path], work_dir: Path
) -> None:
    """Preserve every conflict racer and raise the explicit conflict.

    Every racer that reappeared at a deleted path is moved to a named
    in-vault recovery location (boundary-checked, never replaced,
    never written outside the vault); the conflict message reports each
    ``path -> recovery`` pair explicitly. A clean list returns without
    raising, so this is safe to use as the single conflict exit of the
    final authorities.
    """
    if not conflicts:
        return
    recovered = _preserve_racers(op, conflicts, work_dir)
    if recovered:
        detail = "; ".join(
            f"{path} reappeared (racer preserved at {dest})"
            for path, dest in recovered
        )
    else:
        detail = ", ".join(str(path) for path in conflicts)
    raise ItemConflict("concurrent change detected while deleting item: " + detail)


def _final_conflicts(
    op: fsops.StagedOperation,
    plan: DeletePlan,
    item_dir: Path,
    work_dir: Path,
) -> list[Path]:
    """Final authority: the complete expected-state detection over the
    whole transaction.

    Every staged target must still match its expected fingerprint —
    deleted paths must still be absent, both index writes must still be
    exactly the managed bytes+mode — and every removed subtree
    directory must still be absent. Anything else is an external racer
    (or an escape) that must be preserved and reported; the hook fires
    zero times and the whole transaction (item + both index files)
    rolls back. This is the plain ``rebuild_hook`` success window and
    runs as the authority inside
    :meth:`IndexParticipant.finalize` — before the legacy hook and
    again right after it, so a racer appearing at either boundary (the
    finalize seam or the hook's own write window) is detected.
    """
    conflicts = fsops.commit_detect(op)
    for state in plan.subtree:
        if state.type != "dir":
            continue
        p = work_dir / state.path.relative_to(item_dir)
        if p.exists() or p.is_symlink():
            conflicts.append(p)
    return sorted(set(conflicts), key=str)


class IndexParticipant:
    """Minimal transactional citation-index writer (Task 15 seam).

    Publishes the post-deletion citation index as part of the same
    transaction as the item deletion: ``prepare`` reads
    ``.paper-notes/library.json`` and ``.paper-notes/citation-aliases.json``
    (when both exist — a vault without the index pair keeps the
    pre-R3 deletion semantics), computes the state without the deleted
    key / aliases / paper_id, and stages both files as managed targets;
    ``commit`` writes both new states as staged managed writes (each
    expected-state-guarded, so an external edit / chmod / type swap
    between the two outputs conflicts and rolls everything back);
    ``finalize`` is the merged final authority + legacy hook + success
    transition: after the clean final authority the legacy rebuild
    hook runs exactly once INSIDE the transaction, a second full
    authority detects any edit/chmod/type swap the hook performed on a
    transaction target, and only then is the operation finished — the
    deletion's linearization point, after which nothing can roll the
    effective deletion back. Staging cleanup after that point is
    best-effort: a failure or residue is returned (and surfaced by
    :func:`confirm_delete` as ``deleted_with_cleanup_required``), never
    a rollback of the effective deletion. A racer or hook divergence at
    any pre-linearization point rolls the item AND both index files
    back to their exact bytes+mode with the hook zero times (a
    hook-time divergence fires it exactly once — the deletion itself is
    never reported 'deleted'). The real ``library.json`` /
    ``citation-aliases.json``
    writer (Task 15) plugs in behind this seam.
    """

    def __init__(self, root: Path, plan: DeletePlan):
        self._root = root
        self._plan = plan
        self._lib = root / LOCK_DIR / "library.json"
        self._aliases = root / LOCK_DIR / "citation-aliases.json"
        self._op: fsops.StagedOperation | None = None
        self._active = False
        self._lib_new = ""
        self._aliases_new = ""

    def prepare(self, op: fsops.StagedOperation) -> None:
        """Read the current index, compute the post-deletion state and
        stage both files as managed targets. A missing index pair makes
        the participant inactive."""
        self._op = op
        if not (self._lib.exists() and self._aliases.exists()):
            self._active = False
            return
        lib = json.loads(self._lib.read_text(encoding="utf-8"))
        aliases = json.loads(self._aliases.read_text(encoding="utf-8"))
        papers = {
            k: v
            for k, v in lib.get("papers", {}).items()
            if k != self._plan.key and str(v) != self._plan.paper_id
        }
        new_aliases = {
            k: v
            for k, v in aliases.get("aliases", {}).items()
            if v != self._plan.key
        }
        self._lib_new = (
            json.dumps({"papers": papers}, sort_keys=True, indent=2) + "\n"
        )
        self._aliases_new = (
            json.dumps({"aliases": new_aliases}, sort_keys=True, indent=2) + "\n"
        )
        fsops.stage_target(op, self._lib)
        fsops.stage_target(op, self._aliases)
        self._active = True

    def commit(self) -> None:
        """Write both index files as staged managed writes."""
        if not self._active or self._op is None:
            return
        fsops.write_target(self._op, self._lib, self._lib_new)
        fsops.write_target(self._op, self._aliases, self._aliases_new)

    def finalize(self, hook: Callable[[], None]) -> tuple[str, ...]:
        """Merged final authority, legacy hook and success transition.

        Runs after the item deletion, both index publications and the
        clean commit verdict — the last phase of the transaction:

        1. final authority #1 — the full expected-state detection
           (every staged target still matches its expected fingerprint:
           deleted paths absent, both index files exactly the managed
           bytes+mode — plus every removed subtree directory still
           absent). Any racer — including one injected at this
           finalize seam, right after the pre-finalize authority
           returned clean — is preserved at a named in-vault recovery
           location and the whole transaction (item + both index
           files) rolls back with the hook zero times;
        2. the legacy rebuild hook runs exactly once, INSIDE the
           transaction: any write it performs on a transaction target
           is detected by the second authority, so the hook can never
           silently corrupt the published indexes after the final
           check;
        3. final authority #2 — the same full detection again: an
           external edit/chmod/type swap during the hook (or the
           hook's own divergence from the managed new state) is a
           structured conflict that rolls the item AND both index
           files back with the racer preserved at a named recovery
           location;
        4. success transition — the deletion's linearization point:
           the operation is finished (only fail-free bookkeeping
           remains) and the staging directory is removed best-effort
           (boundary re-checked, never through a symlink). After the
           last expected-state authority no writable callback runs and
           nothing here can roll the effective deletion back.

        Returns the vault-relative, desensitized residue paths still
        present after the cleanup attempt (``()`` when the staging
        area is fully cleaned); :func:`confirm_delete` surfaces any
        residue as the structured ``deleted_with_cleanup_required``
        status instead of a silent ``deleted``.
        """
        op = self._op
        if op is None:
            raise ItemError("participant was not prepared")
        item_dir = paper_directory(self._root, self._plan.key)
        work_dir = _work_directory(item_dir)
        self._final_authority(op, item_dir, work_dir)
        hook()
        self._final_authority(op, item_dir, work_dir)
        # linearization point: the item deletion and both index
        # publications are formally effective. Only fail-free
        # bookkeeping and best-effort cleanup remain — a cleanup
        # failure or residue must never roll the effective transaction
        # back; it is reported as deleted_with_cleanup_required.
        op._finished = True
        try:
            _remove_staging(op)
        except Exception:
            pass  # cleanup must never fail the effective deletion
        return _staging_residue(op.directory, op.vault_root)

    def _final_authority(
        self, op: fsops.StagedOperation, item_dir: Path, work_dir: Path
    ) -> None:
        """Full expected-state detection over the whole transaction.

        A clean detection returns; any conflict preserves every racer
        at a named in-vault recovery location and raises the explicit
        :class:`~paper_notes.items.ItemConflict` (the caller rolls the
        item and both index files back).
        """
        conflicts = _final_conflicts(op, self._plan, item_dir, work_dir)
        if conflicts:
            _raise_conflicts(op, conflicts, work_dir)

    def abort(self, op: fsops.StagedOperation) -> None:
        """Restore both index files before ``fsops.rollback`` consumes
        the staging backups.

        ``fsops.rollback`` only restores a target whose current
        fingerprint still equals the managed-write state; an index file
        edited externally — even after its racer was already moved to
        the recovery area — is preserved as a conflict and never
        restored. ``abort`` therefore moves any external racer still
        sitting at an index path to the recovery area (the same
        boundary-checked no-replace candidate loop; the racer is never
        overwritten) and then restores the staged backup directly
        (bytes+mode, ``copy2``). Index files that still match the
        managed write are left untouched for ``fsops.rollback``.
        """
        if not self._active or op is None:
            return
        for path in (self._lib, self._aliases):
            staged = op.targets.get(path)
            if staged is None or not staged.written:
                continue
            fingerprint = fsops._file_fingerprint(path)
            if fingerprint == fsops._expected_fingerprint(staged):
                continue  # still the managed write: fsops.rollback restores it
            if fingerprint != fsops._FP_ABSENT_TUPLE:
                # an external racer sits at the index path: preserve it
                # at a fresh in-vault recovery location, never overwrite
                moved = False
                for dest in _recovery_candidates(op, Path(path.name)):
                    try:
                        fsops.no_replace_move(
                            path,
                            dest,
                            vault_root=op.vault_root,
                            source_kind="any",
                        )
                    except (fsops.MoveTargetExists, fsops.NoReplaceMoveError):
                        continue  # destination appeared / parent compromised
                    moved = True
                    break
                if not moved:
                    continue  # cannot secure the racer: leave it untouched
            if staged.existed:
                assert staged.backup is not None
                shutil.copy2(staged.backup, path)


def _execute(
    root: Path, plan: DeletePlan, hook: Callable[[], None]
) -> tuple[str, tuple[str, ...]]:
    """Execute the token-authorized deletion transaction.

    Returns ``(operation_id, residue)``: the generated operation id
    (the recovery handle) and the vault-relative, desensitized residue
    paths left by the best-effort post-linearization cleanup (``()``
    when the staging area is fully cleaned). The caller surfaces any
    residue as ``deleted_with_cleanup_required`` instead of a silent
    ``deleted``; the effective deletion is never rolled back.
    """
    item_dir = paper_directory(root, plan.key)
    work_dir = _work_directory(item_dir)
    op: fsops.StagedOperation | None = None
    committed = False
    phase = "pre"  # "pre" | "work"
    participant: IndexParticipant | None = None
    try:
        # 0. exact global markdown scan-input manifest check before any
        #    mutation or staging: additions / deletions / edits / chmods
        #    / type and symlink changes anywhere in the vault's markdown
        #    candidate set conflict read-only
        _verify_global_scan(plan, root, item_dir)

        # 0.5 exact plan.subtree manifest check before any mutation
        _verify_subtree(plan.subtree, item_dir, lambda p: p)

        op = fsops.begin_operation(root, uuid.uuid4().hex)

        # 0.7 citation-index participant: prepare reads the current
        #     index, computes the post-deletion state and stages both
        #     files as managed targets (a vault without the index pair
        #     keeps the pre-R3 semantics: the participant is inactive)
        participant = IndexParticipant(root, plan)
        participant.prepare(op)

        # 1. atomic no-replace item -> hidden work directory: the real
        #    item is never exposed at the canonical path while its files
        #    are being removed
        _rename_dir_noreplace(item_dir, work_dir, vault_root=root)
        phase = "work"

        # 2. recheck the mapped subtree immediately after the move to
        #    close the check->move window
        _verify_subtree(
            plan.subtree,
            work_dir,
            lambda p: work_dir / p.relative_to(item_dir),
        )

        # 2.5 recheck the global markdown set while the item is hidden
        _verify_global_scan(plan, root, work_dir)

        # 3. managed delete of every subtree file: expected-state-guarded
        #    stage (pre-check -> stage -> post-check) then delete, so a
        #    racer edit is never adopted as the baseline and rollback
        #    restores exact bytes and permission modes
        for state in sorted(
            (s for s in plan.subtree if s.type == "file"),
            key=lambda s: str(s.path),
        ):
            target = work_dir / state.path.relative_to(item_dir)
            _stage_expected(op, target, plan.expected[state.path])
            fsops.delete_target(op, target)

        # 4. remove every subtree directory deepest-first (empty and
        #    hidden ones included), ending with the work directory itself
        for state in sorted(
            (s for s in plan.subtree if s.type == "dir"),
            key=lambda s: len(s.path.parts),
            reverse=True,
        ):
            target = work_dir / state.path.relative_to(item_dir)
            target.rmdir()

        # 5. post-verify BEFORE the hook: a failed verification rolls
        #    back with the hook never fired
        _post_verify(root, plan)

        # 5.5 publish both index files as staged managed writes: each
        #     write is expected-state-guarded, so an external
        #     edit/chmod/type swap between the two outputs conflicts and
        #     the whole transaction (item + both index files) rolls back
        participant.commit()

        # 6. commit verdict BEFORE the hook: the real fsops.commit
        #    conflict detection runs against a proxy operation (shared
        #    targets, throwaway staging), so the recovery material
        #    survives the verdict and a conflicted deletion can still
        #    be rolled back completely. A racer that reappeared at a
        #    deleted path is preserved untouched at a named recovery
        #    location; the canonical item is restored byte-for-byte and
        #    the rebuild hook never fires.
        verdict_conflicts = _commit_verdict(op)

        # 6.5 final authority before the finalize seam: the plain
        #     rebuild_hook success window. A racer reappearing at any
        #     deleted path — or any index target no longer matching the
        #     managed bytes+mode — AFTER the clean verdict but BEFORE
        #     the finalize seam is the same commit conflict: preserved,
        #     reported, rolled back, hook zero times (the deletion is
        #     never reported 'deleted' with a racer left behind in the
        #     work directory, and the item deletion and both index
        #     files' new state are only visible together)
        conflicts = sorted(
            set(verdict_conflicts) | set(_final_conflicts(op, plan, item_dir, work_dir)),
            key=str,
        )
        _raise_conflicts(op, conflicts, work_dir)

        # 6.75 participant finalize: index publication, the final
        #     authority and the success transition are merged into the
        #     transaction participant. The legacy rebuild hook is only
        #     a managed participant adapter invoked INSIDE the
        #     transaction between two full expected-state authorities —
        #     never an arbitrary writable callback after the final
        #     check, so a hook-time edit/chmod/type swap of an index
        #     file is detected and rolled back with the item. After
        #     the second authority the deletion is formally effective
        #     (the linearization point): only fail-free bookkeeping and
        #     best-effort staging cleanup remain, and any residue is
        #     returned for the caller to surface as
        #     deleted_with_cleanup_required (never a silent 'deleted',
        #     never a rollback of the effective deletion).
        residue = participant.finalize(hook)
        committed = True
        return op.operation_id, residue
    except fsops.OperationConflict:
        if op is not None and op._finished:
            committed = True  # already effective: never roll it back
        _abort(op, committed, phase, plan, item_dir, work_dir, participant)
        raise ItemConflict("concurrent change detected while deleting item") from None
    except ItemError:
        if op is not None and op._finished:
            committed = True
        _abort(op, committed, phase, plan, item_dir, work_dir, participant)
        raise
    except ItemConflict:
        if op is not None and op._finished:
            committed = True
        _abort(op, committed, phase, plan, item_dir, work_dir, participant)
        raise
    except Exception:
        if op is not None and op._finished:
            committed = True
        _abort(op, committed, phase, plan, item_dir, work_dir, participant)
        raise ItemError("item deletion failed") from None
    except BaseException:
        if op is not None and op._finished:
            committed = True
        _abort(op, committed, phase, plan, item_dir, work_dir, participant)
        raise
