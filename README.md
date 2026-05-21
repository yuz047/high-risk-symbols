# Pump-&-Dump Radar

> Repo: `high-risk-symbols`. Product/dashboard name: **Pump-&-Dump Radar**.

A daily surveillance scan that flags **major-exchange (no-OTC)** US-listed
micro-caps fitting the classic **pump-and-dump** profile. It combines two
independent views and reports the union:

1. **Editable rule set** — a symbol is rule-based high-risk only when **all five**
   criteria hold for the day. Every threshold is editable in `data/params.json`.
2. **PCA(3) structural model** — an unsupervised view over four size/liquidity
   features. The component that best separates the confirmed high-risk anchors
   (documented DOJ/SEC pump-and-dump cases + rule hits) becomes the *risk axis*;
   everything past a percentile cut on the high-risk side is flagged.

A static dashboard reads the JSON snapshots and renders the verdict, the rule
criteria, the fitted PCA model, an interactive cut, and a per-symbol drill-down.

> Surveillance / research tooling. **Not investment advice.** The engine never
> places orders.

---

## The two views

### Editable Rule Set (all five must hold by default)

| # | Criterion | Default threshold |
|---|---|---|
| 1 | Market cap below cap across the last 20 business days | < $500M |
| 2 | Shares outstanding below cap | < 60M |
| 3 | 20-day average volume below cap | < 2M |
| 4 | Price below cap across the last 20 business days | < $7 |
| 5 | IPO underwriter is on the **high-risk watchlist** | watchlist match |

Issuer location is **not** a criterion — documented US-listed pump-and-dumps
are frequently US issuers, so a non-US filter was dropping real cases. The
universe filter keeps names under **$20** (wider than the $7 rule) so near-misses
stay visible in the PCA view. All thresholds live in `data/params.json` and are
also tunable live in the dashboard.

"...has been below X in the last 20 business days" is read conservatively as
*stayed below for the whole window* — the engine tests the 20-day extreme
(worst case), e.g. market cap is evaluated at the trailing 20-day price high.

The underwriter watchlist is **sourced from FINRA/SEC small-cap "ramp-and-dump"
enforcement**: US Tiger Securities, Spartan Capital Securities, Aegis Capital,
Boustead Securities, Network 1 Financial Securities, Sutter Securities, TradeUP
Securities. (See `data/params.json` for the per-firm source notes.)

### PCA(3) structural model

