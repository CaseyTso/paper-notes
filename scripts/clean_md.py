#!/usr/bin/env python3
"""
Clean MinerU output markdown: migrate images into the vault attachment dir,
remove HTML blocks and residual artifacts.

Usage:
    python clean_md.py <input.md> [--in-place] [--output cleaned.md]
                         [--attachments-dir <dir>]

With --attachments-dir:
    Every local `![](images/<name>)` reference is migrated in place:
    the file is byte-copied to <attachments-dir>/<name> (an existing file
    with identical bytes is reused; a same-name file with different bytes
    aborts the run) and the reference becomes the Obsidian embed
    `![[<name>]]`, preserving the original position and order of every
    image. Missing source files or same-name/different-content conflicts
    fail the whole run with a non-zero exit and leave the markdown
    completely untouched (no partial rewrites). MinerU renders figures as
    plain relative markdown links — never base64.

Without --attachments-dir (legacy calls):
    `![](images/...)` embeds are removed exactly as before.

Other operations:
    1. Remove <details>...</details> blocks
    2. Remove <table>...</table> blocks
    3. Remove standalone single-letter panel labels (A-Z on own line)
    4. Remove "(legend continued on next page)" / "(legend on next page)"
    5. Convert ## Figure headings to **Figure ...**  (bold instead of H2)
    6. Convert plain-text Figure titles to bold  **Figure ...**
    7. Collapse 3+ blank lines to 2
    8. Strip trailing whitespace
"""

import re
import shutil
import sys
import os
from pathlib import Path


class ImageMigrationError(Exception):
    """Raised when an image reference cannot be migrated safely."""


def migrate_images(content, md_path, attachments_dir):
    """Migrate ![](images/...) references into attachments_dir.

    Validates every reference first (missing source file or same-name /
    different-content conflict -> ImageMigrationError, nothing is written),
    then copies all files, then rewrites the references to Obsidian
    embeds `![[<name>]]`. Position and document order are preserved.
    Returns the rewritten content.
    """
    ref_re = re.compile(r'!\[[^\]]*\]\(images/([^)\s]+)\)')
    refs = list(ref_re.finditer(content))
    if not refs:
        return content

    md_dir = md_path.parent if md_path is not None else Path('.')
    attachments_dir = Path(attachments_dir)

    # Phase 1: validate everything before touching anything (no writes,
    # not even creating the attachment dir, until validation passes).
    for m in refs:
        name = m.group(1)
        src = md_dir / 'images' / name
        if not src.is_file():
            raise ImageMigrationError(
                f"missing source image for {m.group(0)!r}: {src} "
                "(nothing migrated; markdown left untouched)")
        dst = attachments_dir / name
        if dst.exists() and dst.read_bytes() != src.read_bytes():
            raise ImageMigrationError(
                f"image conflict: {dst} already exists with different "
                f"content than {src} (same name, different bytes; nothing "
                "migrated; markdown left untouched)")

    # Phase 2: copy all files (validation passed; only now may we write).
    attachments_dir.mkdir(parents=True, exist_ok=True)
    for m in refs:
        name = m.group(1)
        dst = attachments_dir / name
        if not dst.exists():
            shutil.copyfile(md_dir / 'images' / name, dst)

    # Phase 3: rewrite references to Obsidian embeds.
    return ref_re.sub(lambda m: f'![[{m.group(1)}]]', content)


def clean_markdown(content, attachments_dir=None, md_path=None):
    """Apply all cleaning steps, return cleaned content."""

    if attachments_dir is not None:
        # Migrate MinerU relative image links into the shared attachment dir.
        content = migrate_images(content, md_path, attachments_dir)
    else:
        # Legacy mode: remove image embeds: ![](images/...) lines
        content = re.sub(r'!\[\]\(images/[^)]+\)\n*', '', content)

    # 1. Remove <details>...</details> blocks (multiline, non-greedy)
    content = re.sub(r'<details>.*?</details>\n*', '', content, flags=re.DOTALL)

    # 2. Remove <table>...</table> blocks
    content = re.sub(r'<table>.*?</table>', '', content, flags=re.DOTALL)

    # 3. Remove standalone hash-image references (leftover from details removal)
    content = re.sub(r'\n[a-f0-9]{64}\.jpg\n', '\n', content)

    # 4. Remove standalone single-letter panel labels (A-Z)
    content = re.sub(r'^\s*[A-I]\s*$', '', content, flags=re.MULTILINE)

    # 5. Remove "(legend continued on next page)" / "(legend on next page)"
    content = re.sub(r'\(legend continued on next page\)\n*', '', content)
    content = re.sub(r'\(legend on next page\)\n*', '', content)

    # 6. Convert ## Figure headings → bold
    content = re.sub(r'^## (Figure [^\n]+)', r'**\1**', content, flags=re.MULTILINE)

    # 7. Bold plain-text Figure titles at line start (not already bolded)
    content = re.sub(
        r'^(Figure S?\d+[^\n]+)$',
        r'**\1**',
        content,
        flags=re.MULTILINE
    )

    # 8. Remove trailing whitespace
    content = re.sub(r' +$', '', content, flags=re.MULTILINE)

    # 9. Collapse 3+ blank lines to 2
    content = re.sub(r'\n{4,}', '\n\n\n', content)

    return content.strip() + '\n'


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Clean MinerU output markdown")
    parser.add_argument("input", help="Input markdown file")
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite input file")
    parser.add_argument("--output", default=None,
                        help="Output file (default: in-place or stdout)")
    parser.add_argument("--attachments-dir", default=None,
                        help="Vault attachment dir (e.g. '<vault>/01 attachments'): "
                             "migrate images/ references into it as Obsidian embeds")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, 'r') as f:
        content = f.read()

    try:
        cleaned = clean_markdown(content,
                                 attachments_dir=args.attachments_dir,
                                 md_path=Path(args.input))
    except ImageMigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.in_place:
        with open(args.input, 'w') as f:
            f.write(cleaned)
        print(f"Cleaned in-place: {args.input} ({len(cleaned)} chars)", file=sys.stderr)
    elif args.output:
        with open(args.output, 'w') as f:
            f.write(cleaned)
        print(f"Cleaned: {args.input} → {args.output} ({len(cleaned)} chars)", file=sys.stderr)
    else:
        print(cleaned)


if __name__ == "__main__":
    main()
