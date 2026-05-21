"""Generate the curated seed universe + underwriter map.

This produces two committed JSON seeds:

  data/universe_seed.json   list of {symbol, name, country, sector, underwriter}
  data/underwriters.json    {_disclaimer, firms, map: {symbol: firm}}

The live daily job replaces the *universe* with the real major-exchange
directory from NASDAQ Trader (OTC excluded), prices with Massive/Polygon
grouped daily bars, and fundamentals with Massive/Polygon ticker overview.
These seeds exist so the dashboard renders offline and so the scanner has a
deterministic fallback when the feeds are unreachable (e.g. a CI sandbox).

Universe = major-exchange listings only (no OTC). The underwriter watchlist is
sourced from FINRA/SEC small-cap "ramp-and-dump" enforcement (see params.json);
the per-symbol firm assignments here are a CURATED SEED and must be reconciled
against syndicate records / EDGAR 424B4 before the flag is authoritative.
"""
from __future__ import annotations
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

# Researched "sus" underwriter watchlist (FINRA/SEC small-cap ramp-and-dump).
HIGH_RISK_UNDERWRITERS = [
    "US Tiger Securities", "Spartan Capital Securities", "Aegis Capital",
    "Boustead Securities", "Network 1 Financial Securities",
    "Sutter Securities", "TradeUP Securities",
]

# Documented pump-and-dump / ramp-and-dump cases (DOJ/SEC). The first five are
# the PCA anchors (data/params.json); the rest seed the universe as real names.
REPORTED_CASES = {
    "CLEU": ("China Liberal Education Holdings", "China", "Education"),
    "OST":  ("Ostin Technology Group", "China", "Consumer Electronics"),
    "VISL": ("Vislink Technologies", "United States", "Communications Equipment"),
    "ABVC": ("ABVC BioPharma", "United States", "Biotechnology"),
    "ALZN": ("Alzamend Neuro", "United States", "Biotechnology"),
    "CEI":  ("Camber Energy", "United States", "Energy"),
    "MMAT": ("Meta Materials", "United States", "Materials"),
    "AREC": ("American Resources Corp.", "United States", "Materials"),
    "HUGE": ("FSD Pharma", "Canada", "Pharmaceuticals"),
    "GTT":  ("GTT Communications", "United States", "Telecom"),
}

# Real low-priced small-cap tickers (major exchanges; OTC 'F'-suffix names
# from the old demo were dropped per the no-OTC rule).
DEMO_TICKERS = [
    "AERT", "ANPA", "ARBB", "BIIO", "BIOX", "BON", "CCM", "CPHI", "CREG", "CUEN",
    "DBVT", "DLPN", "DMAC", "DRTS", "EDTK", "EFSH", "ELMD", "ENSC", "EOSE", "EVTL",
    "FAMI", "FCEL", "FFIE", "FLXS", "FSEA", "GBOX", "GLBS", "GMGI", "GNPX", "GRCL",
    "GRIL", "GSUN", "HCDI", "HOLO", "HYMC", "IDAI", "IMPP", "INBS", "INEO", "IPDN",
    "JFBR", "JWEL", "KAVL", "KBAL", "KNDI", "KPRX", "KRKR", "LGMK", "LIQT", "LITM",
    "LNSR", "LPCN", "MACE", "MDJH", "MGOL", "MINM", "MKFG", "MOMO", "MOXC", "MRIN",
    "MURA", "MVST", "NAAS", "NCPL", "NITO", "NKLA", "NNVC",
]

FOREIGN = ["China", "Hong Kong", "Singapore", "Israel", "Canada", "United Kingdom",
           "Australia", "Cayman Islands"]
SECTORS = ["Biotechnology", "Medical Devices", "Specialty Retail", "Software",
           "Clean Energy", "Semiconductors", "Consumer Electronics",
           "Financial Services", "Mining", "Real Estate", "Education",
           "Pharmaceuticals", "Communications Equipment", "Energy"]
NAME_HEAD = ["Apex", "Summit", "Crown", "Phoenix", "Vision", "NeoTech", "BlueSky",
             "Nova", "Pioneer", "Beacon", "Cascade", "Vector", "Atlas", "Quantum",
             "Catalyst", "Meridian", "Frontier", "Sterling", "Pinnacle", "Vanguard"]
NAME_TAIL = ["Holdings", "Group", "Technology", "Biosciences", "Capital", "Logistics",
             "Resources", "Pharma", "Digital", "Energy", "Materials", "Robotics",
             "Therapeutics", "Networks", "Systems"]


def gen_ticker(rng, taken):
    while True:
        n = rng.choice([3, 4, 4, 4])  # 3-4 letter (major-exchange style, no 5-letter OTC)
        t = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(n))
        if t not in taken:
            taken.add(t)
            return t


def main():
    rng = random.Random(20260520)
    rows = []
    taken = set()

    # 1) documented reported cases (incl. the PCA anchors)
    for sym, (name, country, sector) in REPORTED_CASES.items():
        uw = rng.choice(HIGH_RISK_UNDERWRITERS) if rng.random() < 0.4 else None
        rows.append({"symbol": sym, "name": name, "country": country,
                     "sector": sector, "underwriter": uw, "reported_case": True})
        taken.add(sym)

    # 2) real small-cap tickers — location no longer drives risk
    for sym in DEMO_TICKERS:
        if sym in taken:
            continue
        taken.add(sym)
        country = rng.choice(FOREIGN) if rng.random() < 0.30 else "United States"
        uw = rng.choice(HIGH_RISK_UNDERWRITERS) if rng.random() < 0.33 else None
        rows.append({"symbol": sym, "name": f"{sym} Corp",
                     "country": country, "sector": rng.choice(SECTORS),
                     "underwriter": uw})

    # 3) generated major-exchange micro-caps to widen the pool to ~210 names
    while len(rows) < 210:
        sym = gen_ticker(rng, taken)
        country = rng.choice(FOREIGN) if rng.random() < 0.28 else "United States"
        name = f"{rng.choice(NAME_HEAD)} {rng.choice(NAME_TAIL)}"
        uw = rng.choice(HIGH_RISK_UNDERWRITERS) if rng.random() < 0.30 else None
        rows.append({"symbol": sym, "name": name, "country": country,
                     "sector": rng.choice(SECTORS), "underwriter": uw})

    rows.sort(key=lambda r: r["symbol"])
    (DATA / "universe_seed.json").write_text(json.dumps(rows, indent=2))

    uw_map = {r["symbol"]: r["underwriter"] for r in rows if r["underwriter"]}
    underwriters = {
        "_disclaimer": ("CURATED SEED — illustrative symbol->underwriter assignments. The firm "
                        "watchlist is sourced from FINRA/SEC small-cap ramp-and-dump enforcement, "
                        "but the per-symbol assignments must be reconciled against syndicate records "
                        "or the EDGAR 424B4 prospectus before the flag is treated as authoritative."),
        "_source": "seed (high-risk-symbols/python/seed_universe.py)",
        "firms": HIGH_RISK_UNDERWRITERS,
        "map": dict(sorted(uw_map.items())),
    }
    (DATA / "underwriters.json").write_text(json.dumps(underwriters, indent=2))

    foreign = sum(1 for r in rows if r["country"] not in
                  {"United States", "United States of America", "USA", "US"})
    print(f"universe_seed.json: {len(rows)} symbols ({foreign} foreign, OTC excluded)")
    print(f"underwriters.json:  {len(uw_map)} underwritten symbols across {len(HIGH_RISK_UNDERWRITERS)} firms")


if __name__ == "__main__":
    main()
