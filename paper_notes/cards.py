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

import errno
import hashlib
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .frontmatter import _atomic_write_text
from .fsops import _renameatx_np, _RENAME_EXCL
from .locking import LockConflict, LockError, StaleLockError, acquire_lock, release_lock
from .paths import LITERATURE_ROOT, cards_directory, paper_directory
from .repository import build_index

# Characters unsafe in card filenames (mirrors the literature-card skill's
# slug rules; CJK is preserved).
_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')
_LEADING_TRAILING_DOTS = re.compile(r"^\.+|\.+$")


class CardError(Exception):
    """User/config/validation error for card operations."""
    cleanup_failures: list[Any] = []


class CardConflict(Exception):
    """A card already exists at the target path (zero writes)."""


@dataclass(frozen=True)
class CleanupFailure:
    component: str
    exception: Exception


@dataclass(frozen=True)
class CommitReceipt:
    filename: str
    ident: tuple[int, int]
    content_bytes: bytes

    def __iter__(self):
        return iter(self.ident)

    def __getitem__(self, index: int):
        return self.ident[index]


@dataclass
class StagedReceipt:
    """Identity receipt for a staged (temp) file in the exact-selection state machine.

    Records the parent dirfd capability, the temp name, the ``(dev, ino)`` of
    the staged inode, its regular-file status and its content bytes.
    ``consumed`` marks that the temp has been moved to its final location
    (cleanup can skip it). ``keep_as_residue`` marks that cleanup must NOT
    remove the temp even though it is still identity-owned (partial-state
    diagnostics keep it for manual recovery).
    """

    name: str
    parent_fd: int
    ident: tuple[int, int]
    content_bytes: bytes
    is_regular: bool = True
    consumed: bool = False
    keep_as_residue: bool = False


class CardTransactionState:
    """Exact-selection transaction state machine state (P0-P3).

    Contract note (Owner decision A): managed/cooperative writers share the
    vault lock; non-cooperative writers (e.g. ``MAP_SHARED`` mmap) are outside
    the consistency guarantees. Identity re-checks are best-effort
    precondition observations, never a metadata compare-and-swap.
    """

    def __init__(self, canonical_file: str) -> None:
        self.canonical_file = canonical_file
        # P0 observations (under lock, pinned fd)
        self.initial_source_ident: tuple[int, int] | None = None
        self.initial_source_bytes: bytes | None = None
        self.initial_source_mode: int = 0o644
        self.source_fd: int | None = None
        # P1 staged temps (no visible mutation yet)
        self.card_temp: StagedReceipt | None = None
        self.new_source_temp: StagedReceipt | None = None
        self.rollback_temp: StagedReceipt | None = None
        # Canonical directory binding: the held dirfds' identities for the
        # canonical path root → 05 Literature → <paper> → cards, recorded
        # when the directories were securely opened. Re-verified before P2/P3
        # by nofollow-statting each canonical directory entry through its
        # parent dirfd (not only ancestry — an in-vault relocation keeps
        # ancestry valid while swapping the canonical entries).
        self.root_anchor: tuple[int, int] | None = None
        self.lit_ident: tuple[int, int] | None = None
        self.paper_ident: tuple[int, int] | None = None
        self.cards_ident: tuple[int, int] | None = None
        # commit tracking
        self.source_committed: bool = False
        self.card_committed: bool = False
        # P3 ambiguity: the publish outcome could not be reliably determined
        # (receipt stat failed). No compensation and no possibly-destructive
        # cleanup may run; the state must be resolved by a human/operator.
        self.p3_ambiguous: bool = False
        # Partial business state: the card is likely/known committed but the
        # caller sees an error. Rollback temp and card temp are preserved as
        # recovery evidence.
        self.partial: bool = False


def _verify_root_anchor(
    vault_root: Path, canonical_root_path: Path, root_anchor: tuple[int, int]
) -> None:
    """Ensure canonical_root_path still resolves to the initial root anchor."""
    try:
        curr_st = canonical_root_path.stat()
        if (curr_st.st_dev, curr_st.st_ino) != root_anchor:
            raise CardError(
                f"vault root anchor changed: {canonical_root_path} no longer matches initial root inode"
            )
        resolved_st = vault_root.resolve().stat()
        if (resolved_st.st_dev, resolved_st.st_ino) != root_anchor:
            raise CardError(
                f"vault root resolution changed: {vault_root} no longer resolves to initial root inode"
            )
    except OSError as exc:
        if isinstance(exc, CardError):
            raise
        raise CardError(f"cannot verify vault root anchor: {exc}") from exc


def _verify_ancestry(
    start_fd: int, root_anchor: tuple[int, int], max_depth: int = 64
) -> None:
    """Verify that start_fd is a descendant of root_anchor by walking '..' up.

    Fails closed if filesystem root is reached without matching root_anchor,
    if a loop is detected, if device differs, or if any error occurs.
    """
    curr_fd = None
    try:
        curr_fd = os.dup(start_fd)
    except OSError as exc:
        raise CardError(f"cannot duplicate directory descriptor for ancestry check: {exc}") from exc

    visited = set()
    try:
        for _ in range(max_depth):
            try:
                st = os.fstat(curr_fd)
            except OSError as exc:
                raise CardError(f"cannot stat directory in ancestry verification: {exc}") from exc

            ident = (st.st_dev, st.st_ino)
            if ident == root_anchor:
                return  # Verified! Reached the canonical root anchor!

            if ident[0] != root_anchor[0]:
                raise CardError("directory hierarchy crossed device boundary away from root anchor")

            if ident in visited:
                raise CardError("loop detected in directory ancestry")
            visited.add(ident)

            try:
                parent_fd = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=curr_fd)
            except OSError as exc:
                raise CardError(f"cannot ascend directory tree during ancestry verification: {exc}") from exc

            try:
                parent_st = os.fstat(parent_fd)
            except OSError as exc:
                os.close(parent_fd)
                raise CardError(f"cannot stat parent directory during ancestry verification: {exc}") from exc

            parent_ident = (parent_st.st_dev, parent_st.st_ino)
            if parent_ident == ident:
                # Reached filesystem root without hitting root_anchor
                os.close(parent_fd)
                raise CardError("directory reached filesystem root without matching vault root anchor")

            os.close(curr_fd)
            curr_fd = parent_fd

        raise CardError(f"ancestry verification exceeded max depth of {max_depth}")
    finally:
        if curr_fd is not None:
            try:
                os.close(curr_fd)
            except OSError:
                pass


