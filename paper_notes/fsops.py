"""Transactional filesystem primitives.

- :func:`atomic_replace` replaces a target via a same-directory temp
  file and ``os.replace``, preserving the target's permission bits (or
  an explicit mode), and never leaves temp files behind.
- :func:`staging` is a context manager that creates
  ``<vault>/.paper-notes/.staging/<operation-id>/`` and removes it on
  success and on exception.
- :func:`no_replace_move` moves a file or directory with the dirfd
  anchored no-replace primitive (macOS ``renameatx_np RENAME_EXCL``):
  both parents are anchored as directory file descriptors opened step
  by step from the vault root with ``O_NOFOLLOW``, so a parent path
  swapped for an outside symlink after the last path check can never
  redirect the rename outside the vault; an existing destination is
  never replaced (the late racer stays byte-for-byte);
  :class:`MoveTargetExists` and :class:`NoReplaceMoveError` are the
  structured outcomes.
- :class:`StagedOperation` snapshots every target an operation may
  modify (:func:`stage_target`), records every **managed** write
  (:func:`write_target` / :func:`delete_target`) with its content hash,
  then either :func:`commit` (keep the new state) or :func:`rollback`
  (restore every snapshot that is still exactly the managed write).
  :func:`commit_detect` runs the commit conflict verdict without
  destroying the staging directory, so a conflicted transaction can
  still be rolled back completely. Targets whose content changed
  outside the managed helpers are never touched; they are preserved
  and reported as conflicts.

All targets and the staging area are confined to the vault; operation
ids are validated safe single path components.
"""

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .locking import LOCK_DIR, validate_operation_id

STAGING_DIR = ".staging"

_DEFAULT_MODE = 0o644

# macOS <stdio.h>: fail when the target exists (works for files, empty
# directories and symlinks alike).
_RENAME_EXCL = 0x00000004
# macOS <fcntl.h>: fstatat must not follow the final symlink.
_AT_SYMLINK_NOFOLLOW = 0x0020


class OperationConflict(Exception):
    """A staging directory for this operation already exists."""


class MoveTargetExists(Exception):
    """The no-replace move destination appeared before the rename.

    Both the source and the late destination racer are preserved
    byte-for-byte at their exact paths.
    """


class NoReplaceMoveError(Exception):
    """The no-replace move failed closed without any write.

    Missing or wrong-type sources, symlinks / non-directories anywhere
    in a parent chain, outside-vault parents and unsupported platforms
    all fail closed; the external state is preserved untouched.
    """


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


# ---------------------------------------------------------------------------
# dirfd-anchored atomic no-replace move
# ---------------------------------------------------------------------------
#
# The security boundary is the anchored directory file descriptor, not a
# path pre-check: both parents are opened as O_NOFOLLOW directory fds
# component by component from the vault root (openat), so a parent path
# swapped for a symlink after the last path check can never redirect
# the rename outside the vault. The rename itself is renameatx_np(...
# RENAME_EXCL): an existing destination — file, directory or symlink —
# is never replaced. The source type is re-verified by fstatat (no
# follow) immediately before the rename.


def _openat(dirfd: int, name: str, flags: int) -> int:
    """ctypes ``openat``; raises OSError(errno) on failure."""
    libc = ctypes.CDLL(None, use_errno=True)
    openat = libc.openat
    openat.restype = ctypes.c_int
    openat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    fd = openat(dirfd, os.fsencode(name), flags)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), name)
    return fd


def _open_chain(vault_root: Path, parent: Path) -> int:
    """Open ``parent`` as an O_NOFOLLOW directory fd anchored at the
    vault root, one component at a time.

    The anchor fd is opened on the resolved vault directory (a vault
    path spelled through a symlink such as macOS ``/var`` ->
    ``/private/var`` still anchors on the real directory), while the
    relative component sequence is taken from the lexical absolute
    paths, so an in-vault parent chain is walked component by
    component. Any symlink or non-directory anywhere in the chain
    fails closed with OSError (never followed, never resolved
    through)."""
    fd = os.open(vault_root.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        rel_base = vault_root.absolute()
        for comp in parent.relative_to(rel_base).parts:
            fd = _openat(fd, comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


class _MacStat(ctypes.Structure):
    """macOS ``struct stat`` (LP64 layout, arm64 and x86_64)."""

    _fields_ = [
        ("st_dev", ctypes.c_int32),
        ("st_mode", ctypes.c_uint16),
        ("st_nlink", ctypes.c_uint16),
        ("st_ino", ctypes.c_uint64),
        ("st_uid", ctypes.c_uint32),
        ("st_gid", ctypes.c_uint32),
        ("st_rdev", ctypes.c_int32),
        ("st_atimespec", ctypes.c_int64 * 2),
        ("st_mtimespec", ctypes.c_int64 * 2),
        ("st_ctimespec", ctypes.c_int64 * 2),
        ("st_birthtimespec", ctypes.c_int64 * 2),
        ("st_size", ctypes.c_int64),
        ("st_blocks", ctypes.c_int64),
        ("st_blksize", ctypes.c_int32),
        ("st_flags", ctypes.c_uint32),
        ("st_gen", ctypes.c_uint32),
        ("st_lspare", ctypes.c_int32),
        ("st_qspare", ctypes.c_int64 * 2),
    ]


def _fstatat_mode(fd: int, name: str) -> int | None:
    """``fstatat(fd, name, AT_SYMLINK_NOFOLLOW)`` raw st_mode, or None
    when ``name`` is missing (ENOENT)."""
    libc = ctypes.CDLL(None, use_errno=True)
    fstatat = libc.fstatat
    fstatat.restype = ctypes.c_int
    fstatat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_MacStat),
        ctypes.c_int,
    )
    buf = _MacStat()
    rc = fstatat(fd, os.fsencode(name), ctypes.byref(buf), _AT_SYMLINK_NOFOLLOW)
    if rc != 0:
        err = ctypes.get_errno()
        if err == errno.ENOENT:
            return None
        raise OSError(err, os.strerror(err), name)
    return buf.st_mode