Features (log1p → z-scored): `market_cap`, `avg_volume`, `close_price`,
`num_employees`. PCA(3) is fit by SVD; for each component we measure how well
it separates the known high-risk anchor set (Cohen's *d*). The
**risk-discriminating component** is the one with the largest separation,
oriented so the anchors sit on the high side. Because all four inputs are
size/liquidity measures, the risk axis is effectively a "smallness &
illiquidity" direction — small cap, thin float, thin volume, few employees and
low price all load together. Names at/above the **80th percentile** (tunable)
on that axis are flagged.

Anchors are documented DOJ/SEC pump-and-dump cases — **CLEU, OST, VISL, ABVC,
ALZN** (from `data/params.json`) — replacing the earlier seed anchors. MAG7
and blue-chip names are projected into the fitted space as **visual rulers
only**; they are never scanned, counted, or flagged.

> The legacy PCA(2) nearest-neighbour list has been **retired** — superseded by
> this PCA(3) region.

### Combined verdict

Each symbol is tagged: **Rule + PCA** (highest conviction), **Rule only**,
**PCA only**, **Watch** (3–4 rule flags, not yet flagged), or **Clear**. The
reported high-risk list is the union of the editable rules and the PCA region.

---

## Architecture

```
GitHub Actions (weeknight, after the US close)
        │
        ▼
Python engine ── NASDAQ Trader symbol directory (universe)
                 Massive/Polygon grouped daily bars (price, volume)
                 Massive/Polygon ticker overview (fundamentals)
                 underwriters.json (curated boutique → symbol map)
   data → editable 5-rule set → PCA(3) risk axis → combined verdict
                          │
                          ▼
                  data/*.json ──► static dashboard (GitHub Pages)
```

The Python engine is the source of truth. The dashboard is a single
self-contained HTML file (Chart.js, no build step) that fetches the JSON
snapshots over HTTPS, with an embedded seed as an offline fallback.

---

## Repo layout

```
high-risk-symbols/
├── python/
│   ├── config.py          # thresholds, universe filter, underwriters, anchors
│   ├── data.py            # universe + Massive/Polygon screen/details + synthetic seed
│   ├── rules.py           # the editable five-criterion rule set
│   ├── pca.py             # PCA(3) via SVD + risk-axis selection + cut
│   ├── run_daily.py       # orchestrator → data/*.json (entry point for cron)
│   ├── seed_universe.py   # (re)build the deterministic seed universe + uw map
│   ├── embed_seed.py      # embed current data/ into web/index.html (offline)
│   └── requirements.txt
├── data/
│   ├── params.json        # EDITABLE thresholds + underwriter watchlist + anchors
│   ├── universe_seed.json # curated fallback universe (major exchanges, no OTC)
│   ├── underwriters.json  # curated symbol → firm map (maintained seed)
│   ├── symbols.json       # scan output (one row per symbol)
│   ├── meta.json          # run status, counts, thresholds, fitted PCA model
│   └── history.json       # headline counts per run-day (trend)
├── web/
│   └── index.html         # the dashboard (self-contained, Chart.js)
├── .github/workflows/daily.yml
├── README.md
└── DEPLOY.md
```

---

## Data sourcing

| Field | Source |
|---|---|
| Universe (major-exchange US-listed, **no OTC**) | NASDAQ Trader symbol directory (`nasdaqlisted` + `otherlisted`) |
| Price, 20-day high, avg volume | Massive/Polygon grouped daily bars (`include_otc=false`), one request per market date |
| Shares out, market cap, employees, exchange | Massive/Polygon ticker overview, cached and capped per run; OTC/non-major exchanges dropped |
| **IPO underwriter** | `data/underwriters.json` — the one field no price feed carries |
| Scan parameters (thresholds, cut, watchlist, anchors) | `data/params.json` — editable; no code change needed |

The underwriter **watchlist** (which firms count as high-risk) lives in
`data/params.json` and is sourced from FINRA/SEC small-cap ramp-and-dump
enforcement. The per-symbol **map** (`data/underwriters.json`) is a curated
seed; the specific symbol→firm assignments are illustrative and must be
reconciled against syndicate records or the EDGAR 424B4 prospectus before the
flag is treated as authoritative.

When the feeds are unreachable (e.g. a locked-down CI sandbox), the engine
falls back to a **deterministic synthetic seed** built from `universe_seed.json`,
clearly tagged `synthetic: true` so nothing downstream mistakes it for real
data. The committed `data/*.json` is seed data until the first live Action run
overwrites it.

---

## Run it yourself

Requires Python 3.11+. Live runs need an outbound connection and a
`MASSIVE_API_KEY` (or `POLYGON_API_KEY`) environment variable. The workflow
paces requests through one central throttle so it stays below common free-tier
rate limits; without a key, the deterministic seed path is used.

```bash
pip install -r python/requirements.txt
export MASSIVE_API_KEY="..."   # optional; omit for deterministic seed output
cd python
python seed_universe.py     # (re)build the seed universe + underwriter map
python run_daily.py         # scan → ../data/*.json
python embed_seed.py        # refresh the dashboard's offline fallback
```

Open `web/index.html` in a browser. The percentile slider re-cuts the PCA
region live; the table drills into each symbol's five-criterion breakdown and
PCA placement.

---

## Compliance and disclaimer

Research and surveillance tooling. It is not investment advice and makes no
buy/sell recommendation. The underwriter map and synthetic seed are starting
points, not authoritative records. Treat every flag as a prompt to review, not
a conclusion.

## License

MIT.
