"""Local Massive/Polygon fundamentals backfill.

This is intentionally separate from the daily scan. It hydrates the static
security master with ticker-overview fields (shares, market cap, employees)
in resumable chunks using data/cache/massive_details.json.

Usage:
  MASSIVE_API_KEY=... python python/backfill_fundamentals.py --limit 250

The API key must come from the environment. Do not commit it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import data as md


def _log(msg: str) -> None:
    print(f"[backfill] {msg}", file=sys.stderr, flush=True)


def _has_fundamentals(row: dict) -> bool:
    return row.get("shares_out") is not None and row.get("market_cap") is not None


def _load_master(refresh_directory: bool) -> list[dict]:
    snap = md._read_snapshot(md.SECURITY_MASTER_PATH)
    rows = (snap or {}).get("rows") or []
    if refresh_directory or not md._security_master_cache_usable(snap):
        fresh = md.build_security_master_from_directory()
        if fresh:
            # Preserve any already-hydrated fields by symbol.
            old = {r["symbol"]: r for r in rows}
            rows = [{**r, **{k: v for k, v in old.get(r["symbol"], {}).items()
                            if k in {"country", "shares_out", "market_cap", "num_employees"}
                            and v is not None}}
                    for r in fresh]
    if not rows:
        raise RuntimeError("security master unavailable; cannot backfill")
    return rows


def _detail_payload_to_row(info: dict, seed: dict, fallback: dict) -> dict:
    address = info.get("address") or {}
    return {
        "name": info.get("name") or fallback.get("name"),
        "country": (
            address.get("country")
            or fallback.get("country")
            or seed.get("country")
            or ("United States" if info.get("locale") == "us" else None)
        ),
        "shares_out": (
            info.get("weighted_shares_outstanding")
            or info.get("share_class_shares_outstanding")
            or fallback.get("shares_out")
            or seed.get("shares_out")
        ),
        "market_cap": (
            info.get("market_cap")
            or info.get("marketcap")
            or fallback.get("market_cap")
            or seed.get("market_cap")
        ),
        "num_employees": (
            info.get("total_employees")
            or fallback.get("num_employees")
            or seed.get("num_employees")
        ),
        "primary_exchange": info.get("primary_exchange") or fallback.get("primary_exchange"),
        "ticker_type": info.get("type") or fallback.get("ticker_type") or "CS",
    }


def _write_master(rows: list[dict], dropped: int) -> None:
    rows = sorted(rows, key=lambda r: r["symbol"])
    md._write_snapshot(
        md.SECURITY_MASTER_PATH,
        rows,
        "nasdaq_trader",
        {
            "refresh_policy": "monthly/static",
            "dropped_non_stock": dropped,
            "detail_coverage": sum(1 for r in rows if _has_fundamentals(r)),
            "backfilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=250, help="max new API detail calls this run")
    ap.add_argument("--sleep-sec", type=float, default=15.0, help="sleep after every API call")
    ap.add_argument("--max-minutes", type=float, default=0.0, help="optional wall-clock stop")
    ap.add_argument("--refresh-directory", action="store_true", help="refresh NASDAQ directory first")
    args = ap.parse_args()

    if not md._massive_api_key():
        raise SystemExit("MASSIVE_API_KEY/POLYGON_API_KEY is not set in the environment")

    md.MASSIVE_REQUEST_SLEEP_SEC = max(0.0, args.sleep_sec)

    rows = _load_master(args.refresh_directory)
    seed = {r["symbol"]: r for r in md.load_universe_seed()}
    cache = md._load_details_cache()

    before = sum(1 for r in rows if _has_fundamentals(r))
    missing = [r for r in rows if not _has_fundamentals(r)]
    _log(f"master rows={len(rows)} fundamentals={before} missing={len(missing)}")

    started = time.monotonic()
    fetched = 0
    used_cache = 0
    dropped = 0
    out_by_symbol = {r["symbol"]: dict(r) for r in rows}

    for i, r in enumerate(missing):
        if fetched >= args.limit:
            break
        if args.max_minutes and (time.monotonic() - started) / 60 >= args.max_minutes:
            _log("max runtime reached")
            break

        sym = r["symbol"]
        cached = cache.get(sym)
        info = None
        if cached and isinstance(cached.get("data"), dict) and cached["data"]:
            data = cached["data"]
            info = {
                "name": data.get("name"),
                "address": {"country": data.get("country")},
                "weighted_shares_outstanding": data.get("shares_out"),
                "market_cap": data.get("market_cap"),
                "total_employees": data.get("num_employees"),
                "primary_exchange": data.get("primary_exchange"),
                "type": data.get("ticker_type"),
            }
            used_cache += 1
        else:
            payload = md._massive_get(f"/v3/reference/tickers/{sym}")
            info = (payload or {}).get("results") or {}
            fetched += 1

        merged = {**r, **_detail_payload_to_row(info, seed.get(sym, {}), r)}
        typ = str(merged.get("ticker_type") or "").upper()
        exch = merged.get("primary_exchange")
        if exch and exch not in md.MAJOR_EXCHANGES:
            out_by_symbol.pop(sym, None)
            dropped += 1
        elif typ and typ not in md.ALLOWED_TICKER_TYPES:
            out_by_symbol.pop(sym, None)
            dropped += 1
        elif md._is_excluded_symbol(sym) or md._is_excluded_security_name(merged.get("name")):
            out_by_symbol.pop(sym, None)
            dropped += 1
        else:
            out_by_symbol[sym] = merged

        if (fetched + used_cache) % 10 == 0:
            current = list(out_by_symbol.values())
            _write_master(current, dropped)
            _log(f"progress idx={i+1}/{len(missing)} fetched={fetched} cache={used_cache} "
                 f"fundamentals={sum(1 for x in current if _has_fundamentals(x))}/{len(current)}")

    final = list(out_by_symbol.values())
    _write_master(final, dropped)
    after = sum(1 for r in final if _has_fundamentals(r))
    _log(f"done fetched={fetched} cache={used_cache} dropped={dropped} fundamentals={after}/{len(final)}")


if __name__ == "__main__":
    main()
