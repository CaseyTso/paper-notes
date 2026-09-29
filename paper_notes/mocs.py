"""Topic MOC note creation.

Topic MOC notes live in ``05 Literature/MOCs/`` and carry ``kind:
topic-moc`` frontmatter plus a four-column table (Title, Figure解读,
总结, 卡片).  Each note is a standalone theme overview — independent
from the Literature Library scan rules — and is created exclusively
through the CLI (``moc create``).

The create path is atomic and never overwrites an existing file
(same pattern as ``cards._atomic_write_new``).
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .locking import LockConflict, LockError, StaleLockError, acquire_lock, release_lock
from .paths import moc_folder

# Characters that must never appear in a MOC filename component.
_PATH_SEPARATORS = ("/", "\\")
_NUL = "\x00"
_DOT = "."


class MocError(Exception):
    """User/validation error for MOC operations."""


class MocConflict(Exception):
    """A MOC already exists at the target path (zero writes)."""


@dataclass(frozen=True)
class MocCreateResult:
    title: str
    path: Path


def _atomic_write_new(path: Path, content: str, mode: int = 0o644) -> None:
    """Atomically create a new file (never overwrites an existing one).

    Uses a same-directory temp file plus ``os.link``; the target is
    created with the given mode. Raises :class:`MocConflict` if the
    target already exists at commit time.
    """
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        try:
            os.link(tmp_path, str(path))
        except FileExistsError:
            raise MocConflict(f"topic moc already exists: {path}")
        os.unlink(tmp_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _validate_title(title: str) -> str:
    """Strip and validate the theme name; raise :class:`MocError` on bad input."""
    cleaned = title.strip()
    if not cleaned:
        raise MocError("topic title must not be empty")
    for sep in _PATH_SEPARATORS:
        if sep in cleaned:
            raise MocError(f"topic title must not contain path separator: {sep!r}")
    if _NUL in cleaned:
        raise MocError("topic title must not contain NUL")
    if cleaned == _DOT or cleaned == "..":
        raise MocError(f"topic title must not be a dot-only name: {cleaned!r}")
    return cleaned


_EMPTY_TABLE_TEMPLATE = (
    "---\n"
    "kind: topic-moc\n"
    "title: {title}\n"
    "---\n\n"
    "| Title | Figure解读 | 总结 | 卡片 |\n"
    "| ----- | -------- | --- | --- |\n"
)


def create_moc(vault_root: Path, title: str) -> MocCreateResult:
    """Create a new Topic MOC note under ``05 Literature/MOCs/``.

    The filename is exactly ``<title>.md`` (CJK preserved, no slug).
    Raises :class:`MocConflict` when the file already exists and
    :class:`MocError` for validation problems.  Writes are atomic.

    This is a top-level managed vault mutation: the shared workspace
    write lock (``<vault>/.paper-notes/write.lock``, operation
    ``create_moc``) is acquired *before* any filesystem mutation —
    including the ``MOCs/`` directory creation and any temp file — and
    released after the operation completes. A lock conflict is a
    zero-write outcome (no mkdir, no temp, no note) mapped onto
    :class:`MocConflict` (CLI exit 3); stale and low-level lock errors
    map onto :class:`MocError`. Title validation happens before
    acquisition so bad input never takes the lock. The lock is
    advisory and shared by every managed writer: the operation name is
    only lock metadata, not a separate lock domain.

    Release-failure semantics: if the business operation succeeded but
    releasing the lock fails after retries, :class:`MocError` is
    raised stating that the operation may already be committed and a
    write.lock residue may remain (inspect and resolve the lock before
    retrying); if a primary exception is already in flight,
    ``release_lock`` never replaces it (it only attaches a note).
    """
    root = Path(vault_root)
    cleaned = _validate_title(title)
    lock = _acquire(root)
    try:
        folder = moc_folder(root)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{cleaned}.md"
        if target.exists():
            raise MocConflict(f"topic moc already exists: {target}")
        content = _EMPTY_TABLE_TEMPLATE.format(title=cleaned)
        _atomic_write_new(target, content)
    except BaseException:
        # release_lock never raises over an active primary (it only adds
        # a note); surface the primary untouched.
        release_lock(lock)
        raise
    try:
        release_lock(lock)
    except LockError as exc:
        # The operation may already be committed and a write.lock residue
        # may remain. Inspect and manually resolve the lock before any
        # retry. Never claim a zero-write outcome here.
        raise _release_or_domain_error(root, exc)
    return MocCreateResult(title=cleaned, path=target)


def _release_or_domain_error(root: Path, exc: LockError) -> MocError:
    """Raise the user-facing release-failure error for a committed MOC."""
    raise MocError(
        "create_moc finished but releasing the workspace write lock "
        "failed: the operation may already be committed and a "
        "write.lock residue may remain at "
        f"{root / '.paper-notes' / 'write.lock'}; inspect and resolve "
        "the lock before retrying"
    ) from exc


def _acquire(root: Path):
    """Acquire the shared workspace write lock, mapping lock failures
    onto the MOC error hierarchy (conflict → :class:`MocConflict`)."""
    try:
        return acquire_lock(root, "create_moc")
    except LockConflict as exc:
        raise MocConflict(
            f"vault write lock held: {exc}"
        ) from exc
    except StaleLockError as exc:
        raise MocError(f"vault write lock stale: {exc}") from exc
    except LockError as exc:
        raise MocError(f"vault write lock error: {exc}") from exc
