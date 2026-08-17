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
    """
    root = Path(vault_root)
    cleaned = _validate_title(title)
    folder = moc_folder(root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{cleaned}.md"
    if target.exists():
        raise MocConflict(f"topic moc already exists: {target}")
    content = _EMPTY_TABLE_TEMPLATE.format(title=cleaned)
    _atomic_write_new(target, content)
    return MocCreateResult(title=cleaned, path=target)
