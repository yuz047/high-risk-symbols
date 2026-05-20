"""PCA(3) structural risk view.

Pipeline
--------
1. Features: market_cap, avg_volume, close_price, num_employees.
   All four are strongly right-skewed, so we log1p them, then z-score.
2. PCA(3) via numpy SVD on the standardized matrix.
3. Pick the single component that best SEPARATES the known high-risk anchor
   set (BDMD / TLIH + any strict rule-based hits) from the rest, measured by
   standardized mean difference (Cohen's d). That is the "risk-discriminating
   component".
4. Orient it so the anchors sit on the HIGH side, call the oriented score the
   risk_score, and flag every symbol at/above the PCA_RISK_PERCENTILE cut.

The four features are all size/liquidity measures, so the risk component is
essentially a "smallness / illiquidity" axis — small cap, thin float, thin
volume, few employees, low price all load together on the high-risk side.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from config import (
    PCA_ANCHORS, PCA_FEATURES, PCA_N_COMPONENTS, PCA_REFERENCE_SYMBOLS,
    PCA_RISK_PERCENTILE,
)


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 1 or len(b) < 1:
        return 0.0
    na, nb = len(a), len(b)
    va, vb = (a.var(ddof=1) if na > 1 else 0.0), (b.var(ddof=1) if nb > 1 else 0.0)
    pooled = np.sqrt(((na - 1) * va + (nb - 1) * vb) / max(na + nb - 2, 1))
    if pooled == 0:
        return 0.0
    return float((a.mean() - b.mean()) / pooled)


def _percentile_rank(values: np.ndarray, x: float) -> float:
    """Return x's 0-100 rank against the scanned universe."""
    if len(values) == 0:
        return 0.0
    return float((values <= x).sum() / len(values) * 100)


def run_pca(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = df.copy().reset_index(drop=True)

    # 1) standardize log features
    X = np.log1p(df[PCA_FEATURES].astype(float).to_numpy())
    mu = X.mean(axis=0)
    sd = X.std(axis=0, ddof=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    # 2) PCA via SVD on the centered (already mean-0) matrix
    U, S, Vt = np.linalg.svd(Z, full_matrices=False)
    k = min(PCA_N_COMPONENTS, Vt.shape[0])
    comps = Vt[:k]                      # (k, n_features)
    scores = Z @ comps.T               # (n, k)
    evr = (S ** 2 / (S ** 2).sum())[:k]

    for j in range(k):
        df[f"pc{j+1}"] = scores[:, j]

    # 3) anchor set = confirmed high-risk names
    anchor_mask = df["symbol"].isin(PCA_ANCHORS).to_numpy()
    if "rule_high_risk" in df.columns:
        anchor_mask = anchor_mask | df["rule_high_risk"].to_numpy()

    seps = []
    for j in range(k):
        s = scores[:, j]
        if anchor_mask.sum() >= 1 and (~anchor_mask).sum() >= 1:
            seps.append(_cohens_d(s[anchor_mask], s[~anchor_mask]))
        else:
            # no anchors: fall back to "smallness" — correlate component with
            # standardized market cap; risk = the opposite of size.
            mcap_z = Z[:, PCA_FEATURES.index("market_cap")]
            seps.append(-float(np.corrcoef(s, mcap_z)[0, 1]))

    risk_idx = int(np.argmax(np.abs(seps)))
    orient = 1.0 if seps[risk_idx] >= 0 else -1.0
    risk_score = scores[:, risk_idx] * orient
    df["risk_score"] = risk_score

    # 4) percentile threshold on the high-risk side
    threshold = float(np.percentile(risk_score, PCA_RISK_PERCENTILE))
    df["pca_high_risk"] = df["risk_score"] >= threshold
    # 0-100 rank for display
    order = risk_score.argsort().argsort()
    df["risk_rank"] = (order / max(len(order) - 1, 1) * 100).round(1)

    # oriented loadings of the risk component (feature -> contribution)
    loadings = {f: float(comps[risk_idx][i] * orient) for i, f in enumerate(PCA_FEATURES)}

    references = []
    for ref in PCA_REFERENCE_SYMBOLS:
        X_ref = np.log1p(np.array([[float(ref[f]) for f in PCA_FEATURES]]))
        Z_ref = (X_ref - mu) / sd
        ref_scores = (Z_ref @ comps.T)[0]
        ref_risk = float(ref_scores[risk_idx] * orient)
        references.append({
            "symbol": ref["symbol"],
            "name": ref["name"],
            "group": ref["group"],
            "pc1": round(float(ref_scores[0]), 4),
            "pc2": round(float(ref_scores[1]) if k > 1 else 0.0, 4),
            "pc3": round(float(ref_scores[2]) if k > 2 else 0.0, 4),
            "risk_score": round(ref_risk, 4),
            "risk_rank": round(_percentile_rank(risk_score, ref_risk), 1),
        })

    meta = {
        "features": PCA_FEATURES,
        "n_components": k,
        "explained_variance_ratio": [round(float(x), 4) for x in evr],
        "risk_component_index": risk_idx,            # 0-based: which PC is the risk axis
        "risk_component_label": f"PC{risk_idx+1}",
        "orientation_sign": orient,
        "separation_cohens_d": [round(float(x), 3) for x in seps],
        "risk_separation": round(float(seps[risk_idx]), 3),
        "risk_percentile": PCA_RISK_PERCENTILE,
        "risk_threshold": round(threshold, 4),
        "risk_loadings": {kk: round(vv, 4) for kk, vv in loadings.items()},
        "anchors": PCA_ANCHORS,
        "anchor_count": int(anchor_mask.sum()),
        "reference_symbols": references,
        "reference_note": (
            "MAG7 and blue-chip points are projected into the fitted PCA space "
            "as visual rulers only; they are not scanned, counted, or flagged."
        ),
        "feature_log_mean": {f: round(float(mu[i]), 4) for i, f in enumerate(PCA_FEATURES)},
        "feature_log_std": {f: round(float(sd[i]), 4) for i, f in enumerate(PCA_FEATURES)},
    }
    return df, meta
