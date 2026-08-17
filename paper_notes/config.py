"""Private configuration for paper-notes.

Secrets (the EasyScholar SecretKey and the MinerU Key) live outside the
vault in ``~/Library/Application Support/paper-notes/config.json`` with
mode ``0600``. JSON output and exception messages never carry either
secret value: :func:`mask_secret`, :func:`redacted_config`, and
:func:`redact_text` / :func:`redact_config_text` are the only rendering
surfaces for humans.

``PAPER_NOTES_CONFIG`` overrides the location for tests and for
non-macOS layouts; the default is the macOS Application Support path.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

MASK = "****"


class ConfigError(Exception):
    """The config file is missing, corrupt, or unreadable."""


@dataclass(frozen=True)
class Config:
    """Private settings; the secret value never appears in reprs."""

    easyscholar_secret_key: str | None = None
    mineru_key: str | None = None

    def __repr__(self) -> str:
        es_key = MASK if self.easyscholar_secret_key else None
        mineru_key = MASK if self.mineru_key else None
        return (
            f"Config(easyscholar_secret_key={es_key!r}, "
            f"mineru_key={mineru_key!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


def default_config_path() -> Path:
    """macOS private config location (or ``PAPER_NOTES_CONFIG`` override)."""
    override = os.environ.get("PAPER_NOTES_CONFIG")
    if override:
        return Path(override)
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "paper-notes"
        / "config.json"
    )


def load_config(path: Path | None = None) -> Config:
    """Read the private config; a missing file yields an empty config."""
    config_path = Path(path) if path is not None else default_config_path()
    try:
        if not config_path.exists():
            return Config()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {config_path} is not a JSON object")
    key = raw.get("easyscholar_secret_key")
    if not isinstance(key, str) or not key:
        key = None
    mineru_key = raw.get("mineru_key")
    if not isinstance(mineru_key, str) or not mineru_key:
        mineru_key = None
    return Config(easyscholar_secret_key=key, mineru_key=mineru_key)


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Atomically write the config with mode ``0600`` (parents created)."""
    config_path = Path(path) if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".json", dir=config_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, config_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def mask_secret(value: str) -> str:
    """Fixed mask with no length or content leakage."""
    return MASK


def redacted_config(cfg: Config, path: Path | None = None) -> dict:
    """Config as a dict with every secret replaced by the fixed mask."""
    config_path = Path(path) if path is not None else default_config_path()
    data = asdict(cfg)
    if data.get("easyscholar_secret_key"):
        data["easyscholar_secret_key"] = MASK
    if data.get("mineru_key"):
        data["mineru_key"] = MASK
    return {"config_path": str(config_path), **data}


def redact_text(text: str, secret: str | None) -> str:
    """Replace any occurrence of ``secret`` in ``text`` with the mask."""
    if not secret:
        return text
    return text.replace(secret, MASK)


def redact_config_text(text: str, cfg: Config) -> str:
    """Redact every configured secret (EasyScholar + MinerU) from ``text``."""
    text = redact_text(text, cfg.easyscholar_secret_key)
    return redact_text(text, cfg.mineru_key)
