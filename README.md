# paper-notes

Obsidian-native literature management: portable Markdown/YAML and files in the Obsidian vault are the durable literature assets. The `paper-notes` CLI is the only managed writer; a read-only Obsidian plugin (reader, index, UI) talks to it over a versioned JSON protocol. No Zotero runtime is required — Zotero stays an optional read-only source during migration, and migration removes active `zotero://` links from notes.

[中文说明](README.zh-CN.md)

## Canonical layout

```text
05 Literature/<citation_key>/
├── <citation_key>.md      ← main item (only authoritative bibliographic record)
├── <citation_key>.pdf     ← canonical primary PDF
├── minerUmd_<citation_key>.md
├── Figure解读_<citation_key>.md
├── attachments/           ← additional PDFs and supplementary files
├── cards/                 ← cards derived from this single paper
└── figures/               ← final high-resolution figure assets (<sha256>.png)
```

- The main item YAML is authoritative (`schema_version` / `paper_id` / `citation_key` / `pdf_status` / `reading_status`); derived notes keep only minimal relationship fields. EasyScholar / IF / JCI / JCR / CAS partition metrics are forbidden in Markdown — they are volatile UI-only data, never written to any note.
- The normal workflow never deletes the canonical primary PDF (`<citation_key>.pdf`).
- The legacy layout (title folders with `Figure_<paper_title>/` figure subdirectories) is superseded; migrate it with `migrate legacy-obsidian` (see `references/migration.md`).

## Workflows

- **Item management** — `python3 -m paper_notes.cli item create|show|update|attach-pdf|reconcile|rename-key|delete` with `--json` envelopes (`protocol_version`, `needs_confirmation`, exit codes 0/2/3/4). See `references/cli_protocol.md`.
- **MinerU conversion and Figure interpretation remain a Hermesian + paper-notes skill workflow** (the Obsidian plugin never starts them): `scripts/mineru_upload.py --citation-key <key>` finalizes `minerUmd_<citation_key>.md`; `scripts/clean_md.py` migrates temporary MinerU images into `<paper_dir>/attachments/`; `scripts/render_pdf_figure.py` renders ≥300-dpi full figures from the canonical primary PDF into `<paper_dir>/figures/` (content-hash filenames, both notes share the same embed). Figure interpretation quality rules (Overview, full-figure embeds, source Methods, no guessed panels) are detailed in `references/figure_interpretation.md`.
- **Migration** — `migrate legacy-obsidian --dry-run|--apply <run_id>`, `migrate verify <run_id>`, `migrate rollback <run_id>`; backups live outside the vault under `~/Library/Application Support/paper-notes/migrations/<run_id>/` and are never auto-deleted.

## Repository layout

- `SKILL.md` — agent workflow (Hermes skill)
- `references/` — detailed specifications
- `scripts/` — helper CLIs
- `paper_notes/` — core Python package
- `tests/` — regression tests

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py paper_notes/*.py paper_notes/adapters/*.py paper_notes/migration/*.py
```

Python 3.11+ (tested against 3.11 and 3.13 in CI); `requests>=2.31,<3` and `PyMuPDF>=1.24,<2` (see `requirements.txt`).

## Live-symlink warning

This local checkout may be the live Hermes skill through a symbolic link at
`~/.hermes/skills/research/paper-notes`. Uncommitted edits here can affect new
Hermes sessions immediately. Never commit API tokens, PDFs, vault content, or
Zotero databases.

## License

[MIT](LICENSE)