def _renameatx_np(fromfd: int, fromname: str, tofd: int, toname: str, flags: int) -> None:
    """ctypes ``renameatx_np``; raises OSError(errno) on failure."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.restype = ctypes.c_int
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rc = renameatx_np(
        fromfd, os.fsencode(fromname), tofd, os.fsencode(toname), flags
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), toname)


def no_replace_move(
    source: Path,
    target: Path,
    *,
    vault_root: Path,
    source_kind: str = "any",
) -> None:
    """dirfd-anchored atomic no-replace move of ``source`` onto ``target``.

    Both parents are anchored as O_NOFOLLOW directory fds opened step
    by step from the vault root, so a parent path swapped for a symlink
    after the last path check can never redirect the rename outside the
    vault (zero outside writes; the planted symlink untouched). The
    rename itself is ``renameatx_np(..., RENAME_EXCL)``: an existing
    destination — file, empty or non-empty directory, or symlink — is
    never replaced; the late racer stays at its exact path with its
    exact bytes and :class:`MoveTargetExists` is raised with the source
    preserved. ``source_kind`` (``"file"`` | ``"dir"`` | ``"any"``)
    re-verifies the source type by no-follow fstatat immediately before
    the rename: a source swapped to a different type fails closed with
    :class:`NoReplaceMoveError` and the external state is preserved.
    Missing sources, symlinks/non-directories anywhere in a parent
    chain, outside-vault parents and unsupported platforms also fail
    closed with zero writes.
    """
    if sys.platform != "darwin":
        raise NoReplaceMoveError(
            "atomic no-replace move is unsupported on this platform"
        )
    vault = vault_root.resolve()
    for label, path in (("source", source), ("target", target)):
        resolved = path.resolve()
        if resolved == vault or not resolved.is_relative_to(vault):
            raise NoReplaceMoveError(f"{label} {path} is outside the vault")
    src_fd: int | None = None
    dst_fd: int | None = None
    try:
        try:
            src_fd = _open_chain(vault_root, source.parent)
            dst_fd = _open_chain(vault_root, target.parent)
        except OSError:
            raise NoReplaceMoveError(
                "a parent directory chain is missing, is a symlink, or is "
                "outside the vault; nothing was moved"
            ) from None
        except ValueError:
            raise NoReplaceMoveError(
                f"source {source} or target {target} is outside the vault"
            ) from None
        mode = _fstatat_mode(src_fd, source.name)
        if mode is None:
            raise NoReplaceMoveError(f"source {source} is missing")
        if source_kind == "file" and not stat.S_ISREG(mode):
            raise NoReplaceMoveError(f"source {source} is not a regular file")
        if source_kind == "dir" and not stat.S_ISDIR(mode):
            raise NoReplaceMoveError(f"source {source} is not a directory")
        if source_kind == "any" and not (
            stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)
        ):
            raise NoReplaceMoveError(f"source {source} is not a moveable path")
        try:
            _renameatx_np(src_fd, source.name, dst_fd, target.name, _RENAME_EXCL)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise MoveTargetExists(
                    f"destination {target} appeared before the rename; "
                    "it was preserved and the source was left in place"
                ) from None
            raise NoReplaceMoveError(f"rename failed: {exc}") from None
    finally:
        for fd in (src_fd, dst_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


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


def commit_detect(op: StagedOperation) -> list[Path]:
    """Non-destructive commit verdict: conflict detection identical to
    :func:`commit` (every target compared against its expected
    fingerprint, vault escapes included) without removing the staging
    directory or finishing the operation — a conflicted transaction can
    still be rolled back completely."""
    op._ensure_open()
    conflicts: list[Path] = []
    for target, staged in op.targets.items():
        if not _target_within_vault(op, target):
            conflicts.append(target)
        elif _file_fingerprint(target) != _expected_fingerprint(staged):
            conflicts.append(target)
    return conflicts


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
