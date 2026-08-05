"""Deterministic migration plans (Task 17: discovery + dry-run manifests).

``build_migration_plan`` turns the read-only legacy discovery into a
reviewable plan: a ``run_id``, a source/target inventory with hashes,
counts and bytes, proposed frontmatter transformations, primary-PDF
confirmation requirements, and backlink/conflict/disk-space diagnostics.
A confirmation token binds the exact plan content, so any change in the
vault after the dry run makes the token stale (apply re-checks it).

Manifests are written ONLY under a configurable state root (default
``~/Library/Application Support/paper-notes/migrations/<run_id>/`` on
macOS). The vault itself is never written, and manifests are never
auto-deleted (spec §15.1).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .legacy import (
    LEGACY_KEY_FIELDS,
    LEGACY_PDF_REF_FIELDS,
    LEGACY_STATUS_FIELDS,
    LEGACY_ZOTERO_FIELDS,
    LegacyItem,
    _STATUS_TO_READING,
    _load_frontmatter,
    _normalize_field,
    discover_legacy_items,
)
from ..paths import is_valid_key, literature_root

# uuid4().hex may start with a digit; the validator must accept it.
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# Placeholder recorded in the dry-run plan; the real UUID is minted by
# the apply engine (Task 18), never guessed here.
PAPER_ID_PLACEHOLDER = "new-uuid-at-apply"


def new_run_id() -> str:
    return uuid4().hex


def default_state_root() -> Path:
    """State root for manifests/backups; never inside the vault."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "paper-notes"
            / "migrations"
        )
    return Path.home() / ".paper-notes" / "migrations"


@dataclass(frozen=True)
class MigrationPlan:
    """Reviewable dry-run plan; ``items``/``diagnostics`` are JSON-ready."""

    run_id: str
    vault_root: Path
    state_root: Path
    manifest_path: Path
    items: tuple[dict[str, Any], ...]
    diagnostics: dict[str, Any]
    confirmation_token: str
    duplicate_targets: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "confirmation_token": self.confirmation_token,
            "state_root": str(self.state_root),
            "manifest_path": str(self.manifest_path),
            "items": list(self.items),
            "diagnostics": self.diagnostics,
        }


def _key_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Supplementary-name markers used by the recommended-primary heuristic
# (R1): the main text PDF wins over NMF/supplementary files.
_SUPPL_MARKERS = ("nmf", "suppl", "supplementary")


def _matches(
    item: LegacyItem, keys: set[str], zotero_records: dict[str, Any]
) -> bool:
    """Keys-filter membership (R1): the Figure-filename key and the Zotero
    citation key count as identifiers, not only frontmatter keys."""
    identifiers = {item.source_dir.name, item.title}
    if item.declared_citation_key is not None:
        identifiers.add(item.declared_citation_key)
    if item.figure_key is not None:
        identifiers.add(item.figure_key)
    if identifiers & keys:
        return True
    # The item's Zotero record may carry a newer BBT key than the vault
    # files (metadata drift after a BBT rename); the citekey matches --keys.
    for key in (item.declared_citation_key, item.figure_key):
        record = zotero_records.get(key) if key is not None else None
        if record is not None and record.citation_key in keys:
            return True
    return False


def _recommend_primary(pdfs: list[dict[str, Any]], key: str) -> str | None:
    """Deterministic recommended primary: stem == citation key, then
    ``primary.pdf``, then the first non-supplementary candidate (R1:
    NMF/supplementary files stay candidates, never the recommendation)."""
    if not pdfs:
        return None
    for pdf in pdfs:
        if Path(pdf["path"]).stem == key:
            return pdf["path"]
    for pdf in pdfs:
        if Path(pdf["path"]).name == "primary.pdf":
            return pdf["path"]
    for pdf in pdfs:
        name = Path(pdf["path"]).name.lower()
        if not any(marker in name for marker in _SUPPL_MARKERS):
            return pdf["path"]
    return pdfs[0]["path"]


