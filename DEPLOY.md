# Deploy High-Risk Symbols

Same shape as GreenLight Trader: a **public engine repo** that runs the scan on
a GitHub Actions cron and commits `data/*.json`, plus a **static dashboard**
that reads those JSON files. No Supabase and no Vercel; live data uses a
Massive/Polygon API key stored as a GitHub Actions secret.

The dashboard is a single self-contained `web/index.html` (Chart.js, no build
step), so you have two equally simple hosting options.

---

## Step 1 — Push the engine repo

Create a new **public** repo named `high-risk-symbols` on GitHub (don't add a
README — this folder already has one).

```bash
cd "/Users/yunhanzhang/Desktop/works/high-risk-symbols"
git init
git add .
git commit -m "initial: rule gate + PCA(3) risk model + dashboard + daily cron"
git branch -M main
git remote add origin git@github.com:yuz047/high-risk-symbols.git
git push -u origin main
```

> Keep it **public**: public repos get free unlimited Actions minutes, and
> `raw.githubusercontent.com` only serves public files without auth — that's
> what the dashboard fetches.

## Step 2 — Choose where the dashboard lives

**Option A — host it from this repo (simplest).**
Settings → Pages → Source: `main` / root. The page is at
`https://yuz047.github.io/high-risk-symbols/web/` once Pages builds. It fetches
`../data/*.json` (same repo), so it always shows the latest scan with no extra
wiring.

**Option B — drop it into your personal site (matches the GreenLight setup).**
Copy `web/index.html` to `work/high-risk-symbols.html` in `yuz047.github.io`
and add a card on your Work page. There the page can't reach `../data`, so it
falls back to the next source in its list:
`https://raw.githubusercontent.com/yuz047/high-risk-symbols/main/data`. Both
sources are already wired in the `SOURCES` array near the bottom of the file —
reorder them if you want GitHub-raw to take priority.

Either way, if both fetches fail the page renders the **embedded seed** so it is
never blank.

## Step 3 — Add the Massive/Polygon secret

In the `high-risk-symbols` repo, add the API key as a repository secret:

Settings → Secrets and variables → Actions → New repository secret

Name it `MASSIVE_API_KEY`. The workflow also accepts `POLYGON_API_KEY` locally,
but the checked-in GitHub Action reads `MASSIVE_API_KEY`.

## Step 4 — Enable the daily cron

In the `high-risk-symbols` repo:

1. **Actions** tab → enable workflows if prompted.
2. **High-Risk Symbols daily → Run workflow** to fire it once manually. ~20–35
   min later you should see a commit by `high-risk-bot` updating `data/*.json`
   with **live** Massive/Polygon data (the dashboard's status banner flips
   from amber "Seed data" to green "Live data").

The cron is `30 0 * * 2-6` (00:30 UTC Tue–Sat ≈ 20:30 ET Mon–Fri, just after
the US close). Requests are deliberately paced through a central 15-second
throttle and ticker overview responses are cached between runs.

## Step 5 — Smoke test

Open the dashboard and hard-refresh (⌘-Shift-R). Check:

- Status banner is green **Live data** (amber means the cron hasn't run yet —
  it's showing the committed seed).
- The hero pill shows `via github` or `via local file` (not `embedded seed`).
- KPI tiles, the PCA charts, and the table are populated; the percentile slider
  re-cuts the PCA region live.

If the page looks empty: confirm the repo is **public** and `data/symbols.json`
is committed; check the Actions tab for failed runs.

---

## Maintaining the underwriter map

`data/underwriters.json` is the one input no price feed carries. Replace the
seed entries with your desk's syndicate records (or EDGAR 424B4 prospectus
data) for the nine boutiques. The scan picks it up on the next run — no code
change needed.

## What runs where (cheat sheet)

```
high-risk-symbols (repo)                dashboard
────────────────────────                ─────────
python/  ── runs in GH Actions          web/index.html
.github/workflows/daily.yml                │  fetch() ──► data/*.json
   │  commits data/*.json                  │            (same repo, or raw.github)
   ▼                                        ▼
data/symbols.json  data/meta.json       GitHub Pages (free, https)
data/history.json   (refreshed nightly)
```
