# paper-notes

Hermes Agent skill for converting Zotero PDFs into Obsidian notes with MinerU and detailed figure interpretation.

[中文说明](README.zh-CN.md)

## Layout

- `SKILL.md` — agent workflow
- `references/` — detailed specifications
- `scripts/` — helper CLIs
- `tests/` — regression tests

## Prerequisites

- Python 3.11+ (tested against 3.11 and 3.13 in CI)
- `requests>=2.31,<3` and `PyMuPDF>=1.24,<2` (see `requirements.txt`)
- A running Zotero instance (for `get_citekey.py`), a MinerU API token (for `mineru_upload.py`), and an Obsidian vault (for `clean_md.py` and `render_pdf_figure.py`)
- `scripts/render_pdf_figure.py` renders full figures as lossless ≥300-dpi PNGs directly from the original Zotero PDF — the only source for final figures in notes (MinerU JPG fragments are used solely as locating hints and are removed during cleaning). Final PNGs are written to each paper's `<paper_dir>/Figure_<paper_title>/` and shared by both notes (`minerUmd` and `Figure解读`) as the same `![[<64hex>.png]]` embed.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

## Live-symlink warning

This local checkout may be the live Hermes skill through a symbolic link at
`~/.hermes/skills/research/paper-notes`. Uncommitted edits here can affect new
Hermes sessions immediately. Never commit API tokens, PDFs, vault content, or
Zotero databases.

## License

[MIT](LICENSE)
