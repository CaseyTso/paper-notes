#!/usr/bin/env python3
"""
Get Zotero citation key (Better BibTeX) from item key.

Usage:
    # Single key
    python get_citekey.py <item_key> [--db-path ~/Zotero/zotero.sqlite]

    # Batch mode: read keys from stdin, query all in one DB session
    echo "KEY1\\nKEY2\\nKEY3" | python get_citekey.py --batch [--db-path ...]

Returns citation key as plain text on stdout, or exits with error.
Migration-compatibility mode: returns the existing Better BibTeX key.
If the item has no citation key, a structured
{"status": "requires_core_allocation"} result is printed instead — there
is no title+year fallback generation. Unknown item keys are reported
separately as {"status": "not_found"}.
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
    """Query Zotero SQLite for the citation key of a single item.

    Migration-compatibility mode: returns the existing Better BibTeX
    ``citationKey`` value, or ``None`` when the item has none (the item
    requires core allocation/confirmation). Raises ``KeyError`` when the
    item key does not exist in the library at all, so callers can tell
    ``not_found`` apart from ``requires_core_allocation``.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/Zotero/zotero.sqlite")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Zotero database not found: {db_path}")

    tmp = _copy_db(db_path)
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            if not _item_exists(conn, item_key):
                raise KeyError(item_key)
            return _query_single_from_conn(conn, item_key)
        finally:
            conn.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_citekeys_batch(item_keys, db_path=None):
    """Query multiple item keys in a single DB session.

    Returns {item_key: {"citation_key": str|None, "requires_allocation": bool,
    "not_found": bool}} so the three states are never conflated.
    """
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
            if not _item_exists(conn, key):
                results[key] = {
                    "citation_key": None,
                    "requires_allocation": False,
                    "not_found": True,
                }
                continue
            ck = _query_single_from_conn(conn, key)
            results[key] = {
                "citation_key": ck,
                "requires_allocation": ck is None,
                "not_found": False,
            }
        conn.close()
        return results
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _item_exists(conn, item_key):
    """True when an item with this key exists in the library."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM items WHERE key = ?", (item_key,))
    return cur.fetchone()[0] > 0


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

    # citationKey only; no fallback generation. A missing key means the
    # item requires core allocation/confirmation.
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
    return None


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Get Zotero citation key")
    parser.add_argument("item_key", nargs="?", help="Zotero item key (e.g., M5X4HK96)")
    parser.add_argument("--db-path", default=None, help="Path to zotero.sqlite")
    parser.add_argument("--batch", action="store_true",
                        help="Read item keys from stdin (one per line), output JSON map")

    args = parser.parse_args(argv)

    try:
        if args.batch:
            keys = [line.strip() for line in sys.stdin if line.strip()]
            results = get_citekeys_batch(keys, args.db_path)
            print(json.dumps(results, ensure_ascii=False))
        elif args.item_key:
            ckey = get_citekey(args.item_key, args.db_path)
            if ckey is None:
                # item exists but has no BBT key: requires core allocation
                print(json.dumps({"status": "requires_core_allocation"},
                                 ensure_ascii=False))
            else:
                print(ckey)
        else:
            parser.print_help()
            sys.exit(1)
    except KeyError:
        print(json.dumps({"status": "not_found"}, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
