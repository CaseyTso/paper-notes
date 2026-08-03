"""Command-line entry point for the paper-notes core.

The CLI uses stdlib ``argparse`` for dispatch. Every plugin-facing
operation invoked with ``--json`` emits exactly one JSON envelope on
stdout; human diagnostics go to stderr. Tracebacks are never printed in
JSON mode, so exception reprs cannot leak into machine output.
"""

import argparse
import json
import sys
import traceback

from pathlib import Path
from typing import Any, NoReturn

from . import __version__, attachments, citations, deletion, items
from .identifiers import extract_identifiers, parse_arxiv, parse_doi, parse_pmcid, parse_pmid
from .protocol import (
    EXIT_CONFLICT,
    EXIT_INTERNAL_ERROR,
    EXIT_USER_ERROR,
    PROTOCOL_VERSION,
    Envelope,
    Issue,
    conflict,
    error,
    exit_code_for,
    success,
)


class UserError(Exception):
    """User/config/validation error; maps to exit code 2."""


class ConflictError(Exception):
    """Conflicting state; maps to exit code 3."""


class _JsonAwareArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that still emits a JSON envelope on usage errors."""

    def __init__(self, *args: Any, json_mode: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._json_mode = json_mode

    def error(self, message: str) -> NoReturn:
        if self._json_mode:
            env = error([Issue(code="usage_error", message=message)])
            print(env.model_dump_json(), file=sys.stdout)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USER_ERROR, f"{self.prog}: error: {message}\n")


def build_parser(json_mode: bool) -> _JsonAwareArgumentParser:
    parser = _JsonAwareArgumentParser(
        prog="paper-notes",
        description="Obsidian-native literature management core.",
        json_mode=json_mode,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single JSON envelope on stdout",
    )
    subparsers = parser.add_subparsers(dest="command")
    version_parser = subparsers.add_parser(
        "version", help="print the CLI protocol version", json_mode=json_mode
    )
    version_parser.set_defaults(func=_cmd_version)

    item_parser = subparsers.add_parser(
        "item",
        help="create, show, update, or attach to canonical literature items",
        json_mode=json_mode,
    )
    item_parser.set_defaults(func=_cmd_item_root)
    item_subparsers = item_parser.add_subparsers(dest="item_command")

    create_parser = item_subparsers.add_parser(
        "create",
        help="create a canonical item (metadata, identifier, or PDF backed)",
        json_mode=json_mode,
    )
    create_parser.add_argument("--vault", required=True, help="vault root directory")
    create_parser.add_argument("--doi", action="append", help="DOI (repeatable)")
    create_parser.add_argument("--pmid", action="append", help="PMID (repeatable)")
    create_parser.add_argument("--pmcid", action="append", help="PMCID (repeatable)")
    create_parser.add_argument("--arxiv", action="append", help="arXiv id (repeatable)")
    create_parser.add_argument("--url", action="append", help="web URL (repeatable)")
    create_parser.add_argument("--pdf", help="local PDF path to copy and hash")
    create_parser.add_argument(
        "--confirmed",
        help="JSON file with user-confirmed metadata values",
    )
    create_parser.set_defaults(func=_cmd_item_create)

    show_parser = item_subparsers.add_parser(
        "show",
        help="show an item by citation key or alias",
        json_mode=json_mode,
    )
    show_parser.add_argument("--vault", required=True, help="vault root directory")
    show_parser.add_argument("--key", required=True, help="citation key or alias")
    show_parser.set_defaults(func=_cmd_item_show)

    update_parser = item_subparsers.add_parser(
        "update",
        help="update an item's metadata fields",
        json_mode=json_mode,
    )
    update_parser.add_argument("--vault", required=True, help="vault root directory")
    update_parser.add_argument("--key", required=True, help="citation key or alias")
    update_parser.add_argument(
        "--patch", required=True, help="JSON file with fields to set (null removes)"
    )
    update_parser.set_defaults(func=_cmd_item_update)

    attach_parser = item_subparsers.add_parser(
        "attach-pdf",
        help="copy a PDF (primary) or any regular file (supplementary) into an item",
        json_mode=json_mode,
    )
    attach_parser.add_argument("--vault", required=True, help="vault root directory")
    attach_parser.add_argument("--key", required=True, help="citation key or alias")
    attach_parser.add_argument("--file", required=True, help="local file to copy into the vault")
    attach_parser.add_argument(
        "--supplementary",
        action="store_true",
        help="attach as a supplementary file under attachments/",
    )
    attach_parser.add_argument(
        "--confirm-token",
        help="confirmation token from a previous replace/metadata preview",
    )
    attach_parser.set_defaults(func=_cmd_item_attach_pdf)

    reconcile_parser = item_subparsers.add_parser(
        "reconcile",
        help="preview/confirm pdf_status metadata against the actual primary PDF",
        json_mode=json_mode,
    )
    reconcile_parser.add_argument("--vault", required=True, help="vault root directory")
    reconcile_parser.add_argument("--key", required=True, help="citation key or alias")
    reconcile_parser.add_argument(
        "--confirm-token", help="confirmation token from a previous reconcile preview"
    )
    reconcile_parser.set_defaults(func=_cmd_item_reconcile)

    rename_parser = item_subparsers.add_parser(
        "rename-key",
        help="preview/confirm a global citation-key rename (parser-aware, transactional)",
        json_mode=json_mode,
    )
    rename_parser.add_argument("--vault", required=True, help="vault root directory")
    rename_parser.add_argument("--key", required=True, help="citation key or alias to rename")
    rename_parser.add_argument("--new-key", required=True, help="the new citation key")
    rename_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="produce the impact plan and confirmation token without writing",
    )
    rename_parser.add_argument(
        "--confirm-token",
        help="confirmation token from a previous rename-key dry-run",
    )
    rename_parser.set_defaults(func=_cmd_item_rename_key)

    delete_parser = item_subparsers.add_parser(
        "delete",
        help="preview/confirm permanent deletion of a canonical item (transactional)",
        json_mode=json_mode,
    )
    delete_parser.add_argument("--vault", required=True, help="vault root directory")
    delete_parser.add_argument(
        "--key", required=True, help="citation key or alias to delete"
    )
    delete_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="produce the impact plan and confirmation token without writing",
    )
    delete_parser.add_argument(
        "--confirm-key",
        help="exact canonical citation key confirming the deletion",
    )
    delete_parser.add_argument(
        "--confirm-token",
        help="confirmation token from a previous item delete dry-run",
    )
    delete_parser.set_defaults(func=_cmd_item_delete)
    return parser


def _cmd_version(args: argparse.Namespace) -> Envelope:
    return success({"version": __version__})


def _cmd_item_root(args: argparse.Namespace) -> Envelope:
    raise UserError(
        "missing item subcommand: use create, show, update, attach-pdf, "
        "reconcile, rename-key, or delete"
    )


def _load_json_file(path_text: str, what: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UserError(f"cannot read {what} file {path}: {exc}") from exc
    except ValueError as exc:
        raise UserError(f"{what} file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise UserError(f"{what} file {path} must contain a JSON object")
    return data


def _parse_identifier_args(args: argparse.Namespace) -> list[Any]:
    """Collect parsed strong identifiers from the create flags."""
    identifiers = []
    for value in args.doi or []:
        parsed = parse_doi(value)
        if parsed is None:
            raise UserError(f"unrecognized DOI: {value!r}")
        identifiers.append(parsed)
    for value in args.pmid or []:
        parsed = parse_pmid(value)
        if parsed is None:
            raise UserError(f"unrecognized PMID: {value!r}")
        identifiers.append(parsed)
    for value in args.pmcid or []:
        parsed = parse_pmcid(value)
        if parsed is None:
            raise UserError(f"unrecognized PMCID: {value!r}")
        identifiers.append(parsed)
    for value in args.arxiv or []:
        parsed = parse_arxiv(value)
        if parsed is None:
            raise UserError(f"unrecognized arXiv id: {value!r}")
        identifiers.append(parsed)
    for url in args.url or []:
        found = extract_identifiers(url)
        if not found:
            raise UserError(f"no identifiers found in URL: {url!r}")
        identifiers.extend(found)
    return identifiers


def _run_item(op: Any) -> Any:
    """Translate core item errors onto the CLI's error hierarchy."""
    try:
        return op()
    except items.ItemError as exc:
        raise UserError(str(exc)) from exc
    except items.ItemConflict as exc:
        raise ConflictError(str(exc)) from exc


