"""Benchmark lib/packed_scrape.py's copy path against the LIVE FMS results table.

Read-only apart from clicking the pager's page numbers; returns to page 1.
Nothing is searched, exported or submitted.

Run with:  py tools\\bench_copy.py
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


def timeit(label, fn, n=3):
    fn()
    t = time.time()
    for _ in range(n):
        fn()
    dt = (time.time() - t) / n
    print(f"  {label:44s} {dt * 1000:8.0f} ms")
    return dt


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

        data = ps._read_table_stable(pg)
        if not data or not data["rows"]:
            print("No batch-search results on the page - run a search first.")
            return 1

        total = ps._table_total(pg)
        size = ps._page_size(pg)
        npages = -(-total // size) if total else 0
        print()
        print(f"Live results: {total} rows at {size}/page -> {npages} pages, "
              f"{len(data['rows'])} rows rendered now, "
              f"{len(data['headers'])} columns")

        print("\nREADS")
        timeit("_table_js()  (innerText, every cell)",
               lambda: pg.evaluate(ps._table_js()))
        timeit("_fingerprint()  (textContent, 4 cells)",
               lambda: ps._fingerprint(pg))
        timeit("_active_page()  (getComputedStyle scan)",
               lambda: ps._active_page(pg))
        timeit("_read_table_stable()", lambda: ps._read_table_stable(pg))

        print("\nPAGE TURNS")
        t0 = time.time()
        landed, d = ps._goto_page(pg, 2)
        t2 = time.time() - t0
        print(f"  page 1 -> 2                                   {t2 * 1000:8.0f} ms"
              f"  (landed={landed})")

        t0 = time.time()
        landed, d = ps._goto_page(pg, 3)
        t3 = time.time() - t0
        print(f"  page 2 -> 3                                   {t3 * 1000:8.0f} ms"
              f"  (landed={landed})")

        ps._goto_page(pg, 1)   # leave the user where they were
        print()
        print(f"  Extrapolated copy of {total} rows ({npages} pages): "
              f"~{(t2 + t3) / 2 * npages:.0f} s of pure paging, "
              f"excluding the FMS search itself.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())