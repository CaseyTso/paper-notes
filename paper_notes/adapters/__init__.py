"""External-system adapters (read-only access)."""

from paper_notes.adapters.zotero import (
    Attachment,
    ZoteroAdapter,
    ZoteroRecord,
)


class AdapterError(Exception):
    """A structured metadata source could not be queried.

    Raised by remote metadata adapters for transport failures, non-200
    responses, malformed payloads, and missing records. Callers catch this
    to skip a source; anything else is a programming error.
    """


__all__ = [
    "AdapterError",
    "ZoteroAdapter",
    "ZoteroRecord",
    "Attachment",
]
