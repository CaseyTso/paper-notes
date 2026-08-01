# Repository instructions

- State assumptions before acting; keep changes concise, focused, and verifiable.
- Treat `SKILL.md`, `references/`, `scripts/`, and `tests/` as product source.
- Add or update tests for behavior changes; run unit tests and `python3 -m py_compile scripts/*.py` before committing.
- Never commit `PROGRESS.md`, `BLOCKED.md`, API tokens, PDFs, ZIP archives, SQLite/Zotero databases, Obsidian vault content, or Python caches.
- Preserve current behavior unless the task explicitly changes it. Do not weaken, skip, or delete tests to get a green run.
- Main `README.md` is English; Chinese documentation belongs in `README.zh-CN.md`, linked from the English README.
