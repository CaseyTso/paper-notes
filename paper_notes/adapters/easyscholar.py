"""Read-only EasyScholar metrics adapter.

Queries the EasyScholar open-info search endpoint with the configured
SecretKey and maps the first hit onto a stable normalized result shape
(``source`` / ``journal`` / ``abbreviation`` / ``issn`` / ``level`` /
``metrics``). Metrics are volatile UI data per design section 10: this
adapter never writes to Markdown, never caches to disk, and never
includes the secret in errors.

Nonzero API codes (invalid credentials, rate limits, …) raise
:class:`EasyScholarError` so callers map them to nonzero exit codes.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

import requests

from paper_notes import config as _config
from paper_notes.adapters import AdapterError

API_URL = "https://www.easyscholar.cc/openInfo/getSearchData"


class EasyScholarError(AdapterError):
    """A structured EasyScholar query failure (transport/HTTP/API code)."""


def _to_float(value: Any) -> float | None:
    """Tolerant float parse; missing/blank/non-numeric values become None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _first_str(value: Any) -> str | None:
    """First non-empty string from a value or list of values."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def normalize(record: dict[str, Any], fallback_journal: str | None) -> dict[str, Any]:
    """Map one EasyScholar record onto stable normalized fields."""
    journal = _first_str(record.get("name")) or (fallback_journal or "")
    return {
        "source": "easyscholar",
        "journal": journal,
        "abbreviation": _first_str(record.get("abbreviation")),
        "issn": _first_str(record.get("issn")),
        "level": _first_str(record.get("level")),
        "metrics": {
            "if": _to_float(record.get("sciif")),
            "if5": _to_float(record.get("sciif5")),
            "jci": _to_float(record.get("jci")),
            "jcr_partition": _first_str(record.get("jcr")),
            "cas_partition": _first_str(record.get("cas")),
        },
        "queried_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


class EasyScholarAdapter:
    """Minimal read-only EasyScholar client.

    ``secret_key`` is kept in memory only; it is sent in the request
    ``params`` and never rendered into errors or results.
    """

    def __init__(self, secret_key: str, timeout: float = 20.0) -> None:
        self.secret_key = secret_key
        self.timeout = timeout

    def query(
        self, journal: str | None = None, issn: str | None = None
    ) -> dict[str, Any]:
        if not journal and not issn:
            raise ValueError(
                "EasyScholarAdapter.query requires journal or issn"
            )
        params: dict[str, str] = {"secretKey": self.secret_key}
        if journal:
            params["scienceName"] = journal
        if issn:
            params["issn"] = issn
        try:
            response = requests.get(API_URL, params=params, timeout=self.timeout)
        except requests.RequestException:
            # Never chain the transport exception: its repr may embed the
            # request URL, which carries ``secretKey`` in the query string.
            raise EasyScholarError("easyscholar request failed") from None
        if response.status_code != 200:
            raise EasyScholarError(
                f"easyscholar returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EasyScholarError("easyscholar returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise EasyScholarError("easyscholar returned a non-object payload")
        code = payload.get("code")
        if code != 0:
            message = _first_str(payload.get("msg")) or "unknown error"
            # API messages can echo the credential back; redact every
            # outward-facing message before it reaches logs or the CLI.
            raise EasyScholarError(
                _config.redact_text(
                    f"easyscholar error {code}: {message}", self.secret_key
                )
            )
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise EasyScholarError("easyscholar returned no matching journal")
        record = data[0]
        if not isinstance(record, dict):
            raise EasyScholarError("easyscholar returned a malformed record")
        return normalize(record, journal)


_PREF_RE = re.compile(r'user_pref\(\s*"([^"]+)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)')


def default_prefs_path() -> Path:
    """Zotero's default macOS data-directory preferences file."""
    return Path.home() / "Zotero" / "prefs.js"


def find_secret_in_prefs(prefs_path: Path) -> str | None:
    """Return the EasyScholar SecretKey from a Zotero ``prefs.js``.

    The value is only ever returned to the caller (who must not print
    it); preferences without an EasyScholar key yield ``None``.
    """
    text = prefs_path.read_text(encoding="utf-8", errors="replace")
    for name, value in _PREF_RE.findall(text):
        lowered = name.lower()
        if "easyscholar" in lowered and ("key" in lowered or "secret" in lowered):
            if value:
                return value
    return None
