"""Daily orchestrator.

data -> rules -> PCA(3) -> data/*.json

Outputs (the dashboard reads these; the GitHub Action commits them):
  data/symbols.json   one row per scanned symbol with metrics, rule flags,
                      PCA coordinates, risk score, and the combined verdict
  data/meta.json      run status, counts, thresholds, and the fitted PCA model
  data/history.json   one row per run-day of the headline counts (trend)
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from config import (
    DATA_DIR, HIGH_RISK_UNDERWRITERS, UNIVERSE_MAX_PRICE, LOOKBACK_DAYS,
    MCAP_MAX_USD, SHARES_OUT_MAX, AVG_VOL_MAX, PRICE_MAX_USD,
)
from data import get_market_frame, is_us, load_underwriter_map
from rules import apply_rules, FLAG_COLS
from pca import run_pca


def _log(msg: str) -> None:
    print(f"[run] {msg}", file=sys.stderr, flush=True)


def _category(rule: bool, pca: bool, hit: int) -> str:
    if rule and pca:
        return "both"
    if rule:
        return "rule"
    if pca:
        return "pca"
    if hit >= 3:  # 3-4 of 5 criteria — near-miss watch band
        return "watch"
    return "clear"


def _security_master_stats() -> dict:
    p = DATA_DIR / "security_master.json"
    if not p.exists():
        return {}
    try:
        rows = json.loads(p.read_text()).get("rows") or []
    except Exception:
        return {}
    coverage = sum(
        1 for r in rows
        if r.get("shares_out") is not None and r.get("market_cap") is not None
    )
    return {
        "security_master_rows": len(rows),
        "security_master_detail_coverage": coverage,
    }


def main() -> None:
    df = get_market_frame(prefer_live=True)
    df = apply_rules(df)
    df, pca_meta = run_pca(df)

    df["combined_high_risk"] = df["rule_high_risk"] | df["pca_high_risk"]
    df["category"] = [
        _category(r, p, h)
        for r, p, h in zip(df["rule_high_risk"], df["pca_high_risk"], df["hit_count"])
    ]

    records = []
    for _, r in df.iterrows():
        records.append({
            "symbol": r["symbol"],
            "name": r["name"],
            "country": r["country"],
            "is_us": bool(is_us(r["country"])),
            "underwriter": r["underwriter"] if r["underwriter"] else None,
            "close_price": round(float(r["close_price"]), 2),
            "price_max_20d": round(float(r["price_max_20d"]), 2),
            "mcap_musd": round(float(r["market_cap"]) / 1e6, 1),
            "mcap_max_musd": round(float(r["mcap_max_20d"]) / 1e6, 1),
            "shares_m": round(float(r["shares_out"]) / 1e6, 2),
            "vol_m": round(float(r["avg_volume"]) / 1e6, 3),
            "num_employees": int(r["num_employees"]),
            "stats_days": int(r.get("stats_days", LOOKBACK_DAYS) or LOOKBACK_DAYS),
            "fundamentals_imputed": bool(r.get("fundamentals_imputed", False)),
            "flags": {c: bool(r[c]) for c in FLAG_COLS},
            "hit_count": int(r["hit_count"]),
            "rule_high_risk": bool(r["rule_high_risk"]),
            "pca_candidate": bool(r.get("pca_candidate", False)),
            "pc1": round(float(r["pc1"]), 4),
            "pc2": round(float(r["pc2"]), 4),
            "pc3": round(float(r.get("pc3", 0.0)), 4),
            "risk_score": round(float(r["risk_score"]), 4),
            "risk_rank": float(r["risk_rank"]),
            "pca_high_risk": bool(r["pca_high_risk"]),
            "combined_high_risk": bool(r["combined_high_risk"]),
            "category": r["category"],
        })
    records.sort(key=lambda x: (-x["risk_score"], x["symbol"]))

    counts = {
        "rule_high_risk": int(df["rule_high_risk"].sum()),
        "pca_high_risk": int(df["pca_high_risk"].sum()),
        "both": int((df["rule_high_risk"] & df["pca_high_risk"]).sum()),
        "rule_only": int((df["rule_high_risk"] & ~df["pca_high_risk"]).sum()),
        "pca_only": int((~df["rule_high_risk"] & df["pca_high_risk"]).sum()),
        "combined_total": int(df["combined_high_risk"].sum()),
        "watch": int((df["category"] == "watch").sum()),
        "pca_candidate": int(df["pca_candidate"].sum()) if "pca_candidate" in df.columns else 0,
    }

    source = str(df["source"].iloc[0])
    synthetic = bool(df["synthetic"].iloc[0])
    now = datetime.now(timezone.utc)
    as_of = now.strftime("%Y-%m-%d")

    meta = {
        "as_of": as_of,
        "generated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "synthetic": synthetic,
        "universe_size": int(len(df)),
        "scanned_count": int(len(df)),
        "counts": counts,
        "thresholds": {
            "universe_max_price": UNIVERSE_MAX_PRICE,
            "lookback_days": LOOKBACK_DAYS,
            "mcap_max_usd": MCAP_MAX_USD,
            "shares_out_max": SHARES_OUT_MAX,
            "avg_vol_max": AVG_VOL_MAX,
            "price_max_usd": PRICE_MAX_USD,
        },
        "underwriters": HIGH_RISK_UNDERWRITERS,
        "underwriter_seed_count": len(load_underwriter_map()),
        "pca": pca_meta,
        "database": {
            "scope": "all major-exchange non-OTC common/ADR symbols with daily market stats",
            "static_security_master": "data/security_master.json",
            "daily_market_stats": "data/market_stats.json",
            "static_refresh_policy": "monthly",
            "market_stats_refresh_policy": "daily",
            **_security_master_stats(),
        },
        "data_health": {
            "ok": True,
            "synthetic": synthetic,
            "note": ("SEED DATA — sandbox feeds are firewalled; the GitHub Action "
                     "replaces this with live Massive/Polygon data when the API key is available."
                     if synthetic else "live Massive/Polygon data"),
        },
    }

    (DATA_DIR / "symbols.json").write_text(json.dumps(records, indent=2))
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))

    # history (one row per run-day; replace same-day)
    hist_p = DATA_DIR / "history.json"
    hist = json.loads(hist_p.read_text()) if hist_p.exists() else []
    hist = [h for h in hist if h.get("date") != as_of]
    hist.append({
        "date": as_of,
        "scanned": int(len(df)),
        "rule": counts["rule_high_risk"],
        "pca": counts["pca_high_risk"],
        "both": counts["both"],
        "combined": counts["combined_total"],
        "source": source,
    })
    hist.sort(key=lambda h: h["date"])
    hist_p.write_text(json.dumps(hist[-400:], indent=2))

    _log(f"source={source} synthetic={synthetic} scanned={len(df)} "
         f"rule={counts['rule_high_risk']} pca={counts['pca_high_risk']} "
         f"both={counts['both']} combined={counts['combined_total']}")
    _log(f"risk component {pca_meta['risk_component_label']} "
         f"(d={pca_meta['risk_separation']}, EVR={pca_meta['explained_variance_ratio']})")


if __name__ == "__main__":
    main()
