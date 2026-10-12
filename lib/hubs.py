
"""Custom hub overrides for Misroute Checker."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUBS_CSV = ROOT / "mc" / "custom-hubs.csv"
TO_RE = re.compile(r"^(?:SPEPH|SPXPH|SPE|SPX|SLS|PH)[A-Z0-9]{8,}$", re.I)


def parse_hubs_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("hub name") or low.startswith("hub,"):
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        hub, dc = parts[0], parts[1]
        if TO_RE.match(hub):
            print("Skipping non-hub line in hubs CSV:", hub)
            continue
        rows.append({"hub": hub.upper(), "dc": dc.upper()})
    return rows


def apply_hubs_to_mc(page, path: Path | None = None) -> int:
    hubs = parse_hubs_csv(path or HUBS_CSV)
    if not hubs:
        return 0
    page.evaluate(
        """(hubs) => {
            let old = [];
            try { old = JSON.parse(localStorage.getItem('spx_hubs') || '[]'); } catch (e) {}
            const map = {};
            old.forEach(h => { if (h && h.hub) map[String(h.hub).toUpperCase()] = {hub: String(h.hub).toUpperCase(), dc: h.dc}; });
            hubs.forEach(h => { map[h.hub] = {hub: h.hub, dc: h.dc}; });
            localStorage.setItem('spx_hubs', JSON.stringify(Object.values(map)));
        }""",
        hubs,
    )
    print(f"Loaded {len(hubs)} custom hub mappings into MC")
    return len(hubs)


