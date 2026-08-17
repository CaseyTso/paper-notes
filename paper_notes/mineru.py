"""MinerU conversion through the paper-notes core.

The Obsidian plugin only selects a PDF, configures the MinerU Key,
enqueues session-bound conversions, and displays progress/cancel; every
MinerU API call, result validation, cleaning, and managed vault write is
performed here (ADR 0002). The plugin never calls the MinerU API and
never writes the vault directly.

Pipeline (one paper per batch; never parallel):

1. **Preflight under the vault lock**: resolve the canonical item, then
   observe the Primary PDF (presence / type / sha256 / page count), any
   existing ``minerUmd_<key>.md`` (sha256), and the ``attachments/``
   tree fingerprint. Re-converts require a confirmation token that binds
   those observations; any change makes the token stale (rc 3 conflict).
2. **Network phase (lock released)**: submit a batch to the MinerU API,
   PUT the PDF to the OSS signed URL (no Content-Type), poll
   ``extract-results`` for ``state`` + ``extract_progress`` page counts,
   download the result zip, extract to an OS temp dir, validate and clean.
3. **Commit under a fresh lock**: rebuild the index, re-observe the same
   state, and abort (conflict, zero writes) if anything changed since the
   snapshot or the token. Stage the cleaned Markdown as
   ``minerUmd_<key>.md`` and the extracted images into ``attachments/``
   (same-content dedup; same-name/different-content never overwrites;
   existing attachment images are never deleted). Nothing is ever written
   under ``figures/`` and no ``Figure解读_<key>.md`` is created.

The API adapter is injectable so the whole pipeline is unit-testable
without network access. Low-level exception text is never put into
user-facing messages; the MinerU Key only ever lives in HTTP headers and
is redacted from any surfaced text via ``config.redact_config_text``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from . import config, fsops, items, paths
from .pdf import PdfError, extract_pdf_identifiers, sha256_stream
from .repository import build_index

API_BASE = "https://mineru.net/api/v4"

# Progress stages emitted to the plugin (NDJSON `{"type":"progress", ...}`).
STAGE_UPLOADING = "uploading"
STAGE_WAITING = "waiting"
STAGE_PROCESSING = "processing"
STAGE_DOWNLOADING = "downloading"
STAGE_CLEANING = "cleaning"
STAGE_COMMITTING = "committing"

ProgressCallback = Callable[[dict[str, Any]], None]


class MineruError(Exception):
    """User/config/validation error; maps to CLI exit code 2."""


class MineruConflict(Exception):
    """State changed since the preview/snapshot; maps to exit code 3."""


# ---------------------------------------------------------------------------
# State observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedState:
    paper_id: str
    citation_key: str
    pdf_present: bool
    pdf_sha256: str | None
    pdf_pages: int | None
    old_md_present: bool
    old_md_sha256: str | None
    attachments: dict[str, str]

    def fingerprint(self) -> dict[str, Any]:
        """Deterministic, JSON-serializable binding for the confirm token."""
        return {
            "paper_id": self.paper_id,
            "citation_key": self.citation_key,
            "pdf_present": self.pdf_present,
            "pdf_sha256": self.pdf_sha256,
            "old_md_present": self.old_md_present,
            "old_md_sha256": self.old_md_sha256,
            "attachments": self.attachments,
        }


def _is_real_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    try:
        return sha256_stream(path)
    except OSError as exc:
        raise MineruError(f"cannot read {path}") from exc


def _pdf_sha_and_pages(path: Path) -> tuple[str, int]:
    try:
        result = extract_pdf_identifiers(path)
    except PdfError:
        raise MineruError(f"primary PDF {path} is not a valid PDF") from None
    try:
        import fitz

        with fitz.open(path) as doc:
            pages = doc.page_count
    except Exception:
        pages = 0
    return result.sha256, pages


def _attachments_fingerprint(attachments_dir: Path) -> dict[str, str]:
    """Sorted ``relpath -> sha256`` of every real regular file under
    ``attachments/``. Symlinks/dirs are never treated as usable content."""
    fingerprint: dict[str, str] = {}
    if not attachments_dir.is_dir() or attachments_dir.is_symlink():
        return fingerprint
    for child in sorted(attachments_dir.rglob("*")):
        if child.is_symlink() or not child.is_file():
            continue
        rel = child.relative_to(attachments_dir).as_posix()
        fingerprint[rel] = _file_sha256(child)
    return fingerprint


def _observe_state(root: Path, index: Any, key: str) -> ObservedState:
    """Observe the canonical item's PDF / MinerU MD / attachments state.

    Requires the caller to hold the vault write lock (authoritative index).
    """
    record, _ = items._resolve_record(index, key)
    paper = record.paper
    canonical = paper.citation_key
    pdf_path = paths.pdf_attachment(root, canonical)
    md_path = paths.mineru_markdown(root, canonical)
    att_dir = paths.attachments_directory(root, canonical)

    if _is_real_regular_file(pdf_path):
        try:
            pdf_sha, pages = _pdf_sha_and_pages(pdf_path)
        except MineruError:
            pdf_sha, pages = None, None
        pdf_present = pdf_sha is not None
    else:
        pdf_sha, pages, pdf_present = None, None, False

    if _is_real_regular_file(md_path):
        old_md_present, old_md_sha = True, _file_sha256(md_path)
    else:
        old_md_present, old_md_sha = False, None

    return ObservedState(
        paper_id=str(paper.paper_id),
        citation_key=canonical,
        pdf_present=pdf_present,
        pdf_sha256=pdf_sha,
        pdf_pages=pages,
        old_md_present=old_md_present,
        old_md_sha256=old_md_sha,
        attachments=_attachments_fingerprint(att_dir),
    )


def _confirm_token(observed: ObservedState) -> str:
    payload = {"op": "mineru_convert", **observed.fingerprint()}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _require_key() -> None:
    cfg = config.load_config()
    if not cfg.mineru_key:
        raise MineruError(
            "no MinerU key configured; run "
            "'paper-notes config mineru set-key --stdin' first"
        )
    return cfg.mineru_key


# ---------------------------------------------------------------------------
# Cleaning (package-internal reimplementation of clean_md semantics; the
# frozen scripts/clean_md.py is never touched).
# ---------------------------------------------------------------------------

_HTML_DETAILS = re.compile(r"<details>.*?</details>", re.S)
_HTML_TABLE = re.compile(r"<table>.*?</table>", re.S)
_PANEL_LETTER = re.compile(r"^\s*([A-Z])\s*$", re.M)
_LEGEND_LINE = re.compile(
    r"^\s*\(?(?:legend\s+(?:continued\s+on\s+next\s+page|on\s+next\s+page))\)?\s*$",
    re.I | re.M,
)
_FIGURE_H2 = re.compile(r"^##\s+(Figure\b.*)$", re.M)
_FIGURE_PLAIN = re.compile(r"^(Figure\s+\d+.*)$", re.M)

_IMG_REF = re.compile(r"!\[([^\]]*)\]\((images/[^)\s]+)\)")
_IMG_FILENAME = re.compile(r"^images/([^/\\\x00]+)$")


def clean_markdown_text(raw: str) -> str:
    """Deterministic cleaning pass over the MinerU transcript.

    Drops HTML <details>/<table> blocks, standalone single-letter panel
    labels, and "(legend continued/on next page)" lines; converts Figure
    headings and plain Figure titles to bold; collapses 3+ blank lines to
    2 and strips trailing whitespace.
    """
    text = raw
    text = _HTML_DETAILS.sub("", text)
    text = _HTML_TABLE.sub("", text)
    text = _PANEL_LETTER.sub("", text)
    text = _LEGEND_LINE.sub("", text)
    text = _FIGURE_H2.sub(r"**\1**", text)
    text = _FIGURE_PLAIN.sub(r"**\1**", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ImageEntry:
    """One image to publish into ``<paper_dir>/attachments/``."""

    name: str
    source: Path
    sha256: str


def migrate_images(markdown_text: str, images_dir: Path) -> tuple[str, list[ImageEntry]]:
    """Rewrite ``![](images/<name>)`` refs to Obsidian embeds
    ``![[<name>]]`` and resolve each image file. Missing images or unsafe
    names fail the whole run (no partial output)."""
    if not images_dir.is_dir() or images_dir.is_symlink():
        raise MineruError("MinerU output has no images/ directory to migrate")

    entries: list[ImageEntry] = []
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name_match = _IMG_FILENAME.match(match.group(2))
        if name_match is None:
            raise MineruError(
                f"unsafe MinerU image reference {match.group(2)!r}"
            )
        name = name_match.group(1)
        source = images_dir / name
        if source.is_symlink() or not source.is_file():
            raise MineruError(f"MinerU image {name!r} is missing")
        if name not in seen:
            seen.add(name)
            entries.append(ImageEntry(name=name, source=source, sha256=_file_sha256(source)))
        return f"![[{name}]]"

    rewritten = _IMG_REF.sub(replace, markdown_text)
    if not rewritten.endswith("\n"):
        rewritten += "\n"
    return rewritten, entries


def locate_markdown(extract_dir: Path) -> tuple[Path, Path]:
    """Return (markdown path, its parent dir holding ``images/``).

    Accepts ``full.md`` at the extract root or one level down (MinerU zips
    have shipped both layouts). Refuses ambiguous matches.
    """
    root_md = extract_dir / "full.md"
    if root_md.is_file() and not root_md.is_symlink():
        return root_md, root_md.parent
    candidates = list(extract_dir.glob("*.md"))
    if not candidates:
        for child in extract_dir.iterdir():
            if child.is_dir():
                candidates.extend(child.glob("*.md"))
    md_paths = [c for c in candidates if c.is_file() and not c.is_symlink()]
    if len(md_paths) != 1:
        raise MineruError("MinerU output does not contain exactly one Markdown note")
    md_path = md_paths[0]
    return md_path, md_path.parent


# ---------------------------------------------------------------------------
# API adapter (injectable for tests)
# ---------------------------------------------------------------------------


class MineruApi:
    """Requests-based client for the MinerU v4 API (see references)."""

    def __init__(
        self,
        token: str,
        api_base: str = API_BASE,
        poll_interval: float = 10.0,
        max_polls: int = 180,
        timeout: float = 60.0,
        upload_timeout: float = 900.0,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self._timeout = timeout
        self._upload_timeout = upload_timeout
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def create_batch(self, filename: str) -> tuple[str, str]:
        """POST /file-urls/batch → (batch_id, upload_url)."""
        url = f"{self._api_base}/file-urls/batch"
        payload = {
            "files": [{"name": filename}],
            "model_version": "vlm",
            "language": "en",
        }
        try:
            resp = self._session.post(
                url, headers=self._headers(), json=payload, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise MineruError(f"MinerU upload URL request failed: {exc.__class__.__name__}") from exc
        data = self._response_json(resp)
        if data.get("code") != 0:
            raise MineruError(f"MinerU upload URL request failed: {data.get('msg')}")
        batch_id = data["data"]["batch_id"]
        file_urls = data["data"]["file_urls"]
        if not file_urls:
            raise MineruError("MinerU returned no upload URL")
        return batch_id, file_urls[0]

    def upload(self, pdf_path: Path, upload_url: str) -> None:
        """PUT the PDF to the OSS signed URL with NO Content-Type header."""
        try:
            with open(pdf_path, "rb") as fh:
                resp = self._session.put(
                    upload_url, data=fh, timeout=self._upload_timeout
                )
        except requests.RequestException as exc:
            raise MineruError(f"OSS upload failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise MineruError(f"OSS upload failed (status {resp.status_code})")

    def poll(
        self,
        batch_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        """Poll until done; returns ``full_zip_url``. Emits progress events."""
        url = f"{self._api_base}/extract-results/batch/{batch_id}"
        for _ in range(self._max_polls):
            time.sleep(self._poll_interval)
            try:
                resp = self._session.get(url, headers=self._headers(), timeout=self._timeout)
            except requests.RequestException as exc:
                raise MineruError(f"MinerU status poll failed: {exc.__class__.__name__}") from exc
            data = self._response_json(resp)
            if data.get("code") != 0:
                raise MineruError(f"MinerU status poll failed: {data.get('msg')}")
            extract = data.get("data", {}).get("extract_result", [])
            if not extract:
                continue
            entry = extract[0]
            state = entry.get("state")
            progress = entry.get("extract_progress", {}) or {}
            event = {
                "state": state,
                "extracted_pages": progress.get("extracted_pages"),
                "total_pages": progress.get("total_pages"),
            }
            if on_progress is not None:
                on_progress(event)
            if state == "done":
                url_value = entry.get("full_zip_url", "")
                if not url_value:
                    raise MineruError("MinerU finished without a result zip URL")
                return url_value
            if state == "failed":
                raise MineruError(
                    f"MinerU parsing failed: {entry.get('err_msg') or 'unknown error'}"
                )
        raise MineruError("timed out waiting for MinerU parsing")

    def download(self, full_zip_url: str, dest_dir: Path) -> Path:
        """Download the result zip to ``dest_dir``; curl ``--noproxy`` fallback."""
        zip_path = dest_dir / "mineru_result.zip"
        downloaded = False
        try:
            with requests.get(full_zip_url, stream=True, timeout=self._timeout) as resp:
                if resp.status_code == 200:
                    with open(zip_path, "wb") as fh:
                        shutil.copyfileobj(resp.raw, fh)
                    if zip_path.stat().st_size > 100:
                        downloaded = True
        except requests.RequestException:
            downloaded = False
        if not downloaded:
            try:
                subprocess.run(
                    [
                        "curl", "-sSL", "-o", str(zip_path), full_zip_url,
                        "--noproxy", "*", "--connect-timeout", "30",
                        "--max-time", "180",
                    ],
                    capture_output=True,
                    timeout=200,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                downloaded = False
            else:
                downloaded = zip_path.is_file() and zip_path.stat().st_size > 100
        if not downloaded:
            raise MineruError("failed to download MinerU result zip")
        return zip_path

    def _response_json(self, resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise MineruError(
                f"MinerU returned a non-JSON response (status {resp.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise MineruError("MinerU returned a non-object JSON response")
        return data


def _extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest_dir)
    except zipfile.BadZipFile as exc:
        raise MineruError("MinerU result zip is corrupt") from exc
    return dest_dir


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MineruPreview:
    """Outcome of a re-convert preview (``--dry-run``)."""

    citation_key: str
    paper_id: str
    existing_md: bool
    pdf_sha256: str | None
    old_md_sha256: str | None
    plan: dict
    confirmation_token: str


def preview_convert(vault_root: str | Path, key: str) -> MineruPreview:
    """Read-only preview for a *re-convert* (existing MinerU MD).

    Returns a confirmation token binding the current PDF / old MD /
    attachments state. Fresh conversions have nothing to replace and must
    not be previewed (caller should run the plain convert path).
    """
    root = Path(vault_root)
    _require_key()
    lock = items._acquire(root, "mineru_preview")
    try:
        index = build_index(root)
        items._assert_repository_consistent(index)
        observed = _observe_state(root, index, key)
    finally:
        from .locking import release_lock

        release_lock(lock)

    if not observed.old_md_present:
        raise MineruError(
            "no existing MinerU note to replace; nothing to preview"
        )
    if not observed.pdf_present:
        raise MineruError(f"no primary PDF for {observed.citation_key}")
    token = _confirm_token(observed)
    return MineruPreview(
        citation_key=observed.citation_key,
        paper_id=observed.paper_id,
        existing_md=True,
        pdf_sha256=observed.pdf_sha256,
        old_md_sha256=observed.old_md_sha256,
        plan={
            "action": "mineru_convert",
            "citation_key": observed.citation_key,
            "message": (
                "re-convert will replace the existing MinerU note; "
                "the old note and attachments are untouched until the new "
                "result commits completely"
            ),
            "pdf_sha256": observed.pdf_sha256,
            "old_md_sha256": observed.old_md_sha256,
            "attachments": observed.attachments,
        },
        confirmation_token=token,
    )


@dataclass(frozen=True)
class MineruConvertResult:
    action: str  # "converted" | "reconverted"
    citation_key: str
    paper_id: str
    path: str
    images: list[str]
    total_pages: int


def run_convert(
    vault_root: str | Path,
    key: str,
    *,
    confirm_token: str | None = None,
    progress: ProgressCallback | None = None,
    api: MineruApi | None = None,
    now: Any = None,
) -> MineruConvertResult:
    """Convert the item's Primary PDF through MinerU.

    ``confirm_token`` is required when an existing ``minerUmd_<key>.md``
    must be replaced; fresh conversions run tokenless. ``api`` defaults to
    a real ``MineruApi`` (inject a fake in tests). ``progress`` receives
    ``{"type":"progress", ...}``-shaped event dicts.
    """
    del now  # reserved (deterministic timestamps not needed for writes)
    root = Path(vault_root)
    key_cfg = _require_key()

    # Phase 1 — preflight under the lock, capture the expected state.
    lock = items._acquire(root, "mineru_convert")
    try:
        index = build_index(root)
        items._assert_repository_consistent(index)
        expected = _observe_state(root, index, key)
    finally:
        from .locking import release_lock

        release_lock(lock)

    if not expected.pdf_present:
        raise MineruError(f"no primary PDF for {expected.citation_key}")

    if confirm_token is not None:
        if not expected.old_md_present:
            raise MineruConflict(
                "confirmation token is stale: there is no existing MinerU "
                "note to replace; re-run mineru convert"
            )
        if not _constant_time_eq(str(confirm_token), _confirm_token(expected)):
            raise MineruConflict(
                "confirmation token is stale: the PDF, existing MinerU note, "
                "or attachments changed since the preview; re-run the preview"
            )
        action = "reconverted"
    else:
        if expected.old_md_present:
            raise MineruError(
                "existing MinerU note found; a re-convert requires preview "
                "and confirmation (--dry-run then --confirm-token)"
            )
        action = "converted"

    total_pages = expected.pdf_pages or 0
    if progress is not None:
        progress({"stage": STAGE_UPLOADING, "extracted_pages": 0, "total_pages": total_pages})

    adapter = api if api is not None else MineruApi(key_cfg)

    with tempfile.TemporaryDirectory(prefix="paper-notes-mineru-") as tmp:
        tmp_root = Path(tmp)
        batch_id, upload_url = adapter.create_batch(f"{expected.citation_key}.pdf")
        adapter.upload(paths.pdf_attachment(root, expected.citation_key), upload_url)

        def on_poll(event: dict[str, Any]) -> None:
            if progress is not None:
                progress(
                    {
                        "stage": STAGE_PROCESSING,
                        "state": event.get("state"),
                        "extracted_pages": event.get("extracted_pages"),
                        "total_pages": event.get("total_pages") or total_pages,
                    }
                )

        if progress is not None:
            progress({"stage": STAGE_WAITING, "extracted_pages": 0, "total_pages": total_pages})
        full_zip_url = adapter.poll(batch_id, on_progress=on_poll)
        if progress is not None:
            progress({"stage": STAGE_DOWNLOADING, "extracted_pages": 0, "total_pages": total_pages})
        zip_path = adapter.download(full_zip_url, tmp_root)
        extract_dir = _extract_zip(zip_path, tmp_root / "extract")
        md_path, md_dir = locate_markdown(extract_dir)
        raw = md_path.read_text(encoding="utf-8")
        cleaned = clean_markdown_text(raw)
        images_dir = md_dir / "images"
        migrated, image_entries = migrate_images(cleaned, images_dir)
        if not migrated.strip():
            raise MineruError("MinerU result contains no usable Markdown")

        if progress is not None:
            progress({"stage": STAGE_COMMITTING, "extracted_pages": 0, "total_pages": total_pages})
        return _commit_convert(
            root,
            expected,
            migrated,
            image_entries,
            action,
            total_pages,
        )


def _constant_time_eq(a: str, b: str) -> bool:
    try:
        import hmac

        return hmac.compare_digest(a, b)
    except Exception:
        return a == b


def _commit_convert(
    root: Path,
    expected: ObservedState,
    markdown: str,
    image_entries: list[ImageEntry],
    action: str,
    total_pages: int,
) -> MineruConvertResult:
    """Re-acquire the lock, re-verify state, and stage the publish.

    Any change to the PDF / old MD / attachments since the snapshot is a
    read-only conflict: zero writes, rollback, temp output discarded.
    """
    lock = items._acquire(root, "mineru_commit")
    op: fsops.StagedOperation | None = None
    committed = False
    try:
        index = build_index(root)
        items._assert_repository_consistent(index)
        current = _observe_state(root, index, expected.citation_key)
        if current.fingerprint() != expected.fingerprint():
            raise MineruConflict(
                "MinerU result was discarded: the PDF, existing MinerU note, "
                "or attachments changed during conversion; nothing was written"
            )

        item_dir = paths.paper_directory(root, expected.citation_key)
        if item_dir.is_symlink() or not item_dir.is_dir():
            raise MineruConflict(
                f"item directory {item_dir} is not a real directory; refusing to touch it"
            )
        att_dir = paths.attachments_directory(root, expected.citation_key)
        if att_dir.is_symlink():
            raise MineruConflict(
                f"attachments directory {att_dir} is a symlink; refusing to touch it"
            )
        if att_dir.exists() and not att_dir.is_dir():
            raise MineruConflict(
                f"attachments directory {att_dir} exists but is not a directory"
            )

        op = fsops.begin_operation(root, uuid.uuid4().hex)

        # The cleaned Markdown replaces the old MD only at commit.
        md_target = paths.mineru_markdown(root, expected.citation_key)
        fsops.stage_target(op, md_target)
        fsops.write_target(op, md_target, markdown.encode("utf-8"))

        added_images: list[str] = []
        for entry in image_entries:
            existing = _find_content_in(att_dir, entry.sha256)
            if existing is not None:
                # identical bytes already present under some name: reuse, skip
                continue
            target = att_dir / entry.name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not _is_real_regular_file(target):
                    raise MineruConflict(
                        f"attachment target {target} exists but is not a regular file"
                    )
                raise MineruConflict(
                    f"attachment target {target} already exists with different "
                    "content; refusing to overwrite"
                )
            items._stage_file_copy(op, target, entry.source, entry.sha256)
            added_images.append(entry.name)

        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise MineruConflict(
                "concurrent change detected while committing MinerU result: "
                + ", ".join(str(path) for path in conflicts)
            )
    except fsops.OperationConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise MineruConflict(
            "concurrent change detected while committing MinerU result"
        ) from None
    except MineruConflict:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except MineruError:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    except Exception:
        if op is not None and not committed:
            fsops.rollback(op)
        raise MineruError("MinerU commit failed") from None
    except BaseException:
        if op is not None and not committed:
            fsops.rollback(op)
        raise
    finally:
        from .locking import release_lock

        release_lock(lock)

    md_target = paths.mineru_markdown(root, expected.citation_key)
    rel = md_target.relative_to(root) if md_target.is_absolute() else md_target
    return MineruConvertResult(
        action=action,
        citation_key=expected.citation_key,
        paper_id=expected.paper_id,
        path=str(rel),
        images=sorted(added_images),
        total_pages=total_pages,
    )


def _find_content_in(att_dir: Path, content_sha: str) -> Path | None:
    """First real regular file under ``att_dir`` with this content hash."""
    if not att_dir.is_dir():
        return None
    for child in sorted(att_dir.rglob("*")):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            if sha256_stream(child) == content_sha:
                return child
        except OSError:
            continue
    return None
