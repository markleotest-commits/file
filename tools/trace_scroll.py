"""What scrolls FMS during a batch search, and where is the view left?

Measured on the live page (tools/probe_scroll.py):
  * window.scrollY is ALWAYS 0 -- document height == viewport height. The
    document never scrolls. Every movement the user sees comes from inner
    containers:
      - div.ssc-layout-item.ssc-layout-item-direction-bottom : 2297px of scroll
      - an unnamed div                                        : 1308px of scroll
  * the toolbar Export button lives in the STICKY header, so it is always in
    view
  * the pager and the page-size control sit at viewportY ~3280 and need that
    first container scrolled to the bottom

This records every scroll event, on every element, while running the real
paste_batch() + submit_and_wait_results(), then checks whether the Export
button is actually reachable (visible AND not covered) WITHOUT clicking Export
Batch-Search Results -- clicking that would create a real Reception Order task.
Run with:  py tools\\trace_scroll.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import (CDP, paste_batch, submit_and_wait_results)
from lib import tabs
import lib.packed_scrape as ps

RECORDER = r"""() => {
    if (window.__scrollRec) return 'already';
    window.__scrollRec = [];
    window.__scrollT0 = Date.now();
    const key = e => {
        if (e === document || e === window) return 'WINDOW/DOCUMENT';
        const n = e.nodeName || '?';
        let id = '';
        try { id = e.id || ''; } catch (x) {}
        return n + (id ? '#' + id : '') + '.' +
               (String(e.className || '').split(' ').slice(0, 2).join('.')).slice(0, 46);
    };
    const rec = e => {
        try {
            window.__scrollRec.push({
                t: Date.now() - window.__scrollT0,
                el: key(e),
                top: Math.round(e.scrollTop || window.scrollY),
                left: Math.round(e.scrollLeft || window.scrollX),
                max: Math.round((e.scrollHeight || 0) - (e.clientHeight || 0)),
                from: window.__lastTop === undefined ? null : window.__lastTop,
            });
        } catch (x) {}
    };
    // capture:true because scroll does not bubble
    document.addEventListener('scroll', rec, true);
    window.addEventListener('scroll', rec, true);
    window.__scrollRecOn = true;
    return 'on';
}"""

# Where is the sticky header relative to the bottom-scrolled content, and can
# Export actually be clicked right now?
REACH_JS = r"""() => {
    const exact = t => [...document.querySelectorAll('button, a, span, div, li')]
        .filter(e => e.children.length === 0
                  && (e.textContent || '').trim() === t
                  && e.getBoundingClientRect().width > 0);
    const el = exact('Export')[0];
    if (!el) return { found: false };
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    return {
        found: true,
        rect: { x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height) },
        viewportH: window.innerHeight,
        onScreen: r.top >= 0 && r.bottom <= window.innerHeight
                  && r.left >= 0 && r.right <= window.innerWidth,
        topmostAtCentre: hit ? (hit.tagName + '.' + String(hit.className || '')).slice(0, 40)
                             : null,
        clickable: !!(hit && (hit === el || el.contains(hit) || hit.contains(el))),
    };
}"""


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
            print("No batch-search results on screen -- nothing to reuse as input.")
            return 1
        # raw _table_js() rows are lists; index by header name
        heads = [h.strip() for h in data["headers"]]
        try:
            ci = heads.index("SPX Tracking Number")
        except ValueError:
            print("No 'SPX Tracking Number' column; headers =", heads)
            return 1
        oids = [str(r[ci]).strip() for r in data["rows"] if len(r) > ci][:5]
        oids = [o for o in oids if o]
        print(f"Will paste {len(oids)} OIDs from the current results: {oids}")

        print("\nExport reachable BEFORE anything:", pg.evaluate(REACH_JS))

        print("\nrecorder:", pg.evaluate(RECORDER))
        pg.evaluate("() => { window.__scrollRec = []; window.__scrollT0 = Date.now(); }")

        print("\n--- paste_batch ---")
        t0 = time.time()
        paste_batch(pg, oids, 3000)
        print(f"    done in {time.time() - t0:.1f}s")

        print("\n--- submit_and_wait_results ---")
        t0 = time.time()
        submit_and_wait_results(pg, 120)
        print(f"    done in {time.time() - t0:.1f}s")

        evs = pg.evaluate("() => window.__scrollRec || []")
        print(f"\n{len(evs)} scroll event(s) recorded:")
        seen = {}
        for e in evs:
            k = e["el"]
            seen[k] = seen.get(k, 0) + 1
        for k, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            mx = max(e["max"] for e in evs if e["el"] == k)
            last = [e for e in evs if e["el"] == k][-1]
            print(f"   {k:50s} x{n:3d}  max={mx:5d}  ends at top={last['top']}")
        print("\n  first 25 events in order:")
        for e in evs[:25]:
            print(f"   t={e['t']:6d}ms {e['el']:48s} top={e['top']:5d} / {e['max']}")

        print("\nExport reachable AFTER the search:", pg.evaluate(REACH_JS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())