"""Rule-based high-risk gate.

A symbol is RULE-BASED high-risk only if ALL six criteria hold for the day:

  1. non_us              issuer location is not the United States
  2. mcap_below_300m     market cap stayed below $300M across the last 20 bd
  3. shares_below_40m    shares outstanding below 40M
  4. vol_below_1m        20-day average volume below 1M shares
  5. price_below_5       price stayed below $5 across the last 20 bd
  6. hi_risk_underwriter IPO underwriter is one of the nine monitored boutiques

"...has been below X in the last 20 business days" is read conservatively as
"stayed below X for the whole window" -> we test the 20-day MAX against the
threshold (price_max_20d, and market cap evaluated at that 20-day price high).
"""
from __future__ import annotations
import pandas as pd

from config import (
    MCAP_MAX_USD, SHARES_OUT_MAX, AVG_VOL_MAX, PRICE_MAX_USD,
    HIGH_RISK_UNDERWRITERS,
)
from data import is_us

FLAG_COLS = [
    "non_us", "mcap_below_300m", "shares_below_40m",
    "vol_below_1m", "price_below_5", "hi_risk_underwriter",
]
_UW_SET = {u.lower() for u in HIGH_RISK_UNDERWRITERS}


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Worst-case market cap over the trailing window (shares × 20-day price high).
    df["mcap_max_20d"] = df["shares_out"] * df["price_max_20d"]

    df["non_us"] = ~df["country"].apply(is_us)
    df["mcap_below_300m"] = df["mcap_max_20d"] < MCAP_MAX_USD
    df["shares_below_40m"] = df["shares_out"] < SHARES_OUT_MAX
    df["vol_below_1m"] = df["avg_volume"] < AVG_VOL_MAX
    df["price_below_5"] = df["price_max_20d"] < PRICE_MAX_USD
    df["hi_risk_underwriter"] = df["underwriter"].apply(
        lambda u: bool(u) and str(u).strip().lower() in _UW_SET
    )

    df["hit_count"] = df[FLAG_COLS].sum(axis=1).astype(int)
    df["rule_high_risk"] = df[FLAG_COLS].all(axis=1)
    return df