def _transformations(
    item: LegacyItem,
    vault_root: Path,
    target_key: str | None,
    *,
    pdfs_available: bool | None = None,
) -> list[dict[str, Any]]:
    """Proposed YAML edits for the main note (apply executes these)."""
    if item.main_note is None:
        return []
    note_path = vault_root / item.main_note
    frontmatter = _load_frontmatter(note_path)
    remove = sorted(
        key
        for key in frontmatter
        if _normalize_field(str(key)) in LEGACY_KEY_FIELDS
        or _normalize_field(str(key)) in LEGACY_ZOTERO_FIELDS
        or _normalize_field(str(key)) in LEGACY_STATUS_FIELDS
        or _normalize_field(str(key)) in LEGACY_PDF_REF_FIELDS
    )
    updates: dict[str, str] = {}
    if target_key is not None:
        updates["citation_key"] = target_key
    available = item.pdfs if pdfs_available is None else pdfs_available
    updates["pdf_status"] = "available" if available else "missing"
    status_value = None
    for key, value in frontmatter.items():
        if _normalize_field(str(key)) in LEGACY_STATUS_FIELDS and isinstance(value, str):
            status_value = value
            break
    if status_value in _STATUS_TO_READING:
        updates["reading_status"] = _STATUS_TO_READING[status_value]
    return [
        {
            "path": item.main_note.as_posix(),
            "remove": remove,
            "set": updates,
            # schema_version must land in the raw frontmatter, not only
            # via Pydantic's default at parse time (the plugin index keys
            # on the declared schema; spec §6).
            "add": {"paper_id": PAPER_ID_PLACEHOLDER, "schema_version": 1},
        }
    ]


