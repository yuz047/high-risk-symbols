"""PCA(3) structural risk view.

Pipeline
--------
1. Features: market_cap, avg_volume, close_price, num_employees.
   All four are strongly right-skewed, so we log1p them, then z-score.
2. PCA(3) via numpy SVD on rows with real static fundamentals. Rows whose
   market cap/shares were conservatively imputed remain in the database, but
   they are not used to fit or flag PCA.
3. Mark PCA candidates as every documented anchor plus every symbol with at
   least three of the four editable rule criteria. Nothing is price-pre-filtered
   out before PCA; candidates are simply labelled in the full PCA space.
4. Pick the single component that best SEPARATES that candidate/anchor set from
   the rest, measured by standardized mean difference (Cohen's d). That is the
   "risk-discriminating component".
5. Orient it so the candidates sit on the HIGH side, call the oriented score the
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

    raw_features = df[PCA_FEATURES].astype(float)
    finite_mask = np.isfinite(raw_features.to_numpy()).all(axis=1)
    if "fundamentals_imputed" in df.columns:
        fit_mask = (~df["fundamentals_imputed"].astype(bool).to_numpy()) & finite_mask
    else:
        fit_mask = finite_mask
    min_fit = max(PCA_N_COMPONENTS + 1, 20)
    if fit_mask.sum() < min_fit:
        fit_mask = finite_mask
    df["pca_eligible"] = fit_mask

    # 1) standardize log features using only PCA-eligible rows.
    X_all = np.log1p(raw_features.to_numpy())
    X_fit = X_all[fit_mask]
    mu = X_fit.mean(axis=0)
    sd = X_fit.std(axis=0, ddof=0)
    sd[sd == 0] = 1.0
    Z = (X_all - mu) / sd
    Z_fit = Z[fit_mask]

    # 2) PCA via SVD on the centered (already mean-0) matrix
    U, S, Vt = np.linalg.svd(Z_fit, full_matrices=False)
    k = min(PCA_N_COMPONENTS, Vt.shape[0])
    comps = Vt[:k]                      # (k, n_features)
    scores = Z @ comps.T               # (n, k)
    evr = (S ** 2 / (S ** 2).sum())[:k]

    for j in range(k):
        df[f"pc{j+1}"] = scores[:, j]

    # 3) candidate set = documented anchors + every name with >=3 rule hits.
    # This labels candidates inside the full PCA space; it is not a pre-filter.
    anchor_only_mask = df["symbol"].isin(PCA_ANCHORS).to_numpy()
    candidate_mask = anchor_only_mask.copy()
    if "hit_count" in df.columns:
        candidate_mask = candidate_mask | (df["hit_count"].to_numpy() >= 3)
    df["pca_candidate"] = candidate_mask
    fit_candidate_mask = candidate_mask & fit_mask

    seps = []
    for j in range(k):
        s = scores[:, j]
        fit_non_candidate_mask = fit_mask & ~candidate_mask
        if fit_candidate_mask.sum() >= 1 and fit_non_candidate_mask.sum() >= 1:
            seps.append(_cohens_d(s[fit_candidate_mask], s[fit_non_candidate_mask]))
        else:
            # no anchors: fall back to "smallness" — correlate component with
            # standardized market cap; risk = the opposite of size.
            mcap_z = Z[fit_mask, PCA_FEATURES.index("market_cap")]
            seps.append(-float(np.corrcoef(s[fit_mask], mcap_z)[0, 1]))

    risk_idx = int(np.argmax(np.abs(seps)))
    orient = 1.0 if seps[risk_idx] >= 0 else -1.0
    risk_score = scores[:, risk_idx] * orient
    df["risk_score"] = risk_score

    # 4) percentile threshold on the high-risk side, eligible rows only.
    fit_risk = risk_score[fit_mask]
    threshold = float(np.percentile(fit_risk, PCA_RISK_PERCENTILE))
    df["pca_high_risk"] = fit_mask & (df["risk_score"] >= threshold)
    df["risk_rank"] = [_percentile_rank(fit_risk, float(x)) for x in risk_score]

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
            "risk_rank": round(_percentile_rank(fit_risk, ref_risk), 1),
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
        "anchor_count": int(anchor_only_mask.sum()),
        "fit_count": int(fit_mask.sum()),
        "imputed_static_count": int((~fit_mask).sum()),
        "fit_candidate_count": int(fit_candidate_mask.sum()),
        "candidate_rule_min": 3,
        "candidate_count": int(candidate_mask.sum()),
        "reference_symbols": references,
        "reference_note": (
            "MAG7 and blue-chip points are projected into the fitted PCA space "
            "as visual rulers only; they are not scanned, counted, or flagged."
        ),
        "feature_log_mean": {f: round(float(mu[i]), 4) for i, f in enumerate(PCA_FEATURES)},
        "feature_log_std": {f: round(float(sd[i]), 4) for i, f in enumerate(PCA_FEATURES)},
        "fit_note": (
            "PCA is fitted and flagged only on rows with real static fundamentals; "
            "rows with conservative market-cap/share imputations stay in the database "
            "but are excluded from the PCA cut until static backfill fills them."
        ),
    }
    return df, meta
