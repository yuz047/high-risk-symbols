"""Editable rule-based high-risk classifier.

A symbol is RULE-BASED high-risk only if ALL four editable criteria hold:

  1. mcap_below          market cap stayed below the cap across the last 20 bd
  2. shares_below        shares outstanding below the cap
  3. vol_below           20-day average volume below the cap
  4. price_below         price stayed below the cap across the last 20 bd

The high-risk underwriter watchlist is retained as context, but it is not a
rule flag and does not contribute to hit_count or rule_high_risk.

The non-US-issuer criterion has been removed — documented US-listed pump-and-
dump cases are frequently US issuers, so location was dropping real cases.

"...has been below X in the last 20 business days" is read conservatively as
"stayed below X for the whole window" -> we test the 20-day MAX against the
threshold (price_max_20d, and market cap evaluated at that 20-day price high).

All thresholds and the underwriter watchlist come from config (data/params.json)
so they can be edited without code changes.
"""
from __future__ import annotations
import pandas as pd

from config import (
    MCAP_MAX_USD, SHARES_OUT_MAX, AVG_VOL_MAX, PRICE_MAX_USD,
    HIGH_RISK_UNDERWRITERS,
)

FLAG_COLS = [
    "mcap_below", "shares_below", "vol_below", "price_below",
]
_UW_SET = {u.strip().lower() for u in HIGH_RISK_UNDERWRITERS}


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Worst-case market cap over the trailing window (shares × 20-day price high).
    df["mcap_max_20d"] = df["shares_out"] * df["price_max_20d"]

    df["mcap_below"] = df["mcap_max_20d"] < MCAP_MAX_USD
    df["shares_below"] = df["shares_out"] < SHARES_OUT_MAX
    df["vol_below"] = df["avg_volume"] < AVG_VOL_MAX
    df["price_below"] = df["price_max_20d"] < PRICE_MAX_USD
    df["hi_risk_underwriter"] = df["underwriter"].apply(
        lambda u: bool(u) and str(u).strip().lower() in _UW_SET
    )

    df["hit_count"] = df[FLAG_COLS].sum(axis=1).astype(int)
    df["rule_high_risk"] = df[FLAG_COLS].all(axis=1)
    return df
