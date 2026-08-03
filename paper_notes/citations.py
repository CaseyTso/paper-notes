"""Citation-aware rename-key transaction (spec §4.3 / §8.2, plan Task 13).

``preview_rename_key`` is read-only: it resolves ``key`` (current key or
alias) to a canonical paper, validates the new key and every collision
gate, scans the whole vault with a parser-aware line scanner, and
returns a deterministic plan plus a confirmation token that binds the
``paper_id``, the canonical old/new keys, the fully sorted
moves/edits/occurrences, and every scanned/affected input file
(path/type/sha256/mode). No lock, no writes, hook 0.

``confirm_rename_key`` re-resolves and re-plans **under the workspace
write lock** (spec §17.4) and recomputes the token: any file added /
deleted / edited / type-changed, any note/source/target topology change,
or a target directory that appeared makes the presented token stale and
turns the confirm into a read-only :class:`ItemConflict` with zero
writes and hook 0. Only the exact authorized transition executes.

Execution is one all-or-nothing transaction on the same filesystem:

1. the item directory ``05 Literature/<old>/`` is atomically renamed to
   ``05 Literature/<new>/`` first (``os.rename``, rollback renames it
   back) — this keeps every later staged target on a stable path so the
   existing :class:`~paper_notes.fsops.StagedOperation` fingerprint
   verification can cover all of them at commit;
2. the exact global markdown scan-input manifest (``plan.files``) is
   re-verified at every execution boundary — at entry before any
   mutation/staging, after the item is hidden in the work directory,
   immediately before finalization, and after the final move — so a
   late add/delete/edit/chmod/type/symlink change anywhere in the
   vault's markdown candidate set conflicts read-only with the external
   state preserved at its exact path;
3. every edited file's bytes are re-verified against the plan state
   (check-to-use guard; a manual change aborts with the manual change
   preserved);
4. main note, derived notes, and vault files with occurrences are
   rewritten through the staged operation (mode preserved);
5. key files are renamed inside the directory via managed
   create+delete (so rollback and commit verification stay inside the
   staged operation), restoring the source permission bits;
6. post-verify (fresh index: new current key and old alias resolve to
   the same ``paper_id``, old directory gone, new directory complete)
   runs **before** the rebuild hook, so every rollback path fires the
   hook zero times; the hook fires exactly once on success; commit is
   last.

The scanner is an explicit line-based state machine (never an
unrestricted global regex): fenced code blocks (backticks or tildes, any
length, 0-3 space indentation), inline code spans (any backtick count,
multi-line), 4-space indented code, HTML ``<pre>``/``<code>`` regions,
backslash-escaped literals, and every YAML frontmatter block are
untouched. Pandoc ``[@old]`` / ``[@old; @other]`` (only the citation id)
and wikilinks ``[[old]]`` / ``[[old|x]]`` / ``[[old#h]]`` (only the
target base) are rewritten. Only ``.md`` files are scanned; hidden
directories, the ``.paper-notes`` transaction area, and symlinks are
never followed; unreadable files are a structured conflict.

Low-level exception text never reaches ``ItemError`` / ``ItemConflict``
messages (the CLI JSON envelope is therefore free of secrets).
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from . import fsops
from .frontmatter import FrontmatterError, RoundTripDocument, _parse, _serialize, load_paper_note
from .items import (
    ItemConflict,
    ItemError,
    RENAME_KEY_LOCK_OPERATION,
    _acquire,
    _assert_repository_consistent,
    _file_sha256,
    _is_real_regular_file,
    _noop_rebuild,
    _resolve_record,
    _serialize_content,
)
from .locking import release_lock
from .models import Paper
from .paths import is_valid_key, paper_directory
from .repository import RepositoryIndex, build_index

_STALE_TOKEN_MESSAGE = (
    "confirmation token is stale: the vault changed since the preview; "
    "re-run item rename-key --dry-run"
)

_KEY_FILE_NAMES = (
    "{old}.md",
    "{old}.pdf",
    "minerUmd_{old}.md",
    "Figure解读_{old}.md",
)


# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9+_.-]*")


@dataclass(frozen=True)
class ScanHit:
    """One replaceable slice in a scanned body.

    ``line`` is 0-based; ``start``/``end`` are 0-based columns of the
    slice that will be replaced by the new key (the citation id for
    Pandoc, the target base for wikilinks); ``column`` is the 0-based
    column reported to the user (the ``@`` for Pandoc, the first ``[``
    for wikilinks).
    """

    line: int
    start: int
    end: int
    column: int
    kind: str  # "pandoc" | "wikilink"


def _is_escaped(line: str, idx: int) -> bool:
    """True when ``line[idx]`` is preceded by an odd number of backslashes."""
    backslashes = 0
    i = idx - 1
    while i >= 0 and line[i] == "\\":
        backslashes += 1
        i -= 1
    return backslashes % 2 == 1


def _citation_opener(line: str, idx: int) -> bool:
    """The ``@`` at ``idx`` starts a citation when preceded by an
    unescaped opener (``[`` ``;`` space tab ``-`` ``+``) or line start."""
    if idx == 0:
        return True
    prev = line[idx - 1]
    return prev in "[; \t-+" and not _is_escaped(line, idx - 1)


def _id_end(line: str, at: int, old_key: str) -> int | None:
    """Column one past the citation id when ``@`` at ``at`` is exactly
    ``old_key`` with a non-key boundary; ``None`` otherwise."""
    match = _KEY_RE.match(line, at + 1)
    if match is None or match.group(0) != old_key:
        return None
    end = match.end()
    if end < len(line) and line[end] in (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+_.-"
    ):
        return None
    return end


def scan_markdown(text: str, old_key: str) -> list[ScanHit]:
    """Parser-aware scan of a markdown file for ``old_key`` occurrences.

    A leading YAML frontmatter block is never scanned (reporting line
    numbers are relative to the whole file text). Returns replaceable
    slices for Pandoc citations and wikilinks while keeping fenced code,
    inline code spans, indented code, HTML ``pre``/``code`` regions, and
    escaped literals immune. An unterminated leading frontmatter block
    makes the file unscannable (no hits).
    """
    body, start = _split_frontmatter(text)
    if start == -1:
        return []
    hits = _scan_body(body, old_key)
    if start == 0:
        return hits
    return [ScanHit(h.line + start, h.start, h.end, h.column, h.kind) for h in hits]


def _close_code_span(line: str, code_ticks: int) -> int | None:
    """Column one past the first run of exactly ``code_ticks`` backticks
    closing a multi-line inline code span, or ``None`` when the span
    stays open past ``line``."""
    pos = 0
    while True:
        run = re.search(r"`+", line[pos:])
        if run is None:
            return None
        if len(run.group(0)) == code_ticks:
            return pos + run.end()
        pos += run.end()


def _scan_body(body: str, old_key: str) -> list[ScanHit]:
    """State-machine scan of a markdown body (no frontmatter).

    ``pos`` tracks the scanning offset within the *original* line: when
    a multi-line HTML ``pre``/``code`` region or inline code span closes
    mid-line, the remaining suffix is scanned with columns still
    relative to the original full line (never a trimmed copy)."""
    hits: list[ScanHit] = []
    state = "normal"
    fence_char = ""
    fence_len = 0
    code_ticks = 0
    html_tag = ""
    for lineno, line in enumerate(body.split("\n")):
        pos = 0
        if state == "fence":
            if re.match(
                r"^ {0,3}" + re.escape(fence_char) + r"{%d,}\s*$" % fence_len, line
            ):
                state = "normal"
            continue
        if state == "code":
            closed = _close_code_span(line, code_ticks)
            if closed is None:
                continue
            state = "normal"
            pos = closed
        elif state == "indented_code":
            if line.strip() == "":
                continue  # blank lines inside indented code stay code
            if re.match(r"^ {4,}", line):
                continue
            state = "normal"
        elif state == "html":
            close = re.search(
                r"</" + re.escape(html_tag) + r"\s*>", line, re.IGNORECASE
            )
            if close is None:
                continue
            state = "normal"
            pos = close.end()

        # block-level constructs only apply to lines that start in the
        # normal state: a suffix after a closed span cannot open a
        # block-level code region
        if pos == 0:
            # block-level indented code: a non-blank line indented 4+
            # spaces
            if line.strip() and re.match(r"^ {4,}", line):
                state = "indented_code"
                continue
            # block-level fence opener (0-3 spaces, 3+ backticks/tildes)
            opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if opener:
                fence_char = opener.group(1)[0]
                fence_len = len(opener.group(1))
                state = "fence"
                continue

        # inline scan of the (possibly post-close) suffix; every column
        # is relative to the original full line because ``i`` indexes it
        i = pos
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "\\":
                i += 2  # escaped character is literal (e.g. \[, \[[, \@)
                continue
            if ch == "`":
                j = i
                while j < n and line[j] == "`":
                    j += 1
                ticks = j - i
                close = line.find("`" * ticks, j)
                if close == -1:
                    state = "code"
                    code_ticks = ticks
                    break  # span continues on the next line
                i = close + ticks
                continue
            if ch == "[" and not _is_escaped(line, i):
                if i + 1 < n and line[i + 1] == "[":
                    end = line.find("]]", i + 2)
                    if end == -1:
                        i += 1
                        continue
                    base_end = i + 2
                    while base_end < end and line[base_end] not in "|#]":
                        base_end += 1
                    if line[i + 2:base_end] == old_key:
                        hits.append(ScanHit(lineno, i + 2, base_end, i, "wikilink"))
                    i = end + 2
                    continue
                if i + 1 < n and line[i + 1] == "@":
                    end = _id_end(line, i + 1, old_key)
                    if end is not None:
                        hits.append(ScanHit(lineno, i + 2, end, i + 1, "pandoc"))
                    i += 2
                    continue
                i += 1
                continue
            if ch == "@" and _citation_opener(line, i):
                end = _id_end(line, i, old_key)
                if end is not None:
                    hits.append(ScanHit(lineno, i + 1, end, i, "pandoc"))
                i += 1
                continue
            if ch == "<":
                tag = re.match(r"<(pre|code)(?=[\s>/])", line[i:], re.IGNORECASE)
                if tag is not None:
                    name = tag.group(1).lower()
                    close = re.search(r"</" + name + r"\s*>", line[i:], re.IGNORECASE)
                    if close is not None:
                        i += close.end()
                        continue
                    state = "html"
                    html_tag = name
                    break
                i += 1
                continue
            i += 1
    return hits


def apply_hits(body: str, new_key: str, hits: Sequence[ScanHit]) -> str:
    """Replace the hit slices in ``body`` with ``new_key``.

    Only the citation id / wikilink target base is replaced; every other
    byte of the body (including line endings) is preserved verbatim.
    """
    if not hits:
        return body
    lines = body.split("\n")
    by_line: dict[int, list[ScanHit]] = {}
    for hit in hits:
        by_line.setdefault(hit.line, []).append(hit)
    for lineno, line_hits in by_line.items():
        line = lines[lineno]
        parts: list[str] = []
        cursor = 0
        for hit in sorted(line_hits, key=lambda h: h.start):
            parts.append(line[cursor:hit.start])
            parts.append(new_key)
            cursor = hit.end
        parts.append(line[cursor:])
        lines[lineno] = "".join(parts)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# plan data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenameOccurrence:
    path: Path
    kind: str
    line: int  # 1-based file line
    column: int  # 1-based file column
    context: str
    old: str
    new: str


@dataclass(frozen=True)
class RenameMove:
    source: Path
    target: Path
    kind: str  # "directory" | "file"


@dataclass(frozen=True)
class RenameEdit:
    path: Path
    kind: str  # "main_note" | "derived_note" | "occurrences"


@dataclass(frozen=True)
class FileState:
    path: Path
    type: str  # "file" | "dir" | "other"
    sha256: str | None
    mode: int | None


@dataclass(frozen=True)
class RenamePreview:
    status: str
    action: str
    paper_id: str
    citation_key: str  # canonical old key
    requested_key: str
    resolved_as: str  # "key" | "alias"
    new_key: str
    moves: list[RenameMove]
    edits: list[RenameEdit]
    occurrences: list[RenameOccurrence]
    warnings: list[str]
    confirmation_token: str
    plan: dict[str, Any]


@dataclass(frozen=True)
class RenameResult:
    status: str
    paper_id: str
    citation_key: str  # new key
    old_key: str
    path: str
    moves: list[RenameMove]
    edits: list[RenameEdit]
    occurrences: list[RenameOccurrence]
    warnings: list[str]


@dataclass
class RenamePlan:
    """Internal plan: the public view uses old-dir paths (what the token
    binds and the preview shows); the execution view uses the new-dir
    paths that exist after the directory rename."""

    paper_id: str
    old_key: str
    new_key: str
    requested_key: str
    resolved_as: str
    moves: list[RenameMove]
    edits: list[RenameEdit]
    occurrences: list[RenameOccurrence]
    warnings: list[str]
    files: list[FileState]
    subtree: list[FileState] = field(default_factory=list)  # whole old item subtree (binding)
    expected: dict[Path, tuple] = field(default_factory=dict)  # execution paths -> expected fingerprints
    contents: dict[Path, str | bytes] = field(default_factory=dict)  # execution paths
    note_sha: dict[Path, str] = field(default_factory=dict)  # execution paths
    deletes: list[Path] = field(default_factory=list)  # execution paths
    rename_pairs: list[tuple[Path, Path]] = field(default_factory=list)  # (src, dst) execution paths


# ---------------------------------------------------------------------------
# frontmatter splitting and file scanning
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[str, int]:
    """Return ``(body, body_start_line)`` for a markdown file.

    A leading ``--- ... ---`` block is never scanned; the body starts at
    ``body_start_line`` (0-based). An unterminated leading block makes
    the file unscannable (``("", -1)``).
    """
    if not text.startswith("---"):
        return text, 0
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        return text, 0
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r") == "---":
            return "\n".join(lines[idx + 1:]), idx + 1
    return "", -1


def _read_text(path: Path) -> str:
    """Read a file preserving its exact line endings (``newline=""``
    keeps CRLF bytes; ``Path.read_text`` would normalize them away)."""
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _load_light_doc(path: Path) -> RoundTripDocument:
    """Round-trip frontmatter document without Paper schema validation
    (derived notes carry only the minimum relationship fields)."""
    raw = _read_text(path)
    newline = "\r\n" if "\r\n" in raw else "\n"
    frontmatter, body = _parse(raw)
    return RoundTripDocument(frontmatter=frontmatter, body=body, newline=newline)


def _file_state(path: Path) -> FileState:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        # a type change (symlink / dir / other) since the scan must
        # surface as a different token, never be hashed through
        return FileState(
            path=path, type="other", sha256=None, mode=stat.S_IMODE(st.st_mode)
        )
    return FileState(
        path=path,
        type="file",
        sha256=_file_sha256(path),
        mode=stat.S_IMODE(st.st_mode),
    )


def _link_state(path: Path) -> FileState:
    """Manifest entry for a symlinked ``.md`` scan-input, bound WITHOUT
    following the link: SHA-256 of the raw link-target bytes (readlink
    never resolves) plus the link's own lstat mode. Add/delete/retarget/
    type/mode changes therefore conflict while the target's content is
    never read; an identity read failure surfaces as ``other`` so the
    comparison conflicts instead of leaking a low-level error."""
    try:
        target = os.readlink(path)
        sha = hashlib.sha256(os.fsencode(target)).hexdigest()
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        return FileState(path=path, type="other", sha256=None, mode=None)
    return FileState(path=path, type="link", sha256=sha, mode=mode)


def _scan_item_subtree(old_dir: Path) -> list[FileState]:
    """Read-only manifest of the whole item subtree that will move.

    Binds every regular file (type/sha256/mode), every directory
    (type/mode, including empty directories), and every hidden entry
    inside the subtree to the confirmation token: the item directory
    moves as a whole, so any path add/delete/type/mode change after the
    preview must make the token stale. Symlinks and other non-regular
    entries are a structured preview conflict — never silently moved.
    """
    entries: list[FileState] = []
    for dirpath, dirnames, filenames in os.walk(old_dir):
        dirnames[:] = sorted(dirnames)
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            st = p.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise ItemConflict(
                    f"item subtree {p} is not a directory; refusing to move it"
                )
            entries.append(
                FileState(path=p, type="dir", sha256=None, mode=stat.S_IMODE(st.st_mode))
            )
        for f in sorted(filenames):
            p = Path(dirpath) / f
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ItemConflict(
                    f"item subtree {p} is not a regular file; refusing to move it"
                )
            entries.append(
                FileState(
                    path=p,
                    type="file",
                    sha256=_file_sha256(p),
                    mode=stat.S_IMODE(st.st_mode),
                )
            )
    st = old_dir.lstat()
    entries.append(
        FileState(path=old_dir, type="dir", sha256=None, mode=stat.S_IMODE(st.st_mode))
    )
    return sorted(entries, key=lambda e: str(e.path))


def _scan_vault(
    root: Path, old_key: str, new_key: str
) -> tuple[dict[Path, list[ScanHit]], list[RenameOccurrence], list[FileState], list[str]]:
    """Scan every markdown file in the vault (read-only).

    Hidden directories/files, the ``.paper-notes`` area, symlinked
    directories (never descended) and symlinked files (never read, but
    bound by their raw link identity so retarget/type/mode changes make
    the token stale) are skipped; a symlinked directory whose name ends
    in ``.md`` is bound exactly like a symlinked file. Non-UTF-8 /
    malformed files are skipped with a warning but bound by their exact
    bytes; unreadable files are a structured conflict.
    """
    hits_by_path: dict[Path, list[ScanHit]] = {}
    occurrences: list[RenameOccurrence] = []
    files: list[FileState] = []
    warnings: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        keep: list[str] = []
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            if d.startswith("."):
                continue
            try:
                st = p.lstat()
            except OSError:
                raise ItemConflict(f"cannot inspect {p}") from None
            if stat.S_ISLNK(st.st_mode):
                # a symlink whose target is a directory is listed by
                # os.walk under dirnames, not filenames: bind any
                # non-hidden ``.md`` link exactly like a file symlink
                # (raw link-target hash + lstat mode, never followed)
                # so a later add/delete/retarget/type/mode change makes
                # the token stale, then always prune it from descent.
                if d.endswith(".md"):
                    warnings.append(f"skipped symlink {p}")
                    files.append(_link_state(p))
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                warnings.append(f"skipped symlink {path}")
                # the link itself is still bound to the token WITHOUT
                # following it: raw target hash + lstat mode, so an
                # add/delete/retarget/type/mode change after the preview
                # makes the token stale
                files.append(_link_state(path))
                continue
            try:
                st = path.stat()
            except OSError:
                raise ItemConflict(f"cannot inspect {path}") from None
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                text = _read_text(path)
            except UnicodeDecodeError:
                warnings.append(f"skipped non-UTF-8 markdown file {path}")
                # the file is still bound to the token by its bytes: a
                # change after the preview must make the token stale
                files.append(_file_state(path))
                continue
            except OSError:
                raise ItemConflict(f"cannot read {path}") from None
            hits = scan_markdown(text, old_key)
            if start_of_body(text) == -1:
                warnings.append(f"skipped malformed frontmatter in {path}")
                files.append(_file_state(path))
                continue
            files.append(_file_state(path))
            if not hits:
                continue
            hits_by_path[path] = hits
            text_lines = text.split("\n")
            for hit in hits:
                occurrences.append(
                    RenameOccurrence(
                        path=path,
                        kind=hit.kind,
                        line=hit.line + 1,
                        column=hit.column + 1,
                        context=text_lines[hit.line],
                        old=old_key,
                        new=new_key,
                    )
                )
    return hits_by_path, occurrences, files, warnings


def start_of_body(text: str) -> int:
    """0-based line where the markdown body begins (0 without
    frontmatter, -1 for an unterminated leading block)."""
    return _split_frontmatter(text)[1]


# ---------------------------------------------------------------------------
# plan construction (read-only)
# ---------------------------------------------------------------------------


def _remap_to_body(raw_text: str, hits: Sequence[ScanHit]) -> list[ScanHit]:
    """Shift full-file hit lines into body coordinates (the frontmatter
    codec serializes the body separately). Columns are unaffected."""
    _, start = _split_frontmatter(raw_text)
    if start == 0:
        return list(hits)
    return [ScanHit(h.line - start, h.start, h.end, h.column, h.kind) for h in hits]


def _build_plan(
    root: Path,
    index: RepositoryIndex,
    record: Any,
    requested_key: str,
    new_key: str,
    resolved_as: str,
) -> RenamePlan:
    """Construct the full rename plan (read-only; every collision gate
    blocks here, before any write)."""
    old_key = record.paper.citation_key
    paper_id = str(record.paper.paper_id)

    if not is_valid_key(new_key):
        raise ItemError(f"invalid citation key {new_key!r}")
    if new_key == old_key:
        raise ItemError("new citation key must differ from the current key")
    occupied = set(index.by_key) | set(index.aliases)
    if new_key in occupied:
        raise ItemConflict(
            f"citation key {new_key!r} is already reserved by another item"
        )

    old_dir = paper_directory(root, old_key)
    new_dir = paper_directory(root, new_key)

    # item directory topology: the old directory must be a real
    # directory and the target must be completely absent.
    if old_dir.is_symlink() or not old_dir.is_dir():
        raise ItemConflict(
            f"item directory {old_dir} is not a real directory; refusing to touch it"
        )
    if new_dir.exists() or new_dir.is_symlink():
        raise ItemConflict(
            f"target directory {new_dir} already exists; refusing to merge into it"
        )

    # the whole old item subtree is bound to the token (the directory
    # moves as one unit): every regular file (type/hash/mode), every
    # directory (type/mode, including empty ones) and every hidden
    # entry; symlinks / other types are a structured preview conflict
    subtree = _scan_item_subtree(old_dir)

    # key-dependent files: real regular files only; their new names must
    # not be occupied inside the old directory.
    existing_key_files: list[tuple[Path, Path]] = []
    for name in _KEY_FILE_NAMES:
        src = old_dir / name.format(old=old_key)
        if not src.exists() and not src.is_symlink():
            continue
        if src.is_symlink() or not _is_real_regular_file(src):
            raise ItemConflict(
                f"key file {src} is a symlink or not a regular file; refusing to touch it"
            )
        dst = old_dir / name.format(old=new_key)
        if dst.exists() or dst.is_symlink():
            raise ItemConflict(
                f"target file {dst} is already occupied; refusing to overwrite it"
            )
        existing_key_files.append((src, dst))
    if not any(src.name.endswith(".md") for src, _ in existing_key_files):
        raise ItemConflict(f"main note {old_dir / f'{old_key}.md'} is missing")

    # derived notes (cards) must be real regular files when present
    cards_dir = old_dir / "cards"
    cards: list[Path] = []
    if cards_dir.is_dir():
        if cards_dir.is_symlink():
            raise ItemConflict(
                f"cards directory {cards_dir} is a symlink; refusing to touch it"
            )
        for card in sorted(cards_dir.glob("*.md")):
            if card.is_symlink() or not _is_real_regular_file(card):
                raise ItemConflict(
                    f"derived note {card} is a symlink or not a regular file; refusing to touch it"
                )
            cards.append(card)

    # full-vault scan
    hits_by_path, occurrences, files, warnings = _scan_vault(root, old_key, new_key)

    def moved(path: Path) -> Path:
        rel = path.relative_to(old_dir)
        return new_dir / rel

    contents: dict[Path, str | bytes] = {}
    note_sha: dict[Path, str] = {}
    edits: list[RenameEdit] = []

    # --- main note edit ---
    main_old = old_dir / f"{old_key}.md"
    main_new = new_dir / f"{old_key}.md"
    _, doc = load_paper_note(main_old)
    aliases = [
        a for a in (doc.frontmatter.get("citation_key_aliases") or []) if a != old_key
    ]
    aliases.append(old_key)
    doc.frontmatter["citation_key"] = new_key
    doc.frontmatter["citation_key_aliases"] = aliases
    try:
        Paper(**dict(doc.frontmatter))  # schema pre-validation before any write
    except ValidationError:
        raise ItemError("invalid main note after rename") from None
    doc.body = apply_hits(
        doc.body, new_key, _remap_to_body(_read_text(main_old), hits_by_path.get(main_old, []))
    )
    contents[main_new] = _serialize_content(doc.frontmatter, doc.body, doc.newline)
    note_sha[main_new] = _file_state(main_old).sha256
    edits.append(RenameEdit(path=main_old, kind="main_note"))

    # --- derived note edits (spec-named + cards) ---
    derived_old: list[Path] = []
    for name in (f"minerUmd_{old_key}.md", f"Figure解读_{old_key}.md"):
        p = old_dir / name
        if p.exists() and not p.is_symlink():
            derived_old.append(p)
    derived_old.extend(cards)
    for src in derived_old:
        dst = moved(src)
        raw_text = _read_text(src)
        body_hits = _remap_to_body(raw_text, hits_by_path.get(src, []))
        try:
            doc = _load_light_doc(src)
        except FrontmatterError:
            warnings.append(
                f"derived note {src} has no parseable frontmatter; "
                "relationship fields not updated"
            )
            contents[dst] = apply_hits(raw_text, new_key, hits_by_path.get(src, []))
            note_sha[dst] = _file_state(src).sha256
            edits.append(RenameEdit(path=src, kind="derived_note"))
            continue
        if "citation_key" in doc.frontmatter:
            doc.frontmatter["citation_key"] = new_key
        if "paper" in doc.frontmatter:
            doc.frontmatter["paper"] = f"[[{new_key}]]"
        doc.body = apply_hits(doc.body, new_key, body_hits)
        contents[dst] = _serialize(doc)
        note_sha[dst] = _file_state(src).sha256
        edits.append(RenameEdit(path=src, kind="derived_note"))

    # --- raw vault occurrence edits ---
    for path in sorted(hits_by_path, key=str):
        if path == main_old or path in derived_old:
            continue
        contents[path] = apply_hits(_read_text(path), new_key, hits_by_path[path])
        note_sha[path] = _file_state(path).sha256
        edits.append(RenameEdit(path=path, kind="occurrences"))

    # --- key file renames (create+delete inside the staged operation) ---
    moves: list[RenameMove] = [
        RenameMove(source=old_dir, target=new_dir, kind="directory")
    ]
    deletes: list[Path] = []
    rename_pairs: list[tuple[Path, Path]] = []
    for src, dst in existing_key_files:
        src_exec = moved(src)
        dst_exec = moved(dst)
        moves.append(RenameMove(source=src, target=dst_exec, kind="file"))
        deletes.append(src_exec)
        rename_pairs.append((src_exec, dst_exec))
        if src == main_old or src in derived_old:
            # the renamed copy shares the edited content of the source
            contents[dst_exec] = contents[src_exec]
        # else: the primary PDF bytes are read fresh at execution time

    # execution expected fingerprints: sampled at plan time from the
    # plan-time paths (never trusted to the stage-time state). New
    # targets are expected-absent; everything else keeps its exact
    # plan-time type/hash/mode.
    expected: dict[Path, tuple] = {}
    for path in contents:
        src = (
            old_dir / path.relative_to(new_dir)
            if path.is_relative_to(new_dir)
            else path
        )
        expected[path] = fsops._file_fingerprint(src)
    for src, dst in rename_pairs:
        if dst not in contents:
            expected[dst] = fsops._file_fingerprint(old_dir / dst.name)
        expected[src] = fsops._file_fingerprint(old_dir / src.name)

    # the token's file list: every scanned .md plus affected non-markdown
    # key files (the primary PDF)
    for src, _ in existing_key_files:
        if src.suffix != ".md":
            files.append(_file_state(src))
    files = sorted(files, key=lambda f: str(f.path))

    return RenamePlan(
        paper_id=paper_id,
        old_key=old_key,
        new_key=new_key,
        requested_key=requested_key,
        resolved_as=resolved_as,
        moves=moves,
        edits=sorted(edits, key=lambda e: (str(e.path), e.kind)),
        occurrences=sorted(
            occurrences, key=lambda o: (str(o.path), o.line, o.column, o.kind)
        ),
        warnings=warnings,
        files=files,
        subtree=subtree,
        expected=expected,
        contents=contents,
        note_sha=note_sha,
        deletes=deletes,
        rename_pairs=rename_pairs,
    )


def _rewrite_raw(
    text: str, body: str, start: int, hits: Sequence[ScanHit], new_key: str
) -> str:
    """Apply body hits to a raw file, splicing the untouched frontmatter back."""
    new_body = apply_hits(body, new_key, hits)
    if start == 0:
        return new_body
    lines = text.split("\n")
    return "\n".join(lines[:start] + new_body.split("\n"))


# ---------------------------------------------------------------------------
# token / preview / confirm
# ---------------------------------------------------------------------------


def _rename_token(plan: RenamePlan) -> str:
    payload = {
        "paper_id": plan.paper_id,
        "old_key": plan.old_key,
        "new_key": plan.new_key,
        "moves": sorted(
            (
                {"source": str(m.source), "target": str(m.target), "kind": m.kind}
                for m in plan.moves
            ),
            key=lambda d: (d["source"], d["target"]),
        ),
        "edits": sorted(
            ({"path": str(e.path), "kind": e.kind} for e in plan.edits),
            key=lambda d: (d["path"], d["kind"]),
        ),
        "occurrences": sorted(
            (
                {"path": str(o.path), "kind": o.kind, "line": o.line, "column": o.column}
                for o in plan.occurrences
            ),
            key=lambda d: (d["path"], d["line"], d["column"], d["kind"]),
        ),
        "files": sorted(
            (
                {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
                for f in plan.files
            ),
            key=lambda d: d["path"],
        ),
        "subtree": sorted(
            (
                {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
                for f in plan.subtree
            ),
            key=lambda d: d["path"],
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _plan_json(plan: RenamePlan, token: str) -> dict[str, Any]:
    return {
        "action": "rename_key",
        "paper_id": plan.paper_id,
        "old_key": plan.old_key,
        "new_key": plan.new_key,
        "requested_key": plan.requested_key,
        "resolved_as": plan.resolved_as,
        "moves": [
            {"source": str(m.source), "target": str(m.target), "kind": m.kind}
            for m in plan.moves
        ],
        "edits": [{"path": str(e.path), "kind": e.kind} for e in plan.edits],
        "occurrences": [
            {
                "path": str(o.path),
                "kind": o.kind,
                "line": o.line,
                "column": o.column,
                "context": o.context,
            }
            for o in plan.occurrences
        ],
        "warnings": list(plan.warnings),
        "files": [
            {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
            for f in plan.files
        ],
        "subtree": [
            {"path": str(f.path), "type": f.type, "sha256": f.sha256, "mode": f.mode}
            for f in plan.subtree
        ],
        "confirmation_token": token,
    }


def preview_rename_key(vault_root: str | Path, *, key: str, new_key: str) -> RenamePreview:
    """Read-only dry-run: resolve, validate, scan, and return the plan
    plus a deterministic confirmation token. No lock, zero writes, hook 0."""
    root = Path(vault_root)
    index = build_index(root)
    _assert_repository_consistent(index)
    record, resolved_as = _resolve_record(index, key)
    plan = _build_plan(root, index, record, key, new_key, resolved_as)
    token = _rename_token(plan)
    return RenamePreview(
        status="needs_confirmation",
        action="rename_key",
        paper_id=plan.paper_id,
        citation_key=plan.old_key,
        requested_key=key,
        resolved_as=resolved_as,
        new_key=new_key,
        moves=plan.moves,
        edits=plan.edits,
        occurrences=plan.occurrences,
        warnings=plan.warnings,
        confirmation_token=token,
        plan=_plan_json(plan, token),
    )


def confirm_rename_key(
    vault_root: str | Path,
    *,
    key: str,
    new_key: str,
    confirm_token: str,
    rebuild_hook: Callable[[], None] | None = None,
) -> RenameResult:
    """Execute the token-authorized rename under the workspace lock.

    The authoritative index, plan, and token are recomputed under the
    lock; any change since the preview makes the token stale (read-only
    :class:`ItemConflict`, zero writes, hook 0). The rebuild hook fires
    exactly once on success and zero times on preview/failure.
    """
    root = Path(vault_root)
    hook = rebuild_hook or _noop_rebuild
    lock = _acquire(root, RENAME_KEY_LOCK_OPERATION)
    try:
        index = build_index(root)  # fresh, authoritative index under lock
        _assert_repository_consistent(index)
        record, resolved_as = _resolve_record(index, key)
        # Consumed-authorization detection: the requested key is now a
        # reserved alias and the current key equals the requested new
        # key — the exact transition the token authorized has already
        # happened. This is a stale ItemConflict (rc3, zero writes,
        # hook 0), never a plain same-key user error.
        if resolved_as == "alias" and record.paper.citation_key == new_key:
            raise ItemConflict(_STALE_TOKEN_MESSAGE)
        plan = _build_plan(root, index, record, key, new_key, resolved_as)
        token = _rename_token(plan)
        if not hmac.compare_digest(str(confirm_token), token):
            raise ItemConflict(_STALE_TOKEN_MESSAGE)
        _execute(root, plan, hook)
        return RenameResult(
            status="renamed",
            paper_id=plan.paper_id,
            citation_key=plan.new_key,
            old_key=plan.old_key,
            path=str(paper_directory(root, plan.new_key) / f"{plan.new_key}.md"),
            moves=plan.moves,
            edits=plan.edits,
            occurrences=plan.occurrences,
            warnings=plan.warnings,
        )
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# execution transaction
# ---------------------------------------------------------------------------


def _post_verify(root: Path, plan: RenamePlan) -> None:
    """Fresh-index verification of the rename result; runs before the
    hook so a failed verify never fired the rebuild. Raises on any
    mismatch (the transaction rolls back)."""
    index = build_index(root)
    _assert_repository_consistent(index)
    record = index.by_key.get(plan.new_key)
    if record is None or str(record.paper.paper_id) != plan.paper_id:
        raise ItemError("post-verify failed: new key does not resolve to the renamed paper")
    if index.aliases.get(plan.old_key) != plan.new_key:
        raise ItemError("post-verify failed: old key is not a reserved alias")
    if paper_directory(root, plan.old_key).exists() or paper_directory(
        root, plan.old_key
    ).is_symlink():
        raise ItemError("post-verify failed: old directory still present")
    new_dir = paper_directory(root, plan.new_key)
    if not new_dir.is_dir() or not (new_dir / f"{plan.new_key}.md").is_file():
        raise ItemError("post-verify failed: new item directory is incomplete")


_RENAME_EXCL = 0x00000004  # macOS <stdio.h>: fail when the target exists


def _rename_dir_noreplace(source: Path, target: Path) -> None:
    """Atomically rename ``source`` onto ``target`` only when ``target``
    is absent — an existing target (file, directory, or symlink) is
    never replaced.

    macOS provides ``renamex_np(..., RENAME_EXCL)`` (EEXIST when the
    target exists, even for an empty directory). Other platforms fail
    closed: an unsupported platform raises :class:`ItemError` instead of
    falling back to an overwrite-capable ``os.rename``.
    """
    if sys.platform != "darwin":
        raise ItemError(
            "atomic no-replace directory move is unsupported on this platform"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.restype = ctypes.c_int
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    result = renamex_np(os.fsencode(source), os.fsencode(target), _RENAME_EXCL)
    if result != 0:
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise ItemConflict(
                "concurrent change detected: target appeared before the rename"
            ) from None
        raise OSError(err, os.strerror(err), str(target))


def _restore_dir_rename(held_dir: Path, old_dir: Path) -> None:
    """Restore the item directory with no-replace semantics.

    ``held_dir`` is where the item currently lives (the hidden work
    directory during the editing phase, or the final new directory
    after finalization). A path that appeared at ``old_dir`` during the
    transaction (an external racer) is preserved in place; the item then
    cannot be restored and the conflict is reported explicitly — the
    racer is never overwritten and the item stays at ``held_dir``.
    """
    try:
        _rename_dir_noreplace(held_dir, old_dir)
    except ItemConflict:
        raise ItemConflict(
            "concurrent change detected while renaming citation key: "
            f"a conflicting directory appeared at the original item path {old_dir}; "
            f"it was preserved and the item remains at {held_dir}"
        ) from None


def _stage_expected(
    op: fsops.StagedOperation, path: Path, expected: tuple
) -> None:
    """Stage ``path`` with the plan's expected state verified BEFORE and
    AFTER the stage.

    A racer edit in the window must conflict with the external bytes
    preserved — never become the new baseline (stage snapshots the
    current bytes, so a post-stage re-check is what keeps a
    plan-time→stage-time change from being adopted). New targets must
    be expected-absent before and after their stage.
    """
    if fsops._file_fingerprint(path) != expected:
        raise fsops.OperationConflict(f"target {path} changed since the plan")
    fsops.stage_target(op, path)
    if fsops._file_fingerprint(path) != expected:
        raise fsops.OperationConflict(f"target {path} changed while being staged")


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_key_source(source: Path, expected: tuple) -> bytes:
    """Read a key-file source with the plan's expected state verified
    before AND after the read.

    A deleted / replaced / symlink / directory / mode-changed source
    fails closed as a conflict (the external state is preserved), so
    stale bytes are never written to the renamed target.
    """
    if fsops._file_fingerprint(source) != expected:
        raise fsops.OperationConflict(f"key source {source} changed since the plan")
    data = _read_bytes(source)
    if fsops._file_fingerprint(source) != expected:
        raise fsops.OperationConflict(f"key source {source} changed while reading")
    return data


# ---------------------------------------------------------------------------
# hidden work directory and subtree manifest guards
# ---------------------------------------------------------------------------

_WORK_SUFFIX = ".rename-work"


def _work_directory(old_dir: Path) -> Path:
    """Hidden, same-filesystem work directory the transaction edits in.

    The real item directory is moved here first so the user-visible
    final path ``<new>`` never holds the item while it is being edited:
    a final-target racer at ``<new>`` can only be detected by the final
    no-replace rename, where it is preserved in place (Defect 3). The
    name is deterministic per item (``.{key}.rename-work``) and unique
    in practice because every rename runs under the exclusive workspace
    write lock; a stale leftover fails the entry rename closed (EEXIST)
    instead of being reused.
    """
    return old_dir.parent / f".{old_dir.name}{_WORK_SUFFIX}"


def _manifest_state(path: Path) -> FileState:
    """lstat-based manifest entry: regular file (hash/mode), directory
    (mode), or ``other`` (symlink / fifo / socket). A file that vanished
    between lstat and read surfaces as ``other`` so the manifest
    comparison conflicts instead of leaking a low-level error."""
    st = path.lstat()
    if stat.S_ISREG(st.st_mode):
        try:
            sha = _file_sha256(path)
        except ItemError:
            return FileState(
                path=path, type="other", sha256=None, mode=stat.S_IMODE(st.st_mode)
            )
        return FileState(
            path=path,
            type="file",
            sha256=sha,
            mode=stat.S_IMODE(st.st_mode),
        )
    if stat.S_ISDIR(st.st_mode):
        return FileState(
            path=path, type="dir", sha256=None, mode=stat.S_IMODE(st.st_mode)
        )
    return FileState(
        path=path, type="other", sha256=None, mode=stat.S_IMODE(st.st_mode)
    )


def _scan_manifest(root_dir: Path) -> dict[Path, FileState]:
    """Read-only, lstat-based manifest of ``root_dir`` and everything
    under it (symlinks are never followed; empty directories included)."""
    entries: dict[Path, FileState] = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(dirnames)
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            entries[p] = _manifest_state(p)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            entries[p] = _manifest_state(p)
    entries[root_dir] = _manifest_state(root_dir)
    return entries


def _verify_subtree(
    expected: Sequence[FileState],
    scan_root: Path,
    path_map: Callable[[Path], Path],
) -> None:
    """Execution-time guard: the live subtree at ``scan_root`` must be
    the exact ``expected`` plan manifest (complete path set plus
    type/hash/mode), with paths mapped through ``path_map``. Any
    add/delete/edit/chmod/type/symlink/empty-dir change is a read-only
    :class:`ItemConflict`."""
    current = _scan_manifest(scan_root)
    expected_by_path = {path_map(state.path): state for state in expected}
    for exp_path, state in expected_by_path.items():
        cur = current.get(exp_path)
        if cur is None or (cur.type, cur.sha256, cur.mode) != (
            state.type,
            state.sha256,
            state.mode,
        ):
            raise ItemConflict(
                "concurrent change detected: the item subtree changed since "
                "the plan; nothing was written"
            )
    for cur_path in current:
        if cur_path not in expected_by_path:
            raise ItemConflict(
                "concurrent change detected: the item subtree changed since "
                "the plan; nothing was written"
            )


def _map_old_to_work(path: Path, old_dir: Path, work_dir: Path) -> Path:
    return work_dir / path.relative_to(old_dir)


def _map_to_work(path: Path, new_dir: Path, work_dir: Path) -> Path:
    """Map a plan execution path (new-dir based for item-internal
    targets, untouched for global vault files) onto the work directory."""
    if path.is_relative_to(new_dir):
        return work_dir / path.relative_to(new_dir)
    return path


def _verify_untouched_subtree(
    plan: RenamePlan, work_dir: Path, old_dir: Path, new_dir: Path
) -> None:
    """Pre-finalization guard: every subtree entry the transaction did
    not manage must still carry its exact plan-time type/hash/mode, and
    no unexpected path may have appeared (the managed rename targets
    excepted)."""
    current = _scan_manifest(work_dir)
    expected_by_path = {
        _map_old_to_work(state.path, old_dir, work_dir): state
        for state in plan.subtree
    }
    managed_written = {
        _map_to_work(p, new_dir, work_dir)
        for p in plan.contents
        if p.is_relative_to(new_dir)
    }
    managed_deleted = {_map_to_work(p, new_dir, work_dir) for p in plan.deletes}
    managed_added = {
        _map_to_work(dst, new_dir, work_dir) for _, dst in plan.rename_pairs
    }
    for exp_path, state in expected_by_path.items():
        if exp_path in managed_written or exp_path in managed_deleted:
            continue
        cur = current.get(exp_path)
        if cur is None or (cur.type, cur.sha256, cur.mode) != (
            state.type,
            state.sha256,
            state.mode,
        ):
            raise ItemConflict(
                "concurrent change detected: the item subtree changed since "
                "the plan; nothing was written"
            )
    for cur_path in current:
        if cur_path in expected_by_path or cur_path in managed_added:
            continue
        raise ItemConflict(
            "concurrent change detected: the item subtree changed since "
            "the plan; nothing was written"
        )


def _global_md_manifest(root: Path, skip_subtree: Path | None) -> dict[Path, FileState]:
    """Read-only manifest of the vault's markdown scan-input candidate
    set, mirroring ``_scan_vault``'s exclusion policy exactly: hidden
    directories/files, symlinked directories (never descended) and the
    item subtree (``skip_subtree``, bound independently by
    ``plan.subtree`` and moved to the hidden work directory) are not
    part of the global set.

    Regular UTF-8 / non-UTF-8 / malformed-frontmatter markdown are all
    bound by their exact bytes (type/sha256/mode); symlinked ``.md``
    files are bound WITHOUT following the link (raw link-target hash +
    lstat mode), and so is a symlinked directory whose name ends in
    ``.md``; non-regular non-symlink entries are skipped exactly as
    the plan-time scan skips them. A path that vanishes between the walk
    and its lstat surfaces as a structured conflict, never a low-level
    error.
    """
    entries: dict[Path, FileState] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        pruned: list[str] = []
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            if d.startswith("."):
                continue
            if skip_subtree is not None and p.is_relative_to(skip_subtree):
                continue
            try:
                st = p.lstat()
            except OSError:
                raise ItemConflict(f"cannot inspect {p}") from None
            if stat.S_ISLNK(st.st_mode):
                # directory-targeted symlinks arrive under dirnames:
                # bind non-hidden ``.md`` links like file symlinks
                # (never followed) and never descend into any symlink
                if d.endswith(".md"):
                    entries[p] = _link_state(p)
                continue
            pruned.append(d)
        dirnames[:] = pruned
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                st = path.lstat()
            except OSError:
                raise ItemConflict(f"cannot inspect {path}") from None
            if stat.S_ISLNK(st.st_mode):
                entries[path] = _link_state(path)
            elif stat.S_ISREG(st.st_mode):
                entries[path] = _manifest_state(path)
            # else: fifo/socket/device — never a scan-input at plan time
    return entries


def _verify_global_scan(
    plan: RenamePlan,
    root: Path,
    old_dir: Path,
    new_dir: Path,
    item_root: Path,
) -> None:
    """Execution-time guard: the live vault markdown scan-input set
    (complete candidate path set plus lstat type/hash/mode, symlink
    identity without following) must equal the plan-time snapshot in
    ``plan.files``.

    The affected old item subtree is excluded from both sides: it is
    bound independently by ``plan.subtree`` and currently lives at
    ``item_root`` (old / hidden work / final new). The global
    occurrence paths this transaction itself manages are also excluded
    — they are verified by the per-write expected-state guards and the
    commit re-check, so a comparison against the pre-write plan bytes
    would be a false conflict. Everything else — additions, deletions,
    edits, chmods, file<->dir/symlink type changes, non-UTF-8 /
    malformed markdown byte changes, and symlink add/retarget/type
    changes — conflicts read-only with the external state preserved at
    its exact path.
    """
    managed_global = {p for p in plan.contents if not p.is_relative_to(new_dir)}
    expected = {
        state.path: state
        for state in plan.files
        if not state.path.is_relative_to(old_dir) and state.path not in managed_global
    }
    current = _global_md_manifest(root, item_root)
    for path in managed_global:
        current.pop(path, None)
    for exp_path, state in expected.items():
        cur = current.pop(exp_path, None)
        if cur is None or (cur.type, cur.sha256, cur.mode) != (
            state.type,
            state.sha256,
            state.mode,
        ):
            raise ItemConflict(
                "concurrent change detected: a markdown file changed since "
                "the plan; nothing was written"
            )
    if current:
        raise ItemConflict(
            "concurrent change detected: a markdown file appeared since "
            "the plan; nothing was written"
        )


def _remap_op_targets(
    op: fsops.StagedOperation, work_dir: Path, new_dir: Path
) -> None:
    """Rewrite staged target paths from the work directory to the final
    new directory after the atomic work->new rename (content-based
    fingerprints and the staging backups are unaffected)."""
    remapped: dict[Path, fsops.StagedTarget] = {}
    for path, staged in op.targets.items():
        if path.is_relative_to(work_dir):
            new_path = new_dir / path.relative_to(work_dir)
            staged.missing_parents = [
                new_dir / parent.relative_to(work_dir)
                if parent.is_relative_to(work_dir)
                else parent
                for parent in staged.missing_parents
            ]
            remapped[new_path] = staged
        else:
            remapped[path] = staged
    op.targets = remapped


def _abort(
    op: fsops.StagedOperation | None,
    committed: bool,
    phase: str,
    old_dir: Path,
    new_dir: Path,
    work_dir: Path,
) -> None:
    """Roll back the staged operation and restore the item directory
    according to the phase, using no-replace renames only. Nested
    rollback/restore failures are sanitized; a refused restore (racer at
    the original path) surfaces as its own ItemConflict."""
    try:
        if op is not None and not committed:
            fsops.rollback(op)
        if phase == "work":
            _restore_dir_rename(work_dir, old_dir)
        elif phase == "final":
            _restore_dir_rename(new_dir, old_dir)
    except ItemConflict:
        raise
    except Exception:
        raise ItemError("citation-key rename failed while rolling back") from None


def _execute(root: Path, plan: RenamePlan, hook: Callable[[], None]) -> None:
    old_dir = paper_directory(root, plan.old_key)
    new_dir = paper_directory(root, plan.new_key)
    work_dir = _work_directory(old_dir)
    op: fsops.StagedOperation | None = None
    committed = False
    phase = "pre"  # "pre" | "work" | "final"
    try:
        # 0. exact global markdown scan-input manifest check before any
        #    mutation or staging: additions / deletions / edits / chmods
        #    / type and symlink changes anywhere in the vault's markdown
        #    candidate set conflict read-only
        _verify_global_scan(plan, root, old_dir, new_dir, old_dir)

        # 0.5 exact plan.subtree manifest check before any mutation
        _verify_subtree(plan.subtree, old_dir, lambda p: p)

        op = fsops.begin_operation(root, uuid.uuid4().hex)

        # 1. atomic no-replace old -> hidden work directory: the real
        #    item is never exposed at the final new path while editing
        _rename_dir_noreplace(old_dir, work_dir)
        phase = "work"

        # 2. recheck the mapped subtree immediately after the move to
        #    close the check->move window
        _verify_subtree(
            plan.subtree,
            work_dir,
            lambda p: _map_old_to_work(p, old_dir, work_dir),
        )

        # 2.5 recheck the global markdown set while the item is hidden
        _verify_global_scan(plan, root, old_dir, new_dir, work_dir)

        # 3. check-to-use guard: every existing execution target must
        #    still carry the plan-time state (type/hash/mode); new
        #    targets are verified at their own stage
        for path in sorted(plan.expected, key=str):
            if plan.expected[path][0] == "absent":
                continue
            target = _map_to_work(path, new_dir, work_dir)
            if fsops._file_fingerprint(target) != plan.expected[path]:
                raise ItemConflict(
                    "concurrent change detected: a file changed since the plan; "
                    "nothing was written"
                )

        # 4. managed writes for every edit (main, derived, vault files)
        #    with per-stage expected verification (pre-check -> stage ->
        #    post-check), never a single batch precheck
        for path in sorted(plan.contents, key=str):
            target = _map_to_work(path, new_dir, work_dir)
            _stage_expected(op, target, plan.expected[path])
            fsops.write_target(op, target, plan.contents[path])

        # 5. primary PDF: expected-source-guarded read (verified before
        #    and after the read) into an expected-absent new target
        for src, dst in plan.rename_pairs:
            if dst not in plan.contents:
                wsrc = _map_to_work(src, new_dir, work_dir)
                wdst = _map_to_work(dst, new_dir, work_dir)
                _stage_expected(op, wdst, plan.expected[dst])
                data = _read_key_source(wsrc, plan.expected[src])
                fsops.write_target(op, wdst, data)

        # 6. restore source permission bits on renamed files and re-sync
        #    the staged fingerprint (the create+delete rename defaults to
        #    0644; commit verification must see the real state)
        for src, dst in plan.rename_pairs:
            wsrc = _map_to_work(src, new_dir, work_dir)
            wdst = _map_to_work(dst, new_dir, work_dir)
            if src not in plan.contents:
                # unmanaged key source (the primary PDF): still
                # expected-guarded before reading its mode
                if fsops._file_fingerprint(wsrc) != plan.expected[src]:
                    raise fsops.OperationConflict(
                        f"key source {wsrc} changed before mode restore"
                    )
            mode = stat.S_IMODE(wsrc.stat().st_mode)
            os.chmod(wdst, mode)
            op.targets[wdst].written_fingerprint = fsops._file_fingerprint(wdst)

        # 7. delete the old key-file names (managed, inside the
        #    operation). Sources not already part of the managed
        #    transaction (the primary PDF) get the plan expected-state
        #    guard through precheck -> stage -> postcheck before the
        #    delete; main/derived sources already staged/written keep
        #    the fsops managed-state verification (never compared to the
        #    original plan bytes)
        for path in sorted(plan.deletes, key=str):
            target = _map_to_work(path, new_dir, work_dir)
            if target not in op.targets:
                _stage_expected(op, target, plan.expected[path])
            else:
                fsops.stage_target(op, target)
            fsops.delete_target(op, target)

        # 8. recheck the untouched subtree before finalization
        _verify_untouched_subtree(plan, work_dir, old_dir, new_dir)

        # 8.5 recheck the global markdown set immediately before
        #     finalization (closes the whole editing window)
        _verify_global_scan(plan, root, old_dir, new_dir, work_dir)

        # 9. atomic no-replace work -> final new ONLY at finalization:
        #    a final-target racer (file, empty/non-empty dir, symlink)
        #    blocks the rename and stays exact at its path
        _rename_dir_noreplace(work_dir, new_dir)
        phase = "final"

        # 10. remap staged targets to the final paths before any further
        #     verification (a final-phase conflict must roll back
        #     through the final-path staged targets)
        _remap_op_targets(op, work_dir, new_dir)

        # 10.5 recheck the global markdown set after the final move to
        #      close the finalize window
        _verify_global_scan(plan, root, old_dir, new_dir, new_dir)

        # 11. post-verify BEFORE the hook: a failed verification rolls
        #     back with the hook never fired
        _post_verify(root, plan)

        # 12. rebuild hook exactly once
        hook()

        # 13. commit (last); conflicts have already finished the
        #     operation and surface as ItemConflict, never rolled back
        conflicts = fsops.commit(op)
        committed = True
        if conflicts:
            raise ItemConflict(
                "concurrent change detected while renaming citation key: "
                + ", ".join(str(path) for path in conflicts)
            )
    except fsops.OperationConflict:
        _abort(op, committed, phase, old_dir, new_dir, work_dir)
        raise ItemConflict(
            "concurrent change detected while renaming citation key"
        ) from None
    except ItemError:
        _abort(op, committed, phase, old_dir, new_dir, work_dir)
        raise
    except ItemConflict:
        _abort(op, committed, phase, old_dir, new_dir, work_dir)
        raise
    except Exception:
        _abort(op, committed, phase, old_dir, new_dir, work_dir)
        raise ItemError("citation-key rename failed") from None
    except BaseException:
        _abort(op, committed, phase, old_dir, new_dir, work_dir)
        raise
