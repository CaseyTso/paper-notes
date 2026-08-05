"""Migration apply / verify / rollback engine (Task 18).

``apply_migration`` turns a reviewed dry-run plan (the manifest written
by :mod:`paper_notes.migration.manifest`) into the canonical item
layout, one legacy directory at a time, with these guarantees (spec
§15.4):

- An external backup of every source directory is created under the
  state root **before** staging; it is never auto-deleted.
- The complete new item is built in same-filesystem staging inside the
  vault (``05 Literature/.migrate-<run_id>/``) and only switched into
  its final path with the dirfd-anchored no-replace primitive after
  schema/hash/link validation.
- Old active Zotero fields (``zotero`` / ``zotero link`` / legacy
  ``citation key`` / ``状态`` / ``pdf`` refs) are removed from the main
  note and derived notes; the main note is rewritten as a canonical
  ``Paper`` item with a brand-new ``paper_id`` UUID.
- MinerU / Figure / card / figure content is preserved byte-for-byte
  outside the frontmatter changes; secondary PDFs and stray files land
  in ``attachments/``; the primary PDF becomes ``<key>.pdf``.
- A non-identical target is never overwritten: any target that exists
  with different content stops the run (exit 3 conflict).
- On failure the items already switched are automatically restored from
  their backups.
- Reapplying an identical migration is idempotent; ``verify`` reports
  every deliberately corrupted artifact; ``rollback`` restores the exact
  original source tree.

The confirmation token is checked against the manifest before anything
is written, and every source PDF hash is re-checked against the plan so
vault changes after the dry run stop the apply. The vault root is read
from the manifest (recorded by the CLI dry run) unless the caller
passes ``vault_root`` explicitly. Backups, manifests, and journals live
only under ``state_root/<run_id>/``; Zotero and the real vault are
never touched except for the approved apply.
"""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from ..fsops import MoveTargetExists, NoReplaceMoveError, no_replace_move
from ..models import Paper
from ..paths import literature_root
from .legacy import (
    LEGACY_KEY_FIELDS,
    LEGACY_PDF_REF_FIELDS,
    LEGACY_STATUS_FIELDS,
    LEGACY_ZOTERO_FIELDS,
    _normalize_field,
)
from .manifest import PAPER_ID_PLACEHOLDER, RUN_ID_RE, default_state_root

# Hidden staging directory inside the vault's literature root; it is on
# the same filesystem as the final target, which the switch requires.
STAGING_PREFIX = ".migrate-"

LEGACY_FIELDS = (
    LEGACY_KEY_FIELDS | LEGACY_ZOTERO_FIELDS
    | LEGACY_STATUS_FIELDS | LEGACY_PDF_REF_FIELDS
)

# Canonical derived-note prefixes (spec-named; never main-note material).
DERIVED_PREFIXES = ("minerUmd_", "Figure解读_")

_ZOTERO_MARKERS = ("zotero://", "zotero.org/users")

_RT = YAML(typ="rt")
_RT.width = 4096


class MigrationError(Exception):
    """User-facing migration failure (wrong token, missing manifest, ...)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MigrationConflict(Exception):
    """A conflicting vault state stops the migration without any write."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ApplyResult:
    run_id: str
    status: str  # "applied" | "already_applied"
    vault_root: str
    state_root: str
    migrated: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class VerifyReport:
    run_id: str
    status: str  # "ok" | "problems" | "pending"
    applied: int
    pending: int
    skipped: int
    problems: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RollbackResult:
    run_id: str
    status: str  # "rolled_back" | "nothing_to_roll_back"
    restored: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# Round-trip frontmatter codec (mirrors paper_notes.frontmatter semantics)
# ---------------------------------------------------------------------------


def _load_rt(raw: str) -> tuple[CommentedMap, str, str]:
    """Parse ``---`` frontmatter, preserving comments/order/body bytes.

    Returns ``(frontmatter, body, newline)``; raises :class:`MigrationError`
    when the note does not start with a valid frontmatter block.
    """
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n") if newline == "\r\n" else raw
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise MigrationError("note does not start with a --- frontmatter block")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        raise MigrationError("frontmatter block is never closed with ---")
    try:
        data = _RT.load("\n".join(lines[1:end]))
    except Exception as exc:
        raise MigrationError(f"frontmatter is not valid YAML: {exc}") from exc
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise MigrationError("frontmatter must be a YAML mapping")
    body = "\n".join(lines[end + 1 :])
    return data, body, newline


def _serialize_rt(frontmatter: CommentedMap, body: str, newline: str) -> str:
    buf = io.StringIO()
    _RT.dump(frontmatter, buf)
    content = f"---\n{buf.getvalue()}---\n{body}"
    if newline == "\r\n":
        content = content.replace("\n", "\r\n")
    return content


