"""Which element does FMS actually scroll, and what leaves it at the bottom?

Nothing in this project calls scroll / mouse.wheel / scroll_into_view. So the
page movement comes from Playwright, which scrolls a target into view before
clicking it. The open question is WHICH element scrolls: if FMS keeps the
scrolling on an inner container rather than the document, then window.scrollY
never moves even though the view jumps -- which is exactly what "scrolls up and
down but ends up at the bottom" looks like.

Read-only. Uses locator.scroll_into_view_if_needed(), which performs the same
scrolling a click would, WITHOUT clicking anything.
Run with:  py tools\\probe_scroll.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import CDP
from lib import tabs

# Every element on the page that can actually scroll, plus the window.
SCROLLERS_JS = r"""() => {
    const out = [];
    const walk = document.querySelectorAll('body *');
    for (const e of walk) {
        const cs = getComputedStyle(e);
        const canY = /auto|scroll|overlay/.test(cs.overflowY);
        const canX = /auto|scroll|overlay/.test(cs.overflowX);
        if (!canY && !canX) continue;
        const dy = e.scrollHeight - e.clientHeight;
        const dx = e.scrollWidth - e.clientWidth;
        if (dy <= 4 && dx <= 4) continue;
        out.push({
            tag: e.tagName,
            cls: (String(e.className) || '').slice(0, 60),
            id: e.id || '',
            overflowY: cs.overflowY,
            scrollH: e.scrollHeight, clientH: e.clientHeight,
            top: e.scrollTop, left: e.scrollLeft,
        });
        if (out.length >= 8) break;
    }
    return out;
}"""

TOPS_JS = r"""() => {
    const out = { windowY: Math.round(window.scrollY),
                  windowX: Math.round(window.scrollX),
                  docH: document.documentElement.scrollHeight,
                  winH: window.innerHeight };
    const walk = document.querySelectorAll('body *');
    const rows = [];
    for (const e of walk) {
        const cs = getComputedStyle(e);
        if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
        if (e.scrollHeight - e.clientHeight <= 4) continue;
        rows.push({ key: (e.tagName + '.' + String(e.className || '')).slice(0, 52),
                    top: e.scrollTop,
                    max: e.scrollHeight - e.clientHeight });
        if (rows.length >= 8) break;
    }
    out.scrollers = rows;
    return out;
}"""

# Where do the buttons the bot clicks live, and is each one in view?
# Matched by exact text in JS -- "text=..." is a Playwright selector and is
# NOT valid CSS, so querySelector() rejects it.
LOCS_JS = r"""() => {
    const exact = t => [...document.querySelectorAll('button, a, span, div, li')]
        .filter(e => e.children.length === 0
                  && (e.textContent || '').trim() === t
                  && e.getBoundingClientRect().width > 0);

    const specs = [
        { label: 'Batch Search', el: exact('Batch Search')[0] },
        { label: 'Export',        el: exact('Export')[0] },
        { label: 'Submit',        el: exact('Submit')[0] },
        { label: 'pager next',    el: document.querySelector('.ssc-pagination .pager-next') },
        { label: 'page-size sel', el: document.querySelector('.ssc-select-single-value') },
        { label: 'last table row',el: document.querySelector('tbody tr:last-child') },
    ];

    return specs.map(s => {
        if (!s.el) return { label: s.label, found: false };
        const r = s.el.getBoundingClientRect();
        // Walk up to the nearest ancestor that actually scrolls.
        let sc = s.el.parentElement, chain = null;
        while (sc && sc !== document.body) {
            const cs = getComputedStyle(sc);
            if (/auto|scroll|overlay/.test(cs.overflowY)
                && sc.scrollHeight - sc.clientHeight > 4) {
                chain = { key: (sc.tagName + '.' + String(sc.className || '')).slice(0, 48),
                          top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight,
                          clientH: sc.clientHeight };
                break;
            }
            sc = sc.parentElement;
        }
        return {
            label: s.label, found: true,
            viewportY: Math.round(r.top),
            inView: r.top >= 0 && r.bottom <= window.innerHeight,
            inScrollerView: chain
                ? (r.top >= 0 && r.bottom <= chain.clientH)
                : null,
            scroller: chain,
        };
    });
}"""

SPECS = []


def show(pg, when):
    t = pg.evaluate(TOPS_JS)
    sc = t.pop("scrollers")
    head = (f"  [{when}] window.scrollY={t['windowY']} "
            f"doc={t['docH']}px viewport={t['winH']}px")
    print(head)
    for r in sc:
        print(f"        {r['key']:54s} scrollTop={r['top']:6d} / {r['max']}")
    if not sc:
        print("        (nothing scrolls)")


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

        print("Scrollable elements on the page:")
        for s in pg.evaluate(SCROLLERS_JS):
            print(f"   <{s['tag']} class=\"{s['cls']}\" id=\"{s['id']}\"> "
                  f"overflowY={s['overflowY']} content={s['scrollH']}px "
                  f"visible={s['clientH']}px top={s['top']}")

        print("\nWhere the buttons the bot clicks actually sit:")
        for r in pg.evaluate(LOCS_JS):
            if not r["found"]:
                print(f"   {r['label']:15s} NOT FOUND")
                continue
            sc = r["scroller"]
            print(f"   {r['label']:15s} viewportY={r['viewportY']:6d} "
                  f"inView={str(r['inView']):5s} inScrollerView={str(r['inScrollerView']):5s}"
                  + (f"  scrolls {sc['key']} (top={sc['top']}/{sc['max']}, "
                     f"window {sc['clientH']}px)" if sc else "  no scroller"))

        print("\n--- now. as the page is right now ---")
        show(pg, "current")

        # This is exactly what .click() does before it clicks, with no click.
        print("\n--- scrolling Export into view (what a click does) ---")
        try:
            pg.locator("button:has-text('Export')").first.scroll_into_view_if_needed(
                timeout=5000)
            show(pg, "after Export scrollIntoView")
        except Exception as e:
            print("   Export scroll failed:", str(e).splitlines()[0][:70])

        print("\n--- scrolling the pager into view (what paging does) ---")
        try:
            pg.locator(".ssc-pagination .pager-next").first.scroll_into_view_if_needed(
                timeout=5000)
            show(pg, "after pager scrollIntoView")
        except Exception as e:
            print("   pager scroll failed:", str(e).splitlines()[0][:70])

        print("\n--- and back to Export ---")
        try:
            pg.locator("button:has-text('Export')").first.scroll_into_view_if_needed(
                timeout=5000)
            show(pg, "after Export scrollIntoView again")
        except Exception as e:
            print("   Export scroll failed:", str(e).splitlines()[0][:70])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())