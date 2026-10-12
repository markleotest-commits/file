"""Which click strategy actually turns the FMS pager, and how fast?

Prints the pager's own markup, then tries each strategy in turn and reports
whether the table fingerprint actually moved. Returns to the starting page.
Read-only apart from paging.
Run with:  py tools\\probe_strategies.py
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

ITEMS_JS = r"""() => [...document.querySelectorAll('.ssc-pagination li, .ssc-pagination .pager-item')]
    .map(e => ({ tag: e.tagName, cls: String(e.className),
                 text: (e.textContent || '').trim() }))"""

PAGER_HTML = r"""() => {
    const p = document.querySelector('.ssc-pagination');
    return p ? p.outerHTML.slice(0, 1600) : null;
}"""

STRATS = [
    ("num_pos  (scoped)", lambda pg, n: ps._click_next_num_pos(pg, n)),
    ("next_js  (by number)", lambda pg, n: ps._click_next_js(pg, n, False)),
    ("next_pw  (locator)", lambda pg, n: ps._click_next_pw(pg, n)),
    ("next_sel (.btn-next)", lambda pg, n: ps._click_next_sel(pg)),
    ("next_pos dx=12", lambda pg, n: ps._click_next_pos(pg, 12)),
    ("next_pos dx=20", lambda pg, n: ps._click_next_pos(pg, 20)),
    ("next_pos dx=30", lambda pg, n: ps._click_next_pos(pg, 30)),
    ("next_js  (arrow)", lambda pg, n: ps._click_next_js(pg, n, True)),
]


def main() -> int:
    tabs.begin_session()
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pages = [q for q in ctx.pages if "orderTracking" in (q.url or "")]
        pg = pages[0]

        if not ps._read_table_stable(pg):
            print("No results table.")
            return 1

        print("pager items:", pg.evaluate(ITEMS_JS))
        print("\npager HTML:")
        print((pg.evaluate(PAGER_HTML) or "")[:900])

        # Go back to page 1 so the window of numbers is predictable.
        for _ in range(12):
            if ps._active_page(pg) == 1:
                break
            ps._goto_page(pg, 1)
        ps._read_table_stable(pg)
        print("\nstart page:", ps._active_page(pg))

        fp0 = ps._fingerprint(pg)
        for name, fn in STRATS:
            fp_before = ps._fingerprint(pg)
            t0 = time.time()
            try:
                clicked = fn(pg, 2)
            except Exception as e:
                print(f"  {name:24s} EXC {str(e).splitlines()[0][:40]}")
                continue
            t_click = time.time() - t0
            if not clicked:
                print(f"  {name:24s} reported 'not clicked' ({t_click:.2f}s)")
                continue
            moved = None
            while time.time() - t0 < 10:
                if ps._fingerprint(pg) != fp_before:
                    moved = time.time() - t0
                    break
                time.sleep(0.05)
            print(f"  {name:24s} clicked {t_click * 1000:5.0f} ms -> "
                  f"table moved after "
                  f"{('%.2f s' % moved) if moved is not None else 'NEVER (10 s)'}"
                  f"   now page {ps._active_page(pg)}")
            # back to page 1 for the next attempt
            if ps._active_page(pg) != 1:
                ps._goto_page(pg, 1)
                ps._read_table_stable(pg)

        print("\nfinal page:", ps._active_page(pg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())