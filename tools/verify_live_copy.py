"""End-to-end check of the real copy path on the LIVE FMS results table.

Runs set_page_size_50() + copy_all_pages() exactly as run_packed_step() does,
and reports the timing. Nothing is searched, exported or submitted; it only
pages the results table that is already on screen.

Run with:  py tools\\verify_live_copy.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import CDP
from lib import tabs
import lib.packed_scrape as ps


def main() -> int:
    tabs.begin_session()
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pages = [q for q in ctx.pages if "orderTracking" in (q.url or "")]
        if not pages:
            print("No FMS tab open.")
            return 1
        pg = pages[0]
        pg.bring_to_front()

        if not ps._read_table_stable(pg):
            print("No batch-search results on screen - run a search first.")
            return 1

        total = ps._table_total(pg)
        print(f"\nFMS reports Total {total}.")

        t0 = time.time()
        set_ok = ps.set_page_size_50(pg)
        size = ps._page_size(pg)
        npages = -(-total // size) if total else 0
        print(f"page size {size}/page ({npages} pages) "
              f"in {time.time() - t0:.1f}s  set_page_size_50={set_ok}")

        t0 = time.time()
        rows, pages_used = ps.copy_all_pages(pg)
        dt = time.time() - t0

        print(f"\nRESULT: {len(rows)} rows from {pages_used} pages in {dt:.1f}s")
        if npages:
            print(f"        {(dt / npages):.2f}s per page turn")
        if total:
            print(f"        expected {total} rows -> "
                  f"{'ALL PRESENT' if len(rows) >= total else 'MISSING ' + str(total - len(rows))}")
        oids = [r["SPX Tracking Number"] for r in rows]
        print(f"        distinct OIDs: {len(set(oids))} / {len(oids)}")
        with_to = sum(1 for r in rows if r["Current TO Number"] not in ("", "-"))
        print(f"        rows with a TO: {with_to}")

        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())