"""Where does a validation run actually spend its time?

    py tools\\show_timings.py            # every recorded run
    py tools\\show_timings.py DAVAO      # one DC
    py tools\\show_timings.py DAVAO --compare  # against the run before it

Why this exists: there was NO timing anywhere in the pipeline and the logs were
all 0 bytes, so "it's too slow to automate" could not be argued with -- nobody
could say WHICH phase, or by how much. Bugs 16-20 were four separate causes
hiding behind one slow function; measurement is what found them. Guessing here
is how that gets repeated.

run_dc.py appends one record per run to logs/timings.json:

    {"at": "...", "dc": "DAVAO DC", "pending": 588, "total_seconds": 412.3,
     "phases": {"fms_search": 96.4, "wps_copy": 121.0, ...}}

Reads only that file. Never touches the browser, FMS, or the sheet.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMINGS = ROOT / "logs" / "timings.json"

# Phases that can legitimately be absent (packed scrape is skipped on request),
# so a missing one is not treated as zero-cost when comparing.
ORDER = [
    "sheets_read", "chrome_start", "fms_search", "fms_export",
    "download_verify", "wps_copy", "mc_validate", "dump_all_write",
    "packed_scrape",
]


def load():
    if not TIMINGS.is_file():
        print(f"No timings yet at {TIMINGS}.")
        print("Run a validation first:  py run_dc.py --dc \"DAVAO DC\"")
        return []
    try:
        return json.loads(TIMINGS.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"{TIMINGS} is unreadable ({e}). Delete it and re-run.")
        return []


def bar(frac, width=34):
    return "#" * max(0, min(width, round(frac * width)))


def show_one(rec):
    total = rec.get("total_seconds") or 0
    ph = rec.get("phases") or {}
    print(f"\n  {rec.get('at')}  {rec.get('dc')}  "
          f"{rec.get('pending')} pending  {rec.get('date')}")
    print(f"  TOTAL {total:.1f}s\n")
    for name in sorted(ph, key=lambda k: -ph[k]):
        d = ph[name]
        print(f"    {name:16} {d:8.1f}s  {d / total * 100 if total else 0:5.1f}%  "
              f"{bar(d / total if total else 0)}")
    if ph:
        top = max(ph, key=lambda k: ph[k])
        print(f"    -> {top} is the biggest single cost "
              f"({ph[top]:.0f}s, {ph[top] / total * 100 if total else 0:.0f}% of the run)")


def compare(a, b):
    """b is the newer run. Show only phases that moved."""
    print(f"\n  BEFORE  {a.get('at')}  {a.get('dc')}  {a.get('total_seconds'):.1f}s")
    print(f"  AFTER   {b.get('at')}  {b.get('dc')}  {b.get('total_seconds'):.1f}s")
    dt = (b.get("total_seconds") or 0) - (a.get("total_seconds") or 0)
    pct = dt / (a.get("total_seconds") or 1) * 100
    verb = "faster" if dt < 0 else "SLOWER"
    print(f"  TOTAL   {dt:+.1f}s  ({pct:+.0f}%)  {verb}\n")

    pa, pb = a.get("phases") or {}, b.get("phases") or {}
    rows = []
    for k in set(pa) | set(pb):
        old, new = pa.get(k), pb.get(k)
        if old is None or new is None:
            rows.append((k, old, new, None))
        elif abs(new - old) >= 0.5:
            rows.append((k, old, new, new - old))
    if not rows:
        print("    no phase moved by 0.5s or more")
        return
    for k, old, new, d in sorted(rows, key=lambda r: -(abs(r[3]) if r[3] is not None else 0)):
        if d is None:
            print(f"    {k:16} {str(round(old, 1)) if old is not None else '-':>8} "
                  f"-> {str(round(new, 1)) if new is not None else '-':<8} (phase added/removed)")
        else:
            print(f"    {k:16} {old:8.1f}s -> {new:7.1f}s  {d:+7.1f}s")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    recs = load()
    if not recs:
        return 1
    if args:
        want = args[0].upper()
        recs = [r for r in recs if want in (r.get("dc") or "").upper()]
        if not recs:
            print(f"No recorded run for {want!r}. Have: "
                  + ", ".join(sorted({r.get('dc', '?') for r in load()})))
            return 1

    print(f"{len(recs)} recorded run(s) in {TIMINGS}")
    if "--compare" in flags:
        if len(recs) < 2:
            print("Need at least two runs to compare.")
            return 1
        compare(recs[-2], recs[-1])
    else:
        for rec in recs[-8:]:
            show_one(rec)
        if len(recs) > 8:
            print(f"\n  ... {len(recs) - 8} older run(s) not shown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())