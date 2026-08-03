"""Round-trip frontmatter codec for canonical paper notes.

``load_paper_note`` parses ``<citation_key>.md`` into a validated
:class:`~paper_notes.models.Paper` plus a :class:`RoundTripDocument`
that keeps the ruamel ``CommentedMap`` (comments and key order) and the
exact Markdown body. ``update_paper_note`` applies a small change set
with minimal rewriting: the body is never serialized through YAML, and
the file is only replaced after the merged frontmatter validates.

Newline handling is deterministic: CRLF input is normalized to LF for
parsing, the detected style is remembered, and writes restore it, so a
no-op update leaves the file byte-identical.

Validation failures (including forbidden volatile metric fields) happen
before any write; the source file is left untouched.
"""

import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .models import Paper

_RT = YAML(typ="rt")
_RT.width = 4096  # avoid re-wrapping long scalars on dump


class FrontmatterError(Exception):
    """Structured frontmatter parse/format error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RoundTripDocument:
    """Parsed note with round-trip fidelity.

    ``frontmatter`` is a mutable ruamel CommentedMap (comments and key
    order preserved); ``body`` is the exact Markdown body; ``newline``
    is the newline style detected from the source file.
    """

    frontmatter: CommentedMap
    body: str
    newline: str


def _parse(text: str) -> tuple[CommentedMap, str]:
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise FrontmatterError(
            "missing_frontmatter", "note does not start with a --- frontmatter block"
        )
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError(
            "unterminated_frontmatter", "frontmatter block is never closed with ---"
        )
    yaml_text = "\n".join(lines[1:end])
    try:
        data = _RT.load(yaml_text)
    except Exception as exc:  # ruamel raises varied YAMLError subclasses
        raise FrontmatterError("invalid_yaml", f"frontmatter is not valid YAML: {exc}") from exc
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise FrontmatterError("invalid_yaml", "frontmatter must be a YAML mapping")
    body = "\n".join(lines[end + 1 :])
    return data, body


def _serialize(doc: RoundTripDocument) -> str:
    buf = io.StringIO()
    _RT.dump(doc.frontmatter, buf)
    content = f"---\n{buf.getvalue()}---\n{doc.body}"
    if doc.newline == "\r\n":
        content = content.replace("\n", "\r\n")
    return content


def _atomic_write_text(path: Path, content: str) -> None:
    # Preserve the source file's permission bits: mkstemp creates 0600
    # temp files and os.replace would carry that mode onto the target.
    mode = stat.S_IMODE(path.stat().st_mode)
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


def load_paper_note(path: Path) -> tuple[Paper, RoundTripDocument]:
    """Load and validate a canonical paper note.

    Returns ``(Paper, RoundTripDocument)``; raises :class:`FrontmatterError`
    for missing/unterminated/invalid frontmatter and ``ValidationError``
    for data that does not satisfy the canonical schema.
    """
    # ``open(..., newline="")`` keeps CRLF bytes untouched so the source
    # newline style can be detected and restored on write. (Path.read_text
    # gained a newline parameter only in Python 3.12; the project supports
    # Python 3.11.)
    with path.open(encoding="utf-8", newline="") as fh:
        raw = fh.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    frontmatter, body = _parse(raw)
    paper = Paper(**dict(frontmatter))
    return paper, RoundTripDocument(frontmatter=frontmatter, body=body, newline=newline)


def update_paper_note(path: Path, changes: Mapping[str, Any]) -> Paper:
    """Apply ``changes`` to the frontmatter and atomically rewrite.

    ``None`` removes a key; other values set/replace it. The merged
    frontmatter is validated against :class:`~paper_notes.models.Paper`
    before anything is written — on validation failure the source file
    is untouched. The Markdown body is never parsed or reformatted.
    """
    paper, doc = load_paper_note(path)
    for key, value in changes.items():
        if value is None:
            doc.frontmatter.pop(key, None)
        else:
            doc.frontmatter[key] = value
    validated = Paper(**dict(doc.frontmatter))
    _atomic_write_text(path, _serialize(doc))
    return validated
