#!/usr/bin/env python3
"""
Prune R2R upsert cache entries whose ingestion failed, so CCBE will re-upload
them on the next scan.

By default this script reads the cache at
  /data/context_chat_backend/persistent_storage/r2r_upsert_cache.json
and talks to the R2R API at R2R_BASE_URL (env) with optional R2R_API_KEY or
R2R_API_TOKEN for auth. It removes entries whose document's ingestion_status is
"failed" (or "error"), or that return 404 (not found), then writes the pruned
cache back to the same file (with a timestamped backup).

Usage:
  python3 context_chat_backend/scripts/prune_r2r_upsert_cache.py \
    --cache /data/context_chat_backend/persistent_storage/r2r_upsert_cache.json \
    --base http://127.0.0.1:7272 \
    [--api-key ... | --api-token ...] \
    [--dry-run]

Environment variables (take precedence unless CLI options provided):
  R2R_BASE_URL, R2R_API_KEY, R2R_API_TOKEN, R2R_UPSERT_CACHE_PATH
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

try:
    import httpx
except Exception as exc:  # pragma: no cover
    print("This script requires the 'httpx' package (pip install httpx)", file=sys.stderr)
    raise


def _headers(api_key: str | None, token: str | None) -> Dict[str, str]:
    h: Dict[str, str] = {"accept": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_doc_status(client: httpx.Client, doc_id: str) -> tuple[str | None, int]:
    """Return (ingestion_status, http_status). ingestion_status None for 404.

    Treat 'status' as a fallback key if 'ingestion_status' is absent.
    """
    try:
        r = client.get(f"/v3/documents/{doc_id}")
    except httpx.HTTPError as exc:  # pragma: no cover - networking
        return (None, getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    if r.status_code == 404:
        return (None, 404)
    if r.status_code >= 400:
        return (None, r.status_code)
    try:
        data = r.json().get("results", {})
    except Exception:
        data = {}
    status = data.get("ingestion_status") or data.get("status")
    return (str(status) if status is not None else None, r.status_code)


def load_cache(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache",
        default=os.getenv(
            "R2R_UPSERT_CACHE_PATH",
            "/data/context_chat_backend/persistent_storage/r2r_upsert_cache.json",
        ),
        help="Path to r2r_upsert_cache.json",
    )
    parser.add_argument(
        "--base",
        default=os.getenv("R2R_BASE_URL", "http://127.0.0.1:7272"),
        help="R2R base URL (e.g., http://host:7272)",
    )
    parser.add_argument("--api-key", default=os.getenv("R2R_API_KEY"))
    parser.add_argument("--api-token", default=os.getenv("R2R_API_TOKEN"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("R2R_HTTP_TIMEOUT", "30")))
    parser.add_argument("--dry-run", action="store_true", help="Do not modify the cache, just print actions")

    args = parser.parse_args(argv)
    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"Cache file not found: {cache_path}", file=sys.stderr)
        return 2

    cache = load_cache(cache_path)
    if not isinstance(cache, dict):
        print("Unexpected cache shape: expected a JSON object mapping digest->entry", file=sys.stderr)
        return 2

    headers = _headers(args.api_key, args.api_token)
    client = httpx.Client(base_url=args.base.rstrip("/"), timeout=args.timeout, headers=headers)

    to_remove: list[str] = []
    total = len(cache)
    checked = 0
    failed = 0
    not_found = 0

    print(f"Scanning {total} cache entries against {args.base} ...")
    for digest, entry in list(cache.items()):
        checked += 1
        doc_id = str(entry.get("doc_id") or "").strip()
        if not doc_id:
            # corrupt entry; safe to drop
            to_remove.append(digest)
            continue
        status, http_status = get_doc_status(client, doc_id)
        if http_status == 404:
            not_found += 1
            to_remove.append(digest)
            print(f"- remove (404) {digest} doc_id={doc_id} filename={entry.get('filename')}")
            continue
        if status and status.lower() in {"failed", "error"}:
            failed += 1
            to_remove.append(digest)
            print(f"- remove (ingestion_status={status}) {digest} doc_id={doc_id} filename={entry.get('filename')}")

    print(
        f"Checked={checked}, removing={len(to_remove)} (failed={failed}, not_found={not_found})"
    )
    if args.dry_run:
        print("Dry-run enabled; no changes written.")
        return 0

    if to_remove:
        # Backup current cache
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = cache_path.with_suffix(cache_path.suffix + f".bak.{ts}")
        shutil.copy2(cache_path, backup)
        for digest in to_remove:
            cache.pop(digest, None)
        save_cache(cache_path, cache)
        print(f"Wrote pruned cache to {cache_path} (backup at {backup})")
    else:
        print("No entries to remove.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

