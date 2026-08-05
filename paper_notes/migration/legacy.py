"""Legacy Obsidian discovery (title-folder / Zotero-linked layouts).

Scans one level of ``05 Literature/`` and classifies every directory as
either an already-canonical item (``<key>/<key>.md``) or a legacy item
that still uses the old title-folder layout. For each legacy item it
collects a read-only inventory:

- the main note and its legacy frontmatter fields (``citation key``,
  ``zotero``, ``zotero link``, ``状态``);
- PDFs (with SHA-256 and byte counts), cards, derived notes
  (``minerUmd_*`` / ``Figure解读_*``), and figure assets;
- per-item diagnostics: missing PDF, ambiguous main PDF, missing or
  invalid citation key, target conflicts.

Nothing under the vault is ever written by this module; manifests are
written by :mod:`paper_notes.migration.manifest` under the state root.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..paths import is_valid_key, literature_root

# Spec-named derived notes never act as main-note candidates.
DERIVED_NOTE_PREFIXES = ("minerUmd_", "Figure解读_", "Figure_")

# Filenames that embed a citation key in a derived note (R1: the key of a
# folder without any standalone main note can be recovered from these).
_DERIVED_KEY_RE = re.compile(r"^(?:Figure解读_|minerUmd_)(.+?)\.md$")

# Frontmatter ``type`` values that mark a note as a card (R1): such notes
# are cards even when their filename does not contain "card".
_CARD_TYPES = frozenset({"cards", "card"})

# Legacy frontmatter field names (normalized: lowercase, separators
# removed; CJK characters are preserved by the normalization regex).
LEGACY_KEY_FIELDS = frozenset({"citationkey"})
LEGACY_ZOTERO_FIELDS = frozenset({"zotero", "zoterolink"})
LEGACY_STATUS_FIELDS = frozenset({"状态"})
LEGACY_PDF_REF_FIELDS = frozenset({"pdf", "pdfpath"})

# Legacy 状态 values mapped onto canonical reading_status.
_STATUS_TO_READING = {"已读": "read", "在读": "reading", "未读": "unread"}

_SPECIAL = "".join(chr(c) for c in range(0x4E00, 0x9FFF + 1))
_NORMALIZE_RE = re.compile(r"[^a-z0-9" + _SPECIAL + "]")

_HASH_NAME_RE = re.compile(r"^[a-f0-9]{32,64}$")

_RT = YAML(typ="rt")
_RT.width = 4096


def _normalize_field(name: str) -> str:
    """Lowercase and strip separators so legacy spelling variants match.

    ``citation key`` / ``citation_key`` / ``citation-key`` all normalize
    to ``citationkey``; ``zotero link`` / ``zoteroLink`` to ``zoterolink``.
    CJK field names (``状态``) survive because CJK codepoints are kept.
    """
    return _NORMALIZE_RE.sub("", name.lower())


def _load_frontmatter(path: Path) -> dict[str, Any]:
    """Loosely parse a note's frontmatter; never raises on bad YAML."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    try:
        data = _RT.load("\n".join(lines[1:end]))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return dict(data)


def _is_derived_note(name: str) -> bool:
    return name.startswith(DERIVED_NOTE_PREFIXES) or "card" in name.lower()


def _frontmatter_type_is_card(frontmatter: dict[str, Any]) -> bool:
    """``type: cards`` / ``type: card`` (case-insensitive) marks a card."""
    value = frontmatter.get("type")
    return isinstance(value, str) and value.strip().lower() in _CARD_TYPES


def _is_card(path: Path) -> bool:
    """A card note: frontmatter ``type`` or the legacy "card" filename
    heuristic. Derived notes (``minerUmd_*`` / ``Figure解读_*``) are NOT
    cards just because they are derived."""
    if _frontmatter_type_is_card(_load_frontmatter(path)):
        return True
    return "card" in path.name.lower()


def _is_derived_or_card(path: Path) -> bool:
    """Derived by prefix filename rule OR by card semantics (R1)."""
    if path.name.startswith(DERIVED_NOTE_PREFIXES):
        return True
    return _is_card(path)


def _derived_key(name: str) -> str | None:
    """Citation key embedded in a ``Figure解读_<key>.md`` / ``minerUmd_<key>.md``
    filename; ``None`` when the stem is not a valid key."""
    match = _DERIVED_KEY_RE.match(name)
    if match is None:
        return None
    key = match.group(1)
    return key if is_valid_key(key) else None


