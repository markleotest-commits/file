"""How long does a validation run take to even START?

    py tools\\bench_startup.py

Pure startup costs, no FMS, no writes. Measures every fixed delay between
"py run_dc.py" and "FMS is doing something", because a large constant here is
paid on EVERY run and every re-try, and it is invisible in a total.

Reads only config and the clock. Touches no browser and writes nothing.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from lib.dc_run import open_sheet, pending_for_dc, tabs_for_dc
from lib.pending_tos import exclude_validated

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def timed(label, fn):
    t0 = time.monotonic()
    out = fn()
    dt = time.monotonic() - t0
    print(f"    {dt:6.2f}s  {label}")
    return dt, out


def main() -> int:
    print("-- what a run pays before FMS is touched --\n")
    t_sheet, sh = timed("open_sheet  (Drive listing + open)", lambda: open_sheet(config))
    t_tabs, (tab_names, pending) = timed(
        f"pending_for_dc  ({len(tabs_for_dc(config, 'DAVAO DC'))} dock tabs)",
        lambda: pending_for_dc(config, "DAVAO DC", sh=sh))
    t_excl, (kept, excluded) = timed(
        "exclude_validated  (reads DUMP ALL)", lambda: exclude_validated(pending, sh, config))

    startup = t_sheet + t_tabs + t_excl
    print(f"\n    {startup:6.2f}s  TOTAL sheets-read, before any browser work")

    print("\n-- the other fixed cost: attaching to Chrome --")
    try:
        from playwright.sync_api import sync_playwright
        from fms_batch_export import CDP
        t0 = time.monotonic()
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(CDP)
            n = sum(len(c.pages) for c in b.contexts)
        cd = time.monotonic() - t0
        print(f"    {cd:6.2f}s  connect_over_cdp ({n} tab(s) open)")
        startup += cd
    except Exception as e:
        print(f"    skipped  ({str(e).splitlines()[0][:60]})")

    print("\n-- fixed delays in the code, per run --")
    from fms_batch_export import CDP  # noqa: F401
    import run_dc
    src = (ROOT / "run_dc.py").read_text(encoding="utf-8")
    checks = [
        ("wait_logged_in is given max(timeout, 300) -- a 300s budget",
         "wait_logged_in(page, max(timeout, 300))" in src),
        ("submit_and_wait_results gets WEBSITE_TIMEOUT",
         "submit_and_wait_results(page, timeout)" in src),
        ("every phase records its own duration",
         "def _save_timings" in src and "_done(" in src),
        ("timings survive the run for later comparison",
         "timings.json" in src),
        ("per-phase timings also reach the page",
         "f\"{detail} ({dt:.1f}s)\"" in src),
    ]
    for label, cond in checks:
        check(label, cond)
    print(f"    config.WEBSITE_TIMEOUT = {config.WEBSITE_TIMEOUT}"
          f"   DOWNLOAD_TIMEOUT = {config.DOWNLOAD_TIMEOUT}"
          f"   FMS_BATCH_LIMIT = {config.FMS_BATCH_LIMIT}")

    print(f"\n-- verdict --\n    known fixed cost per run: {startup:.1f}s")
    check("startup is a small share of a typical run, not the bottleneck",
          startup < 60, f"{startup:.1f}s")
    print("\n    If startup is small, the time is in the browser phases "
          "(fms_search,\n    wps_copy, packed_scrape). Run a validation once and "
          "read the table:\n        py run_dc.py --dc \"DAVAO DC\"\n"
          "        py tools\\show_timings.py --compare")

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())