# MinerU conversion is owned by the paper-notes core CLI

The Obsidian plugin may select a PDF, configure the MinerU Key, enqueue session-bound conversions, and display progress, but every MinerU API call and managed vault write is executed by the paper-notes core CLI. The CLI keeps the Key in its user-local `0600` private config, stages and validates Markdown and images, and publishes the canonical outputs; direct plugin API calls and Hermes-session dispatch were rejected because they would duplicate workflow logic, expose secrets to plugin or vault state, and create a second writer with a less predictable lifecycle.

## Implemented surface

- `paper-notes config mineru status|set-key --stdin|delete-key` — the Key is delivered on stdin (never argv/logs/JSON), stored at `~/Library/Application Support/paper-notes/config.json` (mode `0600`), and `status` reports only `configured: bool`.
- `paper-notes mineru convert --vault <root> --key <key>` — one paper per MinerU batch, staged commit under the vault lock, confirmation-token contract for re-converts (`--dry-run` then `--confirm-token`), NDJSON progress lines followed by the standard v1 envelope. Outputs are `minerUmd_<key>.md` plus extracted images under `<paper_dir>/attachments/`; nothing is written under `figures/` and no `Figure解读_<key>.md` is created. Old MD and images stay untouched until the new result commits completely; state changes during conversion surface as `conflict` (rc 3) with zero writes.
