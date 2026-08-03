"""Versioned JSON protocol for plugin-facing CLI operations.

Every operation invoked with ``--json`` emits exactly one :class:`Envelope`
on stdout; human diagnostics go to stderr. Exit codes:

- ``0`` success / needs_confirmation
- ``2`` user/config/validation error
- ``3`` conflict
- ``4`` internal/IO error (assigned at the CLI boundary for unexpected
  exceptions; returned envelopes map through :func:`exit_code_for`)

The ``Issue`` fields ``code`` and ``message`` are stable machine / human
pairs; ``path`` and ``field`` locate the problem when known. Never put
exception reprs (which may carry secrets) into messages here.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

EXIT_OK = 0
EXIT_USER_ERROR = 2
EXIT_CONFLICT = 3
EXIT_INTERNAL_ERROR = 4


class Issue(BaseModel):
    """Stable machine code plus a human message and optional location."""

    code: str
    message: str
    path: str | None = None
    field: str | None = None


class Envelope(BaseModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    status: Literal["success", "needs_confirmation", "conflict", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Issue] = Field(default_factory=list)
    errors: list[Issue] = Field(default_factory=list)


def success(
    data: dict[str, Any] | None = None,
    warnings: list[Issue] | None = None,
) -> Envelope:
    return Envelope(status="success", data=data or {}, warnings=warnings or [])


def needs_confirmation(
    data: dict[str, Any] | None = None,
    warnings: list[Issue] | None = None,
) -> Envelope:
    return Envelope(
        status="needs_confirmation", data=data or {}, warnings=warnings or []
    )


def conflict(errors: list[Issue]) -> Envelope:
    return Envelope(status="conflict", errors=errors or [])


def error(errors: list[Issue]) -> Envelope:
    return Envelope(status="error", errors=errors or [])


def exit_code_for(env: Envelope) -> int:
    """Base exit code for a returned envelope.

    Unexpected internal exceptions are mapped to ``EXIT_INTERNAL_ERROR``
    by the CLI boundary instead of passing through this function.
    """
    if env.status == "conflict":
        return EXIT_CONFLICT
    if env.status == "error":
        return EXIT_USER_ERROR
    return EXIT_OK
