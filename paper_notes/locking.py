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
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


def lock_path(vault_root: Path) -> Path:
    return vault_root / LOCK_DIR / LOCK_FILENAME


def acquire_lock(vault_root: Path, operation: str) -> Path:
    """Create the write lock exclusively.

    ``operation`` must be one of the code-defined :data:`OPERATIONS`;
    the metadata additionally carries a generated non-sensitive
    ``operation_id``. Raises :class:`LockConflict` when a live process
    holds the lock and :class:`StaleLockError` when the holder is dead
    (the stale lock file is left in place for manual resolution). If
    writing the metadata fails, the freshly created lock is removed so
    it cannot poison later writes.
    """
    if operation not in OPERATIONS:
        raise ValueError("invalid operation")  # never echo caller text back
    path = lock_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "operation_id": uuid.uuid4().hex,
        "host": socket.gethostname(),
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _LOCK_MODE)
    except FileExistsError:
        holder = _read_metadata(path)
        if holder is not None and not _process_is_alive(holder.get("pid")):
            raise StaleLockError(
                f"write lock at {path} is stale (holder pid "
                f"{holder.get('pid')!r} is not alive); remove it manually "
                "after confirming no write is in progress"
            ) from None
        raise LockConflict(
            f"write lock at {path} is held by another process"
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh)
    except BaseException:
        # We created this lock (O_EXCL): remove it instead of leaving a
        # poisoned lock that blocks every later write.
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def release_lock(path: Path) -> None:
    """Remove the lock file; a no-op when it is already gone."""
    try:
        path.unlink()
    except FileNotFoundError:
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