def _pick_main_note(directory: Path, directory_name: str) -> tuple[Path | None, list[str]]:
    """Pick the main note of a legacy directory, if any.

    Candidates are top-level Markdown files that are neither derived
    notes nor cards. Notes carrying any legacy field win; otherwise the
    first candidate in sorted order is used. Ambiguity is reported as a
    diagnostic, never resolved destructively.

    R1: a folder-titled note (``<folder>/<folder>.md``) is the
    conventional main note and is exempt from the legacy "card in
    filename" heuristic — a folder named "… Cards Type Paper" still has
    a main note. Frontmatter ``type: cards`` and derived prefixes still
    disqualify it.
    """
    candidates: list[Path] = []
    for path in sorted(directory.iterdir()):
        if not (path.is_file() and path.suffix.lower() == ".md"):
            continue
        if path.stem == directory_name:
            if path.name.startswith(DERIVED_NOTE_PREFIXES):
                continue
            if _frontmatter_type_is_card(_load_frontmatter(path)):
                continue
            candidates.append(path)
        elif not _is_derived_or_card(path):
            candidates.append(path)
    diagnostics: list[str] = []
    if not candidates:
        return None, ["no_main_note"]
    if directory_name in {path.stem for path in candidates}:
        candidates = [next(p for p in candidates if p.stem == directory_name)]
    else:
        with_fields = [
            path
            for path in candidates
            if _legacy_fields(_load_frontmatter(path))
        ]
        if len(with_fields) > 1:
            diagnostics.append("ambiguous_main_note")
        candidates = with_fields or candidates
    return candidates[0], diagnostics


def _legacy_fields(frontmatter: dict[str, Any]) -> list[str]:
    return [
        key
        for key in frontmatter
        if _normalize_field(str(key)) in LEGACY_KEY_FIELDS
        or _normalize_field(str(key)) in LEGACY_ZOTERO_FIELDS
        or _normalize_field(str(key)) in LEGACY_STATUS_FIELDS
        or _normalize_field(str(key)) in LEGACY_PDF_REF_FIELDS
    ]


def _is_canonical_directory(directory: Path) -> bool:
    """A canonical item directory is ``<key>/`` with a ``<key>.md`` note."""
    return is_valid_key(directory.name) and (directory / f"{directory.name}.md").is_file()


def _collect_pdfs(directory: Path) -> list[Path]:
    pdfs = [
        path
        for path in sorted(directory.rglob("*.pdf"))
        if not any(part.startswith(".") for part in path.relative_to(directory).parts)
    ]
    return pdfs


@dataclass(frozen=True)
class LegacyItem:
    """Read-only inventory of one legacy literature directory."""

    source_dir: Path  # relative to the vault root (posix)
    title: str
    main_note: Path | None  # relative to the vault root (posix)
    declared_citation_key: str | None
    figure_key: str | None  # key embedded in a Figure解读/minerUmd filename
    legacy_fields: tuple[str, ...]  # original legacy frontmatter key names
    zotero_fields: tuple[str, ...]  # original frontmatter keys found
    status_field: str | None
    pdfs: tuple[Path, ...]  # relative to the vault root (posix)
    card_count: int
    derived_count: int
    figure_count: int
    has_figure_dir: bool
    total_bytes: int
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class LegacyDiscovery:
    """Full read-only scan result."""

    items: tuple[LegacyItem, ...]
    canonical_keys: tuple[str, ...]
    backlinks: dict[str, int]  # source_dir (posix) -> inbound link count


def discover_legacy_items(vault_root: Path) -> LegacyDiscovery:
    """Classify every ``05 Literature`` directory; never writes anything."""
    root = literature_root(vault_root)
    items: list[LegacyItem] = []
    canonical: list[str] = []
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if _is_canonical_directory(directory):
                canonical.append(directory.name)
                continue
            items.append(_inventory_item(directory, vault_root))
    return LegacyDiscovery(
        items=tuple(items),
        canonical_keys=tuple(sorted(canonical)),
        backlinks=_scan_backlinks(vault_root, items),
    )


