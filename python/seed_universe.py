"""Generate the curated seed universe + underwriter map.

This produces two committed JSON seeds:

  data/universe_seed.json   list of {symbol, name, country, sector, underwriter}
  data/underwriters.json    {_disclaimer, _source, map: {symbol: firm}}

The live daily job replaces the *universe* with the real US-listed directory
from NASDAQ Trader, prices with Massive/Polygon grouped daily bars, and
fundamentals with Massive/Polygon ticker overview. These seeds exist so the
dashboard renders offline and so the scanner has a deterministic fallback
when the data feeds are unreachable (e.g. a locked-down CI sandbox).

The underwriter map is a CURATED SEED. The nine boutiques here specialise in
small foreign micro-cap Nasdaq listings; the specific symbol assignments are
illustrative and MUST be reconciled against the firm's syndicate records or
the EDGAR 424B4 prospectus before the flag is treated as authoritative.
"""
from __future__ import annotations
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

HIGH_RISK_UNDERWRITERS = [
    "US Tiger Securities", "WestPark Capital", "R.F. Lafferty", "Cathay Securities",
    "Prime Number Capital", "Benjamin Securities", "Revere Securities",
    "D. Boral Capital", "Dominari Securities",
]

# Anchors — the first two confirmed high-risk symbols reported.
# Baird Medical (CN medical devices) and Ten-League Intl (MY heavy machinery).
ANCHORS = {
    "BDMD": ("Baird Medical Investment Holdings", "China", "Medical Devices", "Prime Number Capital"),
    "TLIH": ("Ten-League International Holdings", "Malaysia", "Industrial Machinery", "US Tiger Securities"),
}

# Real low-priced small-cap tickers carried over from the internal demo file.
DEMO_TICKERS = [
    "AAGAF","ACDXF","AERT","ANLBF","ANPA","APTOF","ARBB","ATMGF","BBBMF","BIIO",
    "BIOX","BON","BRSYF","BRYFF","CANVF","CCDSF","CCM","CDBMF","CENEF","CLGOF",
    "CPHI","CREG","CUEN","DBVT","DLPN","DMAC","DRTS","EDTK","EFSH","ELMD",
    "ENSC","EOSE","EVTL","FAMI","FCEL","FFIE","FLXS","FSEA","GBOX","GLBS",
    "GMGI","GNPX","GRCL","GRIL","GSUN","HCDI","HOLO","HYMC","IDAI","IMPP",
    "INBS","INEO","IPDN","JFBR","JWEL","KAVL","KBAL","KNDI","KPRX","KRKR",
    "LGMK","LIQT","LITM","LNSR","LPCN","MACE","MDJH","MGOL","MINM","MKFG",
    "MOMO","MOXC","MRIN","MURA","MVST","NAAS","NCPL","NITO","NKLA","NNVC",
]

FOREIGN = ["China","Hong Kong","Singapore","Malaysia","Israel","Cayman Islands",
           "British Virgin Islands","United Kingdom","Australia","Canada","Japan",
           "South Korea","Taiwan","India","Brazil"]
SECTORS = ["Biotechnology","Medical Devices","Industrial Machinery","Specialty Retail",
           "Software","Shipping","Clean Energy","Semiconductors","Consumer Electronics",
           "Financial Services","Mining","Real Estate","Education","Pharmaceuticals"]

NAME_HEAD = ["Golden","Pacific","Sino","Asia","Global","United","Prime","Summit","Orient",
             "Bright","Greenland","Crown","Silverline","Evergreen","Hua","Jin","Lion City",
             "Phoenix","Dragon","Star","Vision","NeoTech","BlueSky","Apex","Nova"]
NAME_TAIL = ["Holdings","Group","Technology","Biosciences","Industrial","Capital",
             "International","Logistics","Resources","Pharma","Digital","Energy",
             "Medical","Materials","Robotics","Education","Therapeutics"]


def gen_ticker(rng, taken):
    while True:
        n = rng.choice([3, 4, 4, 4, 5])
        t = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(n))
        if t not in taken:
            taken.add(t)
            return t


def main():
    rng = random.Random(20260520)
    rows = []
    taken = set()

    # 1) anchors
    for sym, (name, country, sector, uw) in ANCHORS.items():
        rows.append({"symbol": sym, "name": name, "country": country,
                     "sector": sector, "underwriter": uw})
        taken.add(sym)

    # 2) demo tickers — mostly foreign micro-caps; ~70% foreign, ~35% underwritten
    for sym in DEMO_TICKERS:
        if sym in taken:
            continue
        taken.add(sym)
        is_foreign = rng.random() < 0.70
        country = rng.choice(FOREIGN) if is_foreign else "United States"
        uw = rng.choice(HIGH_RISK_UNDERWRITERS) if (is_foreign and rng.random() < 0.45) else None
        rows.append({"symbol": sym, "name": f"{sym} Corp",
                     "country": country, "sector": rng.choice(SECTORS),
                     "underwriter": uw})

    # 3) generated foreign micro-caps to widen the pool to ~230 names
    while len(rows) < 230:
        sym = gen_ticker(rng, taken)
        is_foreign = rng.random() < 0.62
        country = rng.choice(FOREIGN) if is_foreign else "United States"
        name = f"{rng.choice(NAME_HEAD)} {rng.choice(NAME_TAIL)}"
        uw = rng.choice(HIGH_RISK_UNDERWRITERS) if (is_foreign and rng.random() < 0.30) else None
        rows.append({"symbol": sym, "name": name, "country": country,
                     "sector": rng.choice(SECTORS), "underwriter": uw})

    rows.sort(key=lambda r: r["symbol"])

    (DATA / "universe_seed.json").write_text(json.dumps(rows, indent=2))

    uw_map = {r["symbol"]: r["underwriter"] for r in rows if r["underwriter"]}
    underwriters = {
        "_disclaimer": ("CURATED SEED — illustrative symbol->underwriter assignments for the "
                        "nine monitored boutiques. Reconcile against syndicate records or the "
                        "EDGAR 424B4 prospectus before treating the flag as authoritative."),
        "_source": "seed (high-risk-symbols/python/seed_universe.py)",
        "firms": HIGH_RISK_UNDERWRITERS,
        "map": dict(sorted(uw_map.items())),
    }
    (DATA / "underwriters.json").write_text(json.dumps(underwriters, indent=2))

    foreign = sum(1 for r in rows if r["country"] not in
                  {"United States", "United States of America", "USA", "US"})
    print(f"universe_seed.json: {len(rows)} symbols ({foreign} foreign)")
    print(f"underwriters.json:  {len(uw_map)} underwritten symbols across {len(HIGH_RISK_UNDERWRITERS)} firms")


if __name__ == "__main__":
    main()
