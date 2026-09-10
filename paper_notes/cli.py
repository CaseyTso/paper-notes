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

from . import (
    __version__,
    attachments,
    cards,
    citations,
    config,
    csl,
    deletion,
    items,
    mineru,
    mocs,
)
from .identifiers import extract_identifiers, parse_arxiv, parse_doi, parse_pmcid, parse_pmid
from .web_capture import WebCaptureRequest
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
    needs_confirmation,
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
    create_parser.add_argument(
        "--web-capture",
        help="JSON file with a Browser Connector Web Capture (schema v1)",
    )
    create_parser.add_argument(
        "--confirm-token",
        help="opaque confirmation token from a Web Capture review plan",
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="produce metadata preview and confirmation token without writing",
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

    index_parser = subparsers.add_parser(
        "index",
        help="rebuild citation indexes or validate a manuscript",
        json_mode=json_mode,
    )
    index_parser.set_defaults(func=_cmd_index_root)
    index_subparsers = index_parser.add_subparsers(dest="index_command")

    rebuild_parser = index_subparsers.add_parser(
        "rebuild",
        help="deterministically rebuild library.json and citation-aliases.json",
        json_mode=json_mode,
    )
    rebuild_parser.add_argument("--vault", required=True, help="vault root directory")
    rebuild_parser.set_defaults(func=_cmd_index_rebuild)

    validate_parser = index_subparsers.add_parser(
        "validate-manuscript",
        help="validate manuscript citations through the Pandoc AST",
        json_mode=json_mode,
    )
    validate_parser.add_argument("--vault", required=True, help="vault root directory")
    validate_parser.add_argument(
        "--input", required=True, help="manuscript Markdown file"
    )
    validate_parser.set_defaults(func=_cmd_index_validate)

    metrics_parser = subparsers.add_parser(
        "metrics",
        help="query volatile journal metrics (EasyScholar; never writes to Markdown)",
        json_mode=json_mode,
    )
    metrics_parser.set_defaults(func=_cmd_metrics_root)
    metrics_subparsers = metrics_parser.add_subparsers(dest="metrics_command")
    query_parser = metrics_subparsers.add_parser(
        "query",
        help="query and normalize journal metrics",
        json_mode=json_mode,
    )
    query_parser.add_argument("--journal", help="journal name")
    query_parser.add_argument("--issn", help="journal ISSN")
    query_parser.set_defaults(func=_cmd_metrics_query)

    config_parser = subparsers.add_parser(
        "config",
        help="private configuration (secrets stay outside the vault)",
        json_mode=json_mode,
    )
    config_parser.set_defaults(func=_cmd_config_root)
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    es_parser = config_subparsers.add_parser(
        "easyscholar",
        help="EasyScholar private settings",
        json_mode=json_mode,
    )
    es_parser.set_defaults(func=_cmd_config_easyscholar_root)
    es_subparsers = es_parser.add_subparsers(dest="easyscholar_command")
    import_parser = es_subparsers.add_parser(
        "import-zotero",
        help="import the SecretKey from Zotero preferences (never printed)",
        json_mode=json_mode,
    )
    import_parser.add_argument(
        "--prefs", help="path to Zotero prefs.js (default: Zotero data dir)"
    )
    import_mode = import_parser.add_mutually_exclusive_group()
    import_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="detect the key and report, but do not write anything",
    )
    import_mode.add_argument(
        "--confirmed",
        action="store_true",
        help="write the imported key after explicit confirmation",
    )
    import_parser.set_defaults(func=_cmd_config_easyscholar_import)

    mineru_parser = config_subparsers.add_parser(
        "mineru",
        help="MinerU private settings",
        json_mode=json_mode,
    )
    mineru_parser.set_defaults(func=_cmd_config_mineru_root)
    mineru_subparsers = mineru_parser.add_subparsers(dest="mineru_command")
    mineru_status_parser = mineru_subparsers.add_parser(
        "status",
        help="report whether a MinerU key is configured (never the value)",
        json_mode=json_mode,
    )
    mineru_status_parser.set_defaults(func=_cmd_config_mineru_status)
    mineru_set_parser = mineru_subparsers.add_parser(
        "set-key",
        help="save the MinerU key read from stdin (never printed)",
        json_mode=json_mode,
    )
    mineru_set_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read exactly one line (the key) from stdin and store it",
    )
    mineru_set_parser.set_defaults(func=_cmd_config_mineru_set_key)
    mineru_delete_parser = mineru_subparsers.add_parser(
        "delete-key",
        help="remove the stored MinerU key (idempotent)",
        json_mode=json_mode,
    )
    mineru_delete_parser.set_defaults(func=_cmd_config_mineru_delete_key)

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="plan or apply legacy Obsidian migrations",
        json_mode=json_mode,
    )
    migrate_parser.set_defaults(func=_cmd_migrate_root)
    migrate_subparsers = migrate_parser.add_subparsers(dest="migrate_command")
    legacy_parser = migrate_subparsers.add_parser(
        "legacy-obsidian",
        help="discover legacy items and build a migration plan (read-only)",
        json_mode=json_mode,
    )
    legacy_parser.add_argument(
        "--vault",
        help="vault root directory (required for --dry-run; optional for "
        "--apply when the manifest records it)",
    )
    legacy_parser.add_argument(
        "--keys",
        help="comma-separated citation keys or directory names to migrate",
    )
    legacy_parser.add_argument(
        "--keys-file", help="file with one key or directory name per line"
    )
    legacy_parser.add_argument(
        "--state-root",
        help="state root for manifests/backups "
        "(default: ~/Library/Application Support/paper-notes/migrations)",
    )
    legacy_parser.add_argument(
        "--zotero-db",
        help="Zotero sqlite snapshot for identity/PDF candidates "
        "(default: ~/Zotero/zotero.sqlite when --zotero-data-dir is given)",
    )
    legacy_parser.add_argument(
        "--zotero-data-dir",
        help="Zotero data directory for storage PDF resolution "
        "(default: ~/Zotero)",
    )
    legacy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the plan and confirmation token without writing to the vault",
    )
    legacy_parser.add_argument(
        "--apply",
        help="apply a previously reviewed migration plan (run id)",
    )
    legacy_parser.add_argument(
        "--confirm-token",
        help="confirmation token from the matching dry run",
    )
    legacy_parser.set_defaults(func=_cmd_migrate_legacy_obsidian)

    verify_parser = migrate_subparsers.add_parser(
        "verify",
        help="verify an applied migration run against its backup",
        json_mode=json_mode,
    )
    verify_parser.add_argument("run_id", help="migration run id")
    verify_parser.add_argument(
        "--vault", help="vault root (default: recorded in the manifest)"
    )
    verify_parser.add_argument(
        "--state-root",
        help="state root for manifests/backups "
        "(default: ~/Library/Application Support/paper-notes/migrations)",
    )
    verify_parser.set_defaults(func=_cmd_migrate_verify)

    rollback_parser = migrate_subparsers.add_parser(
        "rollback",
        help="restore the original source trees of an applied migration run",
        json_mode=json_mode,
    )
    rollback_parser.add_argument("run_id", help="migration run id")
    rollback_parser.add_argument(
        "--vault", help="vault root (default: recorded in the manifest)"
    )
    rollback_parser.add_argument(
        "--state-root",
        help="state root for manifests/backups "
        "(default: ~/Library/Application Support/paper-notes/migrations)",
    )
    rollback_parser.set_defaults(func=_cmd_migrate_rollback)

    card_parser = subparsers.add_parser(
        "card",
        help="create derived card notes under a paper's cards/ directory",
        json_mode=json_mode,
    )
    card_parser.set_defaults(func=_cmd_card_root)
    card_subparsers = card_parser.add_subparsers(dest="card_command")

    card_create_parser = card_subparsers.add_parser(
        "create",
        help="create a derived card note (minimal relation frontmatter)",
        json_mode=json_mode,
    )
    card_create_parser.add_argument("--vault", required=True, help="vault root directory")
    card_create_parser.add_argument("--key", required=True, help="citation key or alias")
    card_create_parser.add_argument(
        "--title", required=True, help="conclusive one-line card title"
    )
    card_create_parser.add_argument(
        "--selection-file",
        required=True,
        help="file containing the verbatim selected Markdown content",
    )
    card_create_parser.add_argument(
        "--filename",
        help="explicit card filename (default: card_<slug>.md derived from title)",
    )
    card_create_parser.add_argument(
        "--anchor-name", help="block anchor name to insert into the source note"
    )
    card_create_parser.add_argument(
        "--source-note",
        help="source note name (e.g. Figure解读_<key>) for the anchor link back",
    )
    card_create_parser.add_argument(
        "--backlink",
        action="store_true",
        help="insert '> 卡片：[[<card>]]' after the anchor in the source note "
        "(explicit bidirectional link)",
    )
    card_create_parser.set_defaults(func=_cmd_card_create)

    moc_parser = subparsers.add_parser(
        "moc",
        help="create Topic MOC notes under 05 Literature/MOCs/",
        json_mode=json_mode,
    )
    moc_parser.set_defaults(func=_cmd_moc_root)
    moc_subparsers = moc_parser.add_subparsers(dest="moc_command")

    moc_create_parser = moc_subparsers.add_parser(
        "create",
        help="create a Topic MOC note (kind: topic-moc, empty four-column table)",
        json_mode=json_mode,
    )
    moc_create_parser.add_argument("--vault", required=True, help="vault root directory")
    moc_create_parser.add_argument(
        "--title", required=True, help="theme name (also the filename stem; CJK ok)"
    )
    moc_create_parser.set_defaults(func=_cmd_moc_create)

    mineru_parser = subparsers.add_parser(
        "mineru",
        help="convert a paper's Primary PDF through MinerU (core-owned writes)",
        json_mode=json_mode,
    )
    mineru_parser.set_defaults(func=_cmd_mineru_root)
    mineru_subparsers = mineru_parser.add_subparsers(dest="mineru_command")
    convert_parser = mineru_subparsers.add_parser(
        "convert",
        help="convert an item's Primary PDF (fresh or confirmed re-convert)",
        json_mode=json_mode,
    )
    convert_parser.add_argument("--vault", required=True, help="vault root directory")
    convert_parser.add_argument("--key", required=True, help="citation key or alias")
    convert_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview a re-convert and return a confirmation token without writing",
    )
    convert_parser.add_argument(
        "--confirm-token",
        help="confirmation token from a previous mineru convert --dry-run",
    )
    convert_parser.set_defaults(func=_cmd_mineru_convert)

    return parser


