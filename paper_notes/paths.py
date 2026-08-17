"""Canonical paper asset path policy.

Every paper asset is resolved from the vault root and the citation key
under ``05 Literature/<key>/``:

- ``<key>.md``            main note
- ``<key>.pdf``           attached PDF
- ``minerUmd_<key>.md``   MinerU markdown
- ``Figure解读_<key>.md`` figure interpretation note
- ``attachments/``        figure/image attachments
- ``cards/``              flashcards
- ``figures/``            rendered figures

Keys are validated before any path is built (traversal rejection), using
the same character rules as the canonical schema.
"""

from pathlib import Path

from .models import CITATION_KEY_RE

LITERATURE_ROOT = "05 Literature"

_PATH_TRAVERSAL = ".."


def is_valid_key(key: str) -> bool:
    """True if ``key`` is safe to use as a directory/file name component."""
    return (
        isinstance(key, str)
        and bool(CITATION_KEY_RE.fullmatch(key))
        and _PATH_TRAVERSAL not in key
    )


def _require_key(key: str) -> None:
    if not is_valid_key(key):
        raise ValueError(f"invalid citation key: {key!r}")


def literature_root(vault_root: Path) -> Path:
    """The directory that holds all paper directories."""
    return vault_root / LITERATURE_ROOT


def moc_folder(vault_root: Path) -> Path:
    """Directory that holds Topic MOC notes (not a paper directory)."""
    return literature_root(vault_root) / "MOCs"


def paper_directory(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return literature_root(vault_root) / key


def main_note(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / f"{key}.md"


def pdf_attachment(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / f"{key}.pdf"


def mineru_markdown(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / f"minerUmd_{key}.md"


def figure_note(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / f"Figure解读_{key}.md"


def attachments_directory(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / "attachments"


def cards_directory(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / "cards"


def figures_directory(vault_root: Path, key: str) -> Path:
    _require_key(key)
    return paper_directory(vault_root, key) / "figures"
