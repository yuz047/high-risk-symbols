"""Market-data layer for the High-Risk Symbols scanner.

Source priority (highest to lowest):

  1. LIVE — the real US-listed symbol directory from the committed static
     security master, screened for price < UNIVERSE_MAX_PRICE via
     Massive/Polygon grouped daily bars. Company size fields are maintained by
     the separate local fundamentals backfill, not by the daily scan.

  2. SEED/SYNTHETIC — deterministic fundamentals generated from
     ``data/universe_seed.json``. Used when the feeds are unreachable (e.g. a
     locked-down sandbox). Clearly tagged ``synthetic: True`` so nothing
     downstream mistakes it for real data.

Everything logs to stderr so the CI log shows exactly what happened.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config import (
    CACHE_DIR, DATA_DIR, LOOKBACK_DAYS, MASSIVE_API_BASE,
    MASSIVE_DETAIL_CACHE_FLUSH_EVERY, MASSIVE_DETAIL_CACHE_TTL_DAYS,
    MASSIVE_LOOKBACK_CALENDAR_DAYS, MASSIVE_MIN_LIVE_ROWS,
    MASSIVE_SECURITY_MASTER_TTL_DAYS,
    MASSIVE_REQUEST_SLEEP_SEC, MAX_DETAIL_FETCH, NASDAQ_LISTED_URL,
    OTHER_LISTED_URL, UNIVERSE_MAX_PRICE, US_COUNTRY_NAMES,
)

SECURITY_NAME_EXCLUDE_RE = re.compile(
    r"(\b(warrant|right|rights|unit|units|preferred|depositary share|"
    r"depositary shares|note due|notes due|senior note|subordinated note|"
    r"blank check|spac|special purpose acquisition)\b|"
    r"acquisitions?\b.{0,80}\b(corp|corporation|inc|company|co|ltd|limited)\.?)",
    re.IGNORECASE,
)
ALLOWED_TICKER_TYPES = {"CS", "ADRC", "ADRP", "ADRR"}
MAJOR_EXCHANGES = {"XNAS", "XNYS", "XASE", "ARCX", "BATS", "IEXG"}
OTHER_LISTED_EXCHANGE_MAP = {
    "A": "XASE",  # NYSE American
    "N": "XNYS",  # New York Stock Exchange
    "P": "ARCX",  # NYSE Arca
    "Z": "BATS",  # Cboe BZX
    "V": "IEXG",  # IEX
}
SECURITY_MASTER_PATH = DATA_DIR / "security_master.json"
MARKET_STATS_PATH = DATA_DIR / "market_stats.json"
MIN_LIVE_SECURITY_MASTER_ROWS = 1_000


def _log(msg: str) -> None:
    print(f"[data] {msg}", file=sys.stderr, flush=True)


def is_us(country: str | None) -> bool:
    return (country or "").strip() in US_COUNTRY_NAMES


def _is_rate_limit_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    markers = ("Too Many Requests", "Rate limited", "rate limit", "429")
    return any(m in text for m in markers)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sleep(sec: float) -> None:
    if sec > 0:
        time.sleep(sec)


def _is_excluded_symbol(sym: str) -> bool:
    """Drop suffix-style warrants/rights/units/OTC foreign ordinary tickers."""
    if not sym or not sym.isalpha():
        return True
    # Major-exchange common stock can be 1-4 letters. Five-letter suffixes are
    # frequently warrant/right/unit/foreign-ordinary forms: ABCDW, ABCDR, ABCDU,
    # ABCDF. Keep legitimate one-letter names such as W.
    return len(sym) >= 5 and sym[-1] in {"W", "R", "U", "F"}


def _is_excluded_security_name(name: str | None) -> bool:
    return bool(name and SECURITY_NAME_EXCLUDE_RE.search(name))


def _is_inactive_or_delisted(row: dict) -> bool:
    active = row.get("active")
    if isinstance(active, bool) and not active:
        return True
    if isinstance(active, str) and active.strip().lower() in {"false", "0", "no", "n"}:
        return True
    return bool(row.get("delisted_utc") or row.get("delisted"))


def _is_excluded_security_row(row: dict) -> bool:
    sym = str(row.get("symbol") or "").upper()
    typ = str(row.get("ticker_type") or "").upper()
    exch = row.get("primary_exchange")
    name = row.get("name")
    return (
        _is_excluded_symbol(sym)
        or _is_excluded_security_name(name)
        or _is_inactive_or_delisted(row)
        or (typ and typ not in ALLOWED_TICKER_TYPES)
        or (exch and exch not in MAJOR_EXCHANGES)
    )


# --------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------- #
def load_universe_seed() -> list[dict]:
    p = DATA_DIR / "universe_seed.json"
    return json.loads(p.read_text()) if p.exists() else []


def load_underwriter_map() -> dict[str, str]:
    p = DATA_DIR / "underwriters.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text()).get("map", {})


# --------------------------------------------------------------------- #
# Live path
# --------------------------------------------------------------------- #
def _massive_api_key() -> str | None:
    return os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")


def _massive_get(path: str, params: dict | None = None) -> dict | None:
    """GET a Massive/Polygon endpoint with one central throttle point."""
    key = _massive_api_key()
    if not key:
        _log("MASSIVE_API_KEY/POLYGON_API_KEY missing; live path disabled")
        return None

    import requests

    q = dict(params or {})
    q["apiKey"] = key
    url = f"{MASSIVE_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, params=q, timeout=30)
        if r.status_code == 429:
            raise RuntimeError("429 Too Many Requests")
        if r.status_code >= 400:
            _log(f"Massive {path}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        if _is_rate_limit_error(e):
            _log(f"Massive {path}: rate limited; stopping live path")
            raise
        _log(f"Massive {path} raised: {e}")
        return None
    finally:
        _sleep(MASSIVE_REQUEST_SLEEP_SEC)


def build_security_master_from_directory() -> list[dict] | None:
    """Pull major-exchange common/ADR symbols from NASDAQ Trader."""
    import requests
    rows: dict[str, dict] = {}
    for url, sep_col in ((NASDAQ_LISTED_URL, "Symbol"), (OTHER_LISTED_URL, "ACT Symbol")):
        try:
            r = requests.get(url, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or "|" not in r.text:
                _log(f"universe {url}: HTTP {r.status_code}, unexpected body")
                continue
            lines = [ln for ln in r.text.splitlines() if ln and not ln.startswith("File Creation")]
            header = lines[0].split("|")
            sidx = header.index(sep_col)
            nidx = header.index("Security Name") if "Security Name" in header else None
            eidx = header.index("Exchange") if "Exchange" in header else None
            tidx = header.index("Test Issue") if "Test Issue" in header else None
            etfidx = header.index("ETF") if "ETF" in header else None
            for ln in lines[1:]:
                parts = ln.split("|")
                if len(parts) <= sidx:
                    continue
                sym = parts[sidx].strip().upper()
                # Drop warrants/units/rights/classes and other suffix forms.
                if _is_excluded_symbol(sym):
                    continue
                name = parts[nidx].strip() if nidx is not None and len(parts) > nidx else ""
                if _is_excluded_security_name(name):
                    continue
                if tidx is not None and parts[tidx].strip() == "Y":
                    continue
                if etfidx is not None and parts[etfidx].strip() == "Y":
                    continue
                exch = "XNAS"
                if eidx is not None and len(parts) > eidx:
                    exch = OTHER_LISTED_EXCHANGE_MAP.get(parts[eidx].strip(), "")
                if exch not in MAJOR_EXCHANGES:
                    continue
                rows[sym] = {
                    "symbol": sym,
                    "name": name or sym,
                    "primary_exchange": exch,
                    "ticker_type": "CS",
                    "active": True,
                    "delisted_utc": None,
                }
        except Exception as e:
            _log(f"universe {url} raised: {e}")
    out = sorted(rows.values(), key=lambda x: x["symbol"])
    if not out:
        return None
    _log(f"security directory: {len(out)} major-exchange symbols")
    return out


def build_live_universe() -> list[str] | None:
    """Compatibility wrapper: return symbols from the static master source."""
    rows = build_security_master_from_directory()
    return [r["symbol"] for r in rows] if rows else None


def _read_snapshot(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        _log(f"{path.name} unreadable ({e}); ignoring")
        return None


def _snapshot_fresh(path: Path, ttl_days: int) -> bool:
    snap = _read_snapshot(path)
    if not snap:
        return False
    try:
        generated_at = datetime.fromisoformat(snap["generated_at"])
    except Exception:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return generated_at >= _utc_now() - timedelta(days=ttl_days)


def _security_master_cache_usable(snap: dict | None) -> bool:
    """Live runs must not reuse the small synthetic fallback master."""
    if not snap:
        return False
    rows = snap.get("rows") or []
    source = str(snap.get("source") or "").lower()
    if source == "synthetic":
        return False
    if len(rows) < MIN_LIVE_SECURITY_MASTER_ROWS:
        return False
    return True


def _write_snapshot(path: Path, rows: list[dict], source: str, extra: dict | None = None) -> None:
    payload = {
        "generated_at": _utc_now().isoformat(timespec="seconds"),
        "source": source,
        "count": len(rows),
        "rows": rows,
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _has_static_fundamentals(row: dict) -> bool:
    return row.get("shares_out") is not None and row.get("market_cap") is not None


def _security_master_detail_coverage(rows: list[dict]) -> int:
    return sum(1 for r in rows if _has_static_fundamentals(r))


def _merge_security_master_details(rows: list[dict]) -> list[dict]:
    """Merge cached/new Massive details into the directory master."""
    details = fetch_details([r["symbol"] for r in rows])
    out = []
    dropped = 0
    seed_by_symbol = {r["symbol"]: r for r in load_universe_seed()}
    for r in rows:
        sym = r["symbol"]
        d = details.get(sym, {})
        seed = seed_by_symbol.get(sym, {})
        merged = {
            "symbol": sym,
            "name": d.get("name") or r.get("name") or sym,
            "country": d.get("country") or r.get("country") or seed.get("country") or "United States",
            "primary_exchange": d.get("primary_exchange") or r.get("primary_exchange"),
            "ticker_type": str(d.get("ticker_type") or r.get("ticker_type") or "CS").upper(),
            "active": d.get("active", r.get("active", True)),
            "delisted_utc": d.get("delisted_utc") or r.get("delisted_utc"),
            "shares_out": d.get("shares_out") or r.get("shares_out") or seed.get("shares_out"),
            "market_cap": d.get("market_cap") or r.get("market_cap") or seed.get("market_cap"),
            "num_employees": d.get("num_employees") or r.get("num_employees") or seed.get("num_employees"),
        }
        if _is_excluded_security_row(merged):
            dropped += 1
            continue
        out.append(merged)
    _write_snapshot(
        SECURITY_MASTER_PATH, out, "nasdaq_trader",
        {
            "refresh_policy": "monthly/static",
            "dropped_non_stock": dropped,
            "detail_coverage": _security_master_detail_coverage(out),
        },
    )
    _log(f"security master refreshed: {len(out)} symbols "
         f"({_security_master_detail_coverage(out)} with fundamentals; {dropped} dropped)")
    return out


def load_security_master(force_refresh: bool = False) -> list[dict] | None:
    """Monthly/static security master: major exchanges only, no warrants/rights.

    Daily scans must treat this file as read-only. Static fundamentals are
    hydrated by ``backfill_fundamentals.py`` in controlled local batches.
    """
    snap = None if force_refresh else _read_snapshot(SECURITY_MASTER_PATH)
    if _security_master_cache_usable(snap):
        rows = snap.get("rows") or []
        coverage = _security_master_detail_coverage(rows)
        freshness = "fresh" if _snapshot_fresh(SECURITY_MASTER_PATH, MASSIVE_SECURITY_MASTER_TTL_DAYS) else "stale"
        _log(f"security master cache: {len(rows)} symbols ({coverage} with fundamentals; {freshness})")
        return rows

    rows = (snap or {}).get("rows") or []
    if rows:
        _log(f"security master cache ignored: source={snap.get('source')} count={len(rows)}")

    rows = build_security_master_from_directory()
    if rows:
        coverage = _security_master_detail_coverage(rows)
        _log(f"using live directory master in memory: {len(rows)} symbols "
             f"({coverage} with fundamentals; run local backfill for static fields)")
        return rows
    return None


def load_market_stats(symbols: list[str]) -> dict[str, dict] | None:
    """Load daily-changing price/volume stats using Massive grouped bars.

    This is intentionally request-light: one request per date returns OHLCV for
    the whole U.S. stock market. We walk backward until we have LOOKBACK_DAYS
    trading sessions, then compute last close, 20-day high, and average volume.
    """
    universe = set(symbols)
    if not _massive_api_key():
        snap = _read_snapshot(MARKET_STATS_PATH)
        rows = (snap or {}).get("rows") or []
        out = {
            r["symbol"]: {
                "close_price": r["close_price"],
                "price_max_20d": r["price_max_20d"],
                "avg_volume": r["avg_volume"],
                "stats_days": r.get("stats_days"),
            }
            for r in rows
            if r.get("symbol") in universe and str((snap or {}).get("source") or "").lower() != "synthetic"
        }
        if out:
            _log(f"MASSIVE_API_KEY/POLYGON_API_KEY missing; using cached market stats for {len(out)} symbols")
            return out
        _log("MASSIVE_API_KEY/POLYGON_API_KEY missing; live market stats disabled")
        return None

    hist: dict[str, list[dict]] = {}
    trading_days = 0
    end_day = _utc_now().date() - timedelta(days=1)

    for offset in range(MASSIVE_LOOKBACK_CALENDAR_DAYS):
        day = end_day - timedelta(days=offset)
        try:
            payload = _massive_get(
                f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
                {"adjusted": "true", "include_otc": "false"},
            )
        except Exception:
            return None
        rows = (payload or {}).get("results") or []
        if not rows:
            continue
        trading_days += 1
        for bar in rows:
            sym = str(bar.get("T") or "").strip()
            if sym not in universe:
                continue
            try:
                hist.setdefault(sym, []).append({
                    "date": day.isoformat(),
                    "close": float(bar["c"]),
                    "high": float(bar["h"]),
                    "volume": float(bar.get("v") or 0.0),
                })
            except Exception:
                continue
        _log(f"Massive grouped {day}: {len(rows)} bars, {trading_days}/{LOOKBACK_DAYS} trading days")
        if trading_days >= LOOKBACK_DAYS:
            break

    if trading_days < max(5, LOOKBACK_DAYS // 2):
        _log(f"too few Massive trading days ({trading_days}); live price screen unavailable")
        return None

    out: dict[str, dict] = {}
    for sym, bars in hist.items():
        bars = sorted(bars, key=lambda x: x["date"])[-LOOKBACK_DAYS:]
        if not bars:
            continue
        close = bars[-1]["close"]
        if close <= 0:
            continue
        out[sym] = {
            "close_price": close,
            "price_max_20d": max(b["high"] for b in bars),
            "avg_volume": sum(b["volume"] for b in bars) / len(bars),
            "stats_days": len(bars),
        }
    _write_snapshot(
        MARKET_STATS_PATH,
        [{"symbol": k, **v} for k, v in sorted(out.items())],
        "massive",
        {"refresh_policy": "daily/market", "trading_days": trading_days},
    )
    _log(f"Massive market stats: {len(out)} symbols with daily price/volume")
    return out or None


def screen_prices(symbols: list[str]) -> dict[str, dict] | None:
    """Compatibility wrapper for older callers."""
    return load_market_stats(symbols)


def _details_cache_path():
    return CACHE_DIR / "massive_details.json"


def _load_details_cache() -> dict[str, dict]:
    p = _details_cache_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception as e:
        _log(f"details cache unreadable ({e}); ignoring")
        return {}
    cutoff = _utc_now() - timedelta(days=MASSIVE_DETAIL_CACHE_TTL_DAYS)
    out: dict[str, dict] = {}
    for sym, item in raw.items():
        try:
            fetched_at = datetime.fromisoformat(item["fetched_at"])
        except Exception:
            continue
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if fetched_at >= cutoff and isinstance(item.get("data"), dict):
            out[sym] = item
    return out


def _save_details_cache(cache: dict[str, dict]) -> None:
    p = _details_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))


def fetch_details(symbols: list[str]) -> dict[str, dict]:
    """Per-symbol fundamentals from Massive ticker overview."""
    if not _massive_api_key():
        cache = _load_details_cache()
        cached = {sym: cache[sym]["data"] for sym in symbols if sym in cache}
        _log(f"MASSIVE_API_KEY/POLYGON_API_KEY missing; using {len(cached)} cached detail rows")
        return cached

    out: dict[str, dict] = {}
    cache = _load_details_cache()
    seed_by_symbol = {r["symbol"]: r for r in load_universe_seed()}
    fetched = 0
    cache_hits = 0
    for n, sym in enumerate(symbols):
        cached = cache.get(sym)
        if cached:
            out[sym] = cached["data"]
            cache_hits += 1
            continue
        if fetched >= MAX_DETAIL_FETCH:
            continue
        try:
            payload = _massive_get(f"/v3/reference/tickers/{sym}")
            info = (payload or {}).get("results") or {}
            address = info.get("address") or {}
            country = (
                address.get("country")
                or seed_by_symbol.get(sym, {}).get("country")
                or ("United States" if info.get("locale") == "us" else None)
            )
            data = {
                "name": info.get("name") or seed_by_symbol.get(sym, {}).get("name") or sym,
                "country": country,
                "shares_out": (
                    info.get("weighted_shares_outstanding")
                    or info.get("share_class_shares_outstanding")
                ),
                "market_cap": info.get("market_cap") or info.get("marketcap"),
                "num_employees": info.get("total_employees"),
                "primary_exchange": info.get("primary_exchange"),
                "ticker_type": info.get("type"),
                "active": info.get("active"),
                "delisted_utc": info.get("delisted_utc"),
            }
            out[sym] = data
            cache[sym] = {"fetched_at": _utc_now().isoformat(timespec="seconds"), "data": data}
            fetched += 1
        except Exception as e:
            if _is_rate_limit_error(e):
                _log(f"details {n}/{len(symbols)}: Massive rate limited; stopping detail fetch")
                break
            out[sym] = {}
            cache[sym] = {"fetched_at": _utc_now().isoformat(timespec="seconds"), "data": {}}
            fetched += 1
        if n % 50 == 0:
            _log(f"details {n}/{len(symbols)} (cache hits={cache_hits}, fetched={fetched})")
        if fetched and fetched % MASSIVE_DETAIL_CACHE_FLUSH_EVERY == 0:
            _save_details_cache(cache)
    _save_details_cache(cache)
    _log(f"details done: {len(out)}/{len(symbols)} usable responses, cache hits={cache_hits}, fetched={fetched}")
    return out


def _get_live_frame() -> pd.DataFrame | None:
    master = load_security_master()
    if not master:
        return None
    symbols = [r["symbol"] for r in master]
    priced = load_market_stats(symbols)
    if not priced:
        return None
    uw_map = load_underwriter_map()
    rows = []
    dropped_non_stock = 0
    missing_stats = 0
    imputed_static = 0
    for d in master:
        sym = d["symbol"]
        if sym not in priced:
            missing_stats += 1
            continue
        if _is_excluded_security_row(d):
            dropped_non_stock += 1
            continue
        p = priced[sym]
        close = p["close_price"]
        mcap = d.get("market_cap")
        shares = d.get("shares_out")
        # recover whichever size proxy is missing from the other
        if mcap is None and shares:
            mcap = shares * close
        if shares is None and mcap:
            shares = mcap / close
        fundamentals_imputed = False
        if mcap is None and shares is None:
            # Keep the symbol in the database, but make missing static
            # fundamentals conservative so it cannot accidentally satisfy the
            # small-cap / thin-float rules.
            mcap = 10_000_000_000.0
            shares = mcap / close
            fundamentals_imputed = True
            imputed_static += 1
        rows.append({
            "symbol": sym,
            "name": d.get("name") or sym,
            "country": d.get("country"),
            "underwriter": uw_map.get(sym),
            "close_price": close,
            "price_max_20d": p["price_max_20d"],
            "avg_volume": p["avg_volume"],
            "stats_days": p.get("stats_days"),
            "shares_out": shares,
            "market_cap": mcap,
            "num_employees": d.get("num_employees"),  # often None for micro-caps
            "fundamentals_imputed": fundamentals_imputed,
        })
    df = pd.DataFrame(rows)
    if len(df) < MASSIVE_MIN_LIVE_ROWS:
        _log(f"live frame too thin after Massive market stats: {len(df)} rows (<{MASSIVE_MIN_LIVE_ROWS})")
        return None
    emp_median = df["num_employees"].median()
    if pd.isna(emp_median):
        emp_median = 50.0  # small-cap default when the field is universally absent
    df["num_employees"] = df["num_employees"].fillna(emp_median).round().astype(int)
    df["source"] = "massive"
    df["synthetic"] = False
    _log(f"live frame: {len(df)} symbols ({missing_stats} without daily stats; "
         f"{dropped_non_stock} non-stock/OTC rows dropped; {imputed_static} static rows imputed)")
    return df


# --------------------------------------------------------------------- #
# Seed / synthetic path
# --------------------------------------------------------------------- #
# Pinned seed fundamentals for the documented anchor cases (DOJ/SEC reported
# pump-and-dumps) so they sit on the small/illiquid high-risk side and orient
# the PCA axis. Live runs overwrite these with Massive data.
_ANCHOR_FUNDAMENTALS = {
    # symbol: (close, price_max_20d, shares_m, vol_shares, employees)
    "CLEU": (2.20, 3.40, 33.9, 900_000, 180),   # China Liberal Education
    "OST":  (0.60, 2.60, 25.0, 1_400_000, 90),  # Ostin Technology
    "VISL": (0.95, 2.10, 18.0, 700_000, 120),   # Vislink Technologies
    "ABVC": (1.40, 3.20, 22.0, 400_000, 70),    # ABVC BioPharma
    "ALZN": (1.10, 2.80, 12.0, 300_000, 25),    # Alzamend Neuro
}


def _synth_frame() -> pd.DataFrame:
    rows_in = load_universe_seed()
    if not rows_in:
        raise RuntimeError("no universe_seed.json — run seed_universe.py first")
    uw_map = load_underwriter_map()
    out = []
    for r in rows_in:
        sym = str(r["symbol"]).upper()
        if _is_excluded_symbol(sym) or _is_excluded_security_name(r.get("name")):
            continue
        country = r.get("country", "United States")
        uw = r.get("underwriter") or uw_map.get(sym)

        if sym in _ANCHOR_FUNDAMENTALS:
            close, price_max, shares_m, vol, employees = _ANCHOR_FUNDAMENTALS[sym]
            shares = shares_m * 1e6
            out.append({
                "symbol": sym, "name": r.get("name", sym), "country": country,
                "underwriter": uw, "close_price": round(close, 2),
                "price_max_20d": round(price_max, 2), "avg_volume": round(vol, 0),
                "shares_out": round(shares, 0), "market_cap": round(shares * close, 0),
                "num_employees": int(employees),
                "stats_days": LOOKBACK_DAYS,
                "fundamentals_imputed": False,
            })
            continue

        seed = int(hashlib.md5(sym.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        # propensity to look like a pump-and-dump shell (issuer location no
        # longer matters; an at-risk underwriter raises the odds).
        risk = 0.30 + (0.40 if uw else 0.0)
        risky = rng.random() < risk

        if risky:
            close = float(rng.uniform(0.45, 4.6))
            shares_m = float(rng.uniform(4, 38))          # millions
            vol = float(rng.uniform(0.03, 0.95) * 1e6)    # shares
            employees = int(rng.uniform(8, 280))
        else:
            close = float(rng.uniform(2.5, 14.8))
            shares_m = float(rng.uniform(30, 520))
            vol = float(rng.uniform(0.6, 22) * 1e6)
            employees = int(rng.uniform(120, 24000))

        price_max = close * float(rng.uniform(1.02, 1.18))
        shares = shares_m * 1e6
        mcap_now = shares * close
        out.append({
            "symbol": sym,
            "name": r.get("name", sym),
            "country": country,
            "underwriter": uw,
            "close_price": round(close, 2),
            "price_max_20d": round(price_max, 2),
            "avg_volume": round(vol, 0),
            "shares_out": round(shares, 0),
            "market_cap": round(mcap_now, 0),
            "num_employees": employees,
            "stats_days": LOOKBACK_DAYS,
            "fundamentals_imputed": False,
        })
    df = pd.DataFrame(out)
    df["source"] = "synthetic"
    df["synthetic"] = True
    existing_master = _read_snapshot(SECURITY_MASTER_PATH)
    if not _security_master_cache_usable(existing_master):
        _write_snapshot(
            SECURITY_MASTER_PATH,
            [{
                "symbol": r["symbol"],
                "name": r["name"],
                "country": r["country"],
                "primary_exchange": "XNAS",
                "ticker_type": "CS",
                "active": True,
                "delisted_utc": None,
                "shares_out": r["shares_out"],
                "market_cap": r["market_cap"],
                "num_employees": r["num_employees"],
            } for r in out],
            "synthetic",
            {
                "refresh_policy": "monthly/static",
                "detail_coverage": len(out),
            },
        )
    _write_snapshot(
        MARKET_STATS_PATH,
        [{
            "symbol": r["symbol"],
            "close_price": r["close_price"],
            "price_max_20d": r["price_max_20d"],
            "avg_volume": r["avg_volume"],
            "stats_days": r["stats_days"],
        } for r in out],
        "synthetic",
        {"refresh_policy": "daily/market", "trading_days": LOOKBACK_DAYS},
    )
    _log(f"synthetic seed frame: {len(df)} symbols")
    return df


# --------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------- #
def get_market_frame(prefer_live: bool = True) -> pd.DataFrame:
    """Return the scan frame. Tries live; falls back to the synthetic seed."""
    if prefer_live:
        try:
            live = _get_live_frame()
            if live is not None and len(live) >= MASSIVE_MIN_LIVE_ROWS:
                return live
            _log("live path unavailable or too thin; using synthetic seed")
        except Exception as e:
            _log(f"live path raised ({e}); using synthetic seed")
    return _synth_frame()
