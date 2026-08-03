"""Transactional filesystem primitives.

- :func:`atomic_replace` replaces a target via a same-directory temp
  file and ``os.replace``, preserving the target's permission bits (or
  an explicit mode), and never leaves temp files behind.
- :func:`staging` is a context manager that creates
  ``<vault>/.paper-notes/.staging/<operation-id>/`` and removes it on
  success and on exception.
- :class:`StagedOperation` snapshots every target an operation may
  modify (:func:`stage_target`), records every **managed** write
  (:func:`write_target` / :func:`delete_target`) with its content hash,
  then either :func:`commit` (keep the new state) or :func:`rollback`
  (restore every snapshot that is still exactly the managed write).
  Targets whose content changed outside the managed helpers are never
  touched; they are preserved and reported as conflicts.

All targets and the staging area are confined to the vault; operation
ids are validated safe single path components.
"""

import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .locking import LOCK_DIR, validate_operation_id

STAGING_DIR = ".staging"

_DEFAULT_MODE = 0o644


class OperationConflict(Exception):
    """A staging directory for this operation already exists."""


def atomic_replace(target: Path, content: str | bytes, mode: int | None = None) -> None:
    """Atomically write ``content`` to ``target`` on the same filesystem.

    Existing permission bits are preserved unless ``mode`` is given;
    new files default to ``0o644``. On failure the temp file is removed.
    """
    if mode is None:
        mode = (
            stat.S_IMODE(target.stat().st_mode) if target.exists() else _DEFAULT_MODE
        )
    data = content.encode("utf-8") if isinstance(content, str) else content
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def staging_directory(vault_root: Path, operation_id: str) -> Path:
    validate_operation_id(operation_id)
    return vault_root / LOCK_DIR / STAGING_DIR / operation_id


def _create_staging_directory(vault_root: Path, operation_id: str) -> Path:
    directory = staging_directory(vault_root, operation_id)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise OperationConflict(
            f"staging directory {directory} already exists; a previous "
            "operation with this id may still hold recovery material"
        ) from None
    return directory


@contextmanager
def staging(vault_root: Path, operation_id: str) -> Iterator[Path]:
    """Create a transient staging directory; remove it on exit."""
    directory = _create_staging_directory(vault_root, operation_id)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# Fingerprint kinds distinguish absent paths, regular files (with content
# hash and permission mode), directories, and symlinks/other types.
# Symlinks are never followed: lstat classifies them as "other", so type
# changes surface as conflicts instead of underlying OS errors.
_FP_ABSENT = "absent"
_FP_FILE = "file"
_FP_DIR = "dir"
_FP_OTHER = "other"

_FP_ABSENT_TUPLE = (_FP_ABSENT, None, None)


@dataclass
class StagedTarget:
    """State of one staged target."""

    existed: bool
    backup: Path | None = None
    missing_parents: list[Path] = field(default_factory=list)
    staged_fingerprint: tuple = _FP_ABSENT_TUPLE  # state at stage time
    written: bool = False
    written_fingerprint: tuple | None = None  # actual state after the last managed write


@dataclass
class StagedOperation:
    """Snapshot-and-commit transaction over multiple files."""

    vault_root: Path
    operation_id: str
    directory: Path
    targets: dict[Path, StagedTarget] = field(default_factory=dict)
    _finished: bool = False

    def _ensure_open(self) -> None:
        if self._finished:
            raise RuntimeError("operation already committed or rolled back")


def begin_operation(vault_root: Path, operation_id: str) -> StagedOperation:
    """Open a staged operation with a fresh staging directory."""
    directory = _create_staging_directory(vault_root, operation_id)
    return StagedOperation(
        vault_root=vault_root,
        operation_id=operation_id,
        directory=directory,
    )


def _target_within_vault(op: StagedOperation, target: Path) -> bool:
    """True if ``target`` resolves inside the vault (symlinks followed)."""
    vault = op.vault_root.resolve()
    resolved = target.resolve()
    return resolved.is_relative_to(vault) and resolved != vault


