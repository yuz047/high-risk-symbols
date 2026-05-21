"""Central config for the High-Risk Symbols scanner.

Mandate
-------
Surface US-listed symbols (major exchanges only — no OTC) that fit the classic
small-cap "pump-and-dump" risk profile, by combining two independent views:

  1. An editable RULE SET (all five criteria must hold for the day).
  2. A PCA(3) structural view — a single risk-discriminating component
     built from size/liquidity features; everything past a percentile cut
     on the high-risk side of that axis is flagged.

The final list is the UNION of the two views, tagged so each symbol shows
whether it was caught by the editable rules, the PCA region, or both.

The thresholds, the PCA cut, the underwriter watchlist, and the anchors are all
read from data/params.json so they can be edited without touching code.

> Research / surveillance tooling. Not investment advice. The underwriter
> watchlist is sourced from FINRA/SEC small-cap "ramp-and-dump" actions and the
> symbol->firm map is a maintained seed — reconcile both against syndicate
> records (or EDGAR 424B4 prospectuses).
"""
from __future__ import annotations
import json
from pathlib import Path

# --- Paths --------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- Editable parameters (data/params.json) ----------------------------
# Defaults below are used when params.json is absent or a key is missing.
_DEFAULTS = {
    "universe_max_price": 20.0,
    "lookback_days": 20,
    "mcap_max_usd": 500_000_000.0,
    "shares_out_max": 60_000_000.0,
    "avg_vol_max": 2_000_000.0,
    "price_max_usd": 7.0,
    "pca_risk_percentile": 80.0,
    "high_risk_underwriters": [
        "US Tiger Securities", "Spartan Capital Securities", "Aegis Capital",
        "Boustead Securities", "Network 1 Financial Securities",
        "Sutter Securities", "TradeUP Securities",
    ],
    "pca_anchors": ["CLEU", "OST", "VISL", "ABVC", "ALZN"],
}


def _load_params() -> dict:
    p = DATA_DIR / "params.json"
    out = dict(_DEFAULTS)
    if p.exists():
        try:
            user = json.loads(p.read_text())
            for k in _DEFAULTS:
                if k in user and user[k] not in (None, ""):
                    out[k] = user[k]
        except Exception:
            pass
    return out


_P = _load_params()

# --- Universe filter ----------------------------------------------------
# The pool we even bother to scan: major-exchange (no-OTC) names whose last
# close is under this price. Wider than the rule's price test so near-miss
# names stay visible in the PCA view.
UNIVERSE_MAX_PRICE = float(_P["universe_max_price"])
LOOKBACK_DAYS = int(_P["lookback_days"])  # "in the last 20 business days"

# --- Rule-based criteria thresholds (5 criteria; ALL must hold) ---------
MCAP_MAX_USD = float(_P["mcap_max_usd"])      # market cap below cap across the window
SHARES_OUT_MAX = float(_P["shares_out_max"])  # shares outstanding below cap
AVG_VOL_MAX = float(_P["avg_vol_max"])        # 20-day average volume below cap
PRICE_MAX_USD = float(_P["price_max_usd"])    # price below cap across the window
# Kept for issuer-location display only (no longer a rule criterion).
US_COUNTRY_NAMES = {"United States", "United States of America", "USA", "US"}

# --- High-risk underwriter watchlist -----------------------------------
# Sourced from FINRA/SEC small-cap "ramp-and-dump" enforcement (see params.json).
HIGH_RISK_UNDERWRITERS = list(_P["high_risk_underwriters"])

# --- PCA(3) risk model --------------------------------------------------
PCA_FEATURES = ["market_cap", "avg_volume", "close_price", "num_employees"]
PCA_N_COMPONENTS = 3
# Symbols at/above this percentile on the oriented risk component are flagged.
PCA_RISK_PERCENTILE = float(_P["pca_risk_percentile"])
# Anchor names — documented pump-and-dump cases (DOJ/SEC). They orient the risk
# component (their side of the axis is the high-risk side).
PCA_ANCHORS = list(_P["pca_anchors"])

