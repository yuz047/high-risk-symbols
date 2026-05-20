"""Market-data layer for the High-Risk Symbols scanner.

Source priority (highest to lowest):

  1. LIVE — the real US-listed symbol directory from NASDAQ Trader, screened
     for price < UNIVERSE_MAX_PRICE via batched yfinance downloads, with full
     fundamentals (country, shares out, market cap, employees) pulled from
     ``Ticker.info``. yfinance runs through a curl_cffi Chrome-impersonation
     session so Yahoo's TLS bot filter doesn't block data-center IPs
     (GitHub Actions). This is the path that runs in CI every weekday.

  2. SEED/SYNTHETIC — deterministic fundamentals generated from
     ``data/universe_seed.json``. Used when the feeds are unreachable (e.g. a
     locked-down sandbox). Clearly tagged ``synthetic: True`` so nothing
     downstream mistakes it for real data.

Everything logs to stderr so the CI log shows exactly what happened.
"""
from __future__ import annotations
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    DATA_DIR, UNIVERSE_MAX_PRICE, LOOKBACK_DAYS,
    NASDAQ_LISTED_URL, OTHER_LISTED_URL, MAX_DETAIL_FETCH, US_COUNTRY_NAMES,
)


def _log(msg: str) -> None:
    print(f"[data] {msg}", file=sys.stderr, flush=True)


def is_us(country: str | None) -> bool:
    return (country or "").strip() in US_COUNTRY_NAMES


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
def _session():
    """curl_cffi Chrome-impersonation session (or None)."""
    try:
        from curl_cffi import requests as creq  # type: ignore
        return creq.Session(impersonate="chrome120")
    except Exception as e:  # pragma: no cover
        _log(f"curl_cffi unavailable ({e}); yfinance will use its default session")
        return None


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
            tidx = header.index("Test Issue") if "Test Issue" in header else None
            etfidx = header.index("ETF") if "ETF" in header else None
            for ln in lines[1:]:
                parts = ln.split("|")
                if len(parts) <= sidx:
                    continue
                sym = parts[sidx].strip()
                if not sym or not sym.isalpha():  # drop warrants/units/rights (^, ., $, digits)
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
    """Batched price/volume screen via yfinance. Returns per-symbol dict with
    close_price, price_max_20d, avg_volume; only names under the price gate."""
    try:
        import yfinance as yf  # type: ignore
    except Exception as e:
        _log(f"yfinance unavailable ({e}); cannot screen live")
        return None

    sess = _session()
    out: dict[str, dict] = {}
    CHUNK = 200
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        before = len(out)
        try:
            df = yf.download(chunk, period="2mo", interval="1d", group_by="ticker",
                             auto_adjust=True, threads=True, progress=False,
                             session=sess)
        except Exception as e:
            _log(f"yf.download chunk {i//CHUNK} raised: {e}")
            continue
        if df is None or len(df) == 0:
            _log(f"chunk {i//CHUNK}: empty download (Yahoo throttling?) — skipped")
            time.sleep(2.0)
            continue
        for sym in chunk:
            try:
                sub = df[sym] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(subset=["Close"]).tail(LOOKBACK_DAYS)
                if sub.empty:
                    continue
                close = float(sub["Close"].iloc[-1])
                if not (0 < close < UNIVERSE_MAX_PRICE):
                    continue
                out[sym] = {
                    "close_price": close,
                    "price_max_20d": float(sub["Close"].max()),
                    "avg_volume": float(sub["Volume"].tail(LOOKBACK_DAYS).mean()),
                }
            except Exception:
                continue
        kept = len(out) - before
        msg = f"screened {min(i+CHUNK, len(symbols))}/{len(symbols)}; +{kept} this chunk, {len(out)} total under ${UNIVERSE_MAX_PRICE}"
        if kept == 0:
            msg += " (0 kept — throttling or all above gate)"
        _log(msg)
        time.sleep(0.5)
    return out or None


def fetch_details(symbols: list[str]) -> dict[str, dict]:
    """Per-symbol fundamentals from Ticker.info (country, shares, mcap, employees)."""
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}
    sess = _session()
    out: dict[str, dict] = {}
    for n, sym in enumerate(symbols[:MAX_DETAIL_FETCH]):
        try:
            info = yf.Ticker(sym, session=sess).info or {}
            out[sym] = {
                "name": info.get("shortName") or info.get("longName") or sym,
                "country": info.get("country"),
                "shares_out": info.get("sharesOutstanding"),
                "market_cap": info.get("marketCap"),
                "num_employees": info.get("fullTimeEmployees"),
            }
        except Exception:
            out[sym] = {}
        if n % 50 == 0:
            _log(f"details {n}/{min(len(symbols), MAX_DETAIL_FETCH)}")
        time.sleep(0.2)
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
    rows = []
    for sym in cands:
        p = priced[sym]
        d = details.get(sym, {})
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
    if df.empty:
        return None
    emp_median = df["num_employees"].median()
    if pd.isna(emp_median):
        emp_median = 50.0  # small-cap default when the field is universally absent
    df["num_employees"] = df["num_employees"].fillna(emp_median).round().astype(int)
    df["source"] = "yfinance"
    df["synthetic"] = False
    _log(f"live frame: {len(df)} symbols (employees imputed for "
         f"{int(df['num_employees'].eq(round(emp_median)).sum())} missing)")
    return df


# --------------------------------------------------------------------- #
# Seed / synthetic path
# --------------------------------------------------------------------- #
# Pinned seed fundamentals for the two reported anchor names so they are
# unambiguously high-risk on every criterion (they orient the PCA axis and
# must appear in the combined list). Live runs overwrite these with Yahoo data.
_ANCHOR_FUNDAMENTALS = {
    # symbol: (close, price_max_20d, shares_m, vol_shares, employees)
    "BDMD": (2.60, 2.90, 23.0, 120_000, 210),
    "TLIH": (3.15, 3.55, 15.5, 280_000, 160),
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
        foreign = not is_us(country)
        # propensity to look like a pump-and-dump shell
        risk = 0.18 + (0.34 if foreign else 0.0) + (0.30 if uw else 0.0)
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
            if live is not None and len(live) >= 20:
                return live
            _log("live path unavailable or too thin; using synthetic seed")
        except Exception as e:
            _log(f"live path raised ({e}); using synthetic seed")
    return _synth_frame()