def _confirmation_required(
    item: LegacyItem, pdfs: list[dict[str, Any]], key: str | None
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    if len(pdfs) > 1:
        requirements.append(
            {
                "reason": "multiple_pdfs",
                "candidates": [p["path"] for p in pdfs],
                "recommended_primary": (
                    _recommend_primary(pdfs, key) if key is not None else pdfs[0]["path"]
                ),
            }
        )
    if not pdfs:
        requirements.append({"reason": "missing_pdf", "candidates": []})
    if item.declared_citation_key is None:
        requirements.append(
            {"reason": "missing_citation_key", "candidates": []}
        )
    elif not _valid_key(item.declared_citation_key):
        requirements.append(
            {
                "reason": "invalid_citation_key",
                "candidates": [item.declared_citation_key],
            }
        )
    return requirements


def _valid_key(key: str) -> bool:
    # Same rules as the canonical path policy: letter start, BBT
    # separators, no traversal.
    return is_valid_key(key)


def build_migration_plan(
    vault_root: str | Path,
    *,
    keys: list[str] | None = None,
    state_root: str | Path | None = None,
    zotero: Any | None = None,
) -> MigrationPlan:
    """Discover legacy items and build (and persist) a dry-run plan.

    ``zotero`` optionally supplies a read-only Zotero adapter (already
    snapshotted) whose records provide identity fields for generated main
    notes and storage PDF candidates when the vault folder has no PDFs
    (R1, design §15.3). The adapter is never closed here — the caller owns
    its lifecycle.
    """
    vault_root = Path(vault_root)
    state_root = Path(state_root) if state_root is not None else default_state_root()
    discovery = discover_legacy_items(vault_root)

    # Resolve Zotero records BEFORE filtering so --keys can match citation
    # keys known only inside the Zotero snapshot (R1: shiau/xia case).
    zotero_records: dict[str, Any] = {}
    if zotero is not None:
        candidate_keys: list[str] = []
        for item in discovery.items:
            for key in (item.declared_citation_key, item.figure_key):
                if key is not None and _valid_key(key) and key not in candidate_keys:
                    candidate_keys.append(key)
        if candidate_keys:
            resolved = zotero.resolve_by_citation_keys(candidate_keys)
            zotero_records = {
                key: record
                for key, record in resolved.items()
                if record is not None
            }

    selected = list(discovery.items)
    if keys:
        key_set = set(keys)
        selected = [
            item for item in selected if _matches(item, key_set, zotero_records)
        ]

    declared_by_key: dict[str, list[LegacyItem]] = {}
    for item in selected:
        if item.declared_citation_key is not None and _valid_key(
            item.declared_citation_key
        ):
            declared_by_key.setdefault(item.declared_citation_key, []).append(item)
    duplicate_targets = sorted(
        key for key, owners in declared_by_key.items() if len(owners) > 1
    )
    target_conflicts = sorted(
        key for key in declared_by_key if key in set(discovery.canonical_keys)
    )

    items_json: list[dict[str, Any]] = []
    canonical_set = set(discovery.canonical_keys)
    duplicate_set = set(duplicate_targets)
    for item in selected:
        target_key = item.declared_citation_key
        if target_key is not None and not _valid_key(target_key):
            target_key = None
        conflict = (
            target_key in canonical_set or target_key in duplicate_set
        )
        item_diagnostics = list(item.diagnostics)
        if target_key is not None and target_key in canonical_set:
            item_diagnostics.append("target_conflict")
        if target_key is not None and target_key in duplicate_set:
            item_diagnostics.append("duplicate_target")
        target = (
            {
                "citation_key": target_key,
                "directory": (literature_root(vault_root) / target_key)
                .relative_to(vault_root)
                .as_posix(),
            }
            if target_key is not None and not conflict
            else None
        )

        # Zotero record for this item: declared key first, then the
        # Figure-filename key (R1 match order).
        zotero_record = None
        for key in (item.declared_citation_key, item.figure_key):
            if key is not None and key in zotero_records:
                zotero_record = zotero_records[key]
                break

        # Vault PDFs first, then Zotero storage PDF candidates (R1).
        pdfs: list[dict[str, Any]] = [
            {
                "path": pdf.as_posix(),
                "filename": pdf.name,
                "sha256": _key_sha256(vault_root / pdf),
                "bytes": (vault_root / pdf).stat().st_size,
                "source": "vault",
            }
            for pdf in item.pdfs
        ]
        if zotero_record is not None:
            for att in zotero_record.attachments:
                if (
                    att.exists
                    and att.content_type
                    and "pdf" in att.content_type.lower()
                    and att.path
                ):
                    src = Path(att.path)
                    pdfs.append(
                        {
                            "path": att.path,
                            "filename": src.name,
                            "sha256": _key_sha256(src),
                            "bytes": src.stat().st_size,
                            "source": "zotero",
                            "link_mode": att.link_mode,
                        }
                    )

        main_note_action = "generate" if item.main_note is None else "transform"
        identity_source: str | None = None
        identity_fields: dict[str, Any] | None = None
        if main_note_action == "generate":
            if zotero_record is not None:
                identity_source = "zotero_item"
                fields = {
                    "citation_key": zotero_record.citation_key,
                    "title": zotero_record.title,
                    "authors": zotero_record.creators,
                    "journal": zotero_record.journal,
                    "year": zotero_record.year,
                    "DOI": zotero_record.doi,
                }
                # never carry Zotero source keys / zotero:// URLs into the
                # generated frontmatter plan; drop empty values.
                identity_fields = {
                    k: v for k, v in fields.items() if v not in (None, [], "")
                }
            elif item.figure_key is not None:
                identity_source = "figure_filename"
                identity_fields = {"citation_key": item.figure_key}
            item_diagnostics.append("main_note_generate")

        items_json.append(
            {
                "source_dir": item.source_dir.as_posix(),
                "title": item.title,
                "main_note": (
                    item.main_note.as_posix() if item.main_note is not None else None
                ),
                "main_note_action": main_note_action,
                "identity_source": identity_source,
                "identity_fields": identity_fields,
                "citation_key": item.declared_citation_key,
                "figure_key": item.figure_key,
                "zotero_fields": list(item.zotero_fields),
                "status_field": item.status_field,
                "pdfs": pdfs,
                "pdf_count": len(pdfs),
                "card_count": item.card_count,
                "derived_count": item.derived_count,
                "figure_count": item.figure_count,
                "has_figure_dir": item.has_figure_dir,
                "total_bytes": item.total_bytes,
                "target": target,
                "transformations": _transformations(
                    item,
                    vault_root,
                    target_key,
                    pdfs_available=bool(pdfs),
                ),
                "confirmation_required": _confirmation_required(
                    item, pdfs, target_key
                ),
                "diagnostics": item_diagnostics,
            }
        )

    needed_bytes = sum(item.total_bytes for item in selected)
    usage = shutil.disk_usage(vault_root)
    diagnostics: dict[str, Any] = {
        "canonical_items": list(discovery.canonical_keys),
        "target_conflicts": target_conflicts,
        "duplicate_targets": duplicate_targets,
        "backlinks": discovery.backlinks,
        "disk_space": {
            "free_bytes": usage.free,
            "needed_bytes": needed_bytes,
            "sufficient": usage.free >= needed_bytes,
        },
        "inventory": {
            "items": len(items_json),
            "pdfs": sum(item["pdf_count"] for item in items_json),
            "cards": sum(item["card_count"] for item in items_json),
            "total_bytes": needed_bytes,
        },
    }

    token_payload = {
        "items": items_json,
        "canonical_items": list(discovery.canonical_keys),
        "target_conflicts": target_conflicts,
        "duplicate_targets": duplicate_targets,
        "backlinks": discovery.backlinks,
        "disk_needed_bytes": needed_bytes,
    }
    token = hashlib.sha256(
        json.dumps(token_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    run_id = new_run_id()
    manifest_path = state_root / run_id / "manifest.json"
    plan = MigrationPlan(
        run_id=run_id,
        vault_root=vault_root,
        state_root=state_root,
        manifest_path=manifest_path,
        items=tuple(items_json),
        diagnostics=diagnostics,
        confirmation_token=token,
        duplicate_targets=tuple(duplicate_targets),
    )
    _write_manifest(plan.to_json())
    return plan


def _write_manifest(data: dict[str, Any]) -> None:
    """Persist the manifest atomically under the state root (0644)."""
    path = Path(data["manifest_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fchmod(fh.fileno(), 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
