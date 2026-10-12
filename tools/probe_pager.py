"""What does the FMS pager's page-size control actually look like?

_page_size() currently regex-matches /^(NN) (Artikulo|\\/ Page)/ over every
short element and takes the FIRST hit — which is a dropdown OPTION, not the
current selection. That is why it reports 24 when 50 rows are on screen.

Print the pager subtree so the fix is based on the real DOM, not a guess.
Read-only.
Run with:  py tools\\probe_pager.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from fms_batch_export import CDP
from lib import tabs

PAGER_JS = r"""() => {
    // Locate the pager: the element containing the page numbers / page-size ctl
    let ctl = [...document.querySelectorAll('div,span,button,li')]
        .find(e => /^\d{1,4}\s*(Artikulo|Pahina|\/\s*Page)/i.test((e.textContent || '').trim())
                  && e.children.length <= 2);
    if (!ctl) return { error: 'pager control not found' };

    // climb to the pagination container
    let node = ctl;
    for (let i = 0; i < 8 && node.parentElement; i++) {
        node = node.parentElement;
        if (/pag/i.test(String(node.className))) break;
    }

    const leaves = [...node.querySelectorAll('li,span,div,button,a,select,option')]
        .filter(e => e.children.length === 0)
        .map(e => ({
            tag: e.tagName,
            text: (e.textContent || '').trim().slice(0, 26),
            cls: String(e.className || '').slice(0, 40),
            sel: e.getAttribute('aria-selected') || e.getAttribute('selected') || '',
            val: e.getAttribute('value') || '',
            disp: getComputedStyle(e).display,
            vis: getComputedStyle(e).visibility,
        }))
        .filter(x => x.text && x.disp !== 'none');

    return {
        containerCls: String(node.className).slice(0, 80),
        containerTag: node.tagName,
        leaves: leaves.slice(0, 40),
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

        n = pg.evaluate("() => document.querySelectorAll('tbody tr').length")
        print(f"tbody tr currently: {n}   (_page_size() reports "
              f"{__import__('lib.packed_scrape', fromlist=['x'])._page_size(pg)})")

        r = pg.evaluate(PAGER_JS)
        if r.get("error"):
            print("  " + r["error"])
            return 1
        print(f"\npager container: <{r['containerTag']} class=\"{r['containerCls']}\">")
        print(f"{'tag':7s} {'text':28s} {'class':42s} {'sel':6s} {'value':8s} disp")
        for x in r["leaves"]:
            print(f"{x['tag']:7s} {x['text']:28s} {x['cls']:42s} {x['sel']:6s} "
                  f"{x['val']:8s} {x['disp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())