def _cmd_version(args: argparse.Namespace) -> Envelope:
    return success({"version": __version__})


def _cmd_item_root(args: argparse.Namespace) -> Envelope:
    raise UserError(
        "missing item subcommand: use create, show, update, attach-pdf, "
        "reconcile, rename-key, or delete"
    )


def _cmd_index_root(args: argparse.Namespace) -> Envelope:
    raise UserError(
        "missing index subcommand: use rebuild or validate-manuscript"
    )


def _cmd_index_rebuild(args: argparse.Namespace) -> Envelope:
    result = csl.rebuild_indexes(Path(args.vault))
    warnings = [
        Issue(code=record.code, message=record.message, path=str(record.path))
        for record in result.invalid
    ]
    return success(
        {
            "library": str(result.library_path),
            "aliases": str(result.aliases_path),
            "papers": result.papers,
            "aliases_count": result.aliases,
            "invalid_count": len(result.invalid),
        },
        warnings=warnings,
    )


def _cmd_index_validate(args: argparse.Namespace) -> Envelope:
    try:
        report = csl.validate_manuscript(Path(args.vault), Path(args.input))
    except (csl.PandocMissingError, csl.ManuscriptError) as exc:
        raise UserError(str(exc)) from exc
    if report.unknown:
        issues = [
            Issue(
                code="unknown_citation_key",
                message=(
                    f"{args.input}:{entry.line}:{entry.column}: "
                    f"unknown citation key {entry.key!r}"
                ),
                path=str(Path(args.input)),
            )
            for entry in report.unknown
        ]
        return error(issues)
    return success(
        {
            "input": str(Path(args.input)),
            "citations": report.citations,
            "unknown": [],
        }
    )