def _cmd_item_create(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    identifiers = _parse_identifier_args(args)
    confirmed = (
        _load_json_file(args.confirmed, "confirmed metadata")
        if args.confirmed
        else None
    )
    pdf = Path(args.pdf) if args.pdf else None
    result = _run_item(
        lambda: items.create_item(
            Path(args.vault),
            identifiers=identifiers,
            pdf=pdf,
            confirmed=confirmed,
        )
    )
    base = {
        "citation_key": result.citation_key,
        "paper_id": result.paper_id,
        "path": result.path,
        "pdf_sha256": result.pdf_sha256,
    }
    if result.status == "created":
        return success({"action": "created", **base})
    if result.status == "attached":
        return success({"action": result.action, **base})
    return needs_confirmation(
        {
            "confirmation_token": result.confirmation_token,
            "plan": result.plan,
            "candidates": result.candidates,
        }
    )


def _cmd_item_show(args: argparse.Namespace) -> Envelope:
    result = _run_item(lambda: items.show_item(Path(args.vault), key=args.key))
    return success(
        {
            "citation_key": result.citation_key,
            "requested_key": result.requested_key,
            "resolved_as": result.resolved_as,
            "path": str(result.path),
            "frontmatter": result.frontmatter,
        }
    )


def _cmd_item_update(args: argparse.Namespace) -> Envelope:
    patch = _load_json_file(args.patch, "patch")
    result = _run_item(
        lambda: items.update_item(Path(args.vault), key=args.key, patch=patch)
    )
    return success(
        {
            "action": "updated",
            "citation_key": result.citation_key,
            "path": result.path,
            "updated_fields": result.updated_fields,
            "frontmatter": result.frontmatter,
        }
    )


def _cmd_item_attach_pdf(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    result = _run_item(
        lambda: attachments.attach_pdf(
            Path(args.vault),
            key=args.key,
            file=args.file,
            supplementary=args.supplementary,
            confirm_token=args.confirm_token,
        )
    )
    base = {
        "citation_key": result.citation_key,
        "paper_id": result.paper_id,
        "path": result.path,
        "target": result.target,
    }
    if args.supplementary:
        base["sha256"] = result.sha256
    else:
        base["pdf_sha256"] = result.sha256
    if result.status == "needs_confirmation":
        return needs_confirmation(
            {
                "action": result.action,
                "confirmation_token": result.confirmation_token,
                "plan": result.plan,
                **base,
            }
        )
    return success({"action": result.action, **base})


def _cmd_item_reconcile(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    result = _run_item(
        lambda: attachments.reconcile(
            Path(args.vault),
            key=args.key,
            confirm_token=args.confirm_token,
        )
    )
    base = {
        "citation_key": result.citation_key,
        "paper_id": result.paper_id,
        "path": result.path,
        "before": result.before,
        "after": result.after,
    }
    if result.status == "needs_confirmation":
        return needs_confirmation(
            {
                "action": result.action,
                "confirmation_token": result.confirmation_token,
                "plan": result.plan,
                **base,
            }
        )
    return success({"action": result.action, **base})


def _cmd_item_rename_key(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    if args.dry_run and args.confirm_token:
        raise UserError("cannot combine --dry-run with --confirm-token")
    if args.confirm_token:
        result = _run_item(
            lambda: citations.confirm_rename_key(
                Path(args.vault),
                key=args.key,
                new_key=args.new_key,
                confirm_token=args.confirm_token,
            )
        )
        return success(
            {
                "action": "renamed",
                "citation_key": result.citation_key,
                "paper_id": result.paper_id,
                "path": result.path,
                "old_key": result.old_key,
                "moves": [
                    {"source": str(m.source), "target": str(m.target), "kind": m.kind}
                    for m in result.moves
                ],
                "edits": [{"path": str(e.path), "kind": e.kind} for e in result.edits],
                "occurrences": [
                    {
                        "path": str(o.path),
                        "kind": o.kind,
                        "line": o.line,
                        "column": o.column,
                    }
                    for o in result.occurrences
                ],
                "warnings": list(result.warnings),
            }
        )
    result = _run_item(
        lambda: citations.preview_rename_key(
            Path(args.vault), key=args.key, new_key=args.new_key
        )
    )
    return needs_confirmation(
        {
            "action": "rename_key",
            "confirmation_token": result.confirmation_token,
            "plan": result.plan,
        }
    )


def _cmd_item_delete(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    if args.dry_run:
        if args.confirm_key or args.confirm_token:
            raise UserError("cannot combine --dry-run with --confirm-key/--confirm-token")
        result = _run_item(
            lambda: deletion.preview_delete(Path(args.vault), key=args.key)
        )
        return needs_confirmation(
            {
                "action": "delete",
                "citation_key": result.citation_key,
                "paper_id": result.paper_id,
                "requested_key": result.requested_key,
                "resolved_as": result.resolved_as,
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
                "occurrences": [
                    {
                        "path": str(o.path),
                        "kind": o.kind,
                        "line": o.line,
                        "column": o.column,
                    }
                    for o in result.occurrences
                ],
                "warnings": list(result.warnings),
                "confirmation_token": result.confirmation_token,
                "plan": result.plan,
            }
        )
    if not args.confirm_key or not args.confirm_token:
        raise UserError(
            "item delete requires --confirm-key and --confirm-token "
            "(or --dry-run for a read-only preview)"
        )
    result = _run_item(
        lambda: deletion.confirm_delete(
            Path(args.vault),
            key=args.key,
            confirm_key=args.confirm_key,
            confirm_token=args.confirm_token,
        )
    )
    return success(
        {
            "action": "deleted",
            "citation_key": result.citation_key,
            "paper_id": result.paper_id,
            "path": str(result.path),
            "file_count": result.file_count,
            "total_bytes": result.total_bytes,
            "occurrences": [
                {
                    "path": str(o.path),
                    "kind": o.kind,
                    "line": o.line,
                    "column": o.column,
                }
                for o in result.occurrences
            ],
            "warnings": list(result.warnings),
        }
    )


def run_command(args: argparse.Namespace, json_mode: bool) -> Envelope:
    """Dispatch to the selected subcommand and return its envelope.

    ``json_mode`` is passed through so future commands know they must not
    prompt or print human text (plugin-facing contract).
    """
    return args.func(args)


def _render_human(env: Envelope, command: str | None) -> None:
    if env.status == "success" and command == "version":
        print(f"paper-notes {PROTOCOL_VERSION}")
        return
    if env.status in ("success", "needs_confirmation"):
        print(json.dumps(env.data, ensure_ascii=False, indent=2))
        return
    for issue in env.errors + env.warnings:
        print(f"{issue.code}: {issue.message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in argv
    parser = build_parser(json_mode)

    args = parser.parse_args(argv)
    if args.command is None:
        if json_mode:
            parser.print_help(file=sys.stderr)
            env = error([Issue(code="missing_command", message="no subcommand given")])
            print(env.model_dump_json(), file=sys.stdout)
            return EXIT_USER_ERROR
        parser.print_help()
        return EXIT_USER_ERROR

    try:
        env = run_command(args, json_mode)
    except UserError as exc:
        env = error([Issue(code="user_error", message=str(exc))])
        rc = EXIT_USER_ERROR
    except ConflictError as exc:
        env = conflict([Issue(code="conflict", message=str(exc))])
        rc = EXIT_CONFLICT
    except Exception:
        if not json_mode:
            traceback.print_exc(file=sys.stderr)
        env = error([Issue(code="internal_error", message="Internal error")])
        rc = EXIT_INTERNAL_ERROR
    else:
        rc = exit_code_for(env)

    if json_mode:
        print(env.model_dump_json(), file=sys.stdout)
    else:
        _render_human(env, args.command)
    return rc


if __name__ == "__main__":
    sys.exit(main())
