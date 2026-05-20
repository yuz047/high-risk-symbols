"""Embed the current data/*.json into web/index.html as an offline fallback.

The dashboard always tries to fetch fresh data/ (local mirror, then the engine
repo's raw GitHub URL). The embedded copy only renders when both fetches fail
(e.g. opening the file directly with file://). Re-run after reseeding.
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "web" / "index.html"
DATA = ROOT / "data"


def main() -> None:
    symbols = json.loads((DATA / "symbols.json").read_text())
    meta = json.loads((DATA / "meta.json").read_text())
    html = HTML.read_text()

    # compact JSON (no spaces) to keep the file small
    sym_js = json.dumps(symbols, separators=(",", ":"))
    meta_js = json.dumps(meta, separators=(",", ":"))

    import re
    html = re.sub(
        r'(<script id="embedSymbols" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + sym_js + m.group(2), html, flags=re.S)
    html = re.sub(
        r'(<script id="embedMeta" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + meta_js + m.group(2), html, flags=re.S)

    HTML.write_text(html)
    print(f"embedded {len(symbols)} symbols + meta into {HTML.name} "
          f"({HTML.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