def _cmd_metrics_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing metrics subcommand: use query")


def _cmd_metrics_query(args: argparse.Namespace) -> Envelope:
    if not args.journal and not args.issn:
        raise UserError("metrics query requires --journal or --issn")
    cfg = config.load_config()
    if not cfg.easyscholar_secret_key:
        raise UserError(
            "no EasyScholar secret key configured; run "
            "'paper-notes config easyscholar import-zotero --dry-run' first"
        )
    from .adapters import easyscholar

    adapter = easyscholar.EasyScholarAdapter(cfg.easyscholar_secret_key)
    try:
        result = adapter.query(journal=args.journal, issn=args.issn)
    except easyscholar.EasyScholarError as exc:
        raise UserError(
            config.redact_text(str(exc), cfg.easyscholar_secret_key)
        ) from exc
    return success({"metrics": result})


def _cmd_config_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing config subcommand: use easyscholar or mineru")


def _cmd_config_easyscholar_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing easyscholar subcommand: use import-zotero")


def _cmd_config_mineru_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing mineru subcommand: use status, set-key, or delete-key")


def _cmd_config_mineru_status(args: argparse.Namespace) -> Envelope:
    cfg = config.load_config()
    return success(
        {
            "configured": bool(cfg.mineru_key),
            "config_path": str(config.default_config_path()),
        }
    )


