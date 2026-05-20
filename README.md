# High-Risk Symbols

A daily surveillance scan that flags US-listed micro-caps fitting the classic
**pump-and-dump** profile. It combines two independent views and reports the
union:

1. **Rule-based gate** — the legacy logic, verbatim. A symbol is rule-based
   high-risk only when **all six** criteria hold for the day.
2. **PCA(3) structural model** — an unsupervised view over four size/liquidity
   features. The component that best separates the confirmed high-risk anchors
   (BDMD, TLIH + rule hits) becomes the *risk axis*; everything past a
   percentile cut on the high-risk side is flagged.

A static dashboard reads the JSON snapshots and renders the verdict, the rule
criteria, the fitted PCA model, an interactive cut, and a per-symbol drill-down.

> Surveillance / research tooling. **Not investment advice.** The engine never
> places orders.

---

## The two views

### Rule-based gate (all six must hold)

| # | Criterion | Threshold |
|---|---|---|
| 1 | Issuer location is **not** the United States | country ≠ US |
| 2 | Market cap below **$300M** across the last 20 business days | < $300M |
| 3 | Shares outstanding below **40M** | < 40M |
| 4 | 20-day average volume below **1M** shares | < 1M |
| 5 | Price below **$5** across the last 20 business days | < $5 |
| 6 | IPO underwriter is a **high-risk boutique** | one of nine firms |

"...has been below X in the last 20 business days" is read conservatively as
*stayed below for the whole window* — the engine tests the 20-day extreme
(worst case), e.g. market cap is evaluated at the trailing 20-day price high.

The nine monitored underwriters: **US Tiger Securities, WestPark Capital,
R.F. Lafferty, Cathay Securities, Prime Number Capital, Benjamin Securities,
Revere Securities, D. Boral Capital, Dominari Securities.**

### PCA(3) structural model

Features (log1p → z-scored): `market_cap`, `avg_volume`, `close_price`,
`num_employees`. PCA(3) is fit by SVD; for each component we measure how well
it separates the known high-risk anchor set (Cohen's *d*). The
**risk-discriminating component** is the one with the largest separation,
oriented so the anchors sit on the high side. Because all four inputs are
size/liquidity measures, the risk axis is effectively a "smallness &
illiquidity" direction — small cap, thin float, thin volume, few employees and
low price all load together. Names at/above the **85th percentile** (tunable)
on that axis are flagged.

> The legacy PCA(2) nearest-neighbour list to BDMD/TLIH has been **retired** —
> superseded by this PCA(3) region.

### Combined verdict

Each symbol is tagged: **Rule + PCA** (highest conviction), **Rule only**,
**PCA only**, **Watch** (4–5 rule flags, not yet flagged), or **Clear**. The
reported high-risk list is the union of the rule gate and the PCA region.

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
   data → rules (6-criteria gate) → PCA(3) risk axis → combined verdict
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
│   ├── config.py          # thresholds, universe gate, underwriters, anchors
│   ├── data.py            # universe + Massive/Polygon screen/details + synthetic seed
│   ├── rules.py           # the six-criteria rule gate
│   ├── pca.py             # PCA(3) via SVD + risk-axis selection + cut
│   ├── run_daily.py       # orchestrator → data/*.json (entry point for cron)
│   ├── seed_universe.py   # (re)build the deterministic seed universe + uw map
│   ├── embed_seed.py      # embed current data/ into web/index.html (offline)
│   └── requirements.txt
├── data/
│   ├── universe_seed.json # curated fallback universe
│   ├── underwriters.json  # curated boutique → symbol map (maintained seed)
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
| Universe (all US-listed) | NASDAQ Trader symbol directory (`nasdaqlisted` + `otherlisted`) |
| Price, 20-day high, avg volume | Massive/Polygon grouped daily bars, one request per market date |
| Country, shares out, market cap, employees | Massive/Polygon ticker overview, cached and capped per run |
| **IPO underwriter** | `data/underwriters.json` — the one field no price feed carries |

The underwriter map is a **curated seed**. The nine boutiques specialise in
small foreign micro-cap Nasdaq listings; the specific symbol→firm assignments
are illustrative and must be reconciled against the firm's syndicate records or
the EDGAR 424B4 prospectus before the flag is treated as authoritative.

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
region live; the table drills into each symbol's six-criterion breakdown and
PCA placement.

---

## Compliance and disclaimer

Research and surveillance tooling. It is not investment advice and makes no
buy/sell recommendation. The underwriter map and synthetic seed are starting
points, not authoritative records. Treat every flag as a prompt to review, not
a conclusion.

## License

MIT.
