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


def _matches(item: LegacyItem, keys: set[str]) -> bool:
    return (
        item.declared_citation_key in keys
        or item.source_dir.name in keys
        or item.title in keys
    )


def _transformations(
    item: LegacyItem, vault_root: Path, target_key: str | None
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
    updates["pdf_status"] = "available" if item.pdfs else "missing"
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
            "add": {"paper_id": PAPER_ID_PLACEHOLDER},
        }
    ]


def _confirmation_required(item: LegacyItem) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    if len(item.pdfs) > 1:
        requirements.append(
            {
                "reason": "multiple_pdfs",
                "candidates": [p.as_posix() for p in item.pdfs],
            }
        )
    if not item.pdfs:
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
) -> MigrationPlan:
    """Discover legacy items and build (and persist) a dry-run plan."""
    vault_root = Path(vault_root)
    state_root = Path(state_root) if state_root is not None else default_state_root()
    discovery = discover_legacy_items(vault_root)

    selected = list(discovery.items)
    if keys:
        key_set = set(keys)
        selected = [item for item in selected if _matches(item, key_set)]

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
        pdfs = [
            {
                "path": pdf.as_posix(),
                "sha256": _key_sha256(vault_root / pdf),
                "bytes": (vault_root / pdf).stat().st_size,
            }
            for pdf in item.pdfs
        ]
        items_json.append(
            {
                "source_dir": item.source_dir.as_posix(),
                "title": item.title,
                "main_note": (
                    item.main_note.as_posix() if item.main_note is not None else None
                ),
                "citation_key": item.declared_citation_key,
                "zotero_fields": list(item.zotero_fields),
                "status_field": item.status_field,
                "pdfs": pdfs,
                "pdf_count": len(item.pdfs),
                "card_count": item.card_count,
                "derived_count": item.derived_count,
                "figure_count": item.figure_count,
                "has_figure_dir": item.has_figure_dir,
                "total_bytes": item.total_bytes,
                "target": target,
                "transformations": _transformations(item, vault_root, target_key),
                "confirmation_required": _confirmation_required(item),
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
