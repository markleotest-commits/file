"""Is the FMS Export button really there after a batch search?

tools/trace_scroll.py reported Export "not found" after submitting -- but that
probe filtered on children.length === 0, which breaks the moment the button
re-renders with an icon child. So this checks with the SAME locators the bot
uses (get_by_role / get_by_text), plus geometry and a hit-test, at each stage
of a real batch search. Nothing clicks Export Batch-Search Results, so no
Reception Order task is ever created.
Run with:  py tools\\probe_export.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import (CDP, _batch_dialog_open, dismiss_fms_popups,
                              paste_batch, submit_and_wait_results)
from lib import tabs
import lib.packed_scrape as ps

HIT_TEST = r"""() => {
    const btns = [...document.querySelectorAll('button')];
    const out = btns.map(b => ({
        text: (b.textContent || '').trim(),
        cls: String(b.className || '').slice(0, 40),
        kids: b.children.length,
        y: Math.round(b.getBoundingClientRect().y),
        h: Math.round(b.getBoundingClientRect().height),
    })).filter(x => /export/i.test(x.text) || /export/i.test(x.cls));
    // any visible element whose trimmed text is exactly Export, at any depth
    const loose = [...document.querySelectorAll('body *')]
        .filter(e => (e.textContent || '').trim() === 'Export'
                  && e.getBoundingClientRect().width > 0)
        .map(e => ({ tag: e.tagName, kids: e.children.length,
                     y: Math.round(e.getBoundingClientRect().y) }));
    return { buttonish: out, exactExport: loose };
}"""


def report(pg, when):
    role = pg.get_by_role("button", name="Export")
    n_role = role.count()
    vis = None
    box = None
    if n_role:
        try:
            vis = role.first.is_visible()
            box = role.first.bounding_box()
        except Exception as e:
            vis = f"ERR {str(e).splitlines()[0][:40]}"
    txt = pg.get_by_text("Export", exact=True).count()
    print(f"\n[{when}]")
    print(f"   get_by_role(button, Export)   count={n_role} visible={vis}")
    print(f"   get_by_text(Export, exact)   count={txt}")
    if box:
        print(f"   bounding_box                 "
              f"x={box['x']:.0f} y={box['y']:.0f} "
              f"w={box['width']:.0f} h={box['height']:.0f}")
    print(f"   Batch Search dialog open     {_batch_dialog_open(pg)}")
    d = pg.evaluate(HIT_TEST)
    print(f"   buttons matching /export/i   {d['buttonish']}")
    print(f"   any element with text Export {d['exactExport']}")
    return n_role, vis


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

        data = ps._read_table_stable(pg)
        if not data or not data["rows"]:
            print("No results on screen to reuse as search input.")
            return 1
        heads = [h.strip() for h in data["headers"]]
        ci = heads.index("SPX Tracking Number")
        oids = [str(r[ci]).strip() for r in data["rows"] if len(r) > ci][:4]
        oids = [o for o in oids if o]
        print(f"Pasting {len(oids)} OIDs: {oids}")

        report(pg, "BEFORE: results on screen")

        dismiss_fms_popups(pg)
        print("\n--- paste_batch ---")
        paste_batch(pg, oids, 3000)
        report(pg, "AFTER paste (dialog open, not submitted)")

        print("\n--- submit_and_wait_results ---")
        submit_and_wait_results(pg, 120)
        report(pg, "IMMEDIATELY after submit")

        print("\n--- waiting 6 s for the results grid to settle ---")
        time.sleep(6)
        report(pg, "6 s after submit")

        # Does scrolling it into view help? (this is what a click does)
        print("\n--- scroll_into_view_if_needed on Export, then re-check ---")
        try:
            pg.get_by_role("button", name="Export").first.scroll_into_view_if_needed(
                timeout=5000)
            print("   scrolled without error")
        except Exception as e:
            print("   scroll failed:", str(e).splitlines()[0][:70])
        report(pg, "AFTER scrolling Export into view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())