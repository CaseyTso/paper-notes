#!/usr/bin/env python3
"""
MinerU PDF-to-Markdown conversion via batch file upload API.

Usage:
    python mineru_upload.py <pdf_path> <output_dir> [--token TOKEN]

Prerequisites:
    - MinerU API token (from https://mineru.net API管理页面)
    - PDF file ≤ 200MB, ≤ 200 pages

Pipeline:
    1. POST /api/v4/file-urls/batch  → get upload URL + batch_id
    2. PUT PDF to OSS signed URL (NO Content-Type header!)
    3. Poll GET /api/v4/extract-results/batch/{batch_id}
    4. Download full.zip, extract → save full.md

Output:
    Saves full.md to <output_dir>/full.md
    Returns batch_id and extracted file path
"""

import requests
import time
import sys
import os
import zipfile
import shutil
import json
from pathlib import Path

# Default token (user should configure via --token or MINERU_TOKEN env var)
DEFAULT_TOKEN = os.environ.get(
    "MINERU_TOKEN",
    "MISSING_TOKEN"  # Will fail if not overridden via env or --token
)

API_BASE = "https://mineru.net/api/v4"


def upload_pdf(pdf_path, token, model="vlm", language="en"):
    """Upload PDF to MinerU and return batch_id."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    payload = {
        "files": [{"name": os.path.basename(pdf_path)}],
        "model_version": model,
        "language": language
    }

    resp = requests.post(f"{API_BASE}/file-urls/batch", headers=headers, json=payload)
    result = resp.json()

    if result.get("code") != 0:
        raise RuntimeError(f"MinerU upload URL request failed: {result.get('msg')}")

    batch_id = result["data"]["batch_id"]
    upload_url = result["data"]["file_urls"][0]

    # Upload to OSS (CRITICAL: no Content-Type header)
    with open(pdf_path, 'rb') as f:
        upload_resp = requests.put(upload_url, data=f)

    if upload_resp.status_code != 200:
        raise RuntimeError(
            f"OSS upload failed (status {upload_resp.status_code}): "
            f"{upload_resp.text[:300]}"
        )

    return batch_id


def poll_result(batch_id, token, max_polls=180, interval=10):
    """Poll MinerU until parsing completes. Returns full_zip_url."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    result_url = f"{API_BASE}/extract-results/batch/{batch_id}"

    for i in range(max_polls):
        time.sleep(interval)
        resp = requests.get(result_url, headers=headers)
        data = resp.json()

        if data.get("code") != 0:
            print(f"  Poll warning: {data.get('msg')}", file=sys.stderr)
            continue

        extract = data["data"].get("extract_result", [])
        if not extract:
            print(f"  [{i+1}] Waiting...", file=sys.stderr)
            continue

        state = extract[0]["state"]
        progress = extract[0].get("extract_progress", {})
        pages_done = progress.get("extracted_pages", "?")
        pages_total = progress.get("total_pages", "?")

        print(f"  [{i+1}] {state} | {pages_done}/{pages_total} pages", file=sys.stderr)

        if state == "done":
            return extract[0].get("full_zip_url", "")
        elif state == "failed":
            raise RuntimeError(f"Parsing failed: {extract[0].get('err_msg', 'Unknown')}")

    raise TimeoutError("Timed out waiting for MinerU parsing")


def _download_via_curl(url, dest_path, max_retries=3):
    """Download via curl as fallback when Python SSL fails."""
    import subprocess
    for attempt in range(max_retries):
        result = subprocess.run(
            ["curl", "-sSL", "-o", dest_path, url,
             "--connect-timeout", "30", "--max-time", "120"],
            capture_output=True, timeout=180
        )
        if result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 100:
            return True
        if attempt < max_retries - 1:
            time.sleep(3)
    return False