def _inventory_item(directory: Path, vault_root: Path) -> LegacyItem:
    frontmatter: dict[str, Any] = {}
    main_note, note_diagnostics = _pick_main_note(directory, directory.name)
    if main_note is not None:
        frontmatter = _load_frontmatter(main_note)

    pdfs = _collect_pdfs(directory)
    cards_dir = directory / "cards"
    card_count = (
        sum(1 for p in cards_dir.rglob("*.md") if p.is_file())
        if cards_dir.is_dir()
        else 0
    )
    # Top-level cards: filename rule ("card" in name) OR frontmatter
    # ``type: cards`` (R1) — including files whose name never says "card".
    # Derived notes (minerUmd_*/Figure解读_*) are NOT cards.
    card_count += sum(
        1
        for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".md"
        and (p.name != main_note.name if main_note is not None else True)
        and _is_card(p)
    )
    # Derived notes: prefix-derived notes AND cards (excluding the main
    # note itself — a folder-titled note that happens to contain "card"
    # in its name is still the main note, R1).
    derived_count = sum(
        1
        for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".md"
        and (p.name != main_note.name if main_note is not None else True)
        and _is_derived_or_card(p)
    )
    figures_dir = directory / "figures"
    figure_count = (
        sum(1 for p in figures_dir.rglob("*") if p.is_file())
        if figures_dir.is_dir()
        else 0
    )

    total_bytes = sum(
        p.stat().st_size
        for p in directory.rglob("*")
        if p.is_file()
    )

    zotero_fields: list[str] = []
    status_field: str | None = None
    declared_key: str | None = None
    pdf_ref_missing = False
    legacy_fields: list[str] = []
    for key, value in frontmatter.items():
        normalized = _normalize_field(str(key))
        if normalized in LEGACY_KEY_FIELDS and isinstance(value, str) and value.strip():
            declared_key = value.strip()
            legacy_fields.append(str(key))
        elif normalized in LEGACY_ZOTERO_FIELDS:
            zotero_fields.append(str(key))
            legacy_fields.append(str(key))
        elif normalized in LEGACY_STATUS_FIELDS:
            status_field = str(key)
            legacy_fields.append(str(key))
        elif normalized in LEGACY_PDF_REF_FIELDS:
            legacy_fields.append(str(key))
            if (
                isinstance(value, str)
                and value
                and not value.startswith("http")
                and not (directory / value).exists()
            ):
                pdf_ref_missing = True

    # R1: a folder without a standalone main note still yields a plan when
    # a derived note filename (``Figure解读_<key>.md`` / ``minerUmd_<key>.md``)
    # embeds a valid citation key; prefer Figure解读 (sorts first).
    figure_key: str | None = None
    for name in sorted(
        p.name for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".md"
    ):
        key = _derived_key(name)
        if key is not None:
            figure_key = key
            break
    if declared_key is None and figure_key is not None:
        declared_key = figure_key

    diagnostics: list[str] = list(note_diagnostics)
    if pdf_ref_missing:
        diagnostics.append("missing_pdf")
    if len(pdfs) > 1:
        diagnostics.append("ambiguous_main_pdf")
    if declared_key is not None and not is_valid_key(declared_key):
        diagnostics.append("invalid_citation_key")

    rel = directory.relative_to(vault_root).as_posix()
    return LegacyItem(
        source_dir=Path(rel),
        title=str(frontmatter.get("title") or directory.name),
        main_note=(
            Path(main_note.relative_to(vault_root).as_posix())
            if main_note is not None
            else None
        ),
        declared_citation_key=declared_key,
        figure_key=figure_key,
        legacy_fields=tuple(sorted(legacy_fields)),
        zotero_fields=tuple(sorted(zotero_fields)),
        status_field=status_field,
        pdfs=tuple(Path(p.relative_to(vault_root).as_posix()) for p in pdfs),
        card_count=card_count,
        derived_count=derived_count,
        figure_count=figure_count,
        has_figure_dir=figures_dir.is_dir(),
        total_bytes=total_bytes,
        diagnostics=tuple(diagnostics),
    )


def _scan_backlinks(
    vault_root: Path, items: list[LegacyItem]
) -> dict[str, int]:
    """Count Obsidian wikilinks that reference each legacy item.

    A link target is matched against the item's main-note stem, its
    directory name, and its title (the most common ways a legacy note
    is referenced). ``[[path|alias]]`` and ``[[note#heading]]`` forms
    resolve to the target before the pipe/heading marker.
    """
    counters: dict[str, int] = {item.source_dir.as_posix(): 0 for item in items}
    identifiers: dict[str, set[str]] = {}
    for item in items:
        ids = {item.source_dir.name, item.title}
        if item.main_note is not None:
            ids.add(Path(item.main_note).stem)
        identifiers[item.source_dir.as_posix()] = ids

    link_re = re.compile(r"\[\[([^\]]+)\]\]")
    for note in vault_root.rglob("*.md"):
        if not note.is_file():
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in link_re.finditer(text):
            target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            target = target.rsplit("/", 1)[-1]
            if target.endswith(".md"):
                target = target[:-3]
            for source_dir, ids in identifiers.items():
                if target and target in ids:
                    counters[source_dir] += 1
    return counters