def _read_keep_newline(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    """Atomic same-directory write preserving the source permission bits."""
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Tree fingerprints
# ---------------------------------------------------------------------------


def _tree_fingerprint(root: Path) -> dict[str, list]:
    """Deterministic content fingerprint of a directory tree.

    ``{relative_path: [kind, payload, mode]}`` where kind is
    ``"file"`` (payload = sha256), ``"symlink"`` (payload = link
    target) or ``"dir"`` (payload = ""). JSON-serializable.
    """
    out: dict[str, list] = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        rel = path.relative_to(root).as_posix()
        if stat.S_ISREG(st.st_mode):
            out[rel] = [
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.S_IMODE(st.st_mode),
            ]
        elif stat.S_ISLNK(st.st_mode):
            out[rel] = ["symlink", os.readlink(path), stat.S_IMODE(st.st_mode)]
        else:
            out[rel] = ["dir", "", stat.S_IMODE(st.st_mode)]
    return out


# ---------------------------------------------------------------------------
# Manifest / journal access
# ---------------------------------------------------------------------------


def _load_manifest(state_root: Path, run_id: str) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise MigrationError(f"invalid run id: {run_id!r}")
    state_root = Path(state_root)
    manifest_path = state_root / run_id / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MigrationError(
            f"no migration manifest for run {run_id} at {manifest_path}"
        ) from None
    except ValueError as exc:
        raise MigrationError(
            f"migration manifest for run {run_id} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise MigrationError(f"migration manifest for run {run_id} is malformed")
    return data


def _resolve_vault(manifest: dict[str, Any], vault_root: str | Path | None) -> Path:
    if vault_root is not None:
        return Path(vault_root).resolve()
    recorded = manifest.get("vault_root")
    if isinstance(recorded, str) and recorded:
        return Path(recorded).resolve()
    raise MigrationError(
        "the manifest does not record a vault root; pass --vault explicitly"
    )


def _journal_path(state_root: Path, run_id: str) -> Path:
    return Path(state_root) / run_id / "journal.json"


def _load_journal(state_root: Path, run_id: str) -> dict[str, Any]:
    path = _journal_path(state_root, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise MigrationError(
            f"journal for run {run_id} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MigrationError(f"journal for run {run_id} is malformed")
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".journal-", dir=str(path.parent))
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


def record_vault_root(manifest_path: str | Path, vault_root: str | Path) -> None:
    """Record the vault root inside a dry-run manifest (CLI helper).

    The confirmation token is computed over the plan payload only, so
    adding this field does not invalidate it; apply/verify/rollback use
    it to locate the vault when no ``--vault`` flag is passed.
    """
    path = Path(manifest_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["vault_root"] = str(Path(vault_root).resolve())
    _write_json_atomic(path, data)


# ---------------------------------------------------------------------------
# Staged item construction
# ---------------------------------------------------------------------------


def _transform_main_note(
    raw: str,
    *,
    key: str,
    paper_id: str,
    spec: dict[str, Any] | None,
) -> str:
    """Apply the dry-run transformation spec and validate as Paper."""
    frontmatter, body, newline = _load_rt(raw)
    if spec:
        for name in spec.get("remove", []):
            frontmatter.pop(name, None)
        for name, value in spec.get("set", {}).items():
            frontmatter[name] = value
        for name, value in spec.get("add", {}).items():
            if value == PAPER_ID_PLACEHOLDER:
                value = paper_id
            frontmatter[name] = value
    else:
        # Defensive fallback: apply the legacy-field policy directly.
        for name in [
            name
            for name in frontmatter
            if _normalize_field(str(name)) in LEGACY_FIELDS
        ]:
            frontmatter.pop(name, None)
        frontmatter["citation_key"] = key
        frontmatter["paper_id"] = paper_id
    # A canonical main item always declares the schema explicitly (spec
    # §6); Pydantic's default would mask a missing field and the plugin
    # index rejects items without schema_version. Forcing it here is
    # defense in depth for manifests built before the plan carried it.
    frontmatter["schema_version"] = 1
    Paper(**dict(frontmatter))  # raises ValidationError when non-canonical
    return _serialize_rt(frontmatter, body, newline)


def _transform_derived_note(raw: str, *, key: str, paper_id: str) -> str:
    """Remove legacy fields and set identity fields on a derived note."""
    frontmatter, body, newline = _load_rt(raw)
    for name in [
        name
        for name in frontmatter
        if _normalize_field(str(name)) in LEGACY_FIELDS
    ]:
        frontmatter.pop(name, None)
    frontmatter["citation_key"] = key
    frontmatter["paper_id"] = paper_id
    return _serialize_rt(frontmatter, body, newline)


def _unique_attachment_name(used: set[str], name: str) -> str:
    candidate = name
    stem, _, suffix = name.rpartition(".")
    n = 2
    while candidate in used:
        candidate = f"{stem}-{n}.{suffix}" if suffix else f"{name}-{n}"
        n += 1
    used.add(candidate)
    return candidate


_SUPPL_MARKERS = ("nmf", "suppl", "supplementary")


def _select_primary_pdf(
    pdfs: list[dict[str, Any]], key: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Deterministic primary selection.

    Preference: a PDF whose stem equals the citation key, then a PDF
    named exactly ``primary.pdf``, then the first non-supplementary
    candidate (R1: NMF/supplementary files never become the primary),
    then the first recorded candidate. The choice is recorded in the
    journal for verification.
    """
    if not pdfs:
        return None, []
    ordered = list(pdfs)
    for pdf in ordered:
        if Path(pdf["path"]).stem == key:
            return pdf, [p for p in ordered if p is not pdf]
    for pdf in ordered:
        if Path(pdf["path"]).name == "primary.pdf":
            return pdf, [p for p in ordered if p is not pdf]
    non_suppl = [
        pdf
        for pdf in ordered
        if not any(
            marker in Path(pdf["path"]).name.lower() for marker in _SUPPL_MARKERS
        )
    ]
    if non_suppl:
        primary = non_suppl[0]
        return primary, [p for p in ordered if p is not primary]
    primary, *rest = ordered
    return primary, list(rest)


def _generate_main_note(
    item: dict[str, Any], key: str, paper_id: str, *, pdfs_available: bool
) -> str:
    """Build the canonical main note for a folder without one (R1).

    Identity comes from the dry-run plan's ``identity_fields`` (Zotero
    record or Figure-filename key); the title falls back to the folder
    name. Zotero source keys / zotero:// URLs never appear here.
    """
    fields = item.get("identity_fields") or {}
    frontmatter = CommentedMap()
    frontmatter["schema_version"] = 1
    frontmatter["paper_id"] = paper_id
    frontmatter["citation_key"] = key
    title = fields.get("title") or item.get("title") or key
    frontmatter["title"] = title
    authors = fields.get("authors")
    if isinstance(authors, list) and authors:
        frontmatter["authors"] = list(authors)
    journal = fields.get("journal")
    if isinstance(journal, str) and journal:
        frontmatter["journal"] = journal
    year = fields.get("year")
    if year is not None:
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            year_int = None
        if year_int is not None and 1000 <= year_int <= 9999:
            frontmatter["year"] = year_int
    doi = fields.get("DOI")
    if isinstance(doi, str) and doi:
        frontmatter["doi"] = doi
    frontmatter["pdf_status"] = "available" if pdfs_available else "missing"
    Paper(**dict(frontmatter))  # raises ValidationError when non-canonical
    return _serialize_rt(frontmatter, "", "\n")


def _backup_rel(source_dir: str, vault_rel: str) -> str:
    """Convert a vault-relative path to the backup-tree-relative path."""
    return Path(vault_rel).relative_to(Path(source_dir)).as_posix()


def _build_staged_item(
    stage: Path,
    vault: Path,
    item: dict[str, Any],
    key: str,
    paper_id: str,
) -> dict[str, Any]:
    """Build the complete canonical item inside ``stage``.

    Returns the journal fragment for the item: ``files`` (target relpath
    -> backup-relative path for byte-preserved copies), ``transformed``
    (backup-relative paths of notes whose frontmatter was rewritten),
    ``primary_pdf`` and ``secondary_pdfs`` (backup-relative paths).
    """
    source = vault / item["source_dir"]
    source_dir = item["source_dir"]
    stage.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    transformed: list[str] = []
    handled_backup: set[str] = set()

    main_rel = item["main_note"]
    action = item.get("main_note_action")
    pdfs = item.get("pdfs") or []
    if not main_rel:
        if action != "generate":
            raise MigrationError(f"item {item['source_dir']} has no main note")
        content = _generate_main_note(
            item, key, paper_id, pdfs_available=bool(pdfs)
        )
        target_main = stage / f"{key}.md"
        _atomic_write_text(target_main, content, mode=0o644)
    else:
        main_source = vault / main_rel
        spec = item["transformations"][0] if item["transformations"] else None
        content = _transform_main_note(
            _read_keep_newline(main_source),
            key=key,
            paper_id=paper_id,
            spec=spec,
        )
        target_main = stage / f"{key}.md"
        mode = stat.S_IMODE(main_source.stat().st_mode)
        _atomic_write_text(target_main, content, mode=mode)
        transformed.append(_backup_rel(source_dir, main_rel))
        handled_backup.add(_backup_rel(source_dir, main_rel))

    attachments_dir = stage / "attachments"
    used_attachment_names: set[str] = set()

    primary, secondary = _select_primary_pdf(pdfs, key)
    primary_record: str | None = None
    if primary is not None:
        shutil.copy2(vault / primary["path"], stage / f"{key}.pdf")
        if primary.get("source") == "zotero":
            # External storage file: no backup exists; the journal keeps
            # the absolute source path (verify compares the live file).
            files[f"{key}.pdf"] = primary["path"]
            primary_record = primary["path"]
        else:
            files[f"{key}.pdf"] = _backup_rel(source_dir, primary["path"])
            handled_backup.add(_backup_rel(source_dir, primary["path"]))
            primary_record = _backup_rel(source_dir, primary["path"])
    for pdf in secondary:
        name = _unique_attachment_name(
            used_attachment_names, Path(pdf["path"]).name
        )
        attachments_dir.mkdir(exist_ok=True)
        shutil.copy2(vault / pdf["path"], attachments_dir / name)
        if pdf.get("source") == "zotero":
            files[f"attachments/{name}"] = pdf["path"]
        else:
            files[f"attachments/{name}"] = _backup_rel(source_dir, pdf["path"])
            handled_backup.add(_backup_rel(source_dir, pdf["path"]))

    source_figures = source / "figures"
    if source_figures.is_dir():
        shutil.copytree(source_figures, stage / "figures")
        for path in sorted(source_figures.rglob("*")):
            if path.is_file():
                rel = path.relative_to(source_figures).as_posix()
                files[f"figures/{rel}"] = f"figures/{rel}"
                handled_backup.add(f"figures/{rel}")

    source_cards = source / "cards"
    if source_cards.is_dir():
        shutil.copytree(source_cards, stage / "cards")
        for path in sorted(source_cards.rglob("*")):
            if path.is_file():
                rel = path.relative_to(source_cards).as_posix()
                files[f"cards/{rel}"] = f"cards/{rel}"
                handled_backup.add(f"cards/{rel}")

    # Top-level derived notes get canonical key-suffixed names and
    # identity fields; the body is preserved byte-for-byte.
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        backup_rel = path.relative_to(source).as_posix()
        if backup_rel in handled_backup:
            continue
        prefix = next(
            (p for p in DERIVED_PREFIXES if path.name.startswith(p)), None
        )
        if prefix is None:
            continue
        target_name = f"{prefix}{key}.md"
        if (stage / target_name).exists():
            # A second note sharing the same prefix: preserve bytes only.
            name = _unique_attachment_name(used_attachment_names, path.name)
            attachments_dir.mkdir(exist_ok=True)
            shutil.copy2(path, attachments_dir / name)
            files[f"attachments/{name}"] = backup_rel
            handled_backup.add(backup_rel)
            continue
        content = _transform_derived_note(
            _read_keep_newline(path), key=key, paper_id=paper_id
        )
        mode = stat.S_IMODE(path.stat().st_mode)
        _atomic_write_text(stage / target_name, content, mode=mode)
        transformed.append(backup_rel)
        handled_backup.add(backup_rel)

    # Everything else lands in attachments/ with its original bytes.
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        backup_rel = path.relative_to(source).as_posix()
        if backup_rel in handled_backup:
            continue
        if backup_rel.startswith(("figures/", "cards/")):
            continue  # already copied wholesale above
        name = _unique_attachment_name(used_attachment_names, backup_rel)
        target_file = attachments_dir / name
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target_file)
        files[f"attachments/{name}"] = backup_rel
        handled_backup.add(backup_rel)

    return {
        "files": files,
        "transformed": transformed,
        "primary_pdf": primary_record,
        "secondary_pdfs": [
            # Zotero-storage PDFs are external: keep the absolute path so
            # verify/rollback can still locate the source (no backup entry).
            p["path"]
            if p.get("source") == "zotero"
            else _backup_rel(source_dir, p["path"])
            for p in secondary
        ],
    }


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _embed_targets(text: str) -> list[str]:
    targets: list[str] = []
    for match in re.finditer(r"!\[\[([^\]]+)\]\]", text):
        targets.append(match.group(1))
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
        targets.append(match.group(1))
    return targets


def _legacy_remnant_keys(frontmatter: dict[str, Any]) -> list[str]:
    """Field names that must not survive in a migrated note.

    The canonical ``citation_key`` is the only allowed spelling of the
    key field; legacy spellings (``citation key`` etc.) and the
    Zotero/status/pdf-ref families are all remnants.
    """
    remnants: list[str] = []
    for name in frontmatter:
        normalized = _normalize_field(str(name))
        if normalized in LEGACY_FIELDS and name != "citation_key":
            remnants.append(str(name))
    return remnants


def _verify_item(
    item: dict[str, Any],
    jitem: dict[str, Any],
    vault: Path,
    state_root: Path,
    run_id: str,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    key = jitem.get("citation_key")
    source_dir = item["source_dir"]
    source = vault / source_dir
    target = literature_root(vault) / key

    backup_root = state_root / run_id / jitem["backup_dir"]
    if not backup_root.is_dir():
        problems.append(
            {
                "code": "backup_missing",
                "message": f"backup {jitem['backup_dir']} is missing",
                "path": str(backup_root),
            }
        )
        return problems
    if source.exists():
        problems.append(
            {
                "code": "source_still_present",
                "message": f"source directory still exists: {source_dir}",
                "path": str(source),
            }
        )
    if not target.is_dir():
        problems.append(
            {
                "code": "target_missing",
                "message": f"target directory missing: {source_dir} -> {key}",
                "path": str(target),
            }
        )
        return problems

    main_note = target / f"{key}.md"
    if not main_note.is_file():
        problems.append(
            {
                "code": "main_note_missing",
                "message": f"main note missing for {key}",
                "path": str(main_note),
            }
        )
    else:
        try:
            frontmatter, body, _newline = _load_rt(_read_keep_newline(main_note))
            fm = dict(frontmatter)
            fm_paper_id = str(fm.get("paper_id") or "")
            if fm_paper_id != jitem.get("paper_id"):
                problems.append(
                    {
                        "code": "paper_id_mismatch",
                        "message": f"paper_id of {key} does not match the journal",
                        "path": str(main_note),
                    }
                )
            try:
                paper = Paper(**fm)
            except Exception as exc:
                problems.append(
                    {
                        "code": "main_note_schema",
                        "message": f"main note of {key} is not canonical: {exc}",
                        "path": str(main_note),
                    }
                )
                paper = None
            if paper is not None:
                if str(paper.paper_id) != jitem.get("paper_id"):
                    problems.append(
                        {
                            "code": "paper_id_mismatch",
                            "message": (
                                f"paper_id of {key} does not match the journal"
                            ),
                            "path": str(main_note),
                        }
                    )
                if paper.citation_key != key:
                    problems.append(
                        {
                            "code": "citation_key_mismatch",
                            "message": (
                                f"citation_key of {key} does not match the target"
                            ),
                            "path": str(main_note),
                        }
                    )
            remnants = _legacy_remnant_keys(fm)
            if remnants:
                problems.append(
                    {
                        "code": "legacy_fields_remain",
                        "message": (
                            f"{key} still has legacy fields: {sorted(remnants)}"
                        ),
                        "path": str(main_note),
                    }
                )
            blob = json.dumps(fm, ensure_ascii=False)
            if any(marker in blob for marker in _ZOTERO_MARKERS):
                problems.append(
                    {
                        "code": "zotero_link_remains",
                        "message": f"{key} still references Zotero in frontmatter",
                        "path": str(main_note),
                    }
                )
        except MigrationError as exc:
            problems.append(
                {
                    "code": "main_note_schema",
                    "message": f"main note of {key} cannot be parsed: {exc}",
                    "path": str(main_note),
                }
            )

    # Body equality against the backup (frontmatter changes only). For
    # generated main notes (main_note_action == "generate", R1) there is
    # no source backup to compare against — the note was created by apply.
    if item.get("main_note"):
        main_backup = backup_root / Path(item["main_note"]).name
        if main_backup.is_file():
            try:
                if _load_rt(_read_keep_newline(main_note))[1] != _load_rt(
                    _read_keep_newline(main_backup)
                )[1]:
                    problems.append(
                        {
                            "code": "body_changed",
                            "message": (
                                f"main note body of {key} changed outside frontmatter"
                            ),
                            "path": str(main_note),
                        }
                    )
            except MigrationError:
                pass
        else:
            problems.append(
                {
                    "code": "backup_missing",
                    "message": f"backup main note missing for {key}",
                    "path": str(main_backup),
                }
            )

    # Primary PDF.
    primary_backup_rel = jitem.get("primary_pdf")
    if primary_backup_rel:
        backup_pdf = backup_root / primary_backup_rel
        target_pdf = target / f"{key}.pdf"
        if not target_pdf.is_file():
            problems.append(
                {
                    "code": "pdf_missing",
                    "message": f"primary PDF missing for {key}",
                    "path": str(target_pdf),
                }
            )
        elif backup_pdf.is_file() and hashlib.sha256(
            target_pdf.read_bytes()
        ).hexdigest() != hashlib.sha256(backup_pdf.read_bytes()).hexdigest():
            problems.append(
                {
                    "code": "pdf_hash_mismatch",
                    "message": f"primary PDF hash mismatch for {key}",
                    "path": str(target_pdf),
                }
            )
    elif (target / f"{key}.pdf").exists():
        problems.append(
            {
                "code": "pdf_status_mismatch",
                "message": f"{key} has no primary PDF but one exists on disk",
                "path": str(target / f"{key}.pdf"),
            }
        )

    # Byte-preserved copies (secondary PDFs, cards, figures, strays).
    for target_rel, backup_rel in sorted(jitem.get("files", {}).items()):
        target_file = target / target_rel
        backup_file = backup_root / backup_rel
        if not target_file.is_file():
            problems.append(
                {
                    "code": "file_missing",
                    "message": f"migrated file missing for {key}: {target_rel}",
                    "path": str(target_file),
                }
            )
        elif backup_file.is_file() and hashlib.sha256(
            target_file.read_bytes()
        ).hexdigest() != hashlib.sha256(backup_file.read_bytes()).hexdigest():
            problems.append(
                {
                    "code": "hash_mismatch",
                    "message": (
                        f"migrated file hash mismatch for {key}: {target_rel}"
                    ),
                    "path": str(target_file),
                }
            )

    # Derived notes: canonical names, identity fields, preserved bodies.
    main_rel = item.get("main_note")
    for backup_rel in jitem.get("transformed", []):
        if main_rel is not None and backup_rel == Path(main_rel).name:
            continue
        backup_file = backup_root / backup_rel
        prefix = next(
            (
                p
                for p in DERIVED_PREFIXES
                if Path(backup_rel).name.startswith(p)
            ),
            None,
        )
        if prefix is None:
            continue
        target_file = target / f"{prefix}{key}.md"
        if not target_file.is_file():
            problems.append(
                {
                    "code": "file_missing",
                    "message": (
                        f"derived note missing for {key}: {target_file.name}"
                    ),
                    "path": str(target_file),
                }
            )
            continue
        if backup_file.is_file() and _load_rt(
            _read_keep_newline(target_file)
        )[1] != _load_rt(_read_keep_newline(backup_file))[1]:
            problems.append(
                {
                    "code": "body_changed",
                    "message": f"derived note body of {target_file.name} changed",
                    "path": str(target_file),
                }
            )
        try:
            derived_fm, _body, _nl = _load_rt(_read_keep_newline(target_file))
        except MigrationError:
            continue
        if derived_fm.get("citation_key") != key:
            problems.append(
                {
                    "code": "citation_key_mismatch",
                    "message": (
                        f"derived note {target_file.name} has the wrong citation_key"
                    ),
                    "path": str(target_file),
                }
            )
        if derived_fm.get("paper_id") != jitem.get("paper_id"):
            problems.append(
                {
                    "code": "paper_id_mismatch",
                    "message": (
                        f"derived note {target_file.name} has the wrong paper_id"
                    ),
                    "path": str(target_file),
                }
            )
        remnants = _legacy_remnant_keys(dict(derived_fm))
        if remnants:
            problems.append(
                {
                    "code": "legacy_fields_remain",
                    "message": (
                        f"derived note {target_file.name} still has legacy fields: "
                        f"{sorted(remnants)}"
                    ),
                    "path": str(target_file),
                }
            )

    # Figure embeds referenced by the notes must exist.
    for note_name in (f"{key}.md", f"Figure解读_{key}.md"):
        note = target / note_name
        if not note.is_file():
            continue
        for embed in _embed_targets(note.read_text(encoding="utf-8")):
            if not embed.startswith("figures/"):
                continue
            if not (target / embed).is_file():
                problems.append(
                    {
                        "code": "figure_link_missing",
                        "message": f"{note_name} embeds missing figure {embed}",
                        "path": str(target / embed),
                    }
                )

    # No staging residue.
    for leftover in literature_root(vault).glob(f"{STAGING_PREFIX}*"):
        problems.append(
            {
                "code": "staging_residue",
                "message": f"staging residue left behind: {leftover.name}",
                "path": str(leftover),
            }
        )
        break
    return problems


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _fresh_journal(run_id: str, vault: Path, state_root: Path) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "vault_root": str(vault),
        "state_root": str(state_root),
        "status": "in_progress",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
        "rolled_back_at": None,
        "failure": None,
        "items": [],
    }


def _journal_item(
    item: dict[str, Any],
    *,
    key: str,
    paper_id: str,
    built: dict[str, Any],
    target: Path,
    backup_dir: str,
    skipped: str | None = None,
) -> dict[str, Any]:
    if skipped:
        return {
            "source_dir": item["source_dir"],
            "citation_key": key,
            "skipped": skipped,
        }
    return {
        "source_dir": item["source_dir"],
        "citation_key": key,
        "paper_id": paper_id,
        "backup_dir": backup_dir,
        "primary_pdf": built["primary_pdf"],
        "secondary_pdfs": built["secondary_pdfs"],
        "files": built["files"],
        "transformed": built["transformed"],
        "applied_fingerprint": _tree_fingerprint(target),
        "rolled_back": False,
    }


def _skip_reason(item: dict[str, Any]) -> str | None:
    if item.get("target") is None:
        for code in (
            "target_conflict",
            "duplicate_target",
            "invalid_citation_key",
            "missing_citation_key",
        ):
            if code in item.get("diagnostics", []):
                return code
        for requirement in item.get("confirmation_required", []):
            if requirement.get("reason") in (
                "missing_citation_key",
                "invalid_citation_key",
            ):
                return requirement["reason"]
        return "no_target"
    if item.get("main_note") is None and item.get("main_note_action") != "generate":
        return "no_main_note"
    return None


def _restore_item(jitem: dict[str, Any], vault: Path, state_root: Path, run_id: str) -> None:
    """Restore one applied item from its backup; never clobber races."""
    target = literature_root(vault) / jitem["citation_key"]
    source = vault / jitem["source_dir"]
    backup = state_root / run_id / jitem["backup_dir"]
    if target.is_dir():
        if _tree_fingerprint(target) != jitem["applied_fingerprint"]:
            raise MigrationConflict(
                f"target {jitem['citation_key']} changed since apply; "
                "not restored automatically"
            )
        shutil.rmtree(target)
    if not source.exists():
        shutil.copytree(backup, source)
    jitem["rolled_back"] = True


def apply_migration(
    run_id: str,
    confirm_token: str,
    *,
    vault_root: str | Path | None = None,
    state_root: str | Path | None = None,
) -> ApplyResult:
    """Apply a reviewed dry-run plan; see the module docstring."""
    state_root = Path(state_root) if state_root is not None else default_state_root()
    manifest = _load_manifest(state_root, run_id)
    if confirm_token != manifest.get("confirmation_token"):
        raise MigrationError(
            f"confirmation token does not match the plan for run {run_id}"
        )
    vault = _resolve_vault(manifest, vault_root)

    journal = _load_journal(state_root, run_id)
    was_applied = journal.get("status") == "applied"
    if not journal or journal.get("status") in ("rolled_back", "failed"):
        journal = _fresh_journal(run_id, vault, state_root)
        _write_json_atomic(_journal_path(state_root, run_id), journal)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    fresh_applied = 0
    known = {j.get("source_dir"): j for j in journal.get("items", [])}

    staging_root = literature_root(vault) / f"{STAGING_PREFIX}{run_id}"
    if staging_root.exists():
        shutil.rmtree(staging_root)

    try:
        for idx, item in enumerate(manifest["items"]):
            reason = _skip_reason(item)
            key = (
                item["target"]["citation_key"]
                if item.get("target") is not None
                else None
            )
            prior = known.get(item["source_dir"])

            if reason is not None:
                if prior is None:
                    journal["items"].append(
                        _journal_item(item, key=key or "", paper_id="", built={},
                                      target=literature_root(vault), backup_dir="",
                                      skipped=reason)
                    )
                skipped.append({"source_dir": item["source_dir"], "reason": reason})
                continue

            assert key is not None
            target = literature_root(vault) / key

            # Idempotence / crash-resume: an identical applied item is
            # skipped; a divergent target stops without overwrite.
            if prior is not None and not prior.get("rolled_back"):
                if (
                    target.is_dir()
                    and _tree_fingerprint(target) == prior.get("applied_fingerprint")
                ):
                    applied.append(
                        {
                            "source_dir": item["source_dir"],
                            "citation_key": key,
                            "paper_id": prior["paper_id"],
                            "primary_pdf": prior.get("primary_pdf"),
                            "secondary_pdfs": prior.get("secondary_pdfs", []),
                        }
                    )
                    continue
                raise MigrationConflict(
                    f"target {key} differs from the applied state; "
                    "refusing to overwrite"
                )

            source = vault / item["source_dir"]
            if not source.is_dir():
                raise MigrationConflict(
                    f"source directory missing: {item['source_dir']}"
                )
            for pdf in item.get("pdfs") or []:
                pdf_path = vault / pdf["path"]
                if not pdf_path.is_file() or hashlib.sha256(
                    pdf_path.read_bytes()
                ).hexdigest() != pdf["sha256"]:
                    raise MigrationConflict(
                        f"source PDF changed since the dry run: {pdf['path']}"
                    )

            backup_dir = f"backup/{idx:02d}"
            backup_path = state_root / run_id / backup_dir
            if not backup_path.is_dir():
                shutil.copytree(source, backup_path)

            paper_id = str(uuid4())
            stage = staging_root / key
            built = _build_staged_item(stage, vault, item, key, paper_id)

            # The source must be byte-identical to the backup we made
            # before we destroy it (external edit during staging).
            if _tree_fingerprint(source) != _tree_fingerprint(backup_path):
                raise MigrationConflict(
                    f"source changed while staging: {item['source_dir']}"
                )

            try:
                no_replace_move(stage, target, vault_root=vault, source_kind="dir")
            except MoveTargetExists as exc:
                raise MigrationConflict(
                    f"target {key} already exists with different content; "
                    "nothing was overwritten"
                ) from exc
            except NoReplaceMoveError as exc:
                raise MigrationConflict(
                    f"cannot switch {key} into place: {exc}"
                ) from exc
            shutil.rmtree(source)

            journal["items"].append(
                _journal_item(
                    item,
                    key=key,
                    paper_id=paper_id,
                    built=built,
                    target=target,
                    backup_dir=backup_dir,
                )
            )
            applied.append(
                {
                    "source_dir": item["source_dir"],
                    "citation_key": key,
                    "paper_id": paper_id,
                    "primary_pdf": built["primary_pdf"],
                    "secondary_pdfs": built["secondary_pdfs"],
                }
            )
            fresh_applied += 1
            _write_json_atomic(_journal_path(state_root, run_id), journal)
    except BaseException as exc:
        # Restore original paths automatically on failure (spec §15.4).
        failure: dict[str, Any] = {
            "code": type(exc).__name__,
            "message": str(exc),
        }
        try:
            for jitem in journal["items"]:
                if jitem.get("skipped") or jitem.get("rolled_back"):
                    continue
                _restore_item(jitem, vault, state_root, run_id)
        except MigrationConflict as restore_exc:
            failure["restore_conflict"] = str(restore_exc)
        if staging_root.exists():
            shutil.rmtree(staging_root)
        journal["status"] = "failed"
        journal["failure"] = failure
        _write_json_atomic(_journal_path(state_root, run_id), journal)
        raise

    if staging_root.exists():
        shutil.rmtree(staging_root)
    journal["status"] = "applied"
    journal["applied_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_atomic(_journal_path(state_root, run_id), journal)

    status = "already_applied" if (was_applied and fresh_applied == 0) else "applied"
    return ApplyResult(
        run_id=run_id,
        status=status,
        vault_root=str(vault),
        state_root=str(state_root),
        migrated=tuple(applied),
        skipped=tuple(skipped),
    )


# ---------------------------------------------------------------------------
# Verify / rollback
# ---------------------------------------------------------------------------


def verify_migration(
    run_id: str,
    *,
    vault_root: str | Path | None = None,
    state_root: str | Path | None = None,
) -> VerifyReport:
    """Re-check every applied item against its backup and the schema."""
    state_root = Path(state_root) if state_root is not None else default_state_root()
    manifest = _load_manifest(state_root, run_id)
    vault = _resolve_vault(manifest, vault_root)
    journal = _load_journal(state_root, run_id)
    journal_items = {j["source_dir"]: j for j in journal.get("items", [])}

    items: list[dict[str, Any]] = []
    applied = pending = skipped = problems = 0
    for item in manifest["items"]:
        jitem = journal_items.get(item["source_dir"])
        entry: dict[str, Any] = {
            "source_dir": item["source_dir"],
            "citation_key": (
                item["target"]["citation_key"] if item.get("target") else None
            ),
        }
        if jitem is None:
            entry.update(status="pending", problems=[])
            pending += 1
        elif jitem.get("skipped"):
            entry.update(status="skipped", skipped=jitem["skipped"], problems=[])
            skipped += 1
        elif jitem.get("rolled_back"):
            entry.update(status="rolled_back", problems=[])
            pending += 1
        else:
            item_problems = _verify_item(item, jitem, vault, state_root, run_id)
            entry.update(
                status="ok" if not item_problems else "problem",
                problems=item_problems,
            )
            if item_problems:
                problems += 1
            else:
                applied += 1
        items.append(entry)

    status = "problems" if problems else ("ok" if applied else "pending")
    return VerifyReport(
        run_id=run_id,
        status=status,
        applied=applied,
        pending=pending,
        skipped=skipped,
        problems=problems,
        items=tuple(items),
    )


def rollback_migration(
    run_id: str,
    *,
    vault_root: str | Path | None = None,
    state_root: str | Path | None = None,
) -> RollbackResult:
    """Restore every applied item from its external backup."""
    state_root = Path(state_root) if state_root is not None else default_state_root()
    manifest = _load_manifest(state_root, run_id)
    vault = _resolve_vault(manifest, vault_root)
    journal = _load_journal(state_root, run_id)
    if not journal:
        raise MigrationError(
            f"no apply journal for run {run_id}; nothing to roll back"
        )

    restored: list[dict[str, Any]] = []
    for jitem in journal.get("items", []):
        if jitem.get("skipped") or jitem.get("rolled_back"):
            continue
        if not jitem.get("citation_key"):
            continue
        target = literature_root(vault) / jitem["citation_key"]
        source = vault / jitem["source_dir"]
        if not target.exists() and not source.exists():
            # Nothing applied on disk for this entry; mark restored.
            jitem["rolled_back"] = True
            continue
        if _tree_fingerprint(target) != jitem.get("applied_fingerprint"):
            raise MigrationConflict(
                f"target {jitem['citation_key']} changed since apply; "
                "rollback would overwrite it"
            )
        if source.exists():
            raise MigrationConflict(
                f"source path already exists: {jitem['source_dir']}"
            )
        shutil.rmtree(target)
        shutil.copytree(state_root / run_id / jitem["backup_dir"], source)
        jitem["rolled_back"] = True
        restored.append(
            {
                "source_dir": jitem["source_dir"],
                "citation_key": jitem["citation_key"],
            }
        )

    if restored:
        journal["status"] = "rolled_back"
        journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(_journal_path(state_root, run_id), journal)
        return RollbackResult(
            run_id=run_id, status="rolled_back", restored=tuple(restored)
        )
    return RollbackResult(
        run_id=run_id, status="nothing_to_roll_back", restored=()
    )