def download_and_extract(full_zip_url, output_dir):
    """Download result zip and extract full.md to output_dir.

    Tries Python requests first; falls back to curl on SSL errors
    (Python's SSL library can have compatibility issues with certain CDNs).
    """
    zip_path = os.path.join(output_dir, "mineru_result.zip")
    extract_dir = os.path.join(output_dir, "mineru_extract")
    os.makedirs(extract_dir, exist_ok=True)

    # Download — try requests, fall back to curl
    downloaded = False
    try:
        with requests.get(full_zip_url, stream=True, timeout=60) as r:
            with open(zip_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        if os.path.getsize(zip_path) > 100:
            downloaded = True
    except Exception as e:
        print(f"  requests download failed ({e}), trying curl...", file=sys.stderr)

    if not downloaded:
        print(f"  Downloading via curl...", file=sys.stderr)
        if not _download_via_curl(full_zip_url, zip_path):
            raise RuntimeError(f"Failed to download {full_zip_url} via both requests and curl")
    print(f"  Downloaded: {os.path.getsize(zip_path)} bytes", file=sys.stderr)

    # Extract
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)

    os.remove(zip_path)

    # Find full.md
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if f == 'full.md':
                src = os.path.join(root, f)
                dst = os.path.join(output_dir, "full.md")
                shutil.copy2(src, dst)
                size = os.path.getsize(dst)
                print(f"  full.md: {size} bytes", file=sys.stderr)
                return dst

    raise FileNotFoundError("full.md not found in MinerU zip output")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Convert PDF to Markdown via MinerU")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("output_dir", help="Directory to save full.md and temp files")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="MinerU API token")
    parser.add_argument("--model", default="vlm", help="Model version: pipeline|vlm")
    parser.add_argument("--language", default="en", help="Document language")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Max polling seconds (default: 1800)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Poll interval seconds (default: 10)")
    parser.add_argument("--download-only", default=None,
                        help="Skip upload & poll — just download from existing batch_id")
    parser.add_argument("--zip-url", default=None,
                        help="Direct zip URL for download (use with --download-only)")

    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # --download-only mode: skip upload & poll
    if args.download_only:
        zip_url = args.zip_url
        if not zip_url:
            # Query batch status to get zip URL
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {args.token}"
            }
            resp = requests.get(f"{API_BASE}/extract-results/batch/{args.download_only}", headers=headers)
            data = resp.json()
            if data.get("code") != 0:
                print(f"Error: batch query failed: {data.get('msg')}", file=sys.stderr)
                sys.exit(1)
            extract = data["data"].get("extract_result", [])
            if not extract or extract[0].get("state") != "done":
                print(f"Error: batch not done (state={extract[0].get('state','?') if extract else '?'})", file=sys.stderr)
                sys.exit(1)
            zip_url = extract[0].get("full_zip_url", "")
        print(f"Download-only mode: {zip_url[:80]}...", file=sys.stderr)
        md_path = download_and_extract(zip_url, args.output_dir)
        print(json.dumps({"full_md_path": md_path, "status": "success"}))
        return

    if not os.path.exists(args.pdf_path):
        print(f"Error: PDF not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    max_polls = args.timeout // args.interval

    print(f"Uploading to MinerU: {os.path.basename(args.pdf_path)}", file=sys.stderr)
    batch_id = upload_pdf(args.pdf_path, args.token, args.model, args.language)
    print(f"  Batch ID: {batch_id}", file=sys.stderr)

    print("Polling for results...", file=sys.stderr)
    zip_url = poll_result(batch_id, args.token, max_polls, args.interval)
    print(f"  Done: {zip_url[:80]}...", file=sys.stderr)

    print("Downloading and extracting...", file=sys.stderr)
    md_path = download_and_extract(zip_url, args.output_dir)

    # Output result as JSON for agent consumption
    print(json.dumps({
        "batch_id": batch_id,
        "full_md_path": md_path,
        "status": "success"
    }))


if __name__ == "__main__":
    main()