def _cmd_config_mineru_set_key(args: argparse.Namespace) -> Envelope:
    if not args.stdin:
        raise UserError(
            "config mineru set-key requires --stdin; the key is read from "
            "stdin so it never appears in argv or logs"
        )
    raw = sys.stdin.read()
    key = raw.strip()
    if not key:
        raise UserError("no MinerU key received on stdin")
    cfg = config.load_config()
    config.save_config(
        config.Config(easyscholar_secret_key=cfg.easyscholar_secret_key, mineru_key=key)
    )
    return success(
        {
            "configured": True,
            "config_path": str(config.default_config_path()),
        }
    )


def _cmd_config_mineru_delete_key(args: argparse.Namespace) -> Envelope:
    cfg = config.load_config()
    if not cfg.mineru_key:
        # idempotent: nothing configured, nothing to remove
        return success(
            {
                "configured": False,
                "config_path": str(config.default_config_path()),
            }
        )
    config.save_config(
        config.Config(easyscholar_secret_key=cfg.easyscholar_secret_key, mineru_key=None)
    )
    return success(
        {
            "configured": False,
            "config_path": str(config.default_config_path()),
        }
    )


def _cmd_migrate_root(args: argparse.Namespace) -> Envelope:
    raise UserError(
        "missing migrate subcommand: use legacy-obsidian, verify, or rollback"
    )