def _verify_canonical_binding(
    root_anchor: tuple[int, int],
    lit_ident: tuple[int, int],
    paper_ident: tuple[int, int],
    cards_ident: tuple[int, int] | None,
    root_fd: int,
    lit_name: str,
    paper_name: str,
    cards_name: str | None,
) -> None:
    """Re-verify the canonical directory chain still resolves to held inodes.

    Through the held root dirfd, nofollow-stat each canonical directory entry
    (``05 Literature`` → ``<paper>`` → ``cards``) and require it to still point
    at the recorded held inode. This catches in-vault relocations/swaps that
    leave ancestry valid (e.g. moving ``cards`` elsewhere inside the vault and
    recreating an empty canonical ``cards`` directory). ``cards_ident`` may be
    None for checks that only cover the source path.
    """
    try:
        lit_st = os.lstat(lit_name, dir_fd=root_fd)
        if (lit_st.st_dev, lit_st.st_ino) != lit_ident:
            raise CardError(
                f"canonical directory binding changed: {lit_name!r} no longer holds "
                "the opened directory inode"
            )
        # paper must be checked through the lit dirfd; open it nofollow first.
        lit_fd = os.open(lit_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except CardError:
        raise
    except OSError as exc:
        raise CardError(f"cannot verify canonical directory binding: {exc}") from exc
    try:
        paper_fd = os.open(paper_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=lit_fd)
    except OSError as exc:
        os.close(lit_fd)
        raise CardError(f"cannot verify canonical directory binding (paper): {exc}") from exc
    try:
        try:
            paper_st = os.fstat(paper_fd)
            if (paper_st.st_dev, paper_st.st_ino) != paper_ident:
                raise CardError(
                    f"canonical directory binding changed: {paper_name!r} no longer holds "
                    "the opened paper directory inode"
                )
            if cards_ident is None or cards_name is None:
                return
            cards_st = os.lstat(cards_name, dir_fd=paper_fd)
            if (cards_st.st_dev, cards_st.st_ino) != cards_ident:
                raise CardError(
                    f"canonical directory binding changed: {cards_name!r} no longer holds "
                    "the opened cards directory inode"
                )
        finally:
            os.close(paper_fd)
    finally:
        os.close(lit_fd)


def _open_dir_nofollow(
    parent_fd: int, name: str, root_anchor: tuple[int, int] | None = None
) -> int:
    """Open a child directory under parent_fd strictly without following symlinks."""
    if "/" in name or "\\" in name or ".." in name or name in (".", ".."):
        raise CardError(f"invalid directory component: {name!r}")
    if root_anchor is not None:
        _verify_ancestry(parent_fd, root_anchor)
    try:
        st = os.lstat(name, dir_fd=parent_fd)
        if stat.S_ISLNK(st.st_mode):
            raise CardError(f"directory component is a symlink: {name!r}")
        if not stat.S_ISDIR(st.st_mode):
            raise CardError(f"directory component is not a directory: {name!r}")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CardError(f"cannot stat directory component {name!r}: {exc}") from exc

    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise CardError(f"cannot securely open directory {name!r}: {exc}") from exc

    if root_anchor is not None:
        try:
            _verify_ancestry(child_fd, root_anchor)
        except BaseException:
            os.close(child_fd)
            raise

    return child_fd


def _ensure_dir_nofollow(
    parent_fd: int, name: str, mode: int = 0o755, root_anchor: tuple[int, int] | None = None
) -> int:
    """Ensure directory exists under parent_fd and open it with O_DIRECTORY | O_NOFOLLOW."""
    if "/" in name or "\\" in name or ".." in name or name in (".", ".."):
        raise CardError(f"invalid directory component: {name!r}")
    if root_anchor is not None:
        _verify_ancestry(parent_fd, root_anchor)
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CardError(f"cannot create directory {name!r}: {exc}") from exc

    return _open_dir_nofollow(parent_fd, name, root_anchor=root_anchor)


def _read_bytes_dirfd(dir_fd: int, filename: str) -> bytes:
    """Read file content under dir_fd strictly without following symlinks."""
    try:
        st = os.lstat(filename, dir_fd=dir_fd)
        if stat.S_ISLNK(st.st_mode):
            raise CardError(f"file is a symlink: {filename!r}")
        if not stat.S_ISREG(st.st_mode):
            raise CardError(f"file is not a regular file: {filename!r}")
    except OSError as exc:
        if isinstance(exc, CardError):
            raise
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(f"file not found: {filename}") from exc
        raise CardError(f"cannot stat file {filename!r}: {exc}") from exc

    fd = None
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read()
        fd = None
        return data
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(f"file not found: {filename}") from exc
        raise CardError(f"cannot read file {filename!r}: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_all_from_pinned_fd(pinned_fd: int) -> bytes:
    """Read the whole content of a pinned fd from the beginning.

    Best-effort precondition observation only: this is NOT a metadata
    compare-and-swap. A non-cooperative same-inode writer (e.g. ``MAP_SHARED``
    mmap) can change content after this read without any stat field changing
    (documented APFS behavior); such writers are outside the managed-writer
    contract.
    """
    os.lseek(pinned_fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(pinned_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _stat_matches_staged(st: os.stat_result, receipt: StagedReceipt) -> bool:
    return stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == receipt.ident


def _note_unknown_ident_residue(primary_exc: BaseException | None, tmp_name: str) -> None:
    """Record that a temp with unknown identity is kept as residue.

    A temp whose ``(dev, ino)`` was never captured must NEVER be unlinked by
    name: a non-cooperative writer may have swapped a foreign entry into the
    name. The temp (whatever it now holds) is preserved and the residue is
    reported on the propagating exception.
    """
    note = f"temp_residue (identity unknown, preserved): {tmp_name}"
    if primary_exc is not None and hasattr(primary_exc, "add_note"):
        try:
            primary_exc.add_note(note)
        except Exception:
            pass


def _stage_temp_bytes(
    parent_fd: int,
    final_name: str,
    data: bytes,
    mode: int = 0o644,
    root_anchor: tuple[int, int] | None = None,
    register: Callable[["StagedReceipt"], None] | None = None,
) -> StagedReceipt:
    """Create, fill and fsync a unique same-directory temp file, then close the fd.

    The temp name derives from the final name with a random UUID suffix and is
    created with ``O_CREAT | O_EXCL | O_NOFOLLOW``. On any failure the temp is
    removed identity-safely (never a foreign entry). Staging performs no
    visible mutation to canonical files.
    """
    if root_anchor is not None:
        _verify_ancestry(parent_fd, root_anchor)
    tmp_name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    staged_ident: tuple[int, int] | None = None
    try:
        fd = os.open(
            tmp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        st_pre = os.fstat(fd)
        staged_ident = (st_pre.st_dev, st_pre.st_ino)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            st_post = os.fstat(fh.fileno())
        if (st_post.st_dev, st_post.st_ino) != staged_ident or not stat.S_ISREG(st_post.st_mode):
            raise CardError(f"staged temp identity changed during write: {tmp_name!r}")
    except BaseException as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if staged_ident is not None:
            partial = StagedReceipt(
                name=tmp_name,
                parent_fd=parent_fd,
                ident=staged_ident,
                content_bytes=b"",
                is_regular=True,
            )
            try:
                _cleanup_staged_temp(partial, primary_exc=exc)
            except Exception as cleanup_exc:
                # Cleanup failure (identity mismatch → foreign preserved, or
                # transient unlink failure) must NOT be swallowed: attach the
                # residue diagnostic to the propagating exception while the
                # original error remains the primary cause.
                residue_note = (
                    f"temp_residue (cleanup failed, preserved): {tmp_name}: {cleanup_exc}"
                )
                if hasattr(exc, "add_note"):
                    exc.add_note(residue_note)
        else:
            # Identity never captured (e.g. fstat failed right after open):
            # NEVER unlink by name — a non-cooperative writer may have swapped
            # a foreign entry into this name. Keep the temp as residue and
            # report it on the propagating exception.
            _note_unknown_ident_residue(exc, tmp_name)
        raise
    receipt = StagedReceipt(
        name=tmp_name,
        parent_fd=parent_fd,
        ident=staged_ident,
        content_bytes=bytes(data),
        is_regular=True,
    )
    # Register the receipt with the caller before returning so that any
    # failure injected between staging and the caller's own bookkeeping
    # still sees the identity-owned temp registered for cleanup.
    if register is not None:
        register(receipt)
    return receipt


def _verify_staged_identity(receipt: StagedReceipt) -> os.stat_result:
    """Nofollow-stat the staged temp name; it must still match the receipt.

    Fails closed when the name vanished or was swapped for a foreign entry;
    the foreign entry is never touched.
    """
    try:
        st = os.stat(receipt.name, dir_fd=receipt.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise CardError(f"staged temp vanished before commit: {receipt.name!r}")
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise CardError(f"staged temp vanished before commit: {receipt.name!r}")
        raise CardError(f"cannot stat staged temp {receipt.name!r}: {exc}")
    if not _stat_matches_staged(st, receipt):
        raise CardError(
            f"staged temp identity mismatch (foreign entry preserved, residue reported): {receipt.name!r}"
        )
    return st


def _compensate_committed_source(
    paper_fd: int,
    canonical_file: str,
    committed_ident: tuple[int, int],
    rollback: StagedReceipt,
    max_retries: int = 5,
) -> None:
    """Restore the initial source bytes after a committed source replace.

    Strictly identity-bound (shared lock assumed held):

    - the rollback temp must still match its own receipt identity;
    - the canonical source path must still hold the committed (new-source)
      receipt inode.

    Only then is the rollback temp replace()d onto the canonical source.
    Any identity mismatch preserves whatever foreign entry is present, keeps
    the rollback temp as diagnosable residue and raises a partial-state error.
    This is NOT a compare-and-swap: a non-cooperative same-inode writer is
    outside the managed-writer contract.
    """
    # 1. Rollback temp must still be ours; otherwise restoring would blindly
    #    overwrite the canonical path with unknown content.
    try:
        rb_st = os.stat(rollback.name, dir_fd=rollback.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        rollback.keep_as_residue = True
        raise CardError(
            "source compensation aborted: rollback temp vanished; "
            "canonical source left committed (partial state)"
        )
    except OSError as exc:
        rollback.keep_as_residue = True
        raise CardError(f"source compensation aborted: cannot stat rollback temp: {exc}")
    if not _stat_matches_staged(rb_st, rollback):
        rollback.keep_as_residue = True
        raise CardError(
            "source compensation aborted: rollback temp identity mismatch "
            "(foreign entry preserved); canonical source left committed (partial state)"
        )

    # 2. Canonical source must still hold the inode this transaction committed.
    try:
        st = os.lstat(canonical_file, dir_fd=paper_fd)
    except FileNotFoundError:
        rollback.keep_as_residue = True
        raise CardError(
            "source compensation aborted: canonical source vanished; "
            "rollback temp preserved for manual recovery (partial state)"
        )
    except OSError as exc:
        rollback.keep_as_residue = True
        raise CardError(f"source compensation aborted: cannot stat canonical source: {exc}")
    if (st.st_dev, st.st_ino) != committed_ident:
        rollback.keep_as_residue = True
        raise CardError(
            "source compensation aborted: canonical source no longer holds the "
            "committed inode (foreign file preserved); rollback temp preserved "
            "for manual recovery (partial state)"
        )

    last_error: OSError | None = None
    for attempt in range(max_retries):
        # Re-validate BOTH bindings before every replace attempt (not just
        # the first): the rollback temp name must still hold the receipt
        # regular inode and the canonical source must still hold the
        # committed inode. Any mismatch preserves the foreign entry and the
        # rollback temp as residue — a foreign temp is NEVER replaced onto
        # the canonical source. The final re-validation → replace window is
        # the accepted contract window (Owner decision A).
        try:
            rb_st = os.stat(rollback.name, dir_fd=rollback.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            rollback.keep_as_residue = True
            raise CardError(
                "source compensation aborted: rollback temp vanished before retry; "
                "canonical source left committed (partial state)"
            )
        except OSError as exc:
            rollback.keep_as_residue = True
            raise CardError(f"source compensation aborted: cannot stat rollback temp before retry: {exc}")
        if not _stat_matches_staged(rb_st, rollback):
            rollback.keep_as_residue = True
            raise CardError(
                "source compensation aborted: rollback temp identity mismatch before retry "
                "(foreign entry preserved); canonical source left committed (partial state)"
            )
        try:
            st = os.lstat(canonical_file, dir_fd=paper_fd)
        except FileNotFoundError:
            rollback.keep_as_residue = True
            raise CardError(
                "source compensation aborted: canonical source vanished before retry; "
                "rollback temp preserved for manual recovery (partial state)"
            )
        except OSError as exc:
            rollback.keep_as_residue = True
            raise CardError(f"source compensation aborted: cannot stat canonical source before retry: {exc}")
        if (st.st_dev, st.st_ino) != committed_ident:
            rollback.keep_as_residue = True
            raise CardError(
                "source compensation aborted: canonical source no longer holds the committed inode "
                "before retry (foreign file preserved); rollback temp preserved for manual recovery"
            )

        try:
            os.replace(rollback.name, canonical_file, src_dir_fd=paper_fd, dst_dir_fd=paper_fd)
            rollback.consumed = True
            return
        except FileNotFoundError as exc:
            rollback.keep_as_residue = True
            raise CardError(f"source compensation aborted: rollback temp vanished: {exc}")
        except OSError as exc:
            last_error = exc
            # Ambiguity resolution by identity only.
            try:
                st2 = os.lstat(canonical_file, dir_fd=paper_fd)
            except OSError:
                st2 = None
            if st2 is not None and (st2.st_dev, st2.st_ino) == rollback.ident:
                # Restore landed despite the reported error.
                rollback.consumed = True
                return
            if (
                attempt < max_retries - 1
                and exc.errno in (errno.EINTR, errno.EBUSY, errno.EAGAIN)
                and st2 is not None
                and (st2.st_dev, st2.st_ino) == committed_ident
            ):
                continue
            break
    rollback.keep_as_residue = True
    raise CardError(
        f"source compensation replace failed: {last_error}; canonical source left "
        "committed (partial state); rollback temp preserved for manual recovery"
    )


def _cleanup_temp_file(
    parent_fd: int | None,
    tmp_name: str,
    expected_ident: tuple[int, int] | None = None,
    primary_exc: BaseException | None = None,
    max_retries: int = 5,
) -> None:
    """Identity-safe temporary residue cleanup with transient-error retries.

    When ``expected_ident`` is provided, the temp name is nofollow-statted
    first and unlinked only while it still points at the staged ``(dev, ino)``
    regular-file identity. On identity mismatch the foreign entry is NOT
    deleted and a residue error is raised. The final identity stat and the
    unlink are adjacent but not atomic; that window is accepted by contract.
    """
    if parent_fd is None:
        return

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            if expected_ident is not None:
                try:
                    st = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        return
                    last_err = exc
                    if attempt < max_retries - 1 and exc.errno in (errno.EINTR, errno.EBUSY, errno.EAGAIN):
                        continue
                    break
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != expected_ident:
                    msg = (
                        f"temp {tmp_name!r} no longer matches staged identity; "
                        "foreign entry preserved (residue reported)"
                    )
                    residue_err = CardError(msg)
                    if hasattr(residue_err, "add_note"):
                        residue_err.add_note(f"temp_residue: {tmp_name}")
                        if primary_exc is not None:
                            residue_err.add_note(f"Primary error: {primary_exc}")
                    raise residue_err
            os.unlink(tmp_name, dir_fd=parent_fd)
            return
        except CardError:
            raise
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            last_err = exc
            if attempt < max_retries - 1 and (
                exc.errno in (errno.EINTR, errno.EBUSY, errno.EAGAIN) or exc.errno is None
            ):
                continue
            break

    if last_err is not None:
        msg = f"failed to clean up temporary residue {tmp_name!r}: {last_err}"
        if primary_exc is not None:
            msg += f"; primary error: {primary_exc}"
        residue_err = CardError(msg)
        if hasattr(residue_err, "add_note"):
            residue_err.add_note(f"temp_residue: {tmp_name}")
            if primary_exc is not None:
                residue_err.add_note(f"Primary error: {primary_exc}")
        if primary_exc is not None:
            residue_err.__cause__ = primary_exc
        raise residue_err


def _cleanup_staged_temp(
    receipt: StagedReceipt,
    primary_exc: BaseException | None = None,
    max_retries: int = 5,
) -> None:
    """Identity-safe cleanup of a staged temp receipt (skips consumed/residue)."""
    if receipt.consumed or receipt.keep_as_residue:
        return
    _cleanup_temp_file(
        receipt.parent_fd,
        receipt.name,
        expected_ident=receipt.ident,
        primary_exc=primary_exc,
        max_retries=max_retries,
    )


def _atomic_write_new(
    path: Path,
    content: str,
    mode: int = 0o644,
    dir_fd: int | None = None,
    root_anchor: tuple[int, int] | None = None,
    receipt_sink: Any = None,
) -> CommitReceipt:
    """Atomically create a new file (never overwrites an existing one).

    Uses macOS renameatx_np(RENAME_EXCL) within the safe directory descriptor
    to commit the temporary file directly to the final filename without
    leaving behind any secondary directory entries or requiring post-success unlink.
    Captures file identity and content bytes in CommitReceipt before commit.
    After commit, returns the receipt with zero failable bookkeeping syscalls.
    """
    if sys.platform != "darwin":
        raise CardError("atomic no-replace creation is not supported on this platform")

    close_parent = False
    parent_fd = dir_fd
    if parent_fd is None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise CardError("O_NOFOLLOW is not supported on this platform")
        try:
            parent_fd = os.open(
                str(path.parent),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            close_parent = True
        except OSError as exc:
            raise CardError(f"cannot open parent directory of {path}: {exc}") from exc

    filename = path.name
    tmp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    tmp_fd = None
    tmp_created = False
    committed = False
    staged_ident: tuple[int, int] | None = None
    primary_exc: BaseException | None = None
    content_bytes = content.encode("utf-8")
    try:
        if root_anchor is not None:
            _verify_ancestry(parent_fd, root_anchor)

        try:
            st = os.lstat(filename, dir_fd=parent_fd)
            raise CardConflict(f"card already exists: {path}")
        except FileNotFoundError:
            pass

        tmp_fd = os.open(
            tmp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        tmp_created = True
        # Capture identity immediately after open and verify regular so any
        # later failure can be resolved identity-safely (never by-name).
        try:
            st_pre = os.fstat(tmp_fd)
        except OSError as exc:
            raise CardError(f"cannot stat freshly staged temp {tmp_name!r}: {exc}") from exc
        if not stat.S_ISREG(st_pre.st_mode):
            raise CardError(f"staged temp is not a regular file: {tmp_name!r}")
        staged_ident = (st_pre.st_dev, st_pre.st_ino)
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as fh:
            tmp_fd = None
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
            st = os.fstat(fh.fileno())
            ident = (st.st_dev, st.st_ino)
            if ident != staged_ident:
                raise CardError(f"staged temp identity changed during write: {tmp_name!r}")
            receipt = CommitReceipt(filename=filename, ident=ident, content_bytes=content_bytes)

        if receipt_sink is not None:
            receipt_sink(receipt)

        if root_anchor is not None:
            _verify_ancestry(parent_fd, root_anchor)

        # Temp-bound commit: nofollow-stat the temp name immediately before
        # the rename — it must still hold the receipt inode. The window
        # between this check and RENAME_EXCL is the accepted non-cooperative
        # preemption window (Owner decision A).
        try:
            temp_st = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise CardError(f"cannot verify staged temp before commit: {tmp_name!r}: {exc}") from exc
        if not stat.S_ISREG(temp_st.st_mode) or (temp_st.st_dev, temp_st.st_ino) != staged_ident:
            raise CardError(
                f"staged temp no longer matches receipt before commit "
                f"(foreign entry preserved, residue reported): {tmp_name!r}"
            )

        try:
            _renameatx_np(parent_fd, tmp_name, parent_fd, filename, _RENAME_EXCL)
            committed = True
        except FileExistsError:
            raise CardConflict(f"card already exists: {path}")
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise CardConflict(f"card already exists: {path}")
            raise CardError(f"cannot commit card atomically: {exc}") from exc

        return receipt
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        try:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_created and not committed:
                if staged_ident is not None:
                    _cleanup_temp_file(
                        parent_fd,
                        tmp_name,
                        expected_ident=staged_ident,
                        primary_exc=primary_exc,
                    )
                else:
                    # Identity unknown: never unlink by name; keep + report.
                    _note_unknown_ident_residue(primary_exc, tmp_name)
        finally:
            if close_parent and parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass


def _atomic_write_bytes(
    path: Path,
    data: bytes | bytearray,
    dir_fd: int | None = None,
    root_anchor: tuple[int, int] | None = None,
    receipt_sink: Any = None,
    expected_current: CommitReceipt | None = None,
) -> CommitReceipt | None:
    """Atomically overwrite a file with raw bytes preserving its mode.

    If dir_fd is provided, writes within that safe directory descriptor using
    O_NOFOLLOW and renameat. Refuses to overwrite symlinks.
    Captures file identity and content bytes in CommitReceipt before commit.
    After commit, returns the receipt with zero failable bookkeeping syscalls.

    When ``expected_current`` is provided, the pathname is opened with
    ``O_RDONLY | O_NOFOLLOW`` and its inode identity plus a best-effort bytes
    observation are checked against the receipt before, and again immediately
    before, the replace. These are best-effort precondition observations for
    depth-of-defense against ordinary writers sharing the vault lock — NOT a
    compare-and-swap. A non-cooperative same-inode writer (e.g. ``MAP_SHARED``
    mmap) can change content between any observation and the replace without
    any metadata change (documented APFS behavior) and is outside the
    managed-writer contract. On mismatch the helper is a no-op (returns None).
    Temp cleanup is identity-safe: a swapped temp name is never deleted and is
    reported as residue.
    """
    close_parent = False
    parent_fd = dir_fd
    if parent_fd is None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise CardError("O_NOFOLLOW is not supported on this platform")
        try:
            parent_fd = os.open(
                str(path.parent),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            close_parent = True
        except OSError as exc:
            raise CardError(f"cannot open parent directory of {path}: {exc}") from exc

    filename = path.name
    pinned_fd: int | None = None
    tmp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    tmp_fd = None
    tmp_created = False
    staged_ident: tuple[int, int] | None = None
    committed = False
    primary_exc: BaseException | None = None
    content_snapshot = bytes(data)
    try:
        if expected_current is not None:
            try:
                pinned_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ENOENT or (hasattr(errno, "ELOOP") and exc.errno == errno.ELOOP):
                    return None
                raise CardError(f"cannot open expected file {filename!r}: {exc}") from exc
            st = os.fstat(pinned_fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            if (st.st_dev, st.st_ino) != expected_current.ident:
                return None
            if _read_all_from_pinned_fd(pinned_fd) != expected_current.content_bytes:
                # Ordinary concurrent write detected (defense in depth; not CAS)
                return None
            mode = stat.S_IMODE(st.st_mode)
        else:
            try:
                st = os.lstat(filename, dir_fd=parent_fd)
                if stat.S_ISLNK(st.st_mode):
                    raise CardError(f"refusing to write to symlink: {filename}")
                mode = stat.S_IMODE(st.st_mode)
            except FileNotFoundError:
                mode = 0o644
            except OSError as exc:
                raise CardError(f"cannot stat file {filename!r}: {exc}") from exc

        if root_anchor is not None:
            _verify_ancestry(parent_fd, root_anchor)

        tmp_fd = os.open(
            tmp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        tmp_created = True
        # Capture identity immediately after open and verify regular so any
        # later failure can be resolved identity-safely (never by-name).
        try:
            st_pre = os.fstat(tmp_fd)
        except OSError as exc:
            raise CardError(f"cannot stat freshly staged temp {tmp_name!r}: {exc}") from exc
        if not stat.S_ISREG(st_pre.st_mode):
            raise CardError(f"staged temp is not a regular file: {tmp_name!r}")
        staged_ident = (st_pre.st_dev, st_pre.st_ino)
        with os.fdopen(tmp_fd, "wb") as fh:
            tmp_fd = None
            fh.write(content_snapshot)
            fh.flush()
            os.fsync(fh.fileno())
            st = os.fstat(fh.fileno())
            write_ident = (st.st_dev, st.st_ino)
            if write_ident != staged_ident:
                raise CardError(f"staged temp identity changed during write: {tmp_name!r}")
            receipt = CommitReceipt(filename=filename, ident=write_ident, content_bytes=content_snapshot)

        if receipt_sink is not None:
            receipt_sink(receipt)

        if root_anchor is not None:
            _verify_ancestry(parent_fd, root_anchor)

        if expected_current is not None:
            # Best-effort precondition re-observation immediately before the
            # replace: the pathname must still hold the expected inode and the
            # pinned reread must still equal the expected bytes. Not a CAS.
            re_read = _read_all_from_pinned_fd(pinned_fd)
            if re_read != expected_current.content_bytes:
                return None
            try:
                dir_st = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                raise CardError(f"cannot re-stat expected file {filename!r}: {exc}") from exc
            if (dir_st.st_dev, dir_st.st_ino) != expected_current.ident:
                # Path swapped to a foreign entry: do not touch it.
                return None

        # Temp-bound commit (both normal and expected_current modes):
        # nofollow-stat the temp name immediately before the replace — it
        # must still hold the receipt inode. The window between this check
        # and the replace is the accepted non-cooperative preemption window
        # (Owner decision A).
        try:
            temp_st = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise CardError(f"cannot verify staged temp before commit: {tmp_name!r}: {exc}") from exc
        if not stat.S_ISREG(temp_st.st_mode) or (temp_st.st_dev, temp_st.st_ino) != staged_ident:
            raise CardError(
                f"staged temp no longer matches receipt before commit "
                f"(foreign entry preserved, residue reported): {tmp_name!r}"
            )

        os.replace(tmp_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        committed = True
        return receipt
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        try:
            if pinned_fd is not None:
                try:
                    os.close(pinned_fd)
                except OSError:
                    pass
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_created and not committed:
                if staged_ident is not None:
                    _cleanup_temp_file(
                        parent_fd,
                        tmp_name,
                        expected_ident=staged_ident,
                        primary_exc=primary_exc,
                    )
                else:
                    # Identity unknown: never unlink by name; keep + report.
                    _note_unknown_ident_residue(primary_exc, tmp_name)
        finally:
            if close_parent and parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass


def _detect_newline(source_bytes: bytes, pos: int) -> bytes:
    """Detect newline style (CRLF or LF) around ``pos`` in ``source_bytes``."""
    if pos >= 2 and source_bytes[pos - 2 : pos] == b"\r\n":
        return b"\r\n"
    if pos >= 1 and source_bytes[pos - 1 : pos] == b"\n":
        return b"\n"
    return b"\r\n" if b"\r\n" in source_bytes else b"\n"


@dataclass(frozen=True)
class SourceLine:
    index: int
    start_byte: int
    end_byte: int        # excluding newline
    full_end_byte: int   # including newline
    newline: bytes
    raw: bytes           # excluding newline
    text: str            # decoded excluding newline


@dataclass
class MarkdownBlock:
    block_type: str  # "paragraph", "list", "blockquote", "table", "fenced", "unclosed_fenced", "heading", "setext_heading"
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    full_end_byte: int
    existing_anchor: str | None = None
    # Conservative container Setext hazard (Phase4b).
    #
    # A blockquote/callout/list groups many physical lines into ONE block, so
    # a Setext heading nested inside it never reaches the top-level
    # ``setext_heading`` classification. When the de-containerized logical
    # content of such a container contains a nonblank logical line
    # immediately followed by a ``=+``/``-+`` underline line (skipping
    # fenced code inside the container), this flag is set.
    #
    # Fail-closed granularity is WHOLE-BLOCK BY DESIGN: when the flag is set,
    # EVERY exact selection inside this container gets
    # ``anchor_status == "failed"`` with the source byte-for-byte untouched
    # (card is still created, without a 参见 link, and a stable warning
    # mentioning "Setext heading"). We deliberately do NOT attempt to
    # compute per-line ranges and do NOT insert inline/standalone anchors
    # for container Setext headings; the detection may over-approximate
    # (e.g. a list item whose content is all dashes) — over-approximation
    # only turns a risky insert into a conservative failure, which is the
    # accepted Phase4b trade-off. Containers without the hazard keep the
    # existing blockquote/callout/list anchor behavior unchanged.
    container_setext_hazard: bool = False


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
    card_stem: str = ""
    anchor_status: str | None = None


def _format_card_content(
    paper_id: str,
    citation_key: str,
    selection: str,
    anchor_link: str | None,
) -> str:
    """Format full card note content preserving selection verbatim."""
    frontmatter = (
        "---\n"
        f"paper_id: {paper_id}\n"
        f"citation_key: {citation_key}\n"
        f'paper: "[[{citation_key}]]"\n'
        "---\n\n"
    )
    nl = "\r\n" if "\r\n" in selection else "\n"
    if anchor_link:
        if selection.endswith(nl + nl):
            suffix = f"> 参见 {anchor_link}{nl}{nl}## 扩展{nl}"
        elif selection.endswith(nl):
            suffix = f"{nl}> 参见 {anchor_link}{nl}{nl}## 扩展{nl}"
        else:
            suffix = f"{nl}{nl}> 参见 {anchor_link}{nl}{nl}## 扩展{nl}"
    else:
        if selection.endswith(nl + nl):
            suffix = f"## 扩展{nl}"
        elif selection.endswith(nl):
            suffix = f"{nl}## 扩展{nl}"
        else:
            suffix = f"{nl}{nl}## 扩展{nl}"
    return frontmatter + selection + suffix


def _parse_lines(source_bytes: bytes) -> list[SourceLine]:
    """Parse raw bytes into physical line records."""
    lines: list[SourceLine] = []
    pos = 0
    idx = 0
    n = len(source_bytes)
    while pos < n:
        start = pos
        nl_pos = source_bytes.find(b"\n", pos)
        if nl_pos == -1:
            end = n
            full_end = n
            newline = b""
            pos = n
        else:
            full_end = nl_pos + 1
            if nl_pos > start and source_bytes[nl_pos - 1] == ord(b"\r"):
                end = nl_pos - 1
                newline = b"\r\n"
            else:
                end = nl_pos
                newline = b"\n"
            pos = full_end
        raw = source_bytes[start:end]
        text = raw.decode("utf-8", errors="replace")
        lines.append(
            SourceLine(
                index=idx,
                start_byte=start,
                end_byte=end,
                full_end_byte=full_end,
                newline=newline,
                raw=raw,
                text=text,
            )
        )
        idx += 1
    return lines


_SETEXT_UNDERLINE_RE = re.compile(r"^[ ]{0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})[ \t]*$")
_ATX_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}([ \t]+|$)")
_LIST_MARKER_RE = re.compile(r"^[ ]{0,3}([-*+]|\d{1,9}[\.\)])([ \t]+|$)")

# Container marker regexes for alternating stripping / helpers
_CONTAINER_QUOTE_PREFIX_RE = re.compile(r"^[ ]{0,3}>[ ]?")
_CONTAINER_LIST_PREFIX_RE = re.compile(r"^[ ]{0,3}([-*+]|\d{1,9}[\.\)])[ \t]+")


def _is_setext_underline(line_text: str) -> bool:
    return bool(_SETEXT_UNDERLINE_RE.fullmatch(line_text))


def _parse_fence_open(text: str) -> tuple[str, int] | None:
    """Parse opening code fence per CommonMark (0-3 spaces, ```/~~~).

    If backtick fence, the info string must not contain backticks.
    Returns (fence_char, fence_len) if valid opener, else None.
    """
    m = _FENCE_OPEN_RE.match(text)
    if not m:
        return None
    fence_str = m.group(1)
    info = m.group(2)
    fence_char = fence_str[0]
    if fence_char == "`" and "`" in info:
        return None
    return fence_char, len(fence_str)


def _is_fence_close(text: str, fence_char: str, fence_len: int) -> bool:
    """Check if line is a valid closing fence matching the open fence (0-3 spaces,
    same character, length >= open length, no info string allowed)."""
    m = _FENCE_CLOSE_RE.match(text)
    if not m:
        return False
    close_str = m.group(1)
    return close_str[0] == fence_char and len(close_str) >= fence_len


def _decontainer_line(text: str, kind: str = "") -> str:
    """Strip container markers from one physical line (approximate).

    Supports alternating blockquote and list markers repeatedly.
    """
    t = text.expandtabs(4)
    changed = True
    while changed:
        changed = False
        m_q = _CONTAINER_QUOTE_PREFIX_RE.match(t)
        if m_q:
            t = t[m_q.end() :]
            changed = True
            continue
        m_l = _CONTAINER_LIST_PREFIX_RE.match(t)
        if m_l:
            t = t[m_l.end() :]
            changed = True
            continue
    return t


def _line_carries_list_marker(text: str, kind: str = "") -> bool:
    """Whether the physical line starts with a list marker after any
    outer quote markers are stripped."""
    t = text.expandtabs(4)
    while True:
        m_q = _CONTAINER_QUOTE_PREFIX_RE.match(t)
        if m_q:
            t = t[m_q.end() :]
            continue
        break
    return bool(_CONTAINER_LIST_PREFIX_RE.match(t))


@dataclass(frozen=True)
class _ContainerFrame:
    kind: str  # "blockquote" or "list"
    indent: int = 0  # for list: continuation indent width in spaces


def _container_setext_hazard(
    lines: list[SourceLine],
    start_line: int,
    end_line: int,
    kind: str = "",
) -> bool:
    """Conservatively detect a Setext heading nested inside a container block.

    Follows CommonMark container and block parsing rules:
    - Maintains a container stack (blockquotes and list items with continuation widths).
    - Preserves marker width and relative indentation for list continuations;
      un-indented continuation lines do not match list items.
    - Tracks CommonMark fenced code blocks (0-3 spaces before opener/closer,
      closer matching opener char and length >= open length, backtick openers
      disallowing backticks in info strings). Pseudo-underlines inside code fences
      do not trigger hazards.
    - Distinguishes thematic breaks and new list items from Setext underlines:
      a line can only be a Setext underline if it continues inside the EXACT SAME
      container stack as an immediately preceding paragraph text line without opening
      a new container.
    - Alternating container markers (list -> quote, quote -> list, and repeated
      nesting) are fully supported.
    """
    stack: list[_ContainerFrame] = []
    active_fence: tuple[str, int, int] | None = None  # (fence_char, fence_len, stack_depth)
    last_paragraph_stack: tuple[tuple[str, int], ...] | None = None

    for line_idx in range(start_line, end_line + 1):
        raw = lines[line_idx].text
        text = raw.expandtabs(4)

        # Blank line handling: a completely blank line ends any open paragraph.
        # Blockquotes cannot continue across an empty line without '>'.
        # List items can tolerate blank lines between their inner blocks.
        if not text.strip():
            last_paragraph_stack = None
            while stack and stack[-1].kind == "blockquote":
                stack.pop()
            if active_fence and active_fence[2] > len(stack):
                active_fence = None
            continue

        col = 0
        matched_depth = 0
        while matched_depth < len(stack):
            c = stack[matched_depth]
            if c.kind == "blockquote":
                sp = 0
                while col + sp < len(text) and text[col + sp] == " ":
                    sp += 1
                if sp <= 3 and col + sp < len(text) and text[col + sp] == ">":
                    col += sp + 1
                    if col < len(text) and text[col] == " ":
                        col += 1
                    matched_depth += 1
                    continue
                else:
                    break
            elif c.kind == "list":
                sp = 0
                while col + sp < len(text) and text[col + sp] == " ":
                    sp += 1
                if sp >= c.indent:
                    col += c.indent
                    matched_depth += 1
                    continue
                else:
                    break

        if matched_depth < len(stack):
            stack = stack[:matched_depth]
            if active_fence and active_fence[2] > matched_depth:
                active_fence = None
            last_paragraph_stack = None

        if active_fence and active_fence[2] == len(stack):
            inner_text = text[col:]
            if _is_fence_close(inner_text, active_fence[0], active_fence[1]):
                active_fence = None
            last_paragraph_stack = None
            continue

        opened_new_container = False
        while True:
            sp = 0
            while col + sp < len(text) and text[col + sp] == " ":
                sp += 1
            if sp <= 3 and col + sp < len(text) and text[col + sp] == ">":
                col += sp + 1
                if col < len(text) and text[col] == " ":
                    col += 1
                stack.append(_ContainerFrame("blockquote", 0))
                opened_new_container = True
                continue

            m = _LIST_MARKER_RE.match(text[col:])
            if m:
                marker_width = len(m.group(0))
                if not m.group(2):
                    marker_width += 1
                col += len(m.group(0))
                stack.append(_ContainerFrame("list", indent=marker_width))
                opened_new_container = True
                continue
            break

        inner_text = text[col:]
        current_stack_tuple = tuple((c.kind, c.indent) for c in stack)

        if not inner_text.strip():
            last_paragraph_stack = None
            continue

        fence_info = _parse_fence_open(inner_text)
        if fence_info is not None:
            active_fence = (fence_info[0], fence_info[1], len(stack))
            last_paragraph_stack = None
            continue

        if _ATX_HEADING_RE.match(inner_text):
            last_paragraph_stack = None
            continue

        if not opened_new_container and _is_setext_underline(inner_text):
            if last_paragraph_stack == current_stack_tuple:
                return True
            last_paragraph_stack = None
            continue

        last_paragraph_stack = current_stack_tuple

    return False


def _check_setext_heading(lines: list[SourceLine], i: int) -> int | None:
    first_line = lines[i].text
    if not first_line.strip():
        return None
    if first_line.startswith("    ") or first_line.startswith("\t"):
        return None
    if _is_setext_underline(first_line):
        return None

    j = i + 1
    while j < len(lines):
        line_text = lines[j].text
        if _is_setext_underline(line_text):
            return j
        stripped = line_text.strip()
        if not stripped:
            return None
        if line_text.startswith("    ") or line_text.startswith("\t"):
            return None
        if (
            re.match(r"^[ \t]*(`{3,}|~{3,})", line_text)
            or line_text.lstrip().startswith(">")
            or _is_table_start(lines, j)
            or re.match(r"^[ \t]*([-*+]|\d+[\.\)])[ \t]+", line_text)
            or re.match(r"^[ \t]*#{1,6}[ \t]+", line_text)
            or re.fullmatch(r"\^[a-zA-Z0-9-]+", stripped)
        ):
            return None
        j += 1
    return None


def _is_table_delimiter_row(line_text: str) -> bool:
    """Check if line_text is a genuine Markdown table delimiter row."""
    stripped = line_text.strip()
    if not stripped or "|" not in stripped:
        return False
    has_outer_pipe = stripped.startswith("|") or stripped.endswith("|")
    inner = stripped
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = inner.split("|")
    if not cells or (len(cells) == 1 and not has_outer_pipe):
        return False
    for cell in cells:
        c = cell.strip()
        if not c:
            return False
        if not re.fullmatch(r":?-+:?", c):
            return False
    return True


def _has_unescaped_pipe_outside_code(text: str) -> bool:
    """Check if text contains an unescaped pipe character outside code spans."""
    in_code = False
    code_fence_len = 0
    idx = 0
    n = len(text)
    while idx < n:
        ch = text[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == "`":
            start = idx
            while idx < n and text[idx] == "`":
                idx += 1
            cnt = idx - start
            if not in_code:
                in_code = True
                code_fence_len = cnt
            elif cnt == code_fence_len:
                in_code = False
                code_fence_len = 0
            continue
        if ch == "|" and not in_code:
            return True
        idx += 1
    return False


def _is_table_start(lines: list[SourceLine], i: int) -> bool:
    """Check if line index i starts a genuine Markdown table."""
    if i + 1 >= len(lines):
        return False
    if not _has_unescaped_pipe_outside_code(lines[i].text):
        return False
    return _is_table_delimiter_row(lines[i + 1].text)


def _parse_blocks(lines: list[SourceLine]) -> list[MarkdownBlock]:
    """Group physical lines into structural Markdown blocks."""
    blocks: list[MarkdownBlock] = []
    i = 0
    num_lines = len(lines)

    while i < num_lines:
        line = lines[i]
        text = line.text
        stripped = text.strip()

        # Blank lines
        if not stripped:
            i += 1
            continue

        # Fenced code block (``` or ~~~)
        fence_info = _parse_fence_open(text)
        if fence_info is not None:
            fence_char, fence_len = fence_info
            start_line = i
            i += 1
            closed = False
            while i < num_lines:
                if _is_fence_close(lines[i].text, fence_char, fence_len):
                    closed = True
                    break
                i += 1
            end_line = min(i, num_lines - 1)
            blocks.append(
                MarkdownBlock(
                    block_type="fenced" if closed else "unclosed_fenced",
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=lines[start_line].start_byte,
                    end_byte=lines[end_line].end_byte,
                    full_end_byte=lines[end_line].full_end_byte,
                )
            )
            i += 1
            continue

        # Blockquote or Callout (> ...)
        if text.lstrip().startswith(">"):
            start_line = i
            while i + 1 < num_lines:
                nxt = lines[i + 1]
                if nxt.text.lstrip().startswith(">"):
                    i += 1
                else:
                    break
            end_line = i
            blocks.append(
                MarkdownBlock(
                    block_type="blockquote",
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=lines[start_line].start_byte,
                    end_byte=lines[end_line].end_byte,
                    full_end_byte=lines[end_line].full_end_byte,
                    container_setext_hazard=_container_setext_hazard(
                        lines, start_line, end_line, "blockquote"
                    ),
                )
            )
            i += 1
            continue

        # Table: only when confirmed as header followed by delimiter row
        if _is_table_start(lines, i):
            start_line = i
            i += 2  # header + delimiter
            while i < num_lines:
                nxt = lines[i]
                if not nxt.text.strip():
                    break
                if not _has_unescaped_pipe_outside_code(nxt.text):
                    break
                i += 1
            end_line = i - 1
            blocks.append(
                MarkdownBlock(
                    block_type="table",
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=lines[start_line].start_byte,
                    end_byte=lines[end_line].end_byte,
                    full_end_byte=lines[end_line].full_end_byte,
                )
            )
            continue

        # List
        if re.match(r"^[ \t]*([-*+]|\d+[\.\)])[ \t]+", text):
            start_line = i
            while i + 1 < num_lines:
                nxt = lines[i + 1]
                if not nxt.text.strip():
                    if i + 2 < num_lines:
                        after_blank = lines[i + 2]
                        if re.match(r"^[ \t]*([-*+]|\d+[\.\)])[ \t]+", after_blank.text) or (
                            after_blank.text.startswith(("  ", "\t")) and after_blank.text.strip()
                        ):
                            i += 2
                            continue
                    break
                if re.match(r"^[ \t]*([-*+]|\d+[\.\)])[ \t]+", nxt.text) or nxt.text.startswith(("  ", "\t")):
                    i += 1
                    continue
                break
            end_line = i
            blocks.append(
                MarkdownBlock(
                    block_type="list",
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=lines[start_line].start_byte,
                    end_byte=lines[end_line].end_byte,
                    full_end_byte=lines[end_line].full_end_byte,
                    container_setext_hazard=_container_setext_hazard(
                        lines, start_line, end_line, "list"
                    ),
                )
            )
            i += 1
            continue

        # Heading
        if re.match(r"^[ \t]*#{1,6}[ \t]+", text):
            blocks.append(
                MarkdownBlock(
                    block_type="heading",
                    start_line=i,
                    end_line=i,
                    start_byte=line.start_byte,
                    end_byte=line.end_byte,
                    full_end_byte=line.full_end_byte,
                )
            )
            i += 1
            continue

        # Standalone anchor line: skip, not a content block
        if re.fullmatch(r"\^([a-zA-Z0-9-]+)", stripped):
            i += 1
            continue

        # Setext heading
        setext_end = _check_setext_heading(lines, i)
        if setext_end is not None:
            blocks.append(
                MarkdownBlock(
                    block_type="setext_heading",
                    start_line=i,
                    end_line=setext_end,
                    start_byte=lines[i].start_byte,
                    end_byte=lines[setext_end].end_byte,
                    full_end_byte=lines[setext_end].full_end_byte,
                )
            )
            i = setext_end + 1
            continue

        # Paragraph
        start_line = i
        while i + 1 < num_lines:
            nxt = lines[i + 1]
            if not nxt.text.strip():
                break
            if (
                re.match(r"^[ \t]*(`{3,}|~{3,})", nxt.text)
                or nxt.text.lstrip().startswith(">")
                or _is_table_start(lines, i + 1)
                or re.match(r"^[ \t]*([-*+]|\d+[\.\)])[ \t]+", nxt.text)
                or re.match(r"^[ \t]*#{1,6}[ \t]+", nxt.text)
                or re.fullmatch(r"\^[a-zA-Z0-9-]+", nxt.text.strip())
            ):
                break
            i += 1
        end_line = i
        blocks.append(
            MarkdownBlock(
                block_type="paragraph",
                start_line=start_line,
                end_line=end_line,
                start_byte=lines[start_line].start_byte,
                end_byte=lines[end_line].end_byte,
                full_end_byte=lines[end_line].full_end_byte,
            )
        )
        i += 1

    # Detect existing block anchors strictly according to approved layouts
    for block in blocks:
        if block.existing_anchor is not None:
            continue
        if block.block_type in ("paragraph", "heading"):
            last_line_text = lines[block.end_line].text
            m = re.search(r"(?:[ \t])\^([a-zA-Z0-9-]+)[ \t]*$", last_line_text)
            if m:
                block.existing_anchor = m.group(1)
        elif block.block_type in ("list", "blockquote", "table", "fenced"):
            # Approved layout: containing block, exactly 1 blank separator, standalone marker line,
            # followed by either blank line or EOF.
            if block.end_line + 2 < num_lines:
                line_sep1 = lines[block.end_line + 1]
                line_marker = lines[block.end_line + 2]
                if line_sep1.text.strip() == "":
                    m = re.fullmatch(r"\^([a-zA-Z0-9-]+)", line_marker.text.strip())
                    if m:
                        if (
                            block.end_line + 3 >= num_lines
                            or lines[block.end_line + 3].text.strip() == ""
                        ):
                            block.existing_anchor = m.group(1)

    return blocks


def _find_all_anchor_tokens(lines: list[SourceLine]) -> set[str]:
    """Find all block anchor tokens present anywhere in lines."""
    tokens = set()
    for line in lines:
        stripped = line.text.strip()
        m = re.fullmatch(r"\^([a-zA-Z0-9-]+)", stripped)
        if m:
            tokens.add(m.group(1))
            continue
        m = re.search(r"(?:^|[ \t])\^([a-zA-Z0-9-]+)$", stripped)
        if m:
            tokens.add(m.group(1))
    return tokens


def _find_target_block(
    lines: list[SourceLine],
    blocks: list[MarkdownBlock],
    start_byte: int,
    end_byte: int,
) -> MarkdownBlock | None:
    """Find containing block for the non-empty content in byte range.

    Fails closed: every non-empty segment of the selection must map to the
    exact same MarkdownBlock. If any non-empty fragment has no block or falls
    into a different block, returns None.
    """
    target_block: MarkdownBlock | None = None

    for line in lines:
        if line.end_byte <= start_byte or line.start_byte >= end_byte:
            continue
        seg_start = max(line.start_byte, start_byte)
        seg_end = min(line.end_byte, end_byte)
        if seg_end > seg_start:
            seg_bytes = line.raw[seg_start - line.start_byte : seg_end - line.start_byte]
            if seg_bytes.strip():
                line_block: MarkdownBlock | None = None
                for block in blocks:
                    if block.start_line <= line.index <= block.end_line:
                        line_block = block
                        break
                if line_block is None:
                    return None
                if target_block is None:
                    target_block = line_block
                elif target_block is not line_block:
                    return None

    return target_block


def _resolve_and_insert_anchor(
    src_path: Path,
    source_bytes: bytes,
    start_byte: int,
    end_byte: int,
    anchor_name: str,
) -> tuple[str, str | None, bytes | None]:
    """Resolve target block and prepare byte-exact anchor insertion."""
    lines = _parse_lines(source_bytes)
    blocks = _parse_blocks(lines)
    target_block = _find_target_block(lines, blocks, start_byte, end_byte)

    if target_block is None:
        return (
            "failed",
            f"could not identify containing block for selection in {src_path.name}; anchor not inserted",
            None,
        )

    # 1. Unclosed fenced code block must fail closed
    if target_block.block_type == "unclosed_fenced":
        return (
            "failed",
            f"selection is inside an unclosed fenced code block in {src_path.name}; anchor not inserted",
            None,
        )

    # 2. Setext heading must conservatively fail closed (never append to underline)
    if target_block.block_type == "setext_heading":
        return (
            "failed",
            f"selection is a Setext heading in {src_path.name}; anchor insertion into Setext headings is not supported",
            None,
        )

    # 2b. Container Setext hazard: blockquote/callout/list whose logical
    # content contains a nested Setext heading. Whole-block conservative
    # granularity (see MarkdownBlock.container_setext_hazard): every exact
    # selection inside this container fails closed with zero source writes.
    if target_block.container_setext_hazard:
        return (
            "failed",
            f"selection is inside a blockquote/callout/list containing a Setext heading in {src_path.name}; anchor insertion into containers with Setext headings is not supported",
            None,
        )

    # 3. Check if target block already has an existing anchor
    if target_block.existing_anchor is not None:
        if target_block.existing_anchor == anchor_name:
            return (
                "existing",
                f"anchor '^{anchor_name}' already present; left unchanged",
                None,
            )
        else:
            return (
                "failed",
                f"target block already has a different anchor '^{target_block.existing_anchor}'; anchor not inserted",
                None,
            )

    # 4. Check if anchor_name already exists anywhere else in the document
    all_anchors = _find_all_anchor_tokens(lines)
    if anchor_name in all_anchors:
        return (
            "failed",
            f"anchor '^{anchor_name}' already exists on another block in {src_path.name}; anchor not inserted",
            None,
        )

    # 5. Insert anchor into target_block
    if target_block.block_type in ("paragraph", "heading"):
        last_line = lines[target_block.end_line]
        insert_pos = last_line.end_byte
        if last_line.raw.endswith((b" ", b"\t")):
            to_insert = f"^{anchor_name}".encode("utf-8")
        else:
            to_insert = f" ^{anchor_name}".encode("utf-8")
        new_source_bytes = (
            source_bytes[:insert_pos] + to_insert + source_bytes[insert_pos:]
        )
        return "inserted", None, new_source_bytes
    else:
        last_line = lines[target_block.end_line]
        block_end_pos = last_line.full_end_byte
        newline = _detect_newline(source_bytes, block_end_pos)
        anchor_line = f"^{anchor_name}".encode("utf-8") + newline

        if last_line.newline == b"":
            leading_nl = newline
        else:
            leading_nl = b""

        rest = source_bytes[block_end_pos:]
        if not rest:
            # At EOF: insert empty line before marker, marker line, and terminating empty line
            to_insert = leading_nl + newline + anchor_line + newline
        elif rest.startswith(b"\r\n") or rest.startswith(b"\n"):
            # Already followed by at least one blank line: insert empty line before marker and marker line;
            # rest provides the blank line after marker
            to_insert = leading_nl + newline + anchor_line
        else:
            # Followed immediately by text without blank line: insert blank line before marker, marker line, and blank line after marker
            to_insert = leading_nl + newline + anchor_line + newline

        new_source_bytes = (
            source_bytes[:block_end_pos] + to_insert + source_bytes[block_end_pos:]
        )
        return "inserted", None, new_source_bytes


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
    """Locate the end of ``selection`` inside ``note_text``."""
    sel_lines = [ln.strip() for ln in selection.splitlines() if ln.strip()]
    if not sel_lines:
        return None
    anchor_line = sel_lines[-1]
    note_lines = note_text.splitlines()
    for i in range(len(note_lines) - 1, -1, -1):
        if note_lines[i].strip() == anchor_line:
            return i, note_lines[i]
    return None


def _insert_anchor_text(
    text: str, selection: str, anchor_name: str
) -> tuple[bool, str | None, str]:
    marker = f"^{anchor_name}"
    if marker in text:
        return False, f"anchor {marker!r} already present; left unchanged", text
    hit = _find_anchor_target(text, selection)
    if hit is None:
        return (
            False,
            f"could not locate selection in note to insert anchor {marker!r}; "
            "anchor not inserted",
            text,
        )
    index, _line = hit
    lines = text.splitlines()
    leading = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    lines.insert(index + 1, f"{leading}{marker}")
    return True, None, "\n".join(lines) + "\n"


def _insert_backlink_text(
    text: str, anchor_name: str, card_stem: str
) -> tuple[bool, str | None, str]:
    card_link = f"[[{card_stem}]]"
    if card_link in text:
        return False, f"backlink {card_link!r} already present; left unchanged", text
    lines = text.splitlines()
    marker = f"^{anchor_name}"
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == marker:
            lines.insert(i + 1, f"> 卡片：{card_link}")
            return True, None, "\n".join(lines) + "\n"
    return False, (
        f"anchor {marker!r} not found in note; "
        "backlink not inserted"
    ), text


def _insert_anchor(
    note_path: Path, selection: str, anchor_name: str
) -> tuple[bool, str | None]:
    """Legacy fuzzy insert ``^anchor_name`` after selection's last line."""
    text = note_path.read_text(encoding="utf-8")
    inserted, warning, updated = _insert_anchor_text(text, selection, anchor_name)
    if inserted:
        _atomic_write_text(note_path, updated)
    return inserted, warning


def _insert_backlink(
    note_path: Path, anchor_name: str, card_stem: str
) -> tuple[bool, str | None]:
    """Insert ``> 卡片：[[<card_stem>]]`` after the ``^anchor_name`` line."""
    text = note_path.read_text(encoding="utf-8")
    inserted, warning, updated = _insert_backlink_text(text, anchor_name, card_stem)
    if inserted:
        _atomic_write_text(note_path, updated)
    return inserted, warning


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
    source_start_byte: int | None = None,
    source_end_byte: int | None = None,
) -> CardCreateResult:
    """Create a derived card note under ``<paper_dir>/cards/``.

    ``selection`` is the verbatim Markdown of the selected figure note
    content. ``filename`` overrides the auto slug (``card_<slug>.md``).
    When ``source_start_byte`` and ``source_end_byte`` are given, exact
    byte range mode is used: the selection is matched byte-for-byte in
    the canonical Figure解读 note, a deterministic ASCII-safe anchor is
    generated and inserted according to Obsidian block semantics, and the
    card links back to it.
    When ``anchor_name`` and ``source_note`` are given without offsets,
    legacy fuzzy line matching is used.

    Raises :class:`CardConflict` when the target file already exists;
    :class:`CardError` for validation problems. Writes are atomic.
    """
    root = Path(vault_root)
    canonical_root_path = root.resolve()
    if not canonical_root_path.is_dir():
        raise CardError(f"vault root is not a directory: {vault_root}")

    if not hasattr(os, "O_NOFOLLOW"):
        raise CardError("O_NOFOLLOW is not supported on this platform")

    root_fd = None
    lit_fd = None
    paper_fd = None
    cards_fd = None
    lock = None
    is_exact_mode = False
    tx_state: CardTransactionState | None = None
    final_filename: str = ""
    canonical_file: str = ""
    src_path_exact: Path | None = None
    try:
        try:
            root_fd = os.open(str(canonical_root_path), os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise CardError(f"cannot open vault root {canonical_root_path}: {exc}") from exc
        root_st = os.fstat(root_fd)
        root_anchor = (root_st.st_dev, root_st.st_ino)
        _verify_root_anchor(root, canonical_root_path, root_anchor)

        if not title.strip():
            raise CardError("card title must not be empty")
        if not selection.strip():
            raise CardError("selection body must not be empty")

        if filename is not None:
            raw_name = filename.strip()
            if not raw_name:
                raise CardError("card filename must not be empty")
            if "\x00" in raw_name:
                raise CardError("card filename must not contain NUL bytes")
            if "/" in raw_name or "\\" in raw_name:
                raise CardError("card filename must not contain path separators")
            if Path(raw_name).is_absolute():
                raise CardError("card filename must not be an absolute path")
            stem_name = raw_name[:-3] if raw_name.endswith(".md") else raw_name
            if not stem_name or stem_name.startswith(".") or stem_name.endswith("."):
                raise CardError("card filename must not be empty or start/end with a dot")
            if ".." in stem_name or stem_name in (".", ".."):
                raise CardError("card filename must not contain directory traversal")
            final_filename = f"{stem_name}.md"
        else:
            final_filename = f"card_{slugify_card_filename(title)}.md"

        has_start = source_start_byte is not None
        has_end = source_end_byte is not None
        if has_start != has_end:
            raise CardError("source_start_byte and source_end_byte must be provided together")

        is_exact_mode = has_start and has_end
        if is_exact_mode:
            if isinstance(source_start_byte, bool) or not isinstance(source_start_byte, int):
                raise CardError("source_start_byte must be an integer")
            if isinstance(source_end_byte, bool) or not isinstance(source_end_byte, int):
                raise CardError("source_end_byte must be an integer")
            if source_start_byte < 0 or source_end_byte < 0:
                raise CardError("source byte offsets must be non-negative")
            if source_start_byte >= source_end_byte:
                raise CardError(
                    f"source_start_byte ({source_start_byte}) must be strictly less than "
                    f"source_end_byte ({source_end_byte})"
                )
            if not source_note or not source_note.strip():
                raise CardError("source_note is required in exact byte range mode")
            if anchor_name is not None:
                raise CardError("explicit anchor_name is not allowed in exact byte range mode")

        _verify_root_anchor(root, canonical_root_path, root_anchor)
        citation_key, paper_id = _resolve_paper(canonical_root_path, key)
        cards_dir = cards_directory(canonical_root_path, citation_key)

        target = cards_dir / final_filename
        if target.parent != cards_dir or target.name != final_filename:
            raise CardError(f"invalid card filename: {filename!r}")
        card_stem = target.stem

        # Advisory pre-check before lock without mutating filesystem
        try:
            if target.is_symlink() or target.exists():
                raise CardConflict(f"card already exists: {target}")
        except (CardConflict, CardError):
            raise
        except OSError:
            pass

        canonical_stem = f"Figure解读_{citation_key}"
        canonical_file = f"{canonical_stem}.md"

        if is_exact_mode:
            tx_state = CardTransactionState(canonical_file=canonical_file)
            source_note_clean = source_note.strip()
            if "/" in source_note_clean or "\\" in source_note_clean or ".." in source_note_clean:
                raise CardError("source_note must not contain path separators or directory traversal")
            if source_note_clean != canonical_stem and source_note_clean != canonical_file:
                raise CardError(
                    f"source_note must match canonical '{canonical_file}' in exact byte range mode (got {source_note!r})"
                )

        _verify_root_anchor(root, canonical_root_path, root_anchor)
        try:
            lock = acquire_lock(
                canonical_root_path,
                "create_card",
                root_fd=root_fd,
                root_anchor=root_anchor,
            )
        except LockConflict as exc:
            raise CardConflict(f"vault write lock held: {exc}") from exc
        except StaleLockError as exc:
            raise CardError(f"vault write lock stale: {exc}") from exc
        except LockError as exc:
            raise CardError(f"vault write lock error: {exc}") from exc

        _verify_root_anchor(root, canonical_root_path, root_anchor)
        lit_fd = _ensure_dir_nofollow(root_fd, LITERATURE_ROOT, root_anchor=root_anchor)
        paper_fd = _open_dir_nofollow(lit_fd, citation_key, root_anchor=root_anchor)
        cards_fd = _ensure_dir_nofollow(paper_fd, "cards", root_anchor=root_anchor)

        # Record the canonical directory binding for the exact-mode state
        # machine: identities of the held dirfds for the canonical path
        # root → 05 Literature → <paper> → cards. Re-verified before P2/P3.
        if is_exact_mode:
            assert tx_state is not None
            tx_state.root_anchor = root_anchor
            lit_st_held = os.fstat(lit_fd)
            paper_st_held = os.fstat(paper_fd)
            cards_st_held = os.fstat(cards_fd)
            tx_state.lit_ident = (lit_st_held.st_dev, lit_st_held.st_ino)
            tx_state.paper_ident = (paper_st_held.st_dev, paper_st_held.st_ino)
            tx_state.cards_ident = (cards_st_held.st_dev, cards_st_held.st_ino)

        # Under lock and safe cards_fd: re-check target existence before any writes
        try:
            st = os.lstat(final_filename, dir_fd=cards_fd)
            raise CardConflict(f"card already exists: {target}")
        except FileNotFoundError:
            pass

        anchor_link: str | None = None
        anchor_inserted = False
        backlink_inserted = False
        anchor_status: str | None = None
        warnings: list[str] = []

        if is_exact_mode:
            assert tx_state is not None
            # P0: observe and plan under lock — no visible mutation yet.
            stem_hash = hashlib.sha256(card_stem.encode("utf-8")).hexdigest()[:16]
            effective_anchor_name: str | None = f"card-{stem_hash}"

            src_path = paper_directory(canonical_root_path, citation_key) / canonical_file
            src_path_exact = src_path

            # Open the canonical source pinned and read initial bytes.
            source_fd: int | None = None
            source_initial_bytes: bytes | None = None
            source_ident: tuple[int, int] | None = None
            source_mode: int = 0o644
            source_exists = False
            try:
                st = os.lstat(canonical_file, dir_fd=paper_fd)
                if stat.S_ISLNK(st.st_mode):
                    raise CardError(f"source note is a symlink: {canonical_file}")
                if stat.S_ISREG(st.st_mode):
                    source_exists = True
                    source_fd = os.open(canonical_file, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=paper_fd)
            except FileNotFoundError:
                source_exists = False

            if not source_exists:
                # Semantic failure: no-link card + warning, zero source writes.
                anchor_status = "failed"
                warnings.append(
                    f"source note not found: {canonical_file} under paper directory; anchor not inserted"
                )
                final_card_content = _format_card_content(
                    paper_id=paper_id,
                    citation_key=citation_key,
                    selection=selection,
                    anchor_link=None,
                )
            else:
                try:
                    st = os.fstat(source_fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise CardError(f"source note is not a regular file: {canonical_file}")
                    source_ident = (st.st_dev, st.st_ino)
                    source_mode = stat.S_IMODE(st.st_mode)
                    source_initial_bytes = _read_all_from_pinned_fd(source_fd)
                except BaseException:
                    # The pinned fd is handed to tx_state below; a failure
                    # before that handoff must still close it here.
                    os.close(source_fd)
                    source_fd = None
                    raise

                # Keep the P0 pinned fd open through P2: the reread and the
                # final pre-replace stat share this fd, eliminating the
                # read/temp-stat seam of a fresh open. It is closed in the
                # outer finally (or earlier once no source commit is planned).
                tx_state.source_fd = source_fd
                source_fd = None

                tx_state.initial_source_ident = source_ident
                tx_state.initial_source_bytes = source_initial_bytes
                tx_state.initial_source_mode = source_mode

                sel_bytes = selection.encode("utf-8")
                if source_end_byte > len(source_initial_bytes):
                    anchor_status = "failed"
                    warnings.append(
                        f"source byte range [{source_start_byte}:{source_end_byte}] exceeds file length {len(source_initial_bytes)}; anchor not inserted"
                    )
                elif source_initial_bytes[source_start_byte:source_end_byte] != sel_bytes:
                    anchor_status = "failed"
                    warnings.append(
                        f"source text at byte range [{source_start_byte}:{source_end_byte}] does not match selection; anchor not inserted"
                    )
                else:
                    status, warning, new_source_bytes = _resolve_and_insert_anchor(
                        src_path,
                        source_initial_bytes,
                        source_start_byte,
                        source_end_byte,
                        effective_anchor_name,
                    )
                    if warning:
                        warnings.append(warning)

                    if status == "existing":
                        # Existing anchor: source untouched; safe-create final card.
                        anchor_status = "existing"
                        anchor_inserted = False
                        anchor_link = f"[[{canonical_stem}#^{effective_anchor_name}|{canonical_stem}]]"
                    elif status == "inserted" and new_source_bytes is not None:
                        anchor_status = "inserted"
                        anchor_inserted = True
                        anchor_link = f"[[{canonical_stem}#^{effective_anchor_name}|{canonical_stem}]]"
                    else:
                        anchor_status = "failed"
                        anchor_inserted = False
                        anchor_link = None

                final_card_content = _format_card_content(
                    paper_id=paper_id,
                    citation_key=citation_key,
                    selection=selection,
                    anchor_link=anchor_link,
                )

            needs_source_commit = (
                source_exists
                and anchor_status == "inserted"
                and anchor_link is not None
                and new_source_bytes is not None
            )
            if not needs_source_commit and tx_state.source_fd is not None:
                # No source commit planned: release the pinned fd early.
                os.close(tx_state.source_fd)
                tx_state.source_fd = None

            # P1: stage all three temps (same-directory, fsynced) before any
            # visible mutation. Any failure here is identity-safe cleanup only.
            # Receipts are registered on tx_state at staging time so that a
            # failure injected right after a stage call returns still finds
            # the identity-owned temp registered for cleanup.
            card_temp = _stage_temp_bytes(
                cards_fd,
                final_filename,
                final_card_content.encode("utf-8"),
                root_anchor=root_anchor,
                register=lambda r: setattr(tx_state, "card_temp", r),
            )
            card_temp = tx_state.card_temp
            new_source_temp: StagedReceipt | None = None
            rollback_temp: StagedReceipt | None = None
            if needs_source_commit:
                _stage_temp_bytes(
                    paper_fd,
                    canonical_file,
                    new_source_bytes,
                    mode=source_mode,
                    root_anchor=root_anchor,
                    register=lambda r: setattr(tx_state, "new_source_temp", r),
                )
                new_source_temp = tx_state.new_source_temp
                _stage_temp_bytes(
                    paper_fd,
                    canonical_file,
                    source_initial_bytes,
                    mode=source_mode,
                    root_anchor=root_anchor,
                    register=lambda r: setattr(tx_state, "rollback_temp", r),
                )
                rollback_temp = tx_state.rollback_temp

            # P2: commit source (best-effort precondition observations; NOT
            # CAS). Check ordering: staged new-source identity first, then a
            # reread from the P0 pinned fd, then — as the final userspace
            # check immediately before the replace — a nofollow stat of the
            # canonical source path still holding the P0 inode.
            if needs_source_commit:
                # Final pre-commit verification bundle: root anchor, ancestry
                # AND canonical directory binding (lit/paper) — the directory
                # entries must still point at the held inodes.
                _verify_root_anchor(root, canonical_root_path, root_anchor)
                _verify_ancestry(paper_fd, root_anchor)
                _verify_canonical_binding(
                    root_anchor,
                    tx_state.lit_ident,
                    tx_state.paper_ident,
                    None,
                    root_fd,
                    LITERATURE_ROOT,
                    citation_key,
                    None,
                )
                # New-source temp must still match its receipt.
                _verify_staged_identity(new_source_temp)
                # Reread the initial bytes through the P0 pinned fd: ordinary
                # concurrent write detection (best-effort, not CAS).
                if _read_all_from_pinned_fd(tx_state.source_fd) != source_initial_bytes:
                    raise CardError(
                        "source note changed concurrently before commit; source not modified"
                    )
                # Final userspace check, adjacent to the replace: the canonical
                # source path must still hold the P0 inode.
                try:
                    cur_st = os.lstat(canonical_file, dir_fd=paper_fd)
                except FileNotFoundError:
                    raise CardError("source note vanished before commit; staging discarded")
                if (cur_st.st_dev, cur_st.st_ino) != source_ident:
                    raise CardError("source note was swapped before commit; source not modified")
                commit_error: BaseException | None = None
                try:
                    os.replace(new_source_temp.name, canonical_file, src_dir_fd=paper_fd, dst_dir_fd=paper_fd)
                except BaseException as exc:
                    commit_error = exc
                if commit_error is not None:
                    # Ambiguity resolution by identity only (no blind writes).
                    try:
                        post_st = os.lstat(canonical_file, dir_fd=paper_fd)
                    except OSError:
                        post_st = None
                    if post_st is not None and (post_st.st_dev, post_st.st_ino) == new_source_temp.ident:
                        # Replace landed despite the reported error (e.g. a
                        # wrapper exception inside a successful replace):
                        # treat as committed and continue to P3.
                        new_source_temp.consumed = True
                        tx_state.source_committed = True
                    elif post_st is not None and (post_st.st_dev, post_st.st_ino) == source_ident:
                        # Not committed: the canonical path still holds the
                        # initial inode, so treat this as a plain pre-commit
                        # failure; the outer cleanup discards the staging.
                        raise CardError(f"source commit failed before replace: {commit_error}") from commit_error
                    else:
                        # Genuinely ambiguous: mark rollback/new-source temps
                        # as residue and never blindly restore.
                        rollback_temp.keep_as_residue = True
                        new_source_temp.keep_as_residue = True
                        raise CardError(
                            f"source commit ended ambiguous (replace error: {commit_error}); no destructive recovery attempted"
                        ) from commit_error
                tx_state.source_committed = True
                new_source_temp.consumed = True
            # P3: publish final card with RENAME_EXCL — business commit point.
            if anchor_status is not None:
                # Final pre-publish verification bundle: root anchor, ancestry
                # AND canonical directory binding (lit/paper/cards).
                _verify_root_anchor(root, canonical_root_path, root_anchor)
                _verify_ancestry(cards_fd, root_anchor)
                _verify_canonical_binding(
                    root_anchor,
                    tx_state.lit_ident,
                    tx_state.paper_ident,
                    tx_state.cards_ident,
                    root_fd,
                    LITERATURE_ROOT,
                    citation_key,
                    "cards",
                )
                _verify_staged_identity(card_temp)
                publish_error: BaseException | None = None
                try:
                    _renameatx_np(cards_fd, card_temp.name, cards_fd, final_filename, _RENAME_EXCL)
                except BaseException as exc:
                    publish_error = exc
                if publish_error is not None:
                    # Ambiguity resolution by identity: did the publish land
                    # despite the reported error? If the card target now holds
                    # the staged card inode, the business commit point was
                    # reached: mark committed and return success (no raise).
                    receipt_stat_failed = False
                    try:
                        post_st = os.stat(final_filename, dir_fd=cards_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        # The target entry does not exist: the publish
                        # reliably did NOT land. Not ambiguous.
                        post_st = None
                    except OSError:
                        post_st = None
                        receipt_stat_failed = True
                    if post_st is not None and (post_st.st_dev, post_st.st_ino) == card_temp.ident:
                        card_temp.consumed = True
                        tx_state.card_committed = True
                        publish_error = None
                    elif post_st is not None:
                        # Reliably proven: the target is NOT the card receipt.
                        # The publish did not land as the card inode.
                        if (
                            isinstance(publish_error, FileExistsError)
                            or getattr(publish_error, "errno", None) == errno.EEXIST
                        ):
                            conflict = CardConflict(f"card already exists: {target}")
                            conflict.__cause__ = publish_error
                            # Do NOT compensate here: the outer handler
                            # performs the source compensation exactly once.
                            raise conflict
                        # Do NOT compensate here either: raise and let the
                        # outer handler compensate exactly once.
                        raise CardError(f"card publish failed: {publish_error}") from publish_error
                    elif not receipt_stat_failed:
                        # Reliable FileNotFoundError: the target entry does not
                        # exist, so the publish did not land. Raise and let
                        # the outer handler compensate exactly once.
                        if (
                            isinstance(publish_error, FileExistsError)
                            or getattr(publish_error, "errno", None) == errno.EEXIST
                        ):
                            conflict = CardConflict(f"card already exists: {target}")
                            conflict.__cause__ = publish_error
                            raise conflict
                        raise CardError(f"card publish failed: {publish_error}") from publish_error
                    else:
                        # The receipt identity query itself failed (EIO etc.):
                        # genuinely ambiguous. We cannot prove the publish did
                        # NOT land, so compensation is forbidden. Preserve
                        # card temp (may already be the published file or may
                        # still be staged) and rollback temp as recovery
                        # evidence; the outer handler must skip compensation
                        # and every cleanup that could disturb the unknown
                        # business state.
                        tx_state.p3_ambiguous = True
                        tx_state.partial = True
                        card_temp.keep_as_residue = True
                        if rollback_temp is not None:
                            rollback_temp.keep_as_residue = True
                        ambiguous = CardError(
                            f"card publish ended ambiguous (publish error: {publish_error}; "
                            "receipt identity query failed): business state unknown; "
                            "no compensation attempted, temp residue preserved for manual recovery"
                        )
                        ambiguous.__cause__ = publish_error
                        raise ambiguous
                card_temp.consumed = True
                tx_state.card_committed = True

            if (
                tx_state.card_committed
                or anchor_status is None
                or not needs_source_commit
            ) and tx_state.source_fd is not None:
                # Business commit reached (or no source commit was made):
                # release the pinned fd best-effort. A close failure after
                # the business commit must NEVER convert the success into an
                # exception.
                try:
                    os.close(tx_state.source_fd)
                except OSError as close_exc:
                    warnings.append(f"source fd close failed after commit: {close_exc}")
                tx_state.source_fd = None

            # P3 was the business commit point: no failable bookkeeping checks
            # after it. Root/ancestry verification already happened inside P2/P3
            # before each visible mutation.

            # Success path: identity-safe cleanup of the rollback temp. A
            # cleanup failure is a warning only — never a rollback of the
            # committed card/source and never a conversion of the success
            # into a retryable failure.
            if tx_state.rollback_temp is not None and not tx_state.rollback_temp.consumed:
                try:
                    _cleanup_staged_temp(tx_state.rollback_temp)
                    tx_state.rollback_temp.consumed = True
                except Exception as comp_exc:
                    tx_state.rollback_temp.keep_as_residue = True
                    warnings.append(
                        f"rollback temp residue: {tx_state.rollback_temp.name} ({comp_exc})"
                    )
            if tx_state.source_fd is not None:
                # Success: release the pinned source fd best-effort.
                try:
                    os.close(tx_state.source_fd)
                except OSError as close_exc:
                    warnings.append(f"source fd close failed after commit: {close_exc}")
                tx_state.source_fd = None

        elif anchor_name and source_note:
            effective_anchor_name = anchor_name
            source_note_clean = source_note.strip()
            if "/" in source_note_clean or "\\" in source_note_clean or ".." in source_note_clean:
                raise CardError("source_note must not contain path separators or directory traversal")

            cand_md = f"{source_note_clean}.md" if not source_note_clean.endswith(".md") else source_note_clean
            cand_raw = source_note_clean[:-3] if source_note_clean.endswith(".md") else source_note_clean
            src_filename = None
            for cand in (cand_md, cand_raw):
                try:
                    st = os.lstat(cand, dir_fd=paper_fd)
                    if stat.S_ISLNK(st.st_mode):
                        raise CardError(f"source note is a symlink: {source_note!r}")
                    if stat.S_ISREG(st.st_mode):
                        src_filename = cand
                        break
                except FileNotFoundError:
                    continue
            if src_filename is None:
                raise CardError(
                    f"source note not found: {source_note!r} "
                    f"(expected under the paper directory)"
                )

            src_path = paper_directory(canonical_root_path, citation_key) / src_filename
            src_text = _read_bytes_dirfd(paper_fd, src_filename).decode("utf-8")
            anchor_inserted, warning, updated_text = _insert_anchor_text(src_text, selection, anchor_name)
            if warning:
                warnings.append(warning)
                if "already present" in warning:
                    anchor_status = "existing"
                else:
                    anchor_status = "failed"
            else:
                anchor_status = "inserted"
                _verify_root_anchor(root, canonical_root_path, root_anchor)
                _atomic_write_bytes(src_path, updated_text.encode("utf-8"), dir_fd=paper_fd, root_anchor=root_anchor)
                src_text = updated_text

            anchor_link = f"[[{src_path.stem}#^{anchor_name}|{source_note}]]"
            if backlink:
                backlink_inserted, warning, backlinked_text = _insert_backlink_text(
                    src_text, anchor_name, target.stem
                )
                if warning:
                    warnings.append(warning)
                else:
                    _verify_root_anchor(root, canonical_root_path, root_anchor)
                    _atomic_write_bytes(src_path, backlinked_text.encode("utf-8"), dir_fd=paper_fd, root_anchor=root_anchor)

            _verify_root_anchor(root, canonical_root_path, root_anchor)
            card_content = _format_card_content(
                paper_id=paper_id,
                citation_key=citation_key,
                selection=selection,
                anchor_link=anchor_link,
            )
            _atomic_write_new(target, card_content, dir_fd=cards_fd, root_anchor=root_anchor)
        else:
            effective_anchor_name = anchor_name
            anchor_status = None
            _verify_root_anchor(root, canonical_root_path, root_anchor)
            card_content = _format_card_content(
                paper_id=paper_id,
                citation_key=citation_key,
                selection=selection,
                anchor_link=None,
            )
            _atomic_write_new(target, card_content, dir_fd=cards_fd, root_anchor=root_anchor)

        return CardCreateResult(
            citation_key=citation_key,
            paper_id=paper_id,
            path=target,
            anchor_name=effective_anchor_name,
            anchor_inserted=anchor_inserted,
            anchor_link=anchor_link,
            backlink_inserted=backlink_inserted,
            warnings=warnings,
            card_stem=card_stem,
            anchor_status=anchor_status,
        )
    except BaseException as primary_exc:
        cleanup_failures: list[CleanupFailure] = []
        if is_exact_mode and tx_state is not None:
            if tx_state.p3_ambiguous:
                # The P3 publish outcome is unknown: business state may be
                # committed. NEVER compensate and NEVER run cleanups that
                # could disturb the unknown state; the temps are already
                # preserved as recovery evidence. Re-raise with cause kept.
                err = CardError(
                    "transaction ended in ambiguous business state: card publish "
                    "outcome unknown; no compensation or destructive cleanup attempted"
                )
                if hasattr(err, "add_note"):
                    err.add_note(f"Primary error: {primary_exc}")
                    if tx_state.card_temp is not None:
                        err.add_note(f"card temp preserved as residue: {tx_state.card_temp.name}")
                    if tx_state.rollback_temp is not None:
                        err.add_note(f"rollback temp preserved as residue: {tx_state.rollback_temp.name}")
                raise err from primary_exc

            # P3 already committed the card: never roll back the business
            # state; only clean up identity-owned staging residue.
            if not tx_state.card_committed:
                # Card never published: remove the identity-owned card temp.
                if tx_state.card_temp is not None:
                    try:
                        _cleanup_staged_temp(tx_state.card_temp, primary_exc=primary_exc)
                    except Exception as exc:
                        cleanup_failures.append(CleanupFailure(component="card_temp_cleanup", exception=exc))
                # A published card file (no longer a temp) is never unlinked by
                # compensation: there is no card rollback in the P0-P3 machine.

                # Source compensation: only when the source replace already
                # committed and the card never did. Identity-bound only.
                # The source_committed flag covers the replace path; an
                # ambiguity between the flag and reality is resolved by
                # identity: if the canonical path now holds the staged
                # new-source inode, the replace landed and must be
                # compensated too.
                source_needs_compensation = tx_state.source_committed
                if (
                    not source_needs_compensation
                    and tx_state.new_source_temp is not None
                    and paper_fd is not None
                    and canonical_file
                    and not tx_state.new_source_temp.consumed
                ):
                    try:
                        st_maybe = os.lstat(canonical_file, dir_fd=paper_fd)
                        if (st_maybe.st_dev, st_maybe.st_ino) == tx_state.new_source_temp.ident:
                            source_needs_compensation = True
                    except OSError:
                        pass
                if (
                    source_needs_compensation
                    and tx_state.rollback_temp is not None
                    and tx_state.new_source_temp is not None
                    and paper_fd is not None
                    and canonical_file
                ):
                    try:
                        _compensate_committed_source(
                            paper_fd,
                            canonical_file,
                            tx_state.new_source_temp.ident,
                            tx_state.rollback_temp,
                        )
                        tx_state.rollback_temp.consumed = True
                    except Exception as exc:
                        cleanup_failures.append(CleanupFailure(component="source_compensation", exception=exc))

            # Identity-safe cleanup of any remaining staged temps.
            for stage in (tx_state.new_source_temp, tx_state.rollback_temp, tx_state.card_temp):
                if stage is None:
                    continue
                try:
                    _cleanup_staged_temp(stage, primary_exc=primary_exc)
                except Exception as exc:
                    cleanup_failures.append(
                        CleanupFailure(component=f"temp_cleanup:{stage.name}", exception=exc)
                    )

        if cleanup_failures:
            failure_details = [f"{f.component}: {f.exception}" for f in cleanup_failures]
            joined = "; ".join(failure_details)
            err = CardError(
                f"transaction aborted and cleanup failed: [{joined}]; original error: {primary_exc}"
            )
            err.cleanup_failures = cleanup_failures
            if hasattr(err, "add_note"):
                err.add_note(f"Primary error: {primary_exc}")
                for f in cleanup_failures:
                    err.add_note(f"Cleanup failure in {f.component}: {f.exception}")
            raise err from primary_exc

        raise
    finally:
        if tx_state is not None and tx_state.source_fd is not None:
            # Best-effort close: never raise out of finally.
            try:
                os.close(tx_state.source_fd)
            except OSError:
                pass
            tx_state.source_fd = None
        for fd_val in (cards_fd, paper_fd, lit_fd, root_fd):
            if fd_val is not None:
                try:
                    os.close(fd_val)
                except OSError:
                    pass
        if lock is not None:
            release_lock(lock)
