"""Derived card note creation for canonical paper directories.

Cards live in ``<paper_dir>/cards/`` and are the only notes derived from
a paper that are allowed there (per frontmatter_spec §cards/). A card
carries minimal relation frontmatter (``paper_id`` / ``citation_key`` /
``paper`` wikilink), the verbatim selection body, an optional block
anchor link back to the source Figure解读 note, and a trailing
``## 扩展`` section for later elaboration.

The CLI is the only managed writer; this module is the core entry point
used by the ``card create`` subcommand.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import os
import stat
import tempfile

from .frontmatter import _atomic_write_text
from .paths import cards_directory, paper_directory
from .repository import build_index

# Characters unsafe in card filenames (mirrors the literature-card skill's
# slug rules; CJK is preserved).
_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')
_LEADING_TRAILING_DOTS = re.compile(r"^\.+|\.+$")


class CardError(Exception):
    """User/config/validation error for card operations."""


class CardConflict(Exception):
    """A card already exists at the target path (zero writes)."""


def _atomic_write_new(path: Path, content: str, mode: int = 0o644) -> None:
    """Atomically create a new file (never overwrites an existing one).

    Uses a same-directory temp file plus ``os.replace``; the target is
    created with the given mode. Raises :class:`CardConflict` if the
    target already exists at commit time (the caller's pre-check is
    advisory).
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
            raise CardConflict(f"card already exists: {path}")
        os.unlink(tmp_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@dataclass(frozen=True)
class CardCreateResult:
    citation_key: str
    paper_id: str
    path: Path
    anchor_name: str | None
    anchor_inserted: bool
    anchor_link: str | None
    backlink_inserted: bool
    warnings: list[str]


def slugify_card_filename(title: str) -> str:
    """Turn a card title into a safe filename stem (CJK preserved).

    Unsafe characters are replaced with ``_``; leading/trailing dots are
    stripped; empty results fall back to ``card``.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", title).strip()
    cleaned = _LEADING_TRAILING_DOTS.sub("", cleaned)
    cleaned = re.sub(r"[\s]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "card"


def _resolve_paper(vault_root: Path, key: str) -> tuple[str, str]:
    """Resolve citation key (with alias support) to (key, paper_id)."""
    index = build_index(vault_root)
    record = index.by_key.get(key)
    if record is not None:
        return record.paper.citation_key, str(record.paper.paper_id)
    for alias, current in index.aliases.items():
        if alias == key and current in index.by_key:
            rec = index.by_key[current]
            return rec.paper.citation_key, str(rec.paper.paper_id)
    raise CardError(f"unknown citation key or alias: {key!r}")


def _find_anchor_target(note_text: str, selection: str) -> tuple[int, str] | None:
    """Locate the end of ``selection`` inside ``note_text``.

    Returns ``(index, matched_line)`` of the last non-empty selection
    line found in the note (normalized line comparison), or ``None``.
    The caller inserts ``^anchor`` right after that line.
    """
    sel_lines = [ln.strip() for ln in selection.splitlines() if ln.strip()]
    if not sel_lines:
        return None
    anchor_line = sel_lines[-1]
    note_lines = note_text.splitlines()
    for i in range(len(note_lines) - 1, -1, -1):
        if note_lines[i].strip() == anchor_line:
            return i, note_lines[i]
    return None


def _insert_anchor(
    note_path: Path, selection: str, anchor_name: str
) -> tuple[bool, str | None]:
    """Insert ``^anchor_name`` after the selection's last line, if absent.

    Returns ``(inserted, warning)``. The note is rewritten atomically
    only when the anchor is actually inserted; if the anchor already
    exists it is a no-op with a warning.
    """
    text = note_path.read_text(encoding="utf-8")
    marker = f"^{anchor_name}"
    if marker in text:
        return False, f"anchor {marker!r} already present; left unchanged"
    hit = _find_anchor_target(text, selection)
    if hit is None:
        return False, (
            "could not locate the selection's last line in "
            f"{note_path.name}; anchor not inserted"
        )
    index, _line = hit
    lines = text.splitlines()
    leading = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    lines.insert(index + 1, f"{leading}{marker}")
    _atomic_write_text(note_path, "\n".join(lines) + "\n")
    return True, None


def _insert_backlink(
    note_path: Path, anchor_name: str, card_stem: str
) -> tuple[bool, str | None]:
    """Insert ``> 卡片：[[<card_stem>]]`` after the ``^anchor_name`` line.

    Idempotent: if the same card wikilink is already present anywhere in
    the note, it is left unchanged. Returns ``(inserted, warning)``.
    """
    text = note_path.read_text(encoding="utf-8")
    card_link = f"[[{card_stem}]]"
    if card_link in text:
        return False, f"backlink {card_link!r} already present; left unchanged"
    lines = text.splitlines()
    marker = f"^{anchor_name}"
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == marker:
            lines.insert(i + 1, f"> 卡片：{card_link}")
            _atomic_write_text(note_path, "\n".join(lines) + "\n")
            return True, None
    return False, (
        f"anchor {marker!r} not found in {note_path.name}; "
        "backlink not inserted"
    )


def create_card(
    vault_root: str | Path,
    *,
    key: str,
    title: str,
    selection: str,
    filename: str | None = None,
    anchor_name: str | None = None,
    source_note: str | None = None,
    backlink: bool = False,
) -> CardCreateResult:
    """Create a derived card note under ``<paper_dir>/cards/``.

    ``selection`` is the verbatim Markdown of the selected figure note
    content. ``filename`` overrides the auto slug (``card_<slug>.md``).
    When ``anchor_name`` and ``source_note`` are given, the anchor is
    inserted into the source Figure解读 note after the selection's last
    line (idempotent), and the card body links back to it. With
    ``backlink=True`` a ``> 卡片：[[<card>]]`` line is also inserted
    right after the anchor, so the two notes link to each other
    (explicit bidirectional link).

    Raises :class:`CardConflict` when the target file already exists;
    :class:`CardError` for validation problems. Writes are atomic.
    """
    root = Path(vault_root)
    if not title.strip():
        raise CardError("card title must not be empty")
    if not selection.strip():
        raise CardError("selection body must not be empty")

    citation_key, paper_id = _resolve_paper(root, key)
    cards_dir = cards_directory(root, citation_key)
    cards_dir.mkdir(parents=True, exist_ok=True)

    stem = (
        filename.strip()
        if filename and filename.strip()
        else f"card_{slugify_card_filename(title)}"
    )
    if not stem.endswith(".md"):
        stem = f"{stem}.md"
    target = cards_dir / stem
    if target.exists():
        raise CardConflict(f"card already exists: {target}")

    anchor_link = None
    anchor_inserted = False
    backlink_inserted = False
    warnings: list[str] = []

    if anchor_name and source_note:
        src_path = paper_directory(root, citation_key) / f"{source_note}.md"
        if not src_path.is_file():
            candidate = paper_directory(root, citation_key) / source_note
            if candidate.is_file():
                src_path = candidate
            else:
                raise CardError(
                    f"source note not found: {source_note!r} "
                    f"(expected {src_path.name} under the paper directory)"
                )
        anchor_inserted, warning = _insert_anchor(src_path, selection, anchor_name)
        if warning:
            warnings.append(warning)
        anchor_link = f"[[{src_path.stem}#^{anchor_name}|{source_note}]]"
        if backlink:
            backlink_inserted, warning = _insert_backlink(
                src_path, anchor_name, target.stem
            )
            if warning:
                warnings.append(warning)

    body_parts = [selection.rstrip(), ""]
    if anchor_link:
        body_parts.append(f"> 参见 {anchor_link}")
        body_parts.append("")
    body_parts.append("## 扩展")
    body = "\n".join(body_parts) + "\n"

    frontmatter = (
        "---\n"
        f"paper_id: {paper_id}\n"
        f"citation_key: {citation_key}\n"
        f'paper: "[[{citation_key}]]"\n'
        "---\n\n"
    )
    _atomic_write_new(target, frontmatter + body)
    return CardCreateResult(
        citation_key=citation_key,
        paper_id=paper_id,
        path=target,
        anchor_name=anchor_name,
        anchor_inserted=anchor_inserted,
        anchor_link=anchor_link,
        backlink_inserted=backlink_inserted,
        warnings=warnings,
    )
