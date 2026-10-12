"""Why does page 5 of the FMS results pager refuse to turn?

copy_all_pages() stopped after 4 of 12 pages ("Could not reach page 5 —
stopping at 140 row(s)"), and that truncated copy is what fed the QR
generator: 140 unique OIDs where MC's Packed tab says 193. The stop is
only a warning, so the run carried on and produced a short paste.

Read the pager FIRST (read-only), then optionally attempt the turn:

    py tools\\probe_page5.py            # read-only: pager state + window
    py tools\\probe_page5.py --turn     # actually try _goto_page(5) + report
    py tools\\probe_page5.py --turn 6   # ... for another page

--turn logs every strategy, whether it returned True, and why the
fingerprint/active-page proof rejected it. That distinguishes:
  * the number "5" is not rendered (windowed pager) -> click never happens
  * the click lands but the table never changes     -> server throttle
  * the table changes but _active_page != 5         -> wrong-page proof
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lib.packed_scrape as ps  # noqa: E402
from fms_batch_export import CDP  # noqa: E402
from lib import tabs  # noqa: E402

PAGER_NUMS_JS = r"""() => {
    const out = [];
    const items = [...document.querySelectorAll(
        '.ssc-pagination .pager-item, [class*="pagination"] .pager-item')];
    items.forEach(e => {
        const r = e.getBoundingClientRect();
        out.push({
            text: (e.textContent || '').trim().slice(0, 20),
            cls: String(e.className).slice(0, 60),
            active: e.classList.contains('active'),
            w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.x), y: Math.round(r.y),
            inViewport: r.y > -50 && r.y < (window.innerHeight + 50),
        });
    });
    return out;
}"""


def describe(pg) -> None:
    print(f"  _page_size()     : {ps._page_size(pg)}")
    print(f"  _table_total()   : {ps._table_total(pg)}")
    print(f"  _active_page()   : {ps._active_page(pg)}")
    print(f"  _fingerprint()   : {str(ps._fingerprint(pg))[:70]}")
    rows = pg.evaluate("() => document.querySelectorAll('tbody tr').length")
    print(f"  tbody tr         : {rows}")

    nums = pg.evaluate(PAGER_NUMS_JS)
    if not nums:
        print("  pager items      : NONE FOUND (.pager-item missing)")
        return
    active = [n for n in nums if n["active"]]
    print(f"  pager items      : {[n['text'] for n in nums]}")
    print(f"  active item      : {[n['text'] for n in active] or 'none'}")
    offscreen = [n["text"] for n in nums if not n["inViewport"]]
    if offscreen:
        print(f"  OFF-SCREEN items : {offscreen}")


def main() -> int:
    want_turn = "--turn" in sys.argv
    try:
        target = int(sys.argv[sys.argv.index("--turn") + 1])
    except (ValueError, IndexError):
        target = 5

    tabs.begin_session()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pages = [q for q in ctx.pages if "orderTracking" in (q.url or "")]
        if not pages:
            print("No FMS tab open.")
            return 1
        pg = pages[0]

        print("== pager state ==")
        describe(pg)

        # Is the target number even rendered? A windowed pager only shows ~5
        # numbers, and _click_next_num_pos() cannot click what is not there.
        nums = pg.evaluate(PAGER_NUMS_JS)
        texts = [n["text"] for n in nums]
        if str(target) not in texts:
            print(f"\npage '{target}' is NOT rendered — {texts}")
            print("-> _click_next_num_pos can only succeed via the arrow/"
                  "_click_next_js strategies.")
        if not want_turn:
            print("\n(read-only; run with --turn to attempt the page turn)")
            return 0

        act = ps._active_page(pg)
        print(f"\n== attempting _goto_page({target}) from page {act} ==")
        print(f"next_ok would be: {act is not None and target == act + 1}")
        before = ps._fingerprint(pg)
        t0 = time.time()
        landed, data = ps._goto_page(pg, target)
        took = time.time() - t0
        after = ps._fingerprint(pg)
        print(f"landed={landed}  in {took:.1f}s")
        print(f"  active before -> after : {act} -> {ps._active_page(pg)}")
        print(f"  fingerprint changed    : {before != after}")
        print(f"  rows returned          : {len(data['rows']) if data else None}")
        if not landed:
            print("\n== per-strategy ==")
            strats = {
                "_click_next_js": lambda: ps._click_next_js(pg, target, False),
                "_click_next_pw": lambda: ps._click_next_pw(pg, target),
                "_click_next_arrow_js": lambda: ps._click_next_arrow_js(pg),
                "_click_next_num_pos": lambda: ps._click_next_num_pos(pg, target),
            }
            for name, fn in strats.items():
                t1 = time.time()
                try:
                    okv = fn()
                except Exception as e:
                    print(f"  {name:24s} raised {e}")
                    continue
                print(f"  {name:24s} -> {okv}  [{time.time()-t1:.2f}s]")
                if okv:
                    t2 = time.time()
                    changed = None
                    while (time.time() - t2) < ps.TURN_WAIT:
                        fp = ps._fingerprint(pg)
                        if fp and fp != before:
                            changed = True
                            break
                        pg.wait_for_timeout(ps.POLL_MS)
                    if not changed:
                        changed = False
                    print(f"  {'':24s} table changed: {changed} "
                          f"active now: {ps._active_page(pg)}")
                    if changed:
                        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
