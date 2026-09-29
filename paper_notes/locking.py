"""Workspace write-lock management.

The short-lived lock lives at ``<vault>/.paper-notes/write.lock`` and is
created exclusively (``O_CREAT | O_EXCL``) with mode ``0600``. Its JSON
metadata carries the holder PID, start time, operation name, and host —
never any secret material.

Staleness is decided by holder liveness: if the PID recorded in the
lock file is no longer a live process, the lock is stale and
:class:`StaleLockError` is raised. A stale lock is **never** removed
silently; it requires explicit human resolution.
"""

import json
import os
import re
import socket
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_DIR = ".paper-notes"
LOCK_FILENAME = "write.lock"

_LOCK_MODE = 0o600

_METADATA_KEYS = frozenset(
    {"pid", "started_at", "operation", "operation_id", "host"}
)

# Operations are a code-defined whitelist — never arbitrary caller text, so
# credentials cannot be smuggled into lock metadata. The metadata carries a
# generated, non-sensitive operation id instead.
OPERATIONS = frozenset(
    {
        "import",
        "import_items",
        "create_item",
        "update_item",
        "delete_item",
        "show_item",
        "attach_pdf",
        "reconcile",
        "reconcile1",
        "refresh",
        "migrate",
        "show",
        "mineru_preview",
        "mineru_convert",
        "mineru_commit",
        "create_card",
        "create_moc",
        "rebuild_indexes",
    }
)

# Transaction ids used for staging paths must be safe single path
# components: alphanumeric plus _ . -, no "..", <= 32 chars. The first
# character may be a digit so generated hex ids (uuid4().hex) always pass.
_OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")


def validate_operation_id(operation_id: str) -> str:
    """Validate a safe, length-limited transaction id for staging paths.

    Accepts ``[a-z0-9][a-z0-9_.-]{0,31}`` without ``..``. Callers are
    expected to pass a generated non-sensitive id (e.g. a UUID hex).
    """
    if (
        not isinstance(operation_id, str)
        or not _OPERATION_ID_RE.fullmatch(operation_id)
        or ".." in operation_id
    ):
        raise ValueError("invalid operation id")
    return operation_id


class LockConflict(Exception):
    """Another live process holds the write lock."""


class StaleLockError(Exception):
    """The lock's holder is dead; resolve the stale lock manually."""


class LockError(Exception):
    """Lock operation error (e.g. symlink detected, invalid lock directory)."""


class LockHandle(os.PathLike):
    """A safe capability-bearing handle to an acquired workspace lock.

    Holds the opened file descriptor of the containing ``.paper-notes``
    directory along with the exact filesystem device and inode of the
    lock file created during acquisition. Release operations through this
    handle unlink the lock file via ``dir_fd`` only if the directory's
    lock file inode still matches, preventing deletion of replaced or
    foreign lock files.
    """

    def __init__(
        self,
        path: Path | str,
        lock_dir_fd: int | None = None,
        dev: int | None = None,
        ino: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_dir_fd = lock_dir_fd
        self.dev = dev
        self.ino = ino
        self._released = False

    def release(self) -> None:
        """Release the lock held by this handle idempotently and safely."""
        if self._released:
            return
        if self.lock_dir_fd is not None:
            try:
                st = os.stat(LOCK_FILENAME, dir_fd=self.lock_dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.close(self.lock_dir_fd)
                except OSError:
                    pass
                self.lock_dir_fd = None
                self._released = True
                return
            except OSError as exc:
                raise LockError(f"cannot stat lock file during release: {exc}") from exc

            if (st.st_dev, st.st_ino) != (self.dev, self.ino):
                # Lock file was replaced by another inode; do not unlink!
                try:
                    os.close(self.lock_dir_fd)
                except OSError:
                    pass
                self.lock_dir_fd = None
                self._released = True
                return

            try:
                os.unlink(LOCK_FILENAME, dir_fd=self.lock_dir_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Temporary failure (e.g. injected error, EBUSY, etc.)
                # Retain lock_dir_fd and capability open to permit retry
                raise LockError(f"failed to unlink lock file: {exc}") from exc

            try:
                os.close(self.lock_dir_fd)
            except OSError:
                pass
            self.lock_dir_fd = None
            self._released = True

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"LockHandle({self.path!r}, dev={self.dev}, ino={self.ino})"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parent(self) -> Path:
        return self.path.parent

    def exists(self) -> bool:
        return self.path.exists()

    def is_file(self) -> bool:
        return self.path.is_file()

    def stat(self) -> os.stat_result:
        return self.path.stat()

    def read_text(self, *args: Any, **kwargs: Any) -> str:
        return self.path.read_text(*args, **kwargs)

    def __truediv__(self, other: Any) -> Path:
        return self.path / other

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LockHandle):
            return (self.path, self.dev, self.ino) == (other.path, other.dev, other.ino)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.path, self.dev, self.ino))

    def __copy__(self) -> "LockHandle":
        return self

    def __deepcopy__(self, memo: Any) -> "LockHandle":
        return self

    def __reduce__(self) -> Any:
        raise TypeError("LockHandle cannot be pickled: file descriptor lease cannot be serialized")

    def __del__(self) -> None:
        try:
            if hasattr(self, "lock_dir_fd") and self.lock_dir_fd is not None:
                try:
                    os.close(self.lock_dir_fd)
                except OSError:
                    pass
                self.lock_dir_fd = None
        except Exception:
            pass


