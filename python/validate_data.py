"""Validate committed scanner data files.

This intentionally avoids network calls. Use it after local static backfills
and in CI before publishing daily market outputs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import data as md


ROOT = Path(__file__).resolve().parents[1]


def _fail(msg: str) -> None:
    print(f"[validate] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    except Exception:
        return []
    return [ROOT / line for line in out.splitlines() if line]


def _check_no_api_key_in_tracked_files() -> None:
    key = os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")
    if not key:
        return
    needle = key.encode()
    for path in _tracked_files():
        try:
            if needle in path.read_bytes():
                _fail(f"API key appears in tracked file {path.relative_to(ROOT)}")
        except OSError:
            continue


def _check_security_master() -> None:
    path = md.SECURITY_MASTER_PATH
    if not path.exists():
        _fail("data/security_master.json is missing")
    snap = json.loads(path.read_text())
    rows = snap.get("rows") or []
    if len(rows) < md.MIN_LIVE_SECURITY_MASTER_ROWS:
        _fail(f"security master has only {len(rows)} rows")

    bad_rows = []
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        typ = str(row.get("ticker_type") or "").upper()
        exch = row.get("primary_exchange")
        name = row.get("name")
        if (
            md._is_excluded_symbol(sym)
            or md._is_excluded_security_name(name)
            or (typ and typ not in md.ALLOWED_TICKER_TYPES)
            or (exch and exch not in md.MAJOR_EXCHANGES)
        ):
            bad_rows.append(sym or "<blank>")

    if bad_rows:
        sample = ", ".join(bad_rows[:20])
        _fail(f"security master contains excluded instruments: {sample}")

    coverage = md._security_master_detail_coverage(rows)
    print(f"[validate] security_master rows={len(rows)} detail_coverage={coverage}")


def main() -> None:
    _check_security_master()
    _check_no_api_key_in_tracked_files()
    print("[validate] ok")


if __name__ == "__main__":
    main()
