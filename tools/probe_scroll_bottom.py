"""Does being left scrolled at the bottom actually break the Export click?

Measured so far (tools/probe_export.py): the Export button is ALWAYS present,
exactly one get_by_role match, visible, at every stage of a batch search. Its
y moves a lot though -- 4, then 628, then 652 -- because the toolbar moves in
and out of the sticky header as the layout settles.

So the failure is probably not "Export is missing", it is "Export is off-screen
and the scroll Playwright performs to reach it does not work". This forces the
scrolling container to its maximum, then hit-tests Export exactly the way the
browser does when a click is dispatched at an element's centre point.
Run with:  py tools\\probe_scroll_bottom.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import CDP
from lib import tabs

STATE = r"""() => {
    const out = { windowY: Math.round(window.scrollY),
                  winH: window.innerHeight, winW: window.innerWidth,
                  scrollers: [] };
    for (const e of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(e);
        if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
        const max = e.scrollHeight - e.clientHeight;
        if (max <= 4) continue;
        out.scrollers.push({
            key: (e.tagName + '.' + String(e.className || '')).slice(0, 50),
            top: Math.round(e.scrollTop), max: Math.round(max),
            clientH: e.clientHeight,
            rectTop: Math.round(e.getBoundingClientRect().top),
        });
        if (out.scrollers.length >= 6) break;
    }
    const btn = document.querySelector('button.batch-actions-btn')
             || [...document.querySelectorAll('button')]
                  .find(b => (b.textContent || '').trim() === 'Export');
    if (!btn) { out.export = null; return out; }
    const r = btn.getBoundingClientRect();
    out.export = {
        y: Math.round(r.y), bottom: Math.round(r.bottom),
        x: Math.round(r.x), right: Math.round(r.right),
        h: Math.round(r.height), w: Math.round(r.width),
        onScreen: r.top >= 0 && r.bottom <= window.innerHeight
                  && r.left >= 0 && r.right <= window.innerWidth,
    };
    if (out.export.w > 0 && out.export.h > 0) {
        const cx = out.export.x + out.export.w / 2;
        const cy = out.export.y + out.export.h / 2;
        const hit = document.elementFromPoint(cx, cy);
        out.export.centrePoint = [Math.round(cx), Math.round(cy)];
        out.export.topmost = hit
            ? hit.tagName + '.' + String(hit.className || '').slice(0, 34) : null;
        out.export.centreInsideButton = !!(hit && (hit === btn
            || btn.contains(hit) || hit.contains(btn)));
        out.export.elementFromPointNull = hit === null;
    }
    return out;
}"""

SCROLL_TO = r"""(o) => {
    const list = [];
    for (const e of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(e);
        if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
        if (e.scrollHeight - e.clientHeight <= 4) continue;
        list.push(e);
        if (list.length >= 6) break;
    }
    const e = list[o.idx];
    if (!e) return null;
    const max = e.scrollHeight - e.clientHeight;
    e.scrollTop = Math.round(max * o.frac);
    return { key: (e.tagName + '.' + String(e.className || '')).slice(0, 50),
             set: e.scrollTop, max: Math.round(max) };
}"""

# Forces the tall layout a big batch produces, so the scroll behaviour can be
# tested without running a 250-OID search. The spacer is removed at the end.
ADD_TALL = r"""() => {
    const host = document.querySelector('.ssc-layout-item.ssc-layout-item-direction-bottom')
              || document.body;
    const sp = document.createElement('div');
    sp.id = '__tall';
    sp.style.height = '2600px';
    sp.style.background = 'linear-gradient(#eee,#ddd)';
    host.appendChild(sp);
    return { host: host.tagName + '.' + String(host.className || '').slice(0, 44) };
}"""
REMOVE_TALL = "() => { const s = document.getElementById('__tall'); if (s) s.remove(); return true; }"


def show(pg, when):
    s = pg.evaluate(STATE)
    print(f"\n[{when}]  window.scrollY={s['windowY']}  "
          f"viewport={s['winW']}x{s['winH']}")
    for c in s["scrollers"]:
        print(f"    scroller {c['key']:46s} top={c['top']:5d}/{c['max']:5d} "
              f"window={c['clientH']}px  itsTop={c['rectTop']}")
    e = s["export"]
    if not e:
        print("    Export: NOT IN DOM")
        return
    print(f"    Export  y={e['y']:6d} bottom={e['bottom']:6d} x={e['x']:5d} "
          f"onScreen={e['onScreen']}")
    if "centrePoint" in e:
        print(f"            centre={e['centrePoint']} "
              f"topmost={e['topmost']}  "
              f"clickable={e['centreInsideButton']}  "
              f"nullHit={e['elementFromPointNull']}")


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

        show(pg, "as the page is now")

        print("\n--- forcing the tall layout a 250-row batch produces ---")
        print("   host:", pg.evaluate(ADD_TALL))
        pg.wait_for_timeout(400)
        show(pg, "with 2600px of extra content")

        for i in (0, 1):
            r = pg.evaluate(SCROLL_TO, {"idx": i, "frac": 1.0})
            if not r:
                continue
            print(f"\nforced {r['key']} -> scrollTop={r['set']} of {r['max']}")
            show(pg, f"scroller #{i} pinned to the BOTTOM")

        print("\n--- Playwright scroll_into_view_if_needed on Export ---")
        try:
            pg.get_by_role("button", name="Export").first.scroll_into_view_if_needed(
                timeout=5000)
            print("    ok")
        except Exception as e:
            print("    FAILED:", str(e).splitlines()[0][:80])
        show(pg, "after Playwright tried to scroll Export into view")

        print("\n--- removing the spacer ---")
        pg.evaluate(REMOVE_TALL)
        pg.wait_for_timeout(300)
        show(pg, "restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())