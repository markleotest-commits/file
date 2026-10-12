"""Why does the FMS results table show 24 rows/page when the control says 50?

The last run: "Pager set to 50/page (dropdown)" then "Pager analysis: 693
row(s) at 50/page -> 14 page(s)" — but the pages actually rendered ~24 rows
(82 rows read over 3 pages), so the walk stopped at "page 4" far short.

Two candidate causes:
  1. set_page_size_50() changed the size control but the CURRENT result set
     was already paginated at 24 by the time the search finished — the new
     size only applies to the NEXT search.
  2. The rows stream into the table progressively (24 first, then the rest)
     and _read_table_stable() accepts the page after the first chunk.

Read-only snapshot first; --turn then clicks "next" and samples the row
count over ~6 s to watch the table grow (or not):

    py tools\\probe_sizes.py          # snapshot only
    py tools\\probe_sizes.py --turn   # click next, sample row growth

Deliberately NO tabs.begin_session() (that hung before).
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fms_batch_export import CDP  # noqa: E402

SNAPSHOT_JS = r"""() => {
    const out = { sel: [], opts: [], totals: [], pagerNums: [], rows: 0 };
    // every size control on the page: selected value + each option w/ state
    document.querySelectorAll('.ssc-select, [class*="select"]').forEach(s => {
        const sel = s.querySelector('.ssc-select-single-value, [class*="selected"]');
        out.sel.push((sel ? sel.textContent : '').trim().slice(0, 40));
        [...s.querySelectorAll('li, .ssc-option')].forEach(o => {
            if (o.children.length === 0)
                out.opts.push(((o.textContent || '').trim().slice(0, 40) +
                    (/(selected|active)/i.test(o.className) ? ' [SELECTED]' : '')));
        });
    });
    document.querySelectorAll('[class*="total"]').forEach(e => {
        const t = (e.textContent || '').trim();
        if (/\d/.test(t)) out.totals.push(t.slice(0, 80));
    });
    document.querySelectorAll('.ssc-pagination .pager-item, [class*="pagination"] .pager-item')
        .forEach(e => out.pagerNums.push((e.textContent || '').trim()));
    out.rows = document.querySelectorAll('table tbody tr').length;
    return out;
}"""


def snapshot(pg):
    s = pg.evaluate(SNAPSHOT_JS)
    print("  size control selected value(s):", s["sel"] or "(none)")
    print("  size options                 :", s["opts"] or "(none)")
    print("  totals                       :", s["totals"] or "(none)")
    print("  pager numbers                :", s["pagerNums"] or "(none)")
    print("  tbody rows                   :", s["rows"])
    return s


def main() -> int:
    from playwright.sync_api import sync_playwright

    want_turn = "--turn" in sys.argv
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pages = [q for q in ctx.pages if "orderTracking" in (q.url or "")]
        if not pages:
            print("No FMS tab open (orderTracking url missing).")
            return 1
        pg = pages[0]

        print("== live pager snapshot ==")
        snapshot(pg)

        if not want_turn:
            print("\n(read-only; --turn clicks next and samples row growth)")
            return 0

        print("\n== jumping to page 1, then clicking .pager-next ==")
        pg.evaluate(
            """() => {
                const els = [...document.querySelectorAll(
                    '.ssc-pagination .pager-item, [class*="pagination"] .pager-item')];
                const one = els.find(e => (e.textContent || '').trim() === '1');
                if (one) { one.click(); return true; }
                return false;
            }"""
        )
        pg.wait_for_timeout(1500)
        act = pg.evaluate(
            """() => {
                const e = [...document.querySelectorAll('.pager-item')]
                    .find(x => x.classList.contains('active'));
                return e ? (e.textContent || '').trim() : null;
            }"""
        )
        print(f"  on page {act} after jump")

        pg.evaluate(
            """() => {
                const el = document.querySelector(
                    '.ssc-pagination .pager-next, [class*="pagination"] .pager-next, '
                    + '.pager-next');
                if (el) { el.click(); }
            }"""
        )
        counts = []
        for i in range(60):
            n = pg.evaluate("() => document.querySelectorAll('table tbody tr').length")
            counts.append(n)
            if not i or counts[-1] != counts[-2]:
                print(f"  +{i * 0.1:>4.1f}s  rows={n}")
            if i >= 45 and counts[-1] == counts[-3]:
                break        # stable for ~0.3 s
            pg.wait_for_timeout(100)
        print("  row-count trace (every change):", counts)

        print("\n== snapshot after the turn ==")
        snapshot(pg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())