# Large-cap reference names projected into the fitted PCA space as a visual
# ruler only. They are not part of the scan universe, percentile cut, table, or
# high-risk counts. Values are approximate scale anchors for fallback/demo data;
# live scans still use them only as reference points.
PCA_REFERENCE_SYMBOLS = [
    {
        "symbol": "AAPL", "name": "Apple", "group": "MAG7",
        "market_cap": 3_000_000_000_000.0, "avg_volume": 60_000_000.0,
        "close_price": 200.0, "num_employees": 164_000,
    },
    {
        "symbol": "MSFT", "name": "Microsoft", "group": "MAG7",
        "market_cap": 3_200_000_000_000.0, "avg_volume": 20_000_000.0,
        "close_price": 430.0, "num_employees": 228_000,
    },
    {
        "symbol": "NVDA", "name": "NVIDIA", "group": "MAG7",
        "market_cap": 3_000_000_000_000.0, "avg_volume": 250_000_000.0,
        "close_price": 125.0, "num_employees": 29_600,
    },
    {
        "symbol": "AMZN", "name": "Amazon", "group": "MAG7",
        "market_cap": 2_000_000_000_000.0, "avg_volume": 40_000_000.0,
        "close_price": 185.0, "num_employees": 1_500_000,
    },
    {
        "symbol": "GOOGL", "name": "Alphabet", "group": "MAG7",
        "market_cap": 2_000_000_000_000.0, "avg_volume": 30_000_000.0,
        "close_price": 170.0, "num_employees": 182_000,
    },
    {
        "symbol": "META", "name": "Meta", "group": "MAG7",
        "market_cap": 1_300_000_000_000.0, "avg_volume": 15_000_000.0,
        "close_price": 500.0, "num_employees": 70_000,
    },
    {
        "symbol": "TSLA", "name": "Tesla", "group": "MAG7",
        "market_cap": 550_000_000_000.0, "avg_volume": 100_000_000.0,
        "close_price": 175.0, "num_employees": 140_000,
    },
    {
        "symbol": "JPM", "name": "JPMorgan Chase", "group": "Blue chip",
        "market_cap": 550_000_000_000.0, "avg_volume": 10_000_000.0,
        "close_price": 200.0, "num_employees": 317_000,
    },
    {
        "symbol": "BRK.B", "name": "Berkshire Hathaway", "group": "Blue chip",
        "market_cap": 900_000_000_000.0, "avg_volume": 4_000_000.0,
        "close_price": 420.0, "num_employees": 396_500,
    },
    {
        "symbol": "JNJ", "name": "Johnson & Johnson", "group": "Blue chip",
        "market_cap": 350_000_000_000.0, "avg_volume": 8_000_000.0,
        "close_price": 145.0, "num_employees": 131_900,
    },
    {
        "symbol": "XOM", "name": "Exxon Mobil", "group": "Blue chip",
        "market_cap": 500_000_000_000.0, "avg_volume": 15_000_000.0,
        "close_price": 115.0, "num_employees": 62_000,
    },
    {
        "symbol": "PG", "name": "Procter & Gamble", "group": "Blue chip",
        "market_cap": 400_000_000_000.0, "avg_volume": 7_000_000.0,
        "close_price": 165.0, "num_employees": 108_000,
    },
]

# --- Data source env ----------------------------------------------------
# MASSIVE_API_KEY or POLYGON_API_KEY enables the live Massive/Polygon path.
# Without a key, the engine runs on the deterministic seed.
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
MASSIVE_API_BASE = "https://api.massive.com"

# --- Massive/Polygon request budget ------------------------------------
# The grouped daily endpoint gives all-stock OHLCV in one request per date.
# Ticker overview is per symbol, so it is cached and bounded. Keep request
# pacing conservative: 15 seconds stays below a 5-request/minute ceiling.
MASSIVE_REQUEST_SLEEP_SEC = 15.0
MASSIVE_LOOKBACK_CALENDAR_DAYS = 45
MASSIVE_DETAIL_CACHE_TTL_DAYS = 14
MASSIVE_DETAIL_CACHE_FLUSH_EVERY = 10

# Cap on how many price-screened candidates we pull ticker overview for in a
# single run. This keeps first-run request count below the workflow timeout; the
# cache fills in more names over subsequent runs.
MAX_DETAIL_FETCH = 80
MASSIVE_MIN_LIVE_ROWS = 35