def _require_in_vault(op: StagedOperation, target: Path) -> None:
    if not _target_within_vault(op, target):
        raise ValueError(
            f"target {target} resolves outside the vault (or to the vault root)"
        )


def _content_hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# Fingerprint kinds distinguish absent paths, regular files (with content
# hash), directories, and symlinks/other types. Symlinks are never
# followed: lstat classifies them as "other", so type changes surface as
# conflicts instead of underlying OS errors (e.g. IsADirectoryError).
_FP_ABSENT = "absent"
_FP_FILE = "file"
_FP_DIR = "dir"
_FP_OTHER = "other"


def _file_fingerprint(path: Path) -> tuple:
    """('absent', None, None) | ('file', hash, mode) | ('dir', None, None) | ('other', None, None)."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return _FP_ABSENT_TUPLE
    if stat.S_ISLNK(st.st_mode):
        return (_FP_OTHER, None, None)
    if stat.S_ISDIR(st.st_mode):
        return (_FP_DIR, None, None)
    if stat.S_ISREG(st.st_mode):
        return (_FP_FILE, _content_hash(path.read_bytes()), stat.S_IMODE(st.st_mode))
    return (_FP_OTHER, None, None)


def _expected_fingerprint(staged: StagedTarget) -> tuple:
    """The fingerprint the target must currently have for this transaction.

    After a managed write/delete the expected state is the actual
    fingerprint recorded at write time; otherwise it is the state
    recorded at stage time.
    """
    if staged.written:
        assert staged.written_fingerprint is not None
        return staged.written_fingerprint
    return staged.staged_fingerprint


def _verify_expected_state(op: StagedOperation, target: Path, staged: StagedTarget) -> None:
    """Raise OperationConflict unless the target still matches the
    transaction's expected state (staged snapshot or managed write)."""
    if not _target_within_vault(op, target):
        raise OperationConflict(f"target {target} escaped the vault")
    if _file_fingerprint(target) != _expected_fingerprint(staged):
        raise OperationConflict(
            f"target {target} changed since staging (expected the staged state)"
        )


def stage_target(op: StagedOperation, target: Path) -> None:
    """Snapshot ``target`` into the staging area.

    Targets must resolve inside the vault. Existing targets are copied
    (permissions preserved); targets that do not exist yet are recorded
    as new files together with their missing parent chain. No parent
    directory is created here — :func:`write_target` creates it at
    write time, so a failure before the first write leaves the
    directory tree untouched.
    """
    op._ensure_open()
    _require_in_vault(op, target)
    if target in op.targets:
        return
    if target.exists():
        # Snapshot consistency: record the source fingerprint before the
        # copy, copy, then verify before/backup/after fingerprints are
        # identical. A manual edit landing during the copy must fail the
        # stage (backup removed) instead of being accepted as the baseline.
        fp_before = _file_fingerprint(target)
        backup = op.directory / f"{len(op.targets)}.bak"
        shutil.copy2(target, backup)
        fp_after = _file_fingerprint(target)
        fp_backup = _file_fingerprint(backup)
        if fp_before != fp_after or fp_before != fp_backup:
            try:
                backup.unlink()
            except OSError:
                pass
            raise OperationConflict(
                f"target {target} changed while being staged"
            ) from None
        op.targets[target] = StagedTarget(
            existed=True, backup=backup, staged_fingerprint=fp_backup
        )
    else:
        missing: list[Path] = []
        parent = target.parent
        vault = op.vault_root.resolve()
        while not parent.exists() and parent.resolve() != vault:
            missing.append(parent)
            parent = parent.parent
        op.targets[target] = StagedTarget(
            existed=False,
            missing_parents=missing,
            staged_fingerprint=_FP_ABSENT_TUPLE,
        )


def _staged(op: StagedOperation, target: Path) -> StagedTarget:
    staged = op.targets.get(target)
    if staged is None:
        raise RuntimeError(f"target {target} was not staged")
    return staged