def lock_path(vault_root: Path) -> Path:
    return vault_root / LOCK_DIR / LOCK_FILENAME


def _read_metadata_dirfd(dir_fd: int, filename: str) -> dict | None:
    fd = None
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        fd = None
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(data, dict):
        return None
    unknown = set(data) - _METADATA_KEYS
    if unknown:
        return None
    return data


def acquire_lock(
    vault_root: Path,
    operation: str,
    root_fd: int | None = None,
    root_anchor: tuple[int, int] | None = None,
) -> LockHandle:
    """Create the write lock exclusively and return a capability LockHandle.

    ``operation`` must be one of the code-defined :data:`OPERATIONS`;
    the metadata additionally carries a generated non-sensitive
    ``operation_id``. Raises :class:`LockConflict` when a live process
    holds the lock and :class:`StaleLockError` when the holder is dead
    (the stale lock file is left in place for manual resolution). If
    writing the metadata fails, the freshly created lock is removed so
    it cannot poison later writes.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise LockError("O_NOFOLLOW is not supported on this platform")
    if operation not in OPERATIONS:
        raise ValueError("invalid operation")  # never echo caller text back

    owned_root_fd: int | None = None
    if root_fd is not None:
        try:
            st = os.fstat(root_fd)
        except OSError as exc:
            raise LockError(f"cannot fstat provided root_fd: {exc}") from exc
        if root_anchor is not None and (st.st_dev, st.st_ino) != root_anchor:
            raise LockError("provided root_fd does not match trusted root anchor")
        effective_root_fd = root_fd
        resolved_root = Path(vault_root).resolve()
    else:
        resolved_root = Path(vault_root).resolve()
        if not resolved_root.is_dir():
            raise LockError(f"vault root is not a directory: {vault_root}")
        try:
            owned_root_fd = os.open(str(resolved_root), os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise LockError(f"cannot open vault root {resolved_root}: {exc}") from exc
        effective_root_fd = owned_root_fd

    path = lock_path(vault_root)
    metadata = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "operation_id": uuid.uuid4().hex,
        "host": socket.gethostname(),
    }

    lock_dir_fd = None
    fd = None
    lock_dev = None
    lock_ino = None
    success = False
    try:
        # Ensure .paper-notes directory exists under effective_root_fd
        try:
            os.mkdir(LOCK_DIR, mode=0o755, dir_fd=effective_root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise LockError(f"cannot create lock directory {LOCK_DIR}: {exc}") from exc

        # Open .paper-notes directory strictly with O_DIRECTORY | O_NOFOLLOW
        try:
            lock_dir_fd = os.open(
                LOCK_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=effective_root_fd,
            )
        except OSError as exc:
            raise LockError(f"cannot securely open lock directory {LOCK_DIR}: {exc}") from exc

        # Attempt to create lock exclusively with O_NOFOLLOW
        try:
            fd = os.open(
                LOCK_FILENAME,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                _LOCK_MODE,
                dir_fd=lock_dir_fd,
            )
        except FileExistsError:
            holder = _read_metadata_dirfd(lock_dir_fd, LOCK_FILENAME)
            if holder is not None and not _process_is_alive(holder.get("pid")):
                raise StaleLockError(
                    f"write lock at {path} is stale (holder pid "
                    f"{holder.get('pid')!r} is not alive); remove it manually "
                    "after confirming no write is in progress"
                ) from None
            raise LockConflict(
                f"write lock at {path} is held by another process"
            ) from None
        except OSError as exc:
            raise LockError(f"cannot create lock file: {exc}") from exc

        try:
            st = os.fstat(fd)
            lock_dev = st.st_dev
            lock_ino = st.st_ino
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(metadata, fh)
            fd = None
            success = True
        except BaseException:
            if lock_dir_fd is not None and lock_dev is not None and lock_ino is not None:
                try:
                    cur_st = os.stat(LOCK_FILENAME, dir_fd=lock_dir_fd, follow_symlinks=False)
                    if (cur_st.st_dev, cur_st.st_ino) == (lock_dev, lock_ino):
                        os.unlink(LOCK_FILENAME, dir_fd=lock_dir_fd)
                except OSError:
                    pass
            raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not success and lock_dir_fd is not None:
            try:
                os.close(lock_dir_fd)
            except OSError:
                pass
        if owned_root_fd is not None:
            try:
                os.close(owned_root_fd)
            except OSError:
                pass

    return LockHandle(path, lock_dir_fd=lock_dir_fd, dev=lock_dev, ino=lock_ino)


def _safe_open_dir_nofollow(path: Path) -> int:
    """Traverse path component-by-component with O_DIRECTORY | O_NOFOLLOW without following symlinks."""
    parts = list(path.absolute().parts)
    if len(parts) >= 2 and parts[1] in ("var", "tmp", "etc") and os.path.islink("/" + parts[1]):
        target = os.readlink("/" + parts[1])
        parts = ["/"] + [p for p in target.strip("/").split("/")] + parts[2:]

    curr_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    for part in parts[1:]:
        try:
            nxt_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=curr_fd)
            os.close(curr_fd)
            curr_fd = nxt_fd
        except Exception:
            os.close(curr_fd)
            raise
    return curr_fd


def release_lock(handle: Path | LockHandle | str) -> None:
    """Release the lock file safely and idempotently.

    When given a :class:`LockHandle`, unlinks the lock node via the held
    directory descriptor after confirming dev+ino match, closing the fd.
    If unlinking encounters a transient error, retries up to 5 times so
    that caller finally blocks succeed without error.
    If retries are exhausted, raises LockError (without overwriting any
    in-flight primary exception without explanation).
    When given a path or string (legacy fallback), traverses to the vault
    root component-by-component with ``O_DIRECTORY | O_NOFOLLOW`` (without
    resolving symlinks), then opens ``.paper-notes`` with ``O_NOFOLLOW``
    before unlinking, failing closed if any component is an untrusted symlink.
    """
    if isinstance(handle, LockHandle):
        last_error: LockError | None = None
        for _ in range(5):
            try:
                handle.release()
                return
            except LockError as exc:
                last_error = exc
                continue

        if last_error is not None:
            active_exc = sys.exception() if hasattr(sys, "exception") else sys.exc_info()[1]
            if active_exc is not None:
                if hasattr(active_exc, "add_note"):
                    active_exc.add_note(f"Additionally, release_lock failed: {last_error}")
                return
            raise last_error
        return

    p = Path(handle)
    if p.name == LOCK_FILENAME and p.parent.name == LOCK_DIR:
        vault_root = p.parent.parent
        vault_fd = None
        lock_dir_fd = None
        try:
            vault_fd = _safe_open_dir_nofollow(vault_root)
            lock_dir_fd = os.open(
                LOCK_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=vault_fd,
            )
            st = os.stat(LOCK_FILENAME, dir_fd=lock_dir_fd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                os.unlink(LOCK_FILENAME, dir_fd=lock_dir_fd)
        except OSError:
            pass
        finally:
            if lock_dir_fd is not None:
                try:
                    os.close(lock_dir_fd)
                except OSError:
                    pass
            if vault_fd is not None:
                try:
                    os.close(vault_fd)
                except OSError:
                    pass
    else:
        try:
            st = p.lstat()
            if not stat.S_ISLNK(st.st_mode):
                p.unlink()
        except OSError:
            pass


def _read_metadata(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # unreadable/corrupt: treat conservatively
    if not isinstance(data, dict):
        return None
    unknown = set(data) - _METADATA_KEYS
    if unknown:
        return None
    return data


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return True
    return True
