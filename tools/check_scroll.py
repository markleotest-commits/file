"""FMS scroll-position guard: the Export button must survive a scrolled page.

Why this exists (measured live, tools/probe_scroll.py):
  FMS does NOT scroll the document. window.scrollY is always 0 and the document
  height equals the viewport height. Everything that moves is an inner
  container -- with a full results grid,
  div.ssc-layout-item.ssc-layout-item-direction-bottom carries ~2297 px of
  travel and the pager / page-size control sit at viewportY ~3280.

  Nothing in this project calls scroll or mouse.wheel, so every jump the user
  sees is Playwright scrolling a target into view before a click. Paging the
  results grid (phase 8) clicks controls ~2297 px down and used to leave the
  container pinned at the bottom.

This builds a page with that same shape -- a scrolling content container, the
Export button at the top of it, a pager at the bottom -- and asserts that
export_reach() describes the truth and scroll_fms_to_top() recovers.
Run with:  py tools\\check_scroll.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

import fms_batch_export as f

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; height: 100%; overflow: hidden; }
  /* the content area scrolls; the document itself cannot */
  #content { height: 200px; overflow-y: auto; }
  button { padding: 4px 10px; }
  #tall { height: 1800px; }
  #pager { position: absolute; top: 1700px; }
</style></head><body>
  <div id="content">
    <div class="ssc-layout-item ssc-layout-item-direction-bottom">
      <button class="ssc-button batch-actions-btn" id="exp">Export</button>
      <div id="tall"></div>
      <div id="pager"><span class="pager-next pager-step">NEXT</span></div>
    </div>
  </div>
</body></html>"""

SCROLL_BOTTOM = r"""() => {
    const list = [];
    for (const e of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(e);
        if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
        if (e.scrollHeight - e.clientHeight <= 4) continue;
        list.push(e);
    }
    for (const e of list) e.scrollTop = e.scrollHeight - e.clientHeight;
    return list.length;
}"""

SCROLL_TALLY = r"""() => {
    let n = 0;
    for (const e of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(e);
        if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
        if (e.scrollTop > 0) n++;
    }
    if (window.scrollY) n++;
    return n;
}"""

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch().new_page()
        b.set_content(HTML)
        b.wait_for_timeout(200)

        print("\n-- the document itself must not be the scroller --")
        check("window.scrollY is 0 on a tall page",
              b.evaluate("() => window.scrollY") == 0)
        check("there is at least one inner scroller",
              b.evaluate(SCROLL_TALLY) >= 0
              and b.evaluate("() => document.getElementById('content').scrollHeight"
                             " > document.getElementById('content').clientHeight"))

        print("\n-- Export at the top of the content --")
        r = f.export_reach(b)
        check("export_reach() finds the button", r.get("found") is True, str(r)[:70])
        check("Export is on screen", r.get("onScreen") is True,
              f"y={r.get('y')} winH={r.get('winH')}")
        check("Export is not covered", r.get("covered") is False,
              f"topmost={r.get('topmost')}")
        check("nothing was scrolled yet", f.scroll_fms_to_top(b) == 0)

        print("\n-- pin everything to the bottom, as phase 8 paging does --")
        n = b.evaluate(SCROLL_BOTTOM)
        check("the page is now scrolled", b.evaluate(SCROLL_TALLY) > 0,
              f"{n} scroller(s)")

        print("\n-- scroll_fms_to_top() recovers --")
        moved = f.scroll_fms_to_top(b)
        check("it reports what it reset", moved > 0, f"moved={moved}")
        check("nothing is left scrolled", b.evaluate(SCROLL_TALLY) == 0)
        r = f.export_reach(b)
        check("Export is on screen again", r.get("onScreen") is True,
              f"y={r.get('y')} winH={r.get('winH')}")
        check("Export is clickable again", r.get("covered") is False,
              f"topmost={r.get('topmost')}")

        print("\n-- a short window must be reported honestly, not silently --")
        b.set_viewport_size({"width": 900, "height": 120})
        b.wait_for_timeout(150)
        r = f.export_reach(b)
        check("export_reach() still returns the geometry", r.get("found") is True)
        check("off-screen is detected as off-screen", "onScreen" in r,
              f"onScreen={r.get('onScreen')} y={r.get('y')} winH={r.get('winH')}")

        print("\n-- a missing button must report found=False, not crash --")
        b.evaluate("() => document.getElementById('exp').remove()")
        r = f.export_reach(b)
        check("export_reach() reports found=False", r.get("found") is False, str(r)[:60])

        b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())