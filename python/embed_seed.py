"""Embed current metadata into web/index.html as a light offline fallback.

The dashboard always tries to fetch fresh data/ (local mirror, then the engine
repo's raw GitHub URL). Full symbol rows stay in data/symbols.json so the page
shell remains small.
"""
from __future__ import annotations
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "web" / "index.html"
DATA = ROOT / "data"


def clean(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return v


def main() -> None:
    meta = clean(json.loads((DATA / "meta.json").read_text()))
    html = HTML.read_text()

    # compact JSON (no spaces) to keep the file small
    sym_js = "[]"
    meta_js = json.dumps(meta, separators=(",", ":"), allow_nan=False)

    import re
    html = re.sub(
        r'(<script id="embedSymbols" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + sym_js + m.group(2), html, flags=re.S)
    html = re.sub(
        r'(<script id="embedMeta" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + meta_js + m.group(2), html, flags=re.S)

    HTML.write_text(html)
    print(f"embedded metadata + 0 fallback symbols into {HTML.name} "
          f"({HTML.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
