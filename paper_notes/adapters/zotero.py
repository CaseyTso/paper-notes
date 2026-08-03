"""Migration-only, read-only Zotero access.

All temporary Zotero access is isolated behind this adapter. The database
is copied to a private temporary file before it is opened read-only, and
the copy is always deleted afterwards (success and error paths).

Safety guarantees:

- A non-empty ``zotero.sqlite-wal`` journal is rejected structurally
  (:class:`ZoteroSnapshotError`) — copying only the main DB would silently
  read a stale snapshot while Zotero is running.
- Imported attachment paths resolve only inside
  ``<data_dir>/storage/<attachmentItemKey>/``; absolute paths and ``..``
  traversal are rejected (never resolved outside the storage boundary).
- Creator ``fieldMode`` follows the real Zotero semantics: 0 = family/given,
  1 = literal (group author).
- Linked URLs come from the attachment item's ``url`` field; unknown link
  modes are preserved explicitly as ``unknown(N)``; every attachment keeps
  its ``content_type``.
- Records returned by :meth:`ZoteroAdapter.resolve` are in-memory migration
  objects with no note-rendering surface. Zotero source keys must never be
  serialized into canonical vault notes.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_FIELD_CITATION_KEY = "citationKey"
_FIELD_TITLE = "title"
_FIELD_DATE = "date"
_FIELD_URL = "url"

# Attachment link modes (Zotero itemAttachments.linkMode is an INTEGER).
LINK_IMPORTED = "imported_file"
LINK_LINKED_FILE = "linked_file"
LINK_LINKED_URL = "linked_url"
LINK_IMPORTED_URL = "imported_url"

_LINK_MODE_NAMES = {
    0: LINK_IMPORTED,
    1: LINK_IMPORTED_URL,
    2: LINK_LINKED_FILE,
    3: LINK_LINKED_URL,
}

# Item types that are never bibliographic records.
_NON_BIBLIOGRAPHIC = frozenset({"note", "attachment", "annotation"})


class ZoteroSnapshotError(Exception):
    """The Zotero database cannot be snapshotted consistently right now.

    Raised when a non-empty WAL journal exists or the source changes while
    it is being copied: Zotero is running and the main sqlite file alone is
    a stale snapshot. Close Zotero and retry.
    """


class ZoteroCleanupError(Exception):
    """A temporary copy component could not be cleaned up.

    Raised instead of silently swallowing the cleanup failure; the
    temporary path is included so callers can report/retry. The original
    exception is preserved in the ``__cause__`` chain where applicable.
    """

    def __init__(self, message: str, temp_path: str = ""):
        super().__init__(message)
        self.temp_path = temp_path


def _link_mode_name(link_mode: int | str | None) -> str:
    """Map the Zotero integer linkMode to its semantic name; unknown
    integers are preserved explicitly (``unknown(4)``)."""
    if isinstance(link_mode, str):
        return link_mode
    if link_mode is None:
        return LINK_IMPORTED
    return _LINK_MODE_NAMES.get(link_mode, f"unknown({link_mode})")


@dataclass
class Attachment:
    """One attachment; ``path`` is None when it cannot be resolved safely
    (missing file, traversal attempt, unknown link mode)."""

    item_key: str
    path: Optional[str]
    link_mode: str
    content_type: Optional[str]
    exists: bool


@dataclass
class ZoteroRecord:
    """In-memory migration record for one bibliographic item.

    ``source_key`` is the Zotero item key — kept in memory only and never
    serialized into canonical vault notes (no note-rendering methods here).
    """

    source_key: str
    item_type: str
    title: Optional[str]
    citation_key: Optional[str]
    year: Optional[str]
    creators: list[dict] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


class ZoteroAdapter:
    """Read-only adapter over a private copy of zotero.sqlite.

    Usage (context manager preferred — the temporary copy is always
    removed, on success and on error)::

        with ZoteroAdapter(db_path, data_dir) as ad:
            rec = ad.resolve("ATTACHKEY")
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        data_dir: str | Path | None = None,
    ):
        if db_path is None:
            db_path = Path.home() / "Zotero" / "zotero.sqlite"
        if data_dir is None:
            data_dir = Path.home() / "Zotero"
        db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        if not db_path.exists():
            raise FileNotFoundError(f"Zotero database not found: {db_path}")
        self._check_wal(db_path)
        # copy BEFORE opening read-only: the live library is never touched.
        # Failure-atomic: a failed copy/connect never leaves a temp file;
        # a cleanup failure is reported explicitly (never silent).
        fd, tmp = tempfile.mkstemp(prefix="paper-notes-zotero-", suffix=".sqlite")
        os.close(fd)
        self.tmp_db = Path(tmp)
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False
        try:
            # TOCTOU guard: WAL and source fingerprint checked around the
            # copy — any change means Zotero is writing; refuse the stale
            # snapshot instead of silently reading it.
            fp_before = self._source_fingerprint(db_path)
            shutil.copy2(db_path, tmp)
            self._check_wal(db_path)
            if self._source_fingerprint(db_path) != fp_before:
                raise ZoteroSnapshotError(
                    "Zotero database changed while it was being copied; "
                    "close Zotero and retry"
                )
            self._conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        except BaseException as exc:
            try:
                self._cleanup_copy(strict=True)
            except ZoteroCleanupError as cleanup_exc:
                raise cleanup_exc from exc
            raise

    @staticmethod
    def _source_fingerprint(db_path: Path) -> tuple[int, int]:
        st = db_path.stat()
        return (st.st_size, st.st_mtime_ns)

    @staticmethod
    def _check_wal(db_path: Path) -> None:
        """Reject a non-empty WAL journal: copying the main DB alone would
        silently produce a stale snapshot."""
        wal = Path(str(db_path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise ZoteroSnapshotError(
                "Zotero database has a non-empty WAL journal "
                "(zotero.sqlite-wal); close Zotero and retry — copying now "
                "would silently read stale data"
            )

    @staticmethod
    def _unlink_best_effort(tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Close the read-only connection and delete the private copy.

        Connection close and temp-copy deletion are decoupled (try/finally
        semantics): a connection-close error still deletes the copy. The
        strict cleanup deletes main + -wal + -shm and only marks the
        adapter closed when every component is gone; any failure raises
        :class:`ZoteroCleanupError` (with the temp path) so the caller can
        retry — never silently marked closed.
        """
        if self._closed:
            return
        conn, self._conn = self._conn, None
        close_error = None
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001 - decoupled below
                close_error = exc
        try:
            self._cleanup_copy(strict=True)
        except ZoteroCleanupError as cleanup_exc:
            if close_error is not None:
                raise ZoteroCleanupError(
                    "connection close and temp copy cleanup both failed",
                    temp_path=str(self.tmp_db),
                ) from close_error
            raise
        if close_error is not None:
            raise close_error
        self._closed = True

    def _cleanup_copy(self, strict: bool) -> None:
        """Delete the private copy: main .sqlite plus -wal/-shm sidecars.

        ``strict=True`` raises :class:`ZoteroCleanupError` (with
        ``temp_path``) when any component fails to unlink; ``strict=False``
        is best-effort (used by ``__del__``).
        """
        errors = []
        for component in (
            self.tmp_db,
            Path(str(self.tmp_db) + "-wal"),
            Path(str(self.tmp_db) + "-shm"),
        ):
            try:
                component.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{component}: {exc}")
        if errors and strict:
            raise ZoteroCleanupError(
                "temporary copy cleanup failed: " + "; ".join(errors),
                temp_path=str(self.tmp_db),
            )

    def __enter__(self) -> "ZoteroAdapter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        """Best-effort fallback cleanup for callers that never call
        close()/never use the context manager."""
        try:
            if not self._closed and self.tmp_db is not None:
                self._cleanup_copy(strict=False)
        except Exception:
            pass

    # -- queries -------------------------------------------------------

    def resolve(self, item_key: str) -> Optional[ZoteroRecord]:
        """Resolve an item key (attachment or bibliographic) to the parent
        bibliographic record. Notes, annotations and unknown items resolve
        to ``None``; only author creators, titles, dates and BBT citation
        keys are extracted (no notes, tags, collections or annotations)."""
        if self._closed:
            raise RuntimeError("adapter is closed")
        parent_id = self._parent_item_id(item_key)
        if parent_id is None:
            return None
        return self._bibliographic_record(parent_id)

    # -- internals -----------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "adapter not initialized"
        return self._conn

    def _parent_item_id(self, item_key: str) -> Optional[int]:
        cur = self._require_conn().cursor()
        cur.execute("SELECT itemID, itemTypeID FROM items WHERE key = ?", (item_key,))
        row = cur.fetchone()
        if row is None:
            return None
        item_id, item_type_id = row
        # attachment child -> parent bibliographic item; the parent must
        # itself be bibliographic (real library: embedded images parented
        # to notes must never pull note content into records)
        cur.execute(
            "SELECT parentItemID FROM itemAttachments WHERE itemID = ?", (item_id,)
        )
        parent = cur.fetchone()
        if parent is not None and parent[0] is not None:
            cur.execute(
                "SELECT itemTypeID FROM items WHERE itemID = ?", (parent[0],)
            )
            ptype = cur.fetchone()
            if ptype is not None and self._is_bibliographic(ptype[0]):
                return parent[0]
            return None
        if not self._is_bibliographic(item_type_id):
            return None
        return item_id

    def _is_bibliographic(self, item_type_id: int) -> bool:
        cur = self._require_conn().cursor()
        cur.execute(
            "SELECT typeName FROM itemTypes WHERE itemTypeID = ?", (item_type_id,)
        )
        row = cur.fetchone()
        return bool(row and row[0] not in _NON_BIBLIOGRAPHIC)

    def _field_value(self, item_id: int, field_name: str) -> Optional[str]:
        cur = self._require_conn().cursor()
        cur.execute(
            """
            SELECT idv.value FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID = ? AND f.fieldName = ?
            """,
            (item_id, field_name),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def _creators(self, item_id: int) -> list[dict]:
        """Real Zotero semantics: fieldMode 0 = family/given, 1 = literal."""
        cur = self._require_conn().cursor()
        cur.execute(
            """
            SELECT cr.firstName, cr.lastName, cr.fieldMode
            FROM itemCreators ic
            JOIN creators cr ON ic.creatorID = cr.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID = ? AND ct.creatorType = 'author'
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        )
        out = []
        for first, last, mode in cur.fetchall():
            if mode == 1:
                out.append({"literal": last})
            else:
                out.append({"family": last, "given": first})
        return out

    def _attachments(self, item_id: int) -> list[Attachment]:
        cur = self._require_conn().cursor()
        cur.execute(
            """
            SELECT i.key, ia.linkMode, ia.path, ia.contentType
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.parentItemID = ?
            ORDER BY ia.itemID
            """,
            (item_id,),
        )
        out = []
        for key, link_mode, path, content_type in cur.fetchall():
            link_mode = _link_mode_name(link_mode)
            url = self._attachment_url(key)
            resolved, exists = self._resolve_attachment_path(
                link_mode, path, key, url
            )
            out.append(
                Attachment(
                    item_key=key,
                    path=resolved,
                    link_mode=link_mode,
                    content_type=content_type,
                    exists=exists,
                )
            )
        return out

    def _attachment_url(self, attachment_item_key: str) -> Optional[str]:
        """Linked URLs live in the attachment item's ``url`` field (real
        Zotero: linkMode=3 records have an empty itemAttachments.path)."""
        cur = self._require_conn().cursor()
        cur.execute(
            """
            SELECT idv.value FROM items i
            JOIN itemData id ON i.itemID = id.itemID
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE i.key = ? AND f.fieldName = ?
            """,
            (attachment_item_key, _FIELD_URL),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def _storage_base(self, attachment_key: str) -> Optional[Path]:
        """Storage base for one attachment key: a single path segment
        inside ``<data_dir>/storage/`` — never an escape from the root."""
        storage_root = (self.data_dir / "storage").resolve()
        if (
            not attachment_key
            or "/" in attachment_key
            or "\\" in attachment_key
            or attachment_key in (".", "..")
        ):
            return None
        base = (storage_root / attachment_key).resolve()
        if not base.is_relative_to(storage_root):
            return None
        return base

    def _resolve_attachment_path(
        self,
        link_mode: str,
        path: str | None,
        attachment_key: str,
        url: str | None = None,
    ) -> tuple[Optional[str], bool]:
        if link_mode in (LINK_IMPORTED, LINK_IMPORTED_URL):
            # real layout: <data_dir>/storage/<attachmentItemKey>/<filename>
            base = self._storage_base(attachment_key)
            if base is None or not path or not path.startswith("storage:"):
                return None, False
            target = (base / path[len("storage:"):]).resolve()
            # reject absolute-path and .. traversal outside the boundary
            if not target.is_relative_to(base):
                return None, False
            return str(target), os.path.isfile(target)
        if link_mode == LINK_LINKED_FILE:
            # linked file: user-chosen absolute path kept as-is
            resolved = path or ""
            return resolved, os.path.isfile(resolved)
        if link_mode == LINK_LINKED_URL:
            # URL from the attachment item's url field
            return (url or ""), False
        if link_mode == LINK_IMPORTED_URL:
            return (url or ""), False
        # unknown link mode: preserved, never resolved
        return None, False

    def _bibliographic_record(self, item_id: int) -> ZoteroRecord:
        cur = self._require_conn().cursor()
        cur.execute("SELECT key, itemTypeID FROM items WHERE itemID = ?", (item_id,))
        key, item_type_id = cur.fetchone()
        cur.execute(
            "SELECT typeName FROM itemTypes WHERE itemTypeID = ?", (item_type_id,)
        )
        item_type = cur.fetchone()[0]
        date = self._field_value(item_id, _FIELD_DATE)
        return ZoteroRecord(
            source_key=key,
            item_type=item_type,
            title=self._field_value(item_id, _FIELD_TITLE),
            citation_key=self._field_value(item_id, _FIELD_CITATION_KEY),
            year=_year_of(date) if date else None,
            creators=self._creators(item_id),
            attachments=self._attachments(item_id),
        )


def _year_of(date: str) -> Optional[str]:
    import re

    m = re.match(r"\d{4}", date)
    return m.group(0) if m else None
