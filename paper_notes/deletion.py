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
7. the rebuild hook fires exactly once, and only when the commit
   verdict is clean, so every rollback path fires the hook zero times;
8. the transaction is then finalized (the recovery material is
   discarded and the staged operation finished).

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
    _rename_dir_noreplace,
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
from .locking import LOCK_DIR, release_lock
from .paths import paper_directory
from .repository import build_index

DELETE_LOCK_OPERATION = "delete_item"

_STALE_TOKEN_MESSAGE = (
    "confirmation token is stale: the vault changed since the preview; "
    "re-run item delete --dry-run"
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
        _execute(root, plan, hook)
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
) -> None:
    """Roll back the staged operation and restore the item directory
    according to the phase, using no-replace renames only. Directories
    already removed by the deletion loop are recreated (shallowest
    first) with their recorded modes before the staged files are
    restored into them. Nested rollback/restore failures are sanitized;
    a refused restore (racer at the original path) surfaces as its own
    ItemConflict.

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
            fsops.rollback(op)
        if phase == "work":
            try:
                _rename_dir_noreplace(work_dir, item_dir)
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


def _preserve_racers(
    op: fsops.StagedOperation, conflicts: list[Path], work_dir: Path
) -> list[tuple[Path, Path]]:
    """Move every external racer that reappeared at a deleted path to a
    named recovery location (``.paper-notes/recovery/<operation>/``,
    same filesystem, atomic no-replace) so the staged deletion can roll
    back to the exact pre-delete item while the racer survives
    untouched with its exact bytes. Returns ``(racer_path, recovery)``
    pairs that the conflict message reports explicitly — the racer is
    never overwritten, deleted, or silently adopted as the deletion
    baseline.
    """
    recovery_root = op.vault_root / LOCK_DIR / _RECOVERY_SUBDIR / op.operation_id
    recovered: list[tuple[Path, Path]] = []
    for target in sorted(conflicts, key=str):
        if not target.exists() and not target.is_symlink():
            continue  # vanished again; nothing to preserve
        dest = recovery_root / target.relative_to(work_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, dest)
        recovered.append((target, dest))
    return recovered


def _execute(root: Path, plan: DeletePlan, hook: Callable[[], None]) -> None:
    item_dir = paper_directory(root, plan.key)
    work_dir = _work_directory(item_dir)
    op: fsops.StagedOperation | None = None
    committed = False
    phase = "pre"  # "pre" | "work"
    try:
        # 0. exact global markdown scan-input manifest check before any
        #    mutation or staging: additions / deletions / edits / chmods
        #    / type and symlink changes anywhere in the vault's markdown
        #    candidate set conflict read-only
        _verify_global_scan(plan, root, item_dir)

        # 0.5 exact plan.subtree manifest check before any mutation
        _verify_subtree(plan.subtree, item_dir, lambda p: p)

        op = fsops.begin_operation(root, uuid.uuid4().hex)

        # 1. atomic no-replace item -> hidden work directory: the real
        #    item is never exposed at the canonical path while its files
        #    are being removed
        _rename_dir_noreplace(item_dir, work_dir)
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

        # 6. commit verdict BEFORE the hook: the real fsops.commit
        #    conflict detection runs against a proxy operation (shared
        #    targets, throwaway staging), so the recovery material
        #    survives the verdict and a conflicted deletion can still
        #    be rolled back completely. A racer that reappeared at a
        #    deleted path is preserved untouched at a named recovery
        #    location; the canonical item is restored byte-for-byte and
        #    the rebuild hook never fires.
        verdict_conflicts = _commit_verdict(op)
        if verdict_conflicts:
            recovered = _preserve_racers(op, verdict_conflicts, work_dir)
            if recovered:
                detail = "; ".join(
                    f"{path} reappeared (racer preserved at {dest})"
                    for path, dest in recovered
                )
            else:
                detail = ", ".join(str(path) for path in verdict_conflicts)
            raise ItemConflict(
                "concurrent change detected while deleting item: " + detail
            )

        # 7. rebuild hook exactly once — only after the commit verdict
        #    came back clean, so every rollback path fires it zero times
        hook()

        # 8. finalize the transaction (the verdict ran against a proxy,
        #    so the recovery material is discarded here): the staged
        #    operation is finished and nothing is restored any more
        shutil.rmtree(op.directory, ignore_errors=True)
        op._finished = True
        committed = True
    except fsops.OperationConflict:
        _abort(op, committed, phase, plan, item_dir, work_dir)
        raise ItemConflict("concurrent change detected while deleting item") from None
    except ItemError:
        _abort(op, committed, phase, plan, item_dir, work_dir)
        raise
    except ItemConflict:
        _abort(op, committed, phase, plan, item_dir, work_dir)
        raise
    except Exception:
        _abort(op, committed, phase, plan, item_dir, work_dir)
        raise ItemError("item deletion failed") from None
    except BaseException:
        _abort(op, committed, phase, plan, item_dir, work_dir)
        raise
