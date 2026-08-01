#!/usr/bin/env python3
"""
Get Zotero citation key (Better BibTeX) from item key.

Usage:
    # Single key
    python get_citekey.py <item_key> [--db-path ~/Zotero/zotero.sqlite]

    # Batch mode: read keys from stdin, query all in one DB session
    echo "KEY1\\nKEY2\\nKEY3" | python get_citekey.py --batch [--db-path ...]

Returns citation key as plain text on stdout, or exits with error.
If the item has no citation key, falls back to generating one from metadata.
"""

import sqlite3
import os
import sys
import shutil
import tempfile
import time


def _copy_db(db_path):
    """Copy Zotero DB to temp to avoid lock conflicts.
    Uses timestamp to avoid collision with other instances."""
    suffix = f"zotero_ck_{int(time.time() * 1000)}.sqlite"
    tmp = os.path.join(tempfile.gettempdir(), suffix)
    shutil.copy2(db_path, tmp)
    return tmp


def get_citekey(item_key, db_path=None):
    """Query Zotero SQLite for the citation key of a single item."""
    if db_path is None:
        db_path = os.path.expanduser("~/Zotero/zotero.sqlite")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Zotero database not found: {db_path}")

    tmp = _copy_db(db_path)
    try:
        return _query_single(tmp, item_key)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_citekeys_batch(item_keys, db_path=None):
    """Query multiple item keys in a single DB session.
    Returns dict: {item_key: citation_key}"""
    if db_path is None:
        db_path = os.path.expanduser("~/Zotero/zotero.sqlite")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Zotero database not found: {db_path}")

    tmp = _copy_db(db_path)
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        cur = conn.cursor()

        results = {}
        for key in item_keys:
            ck = _query_single_from_conn(conn, key)
            results[key] = ck
        conn.close()
        return results
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _query_single(db_path, item_key):
    """Query a single key from an already-copied DB."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return _query_single_from_conn(conn, item_key)
    finally:
        conn.close()


def _query_single_from_conn(conn, item_key):
    """Query from an existing connection."""
    cur = conn.cursor()

    # Try citationKey first
    cur.execute("""
        SELECT idv.value FROM items i
        JOIN itemData id ON i.itemID = id.itemID
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        WHERE i.key = ? AND f.fieldName = 'citationKey'
    """, (item_key,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]

    # Fallback: generate from title + year
    cur.execute("""
        SELECT f.fieldName, idv.value FROM items i
        JOIN itemData id ON i.itemID = id.itemID
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        WHERE i.key = ? AND f.fieldName IN ('title', 'date')
    """, (item_key,))
    meta = {}
    for field, value in cur.fetchall():
        meta[field] = value if value else ""

    title = meta.get('title', 'unknown')
    year = (meta.get('date', '0000') or '0000')[:4]
    words = title.split()
    key_words = []
    skip = {'the', 'a', 'an', 'of', 'in', 'on', 'to', 'for', 'and', 'with'}
    for w in words:
        clean = ''.join(c for c in w if c.isalnum())
        if clean and clean.lower() not in skip:
            key_words.append(clean)
        if len(key_words) >= 4:
            break

    if key_words:
        return key_words[0].lower() + ''.join(w.capitalize() for w in key_words[1:]) + year
    return f"unknown{year}"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Get Zotero citation key")
    parser.add_argument("item_key", nargs="?", help="Zotero item key (e.g., M5X4HK96)")
    parser.add_argument("--db-path", default=None, help="Path to zotero.sqlite")
    parser.add_argument("--batch", action="store_true",
                        help="Read item keys from stdin (one per line), output JSON map")

    args = parser.parse_args()

    try:
        if args.batch:
            keys = [line.strip() for line in sys.stdin if line.strip()]
            results = get_citekeys_batch(keys, args.db_path)
            import json
            print(json.dumps(results, ensure_ascii=False))
        elif args.item_key:
            ckey = get_citekey(args.item_key, args.db_path)
            print(ckey)
        else:
            parser.print_help()
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