def _cmd_migrate_legacy_obsidian(args: argparse.Namespace) -> Envelope:
    from .migration import build_migration_plan, default_state_root
    from .migration.transaction import (
        MigrationConflict,
        MigrationError,
        apply_migration,
        record_vault_root,
    )

    state_root = Path(args.state_root) if args.state_root else default_state_root()

    if args.apply:
        if args.dry_run:
            raise UserError("cannot combine --apply with --dry-run")
        if not args.confirm_token:
            raise UserError(
                "migrate legacy-obsidian --apply requires --confirm-token"
            )
        if args.keys or args.keys_file:
            raise UserError("cannot combine --apply with --keys/--keys-file")
        vault = Path(args.vault) if args.vault else None
        try:
            result = apply_migration(
                args.apply,
                args.confirm_token,
                vault_root=vault,
                state_root=state_root,
            )
        except MigrationError as exc:
            raise UserError(str(exc)) from exc
        except MigrationConflict as exc:
            raise ConflictError(str(exc)) from exc
        return success(
            {
                "action": result.status,
                "run_id": result.run_id,
                "vault_root": result.vault_root,
                "state_root": result.state_root,
                "migrated": list(result.migrated),
                "skipped": list(result.skipped),
            }
        )

    if args.confirm_token:
        raise UserError("--confirm-token requires --apply")
    if not args.dry_run:
        raise UserError(
            "migrate legacy-obsidian requires --dry-run or --apply"
        )
    if not args.vault:
        raise UserError("migrate legacy-obsidian --dry-run requires --vault")
    if args.keys and args.keys_file:
        raise UserError("cannot combine --keys with --keys-file")
    keys: list[str] | None = None
    if args.keys:
        keys = [part.strip() for part in args.keys.split(",") if part.strip()]
    elif args.keys_file:
        try:
            keys = [
                line.strip()
                for line in Path(args.keys_file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            raise UserError(f"cannot read keys file {args.keys_file}: {exc}") from exc
    vault = Path(args.vault)
    if args.zotero_db or args.zotero_data_dir:
        # R1: consume a Zotero snapshot (read-only copy made by the
        # adapter) for generated-main-note identity and storage PDFs.
        from .adapters.zotero import ZoteroAdapter

        db_path = args.zotero_db or (Path.home() / "Zotero" / "zotero.sqlite")
        data_dir = args.zotero_data_dir or (Path.home() / "Zotero")
        try:
            with ZoteroAdapter(db_path=db_path, data_dir=data_dir) as ad:
                plan = build_migration_plan(
                    vault, keys=keys, state_root=state_root, zotero=ad
                )
        except OSError as exc:
            raise UserError(f"cannot read Zotero snapshot: {exc}") from exc
    else:
        plan = build_migration_plan(vault, keys=keys, state_root=state_root)
    # Record the vault root so apply/verify/rollback can locate it later
    # without repeating --vault (additive; the token is unaffected).
    record_vault_root(plan.manifest_path, vault)
    return needs_confirmation(
        {
            "action": "migrate_legacy_obsidian",
            "run_id": plan.run_id,
            "confirmation_token": plan.confirmation_token,
            "vault_root": str(vault.resolve()),
            "state_root": str(plan.state_root),
            "manifest_path": str(plan.manifest_path),
            "items": list(plan.items),
            "diagnostics": plan.diagnostics,
            "writes": {
                "vault_writes": 0,
                "state_root": str(plan.state_root),
            },
        }
    )


def _cmd_migrate_verify(args: argparse.Namespace) -> Envelope:
    from .migration import default_state_root
    from .migration.transaction import (
        MigrationError,
        verify_migration,
    )

    state_root = Path(args.state_root) if args.state_root else default_state_root()
    vault = Path(args.vault) if args.vault else None
    try:
        report = verify_migration(
            args.run_id, vault_root=vault, state_root=state_root
        )
    except MigrationError as exc:
        raise UserError(str(exc)) from exc
    items = [
        {
            "source_dir": entry["source_dir"],
            "citation_key": entry["citation_key"],
            "status": entry["status"],
            "problems": entry["problems"],
        }
        for entry in report.items
    ]
    base = {
        "run_id": report.run_id,
        "status": report.status,
        "applied": report.applied,
        "pending": report.pending,
        "skipped": report.skipped,
        "problems": report.problems,
        "items": items,
    }
    if report.status == "problems":
        return error(
            [
                Issue(
                    code="verify_problem",
                    message=f"{entry['source_dir']}: {problem['message']}",
                    path=problem.get("path"),
                )
                for entry in report.items
                for problem in entry["problems"]
            ]
        )
    return success(base)


def _cmd_migrate_rollback(args: argparse.Namespace) -> Envelope:
    from .migration import default_state_root
    from .migration.transaction import (
        MigrationConflict,
        MigrationError,
        rollback_migration,
    )

    state_root = Path(args.state_root) if args.state_root else default_state_root()
    vault = Path(args.vault) if args.vault else None
    try:
        result = rollback_migration(
            args.run_id, vault_root=vault, state_root=state_root
        )
    except MigrationError as exc:
        raise UserError(str(exc)) from exc
    except MigrationConflict as exc:
        raise ConflictError(str(exc)) from exc
    return success(
        {
            "action": result.status,
            "run_id": result.run_id,
            "restored": list(result.restored),
        }
    )


def _cmd_card_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing card subcommand: use create")


def _cmd_card_create(args: argparse.Namespace) -> Envelope:
    selection_path = Path(args.selection_file)
    try:
        selection = selection_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"cannot read selection file {selection_path}: {exc}") from exc
    result = _run_card(
        lambda: cards.create_card(
            Path(args.vault),
            key=args.key,
            title=args.title,
            selection=selection,
            filename=args.filename,
            anchor_name=args.anchor_name,
            source_note=args.source_note,
            backlink=args.backlink,
        )
    )
    data = {
        "citation_key": result.citation_key,
        "paper_id": result.paper_id,
        "path": str(result.path),
        "anchor_name": result.anchor_name,
        "anchor_inserted": result.anchor_inserted,
        "anchor_link": result.anchor_link,
        "backlink_inserted": result.backlink_inserted,
    }
    warnings = [Issue(code="card_warning", message=w) for w in result.warnings]
    return success(data, warnings=warnings)


def _run_card(op: Any) -> Any:
    """Translate core card errors onto the CLI's error hierarchy."""
    try:
        return op()
    except cards.CardError as exc:
        raise UserError(str(exc)) from exc
    except cards.CardConflict as exc:
        raise ConflictError(str(exc)) from exc


def _cmd_moc_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing moc subcommand: use create")


def _cmd_moc_create(args: argparse.Namespace) -> Envelope:
    vault = Path(args.vault)
    result = _run_moc(
        lambda: mocs.create_moc(vault, title=args.title)
    )
    rel_path = result.path.relative_to(vault) if result.path.is_absolute() else result.path
    data = {
        "title": result.title,
        "path": str(rel_path),
        "kind": "topic-moc",
    }
    return success(data)


def _run_moc(op: Any) -> Any:
    """Translate core MOC errors onto the CLI's error hierarchy."""
    try:
        return op()
    except mocs.MocError as exc:
        raise UserError(str(exc)) from exc
    except mocs.MocConflict as exc:
        raise ConflictError(str(exc)) from exc


def _cmd_mineru_root(args: argparse.Namespace) -> Envelope:
    raise UserError("missing mineru subcommand: use convert")


def _cmd_mineru_convert(args: argparse.Namespace) -> Envelope:
    from .protocol import needs_confirmation

    if args.dry_run and args.confirm_token:
        raise UserError("cannot combine --dry-run with --confirm-token")

    def progress(event: dict) -> None:
        # NDJSON progress lines (plugin stream contract); human mode is
        # silent so stdout stays a single envelope.
        if args.json:
            print(json.dumps({"type": "progress", **event}, ensure_ascii=False))

    try:
        if args.dry_run:
            preview = mineru.preview_convert(Path(args.vault), key=args.key)
            return needs_confirmation(
                {
                    "action": "mineru_convert",
                    "citation_key": preview.citation_key,
                    "paper_id": preview.paper_id,
                    "existing_md": preview.existing_md,
                    "pdf_sha256": preview.pdf_sha256,
                    "old_md_sha256": preview.old_md_sha256,
                    "confirmation_token": preview.confirmation_token,
                    "plan": preview.plan,
                }
            )
        result = mineru.run_convert(
            Path(args.vault),
            key=args.key,
            confirm_token=args.confirm_token,
            progress=progress,
        )
    except mineru.MineruError as exc:
        raise UserError(config.redact_config_text(str(exc), config.load_config())) from exc
    except mineru.MineruConflict as exc:
        raise ConflictError(config.redact_config_text(str(exc), config.load_config())) from exc
    return success(
        {
            "action": result.action,
            "citation_key": result.citation_key,
            "paper_id": result.paper_id,
            "path": result.path,
            "images": result.images,
            "total_pages": result.total_pages,
        }
    )


def _cmd_config_easyscholar_import(args: argparse.Namespace) -> Envelope:
    from .adapters import easyscholar

    prefs_path = (
        Path(args.prefs) if args.prefs else easyscholar.default_prefs_path()
    )
    try:
        secret = easyscholar.find_secret_in_prefs(prefs_path)
    except OSError as exc:
        raise UserError(
            f"cannot read Zotero preferences {prefs_path}: {exc}"
        ) from exc
    target = config.default_config_path()
    data = {
        "found": secret is not None,
        "config_path": str(target),
        "written": False,
    }
    if args.dry_run:
        return success(data)
    if not args.confirmed:
        return needs_confirmation(
            {
                **data,
                "reason": "run with --dry-run to preview, --confirmed to write",
            }
        )
    if secret is None:
        raise UserError(
            "no EasyScholar secret key found in Zotero preferences"
        )
    config.save_config(
        config.Config(easyscholar_secret_key=secret), path=target
    )
    return success({"found": True, "config_path": str(target), "written": True})


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
    from pydantic import ValidationError

    confirmed = (
        _load_json_file(args.confirmed, "confirmed metadata")
        if args.confirmed
        else None
    )

    if getattr(args, "dry_run", False) and args.web_capture:
        raise UserError("--dry-run cannot be combined with --web-capture")

    if args.web_capture:
        if (
            args.doi
            or args.pmid
            or args.pmcid
            or args.arxiv
            or args.url
            or args.pdf
        ):
            raise UserError(
                "--web-capture cannot be combined with legacy create sources"
            )
        data = _load_json_file(args.web_capture, "web capture")
        try:
            capture = WebCaptureRequest.model_validate(data)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
                for err in exc.errors()
            )
            raise UserError(f"invalid web capture: {details}") from exc
        result = _run_item(
            lambda: items.create_item_from_web_capture(
                Path(args.vault),
                capture=capture,
                confirmed=confirmed,
                confirm_token=args.confirm_token,
            )
        )
    else:
        if getattr(args, "dry_run", False):
            if args.confirm_token:
                raise UserError("--dry-run cannot be combined with --confirm-token")
            identifiers = _parse_identifier_args(args)
            pdf = Path(args.pdf) if args.pdf else None
            result = _run_item(
                lambda: items.preview_create(
                    Path(args.vault),
                    identifiers=identifiers,
                    pdf=pdf,
                    confirmed=confirmed,
                )
            )
        else:
            identifiers = _parse_identifier_args(args)
            pdf = Path(args.pdf) if args.pdf else None
            result = _run_item(
                lambda: items.create_item(
                    Path(args.vault),
                    identifiers=identifiers,
                    pdf=pdf,
                    confirmed=confirmed,
                    confirm_token=args.confirm_token,
                )
            )
    base = {
        "citation_key": result.citation_key,
        "paper_id": result.paper_id,
        "path": result.path,
        "pdf_sha256": result.pdf_sha256,
    }
    if result.status == "created":
        data = {"action": "created", **base}
        if result.candidates:
            data["candidates"] = result.candidates
        return success(data)
    if result.status == "attached":
        return success({"action": result.action, **base})
    data = {
        "confirmation_token": result.confirmation_token,
        "plan": result.plan,
        "candidates": result.candidates,
    }
    if result.citation_key is not None:
        data["citation_key"] = result.citation_key
    if result.action is not None:
        data["action"] = result.action
    if result.paper_id is not None:
        data["paper_id"] = result.paper_id
    if result.path is not None:
        data["path"] = result.path
    if result.pdf_sha256 is not None:
        data["pdf_sha256"] = result.pdf_sha256
    return needs_confirmation(data)


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
