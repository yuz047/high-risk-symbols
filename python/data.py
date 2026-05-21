"""Market-data layer for the High-Risk Symbols scanner.

Source priority (highest to lowest):

  1. LIVE — the real US-listed symbol directory from NASDAQ Trader, screened
     for price < UNIVERSE_MAX_PRICE via Massive/Polygon grouped daily bars.
     Company size fields are pulled from Massive ticker overview with a small
     persistent cache so the daily job does not repeat per-symbol calls.

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
    MASSIVE_REQUEST_SLEEP_SEC, MAX_DETAIL_FETCH, NASDAQ_LISTED_URL,
    OTHER_LISTED_URL, UNIVERSE_MAX_PRICE, US_COUNTRY_NAMES,
)

SECURITY_NAME_EXCLUDE_RE = re.compile(
    r"\b(warrant|right|rights|unit|units|preferred|depositary share|"
    r"depositary shares|note due|notes due|senior note|subordinated note)\b",
    re.IGNORECASE,
)
ALLOWED_TICKER_TYPES = {"CS", "ADRC", "ADRP", "ADRR"}


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


def build_live_universe() -> list[str] | None:
    """Pull the full US-listed symbol directory from NASDAQ Trader."""
    import requests
    syms: list[str] = []
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
            tidx = header.index("Test Issue") if "Test Issue" in header else None
            etfidx = header.index("ETF") if "ETF" in header else None
            for ln in lines[1:]:
                parts = ln.split("|")
                if len(parts) <= sidx:
                    continue
                sym = parts[sidx].strip()
                # Drop warrants/units/rights/classes and other suffix forms
                # such as ABC-W, ABC.W, ABC/U, ABC^A, or symbols with digits.
                if not sym or not sym.isalpha():
                    continue
                name = parts[nidx].strip() if nidx is not None and len(parts) > nidx else ""
                if SECURITY_NAME_EXCLUDE_RE.search(name):
                    continue
                if tidx is not None and parts[tidx].strip() == "Y":
                    continue
                if etfidx is not None and parts[etfidx].strip() == "Y":
                    continue
                syms.append(sym)
        except Exception as e:
            _log(f"universe {url} raised: {e}")
    syms = sorted(set(syms))
    if not syms:
        return None
    _log(f"live universe: {len(syms)} US-listed symbols")
    return syms


def screen_prices(symbols: list[str]) -> dict[str, dict] | None:
    """Screen prices using Massive grouped daily bars.

    This is intentionally request-light: one request per date returns OHLCV for
    the whole U.S. stock market. We walk backward until we have LOOKBACK_DAYS
    trading sessions, then compute 20-day price high and average volume.
    """
    if not _massive_api_key():
        _log("MASSIVE_API_KEY/POLYGON_API_KEY missing; live price screen disabled")
        return None

    universe = set(symbols)
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
        if not (0 < close < UNIVERSE_MAX_PRICE):
            continue
        out[sym] = {
            "close_price": close,
            "price_max_20d": max(b["high"] for b in bars),
            "avg_volume": sum(b["volume"] for b in bars) / len(bars),
        }
    _log(f"Massive price screen: {len(out)} symbols under ${UNIVERSE_MAX_PRICE}")
    return out or None


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
    out: dict[str, dict] = {}
    cache = _load_details_cache()
    seed_by_symbol = {r["symbol"]: r for r in load_universe_seed()}
    max_n = min(len(symbols), MAX_DETAIL_FETCH)
    fetched = 0
    cache_hits = 0
    for n, sym in enumerate(symbols[:MAX_DETAIL_FETCH]):
        cached = cache.get(sym)
        if cached:
            out[sym] = cached["data"]
            cache_hits += 1
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
            }
            out[sym] = data
            cache[sym] = {"fetched_at": _utc_now().isoformat(timespec="seconds"), "data": data}
            fetched += 1
        except Exception as e:
            if _is_rate_limit_error(e):
                _log(f"details {n}/{max_n}: Massive rate limited; stopping detail fetch")
                break
            out[sym] = {}
            cache[sym] = {"fetched_at": _utc_now().isoformat(timespec="seconds"), "data": {}}
            fetched += 1
        if n % 50 == 0:
            _log(f"details {n}/{max_n} (cache hits={cache_hits}, fetched={fetched})")
        if fetched and fetched % MASSIVE_DETAIL_CACHE_FLUSH_EVERY == 0:
            _save_details_cache(cache)
    _save_details_cache(cache)
    _log(f"details done: {len(out)}/{max_n} usable responses, cache hits={cache_hits}, fetched={fetched}")
    return out


def _get_live_frame() -> pd.DataFrame | None:
    universe = build_live_universe()
    if not universe:
        return None
    priced = screen_prices(universe)
    if not priced:
        return None
    # Prioritise the cheapest names (closest to the rule profile) for the
    # bounded detail fetch, then only build rows for the symbols we detailed.
    cands = sorted(priced.keys(), key=lambda s: priced[s]["close_price"])[:MAX_DETAIL_FETCH]
    details = fetch_details(cands)
    uw_map = load_underwriter_map()
    # Major-exchange common/ADR equity only — drop OTC, warrants, rights,
    # units, preferreds, notes, and other non-stock security types.
    major = {"XNAS", "XNYS", "XASE", "ARCX", "BATS", "IEXG"}
    rows = []
    dropped_non_stock = 0
    for sym in cands:
        p = priced[sym]
        d = details.get(sym, {})
        exch = d.get("primary_exchange")
        typ = str(d.get("ticker_type") or "").upper()
        if exch and exch not in major:
            dropped_non_stock += 1
            continue
        if typ and typ not in ALLOWED_TICKER_TYPES:
            dropped_non_stock += 1
            continue
        close = p["close_price"]
        mcap = d.get("market_cap")
        shares = d.get("shares_out")
        # recover whichever size proxy is missing from the other
        if mcap is None and shares:
            mcap = shares * close
        if shares is None and mcap:
            shares = mcap / close
        rows.append({
            "symbol": sym,
            "name": d.get("name") or sym,
            "country": d.get("country"),
            "underwriter": uw_map.get(sym),
            "close_price": close,
            "price_max_20d": p["price_max_20d"],
            "avg_volume": p["avg_volume"],
            "shares_out": shares,
            "market_cap": mcap,
            "num_employees": d.get("num_employees"),  # often None for micro-caps
        })
    df = pd.DataFrame(rows)
    # Require the size/liquidity fields the rules and PCA truly need. Employees
    # is commonly missing for micro-caps, so impute it rather than drop the row.
    need = ["market_cap", "shares_out", "avg_volume", "close_price"]
    df = df.dropna(subset=need)
    if len(df) < MASSIVE_MIN_LIVE_ROWS:
        _log(f"live frame too thin after Massive details: {len(df)} rows (<{MASSIVE_MIN_LIVE_ROWS})")
        return None
    emp_median = df["num_employees"].median()
    if pd.isna(emp_median):
        emp_median = 50.0  # small-cap default when the field is universally absent
    df["num_employees"] = df["num_employees"].fillna(emp_median).round().astype(int)
    df["source"] = "massive"
    df["synthetic"] = False
    _log(f"live frame: {len(df)} symbols ({dropped_non_stock} non-stock/OTC rows dropped; employees imputed for "
         f"{int(df['num_employees'].eq(round(emp_median)).sum())} missing)")
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
        sym = r["symbol"]
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
        })
    df = pd.DataFrame(out)
    df["source"] = "synthetic"
    df["synthetic"] = True
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