def write_target(op: StagedOperation, target: Path, content: str | bytes) -> None:
    """Managed write: the only way a transaction modifies a target.

    Expected-state-aware atomic replace: pre-check, write the full
    content to a same-directory temp file, then re-check the vault
    boundary and the expected fingerprint immediately before
    ``os.replace``. Any change in between (e.g. a manual edit during a
    long write of a large file) removes the temp file and raises
    :class:`OperationConflict` instead of overwriting.
    """
    op._ensure_open()
    staged = _staged(op, target)
    _verify_expected_state(op, target, staged)  # pre-check
    data = content.encode("utf-8") if isinstance(content, str) else content
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        mode = (
            stat.S_IMODE(target.stat().st_mode) if target.exists() else _DEFAULT_MODE
        )
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # re-check immediately before the replace; drop the temp file if
        # the target state moved while we were writing
        if not _target_within_vault(op, target) or _file_fingerprint(
            target
        ) != _expected_fingerprint(staged):
            os.unlink(tmp)
            raise OperationConflict(
                f"target {target} changed while writing"
            ) from None
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    staged.written = True
    staged.written_fingerprint = _file_fingerprint(target)  # actual state after write


def delete_target(op: StagedOperation, target: Path) -> None:
    """Managed delete: records the deletion as the managed state.

    Refuses to run (OperationConflict) when the target no longer matches
    the transaction's expected state or resolves outside the vault.
    """
    op._ensure_open()
    staged = _staged(op, target)
    _verify_expected_state(op, target, staged)
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    staged.written = True
    staged.written_fingerprint = _FP_ABSENT_TUPLE


def _rollback_target(op: StagedOperation, target: Path, staged: StagedTarget, conflicts: list[Path]) -> None:
    if not _target_within_vault(op, target):
        conflicts.append(target)  # escaped the vault: never touch it
        return
    fingerprint = _file_fingerprint(target)
    expected = _expected_fingerprint(staged)
    if staged.existed:
        assert staged.backup is not None
        if staged.written:
            if fingerprint == expected:
                os.replace(staged.backup, target)  # managed write/delete undone
            else:
                conflicts.append(target)  # changed (or deleted) externally
        elif fingerprint == staged.staged_fingerprint:
            pass  # never written and unchanged: complete no-op (inode kept)
        else:
            conflicts.append(target)  # changed externally before any write
    elif staged.written:
        if fingerprint == _FP_ABSENT_TUPLE:
            # managed delete of a new file: current absent state already
            # equals the pre-transaction state; just clean up created
            # empty parent directories.
            for parent in staged.missing_parents:
                try:
                    parent.rmdir()
                except OSError:
                    pass
        elif fingerprint == expected:
            target.unlink()  # managed new file removed
            for parent in staged.missing_parents:
                try:
                    parent.rmdir()  # newly created empty parents removed
                except OSError:
                    pass
        else:
            conflicts.append(target)  # modified/replaced externally
    elif fingerprint != _FP_ABSENT_TUPLE:
        conflicts.append(target)  # created/replaced externally


def commit(op: StagedOperation) -> list[Path]:
    """Finish the operation, keeping the new state.

    Backups are discarded with the staging directory. Every target is
    compared against its expected fingerprint: targets that changed
    outside the managed helpers (or escaped the vault, or changed type)
    are preserved untouched and returned as conflicts.
    """
    op._ensure_open()
    conflicts: list[Path] = []
    for target, staged in op.targets.items():
        if not _target_within_vault(op, target):
            conflicts.append(target)
        elif _file_fingerprint(target) != _expected_fingerprint(staged):
            conflicts.append(target)
    shutil.rmtree(op.directory, ignore_errors=True)
    op._finished = True
    return conflicts


def rollback(op: StagedOperation) -> list[Path]:
    """Restore every staged snapshot that is still the managed state.

    Managed writes/deletes are undone (new files and their empty parent
    chain are removed). Targets that changed outside the managed
    helpers — or that no longer resolve inside the vault — are
    preserved untouched and reported as conflicts.
    """
    op._ensure_open()
    conflicts: list[Path] = []
    for target, staged in op.targets.items():
        _rollback_target(op, target, staged, conflicts)
    shutil.rmtree(op.directory, ignore_errors=True)
    op._finished = True
    return conflicts
