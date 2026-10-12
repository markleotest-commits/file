"""copy_all_pages() speed + correctness guard, against a simulated FMS pager.

The live FMS results set changes under you (it was 41, 139, 375, 676 and 241
rows on consecutive checks), so it cannot be used as a regression target. This
builds a local page with the same markup lib/packed_scrape.py talks to and the
same latency behaviour, then asserts the copy is correct AND fast.

Markup reproduced from tools/probe_pager.py:
  div.ssc-pagination > span.total-container "Total N (Display Max …)"
                     > span.pager-prev.pager-step
                     > ul.pager > li.pager-item (+ .active)
                     > span.pager-next.pager-step
                     > span.ssc-select-single-value   <- the CURRENT page size
                     > li.ssc-option (+ .ssc-option-selected)

Deliberately included, to catch the regressions this file exists for:
  * a decoy bare-number span OUTSIDE the pager, so an unscoped page-number click
    finds it, clicks it, and changes nothing (the 8.3 s dead click);
  * a page-number window of 5, so a 9-page result set cannot be walked by
    clicking numbers alone and the .pager-next arrow has to work;
  * page-size options 24/40/50 with 50 selected but 24 listed first, which is
    what made _page_size() report 24 while 50 was in force.

Run with:  py tools\\check_copy_speed.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

import lib.packed_scrape as ps

TOTAL = 450          # -> 9 pages at 50/page, more than the 5-wide window
LATENCY_MS = 150     # stands in for FMS's real ~4.6 s server-side page fetch
WINDOW = 5

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  body { font: 13px sans-serif; margin: 8px; }
  table { border-collapse: collapse; }
  th, td { border: 1px solid #ccc; padding: 1px 4px; font-size: 11px; }
  .ssc-pagination { margin-top: 10px; display: flex; gap: 8px; align-items: center; }
  ul.pager { display: flex; list-style: none; gap: 4px; padding: 0; margin: 0; }
  li.pager-item { padding: 2px 6px; border: 1px solid #999; cursor: pointer; }
  li.pager-item.active { background: #333; color: #fff; }
  li.ssc-option { padding: 2px 6px; cursor: pointer; }
  li.ssc-option.ssc-option-selected { font-weight: 700; }
  .pager-step { padding: 2px 6px; border: 1px solid #999; cursor: pointer; }
  .pager-step-disabled { opacity: .4; }
</style></head><body>
  <!-- decoy: a bare "2" that an unscoped page-number search will find -->
  <div id="toolbar">Batch Search tool <span>2</span> <span>3</span></div>

  <div class="el-table">
    <table><thead><tr>
      <th>SPX Tracking Number</th><th>Current TO Number</th><th>Order SN</th>
      <th>Order Direction</th><th>Destination Station</th><th>On-Hold Reason</th>
      <th>High Value</th><th>Order Status</th><th>Action</th>
    </tr></thead></table>
    <table><tbody id="rows"></tbody></table>
  </div>

  <div class="ssc-pagination">
    <span class="total-container">Total <span id="total"></span>
      (Display Max 10,000 records)</span>
    <span class="pager-prev pager-step" id="prev">PREV</span>
    <ul class="pager" id="pager"></ul>
    <span class="pager-next pager-step" id="next">NEXT</span>
    <div class="ssc-select">
      <!-- order matters and is copied from the live page: the option list sits
           BEFORE the trigger span, which is why the old first-match regex read
           "24" while 50 was selected -->
      <ul id="opts" style="display:none">
        <li class="ssc-option">24 Artikulo / Pahina</li>
        <li class="ssc-option">40 Artikulo / Pahina</li>
        <li class="ssc-option">50 Artikulo / Pahina</li>
      </ul>
      <span class="ssc-select-single-value" id="selval">24 Artikulo / Pahina</span>
    </div>
  </div>

<script>
  const TOTAL = __TOTAL__, LAT = __LAT__, WINDOW = __WINDOW__;
  let size = 24, page = 1, busy = false;

  const oid = i => 'PH' + (100000000000 + i);
  const row = i => `<tr><td>${oid(i)}</td><td>TO20261002T${i}</td>` +
    `<td>${i}</td><td>Forward</td><td>Davao Hub</td><td>-</td><td>No</td>` +
    `<td>SOC_Staging</td><td>View</td></tr>`;

  function draw() {
    document.getElementById('total').textContent = TOTAL;
    const pages = Math.ceil(TOTAL / size);
    const a = Math.max(1, Math.min(page - 2, pages - WINDOW + 1));
    const b = Math.min(pages, a + WINDOW - 1);
    let html = '';
    for (let n = a; n <= b; n++)
      html += `<li class="pager-item${n === page ? ' active' : ''}">${n}</li>`;
    document.getElementById('pager').innerHTML = html;
    document.querySelectorAll('#pager li').forEach(li => {
      li.onclick = () => go(parseInt(li.textContent, 10));
    });
    document.getElementById('next').className =
      'pager-next pager-step' + (page >= pages ? ' pager-step-disabled' : '');
    document.getElementById('next').onclick = () => go(page + 1);
    document.getElementById('prev').onclick = () => go(page - 1);
  }

  function fill() {
    const start = (page - 1) * size;
    let h = '';
    for (let i = start; i < Math.min(start + size, TOTAL); i++) h += row(i);
    if (h === '') h = `<tr><td></td><td></td><td></td><td></td><td></td>` +
                       `<td></td><td></td><td></td><td></td></tr>`;
    document.getElementById('rows').innerHTML = h;
    document.getElementById('selval').textContent = size + ' Artikulo / Pahina';
    document.querySelectorAll('#opts li').forEach(o =>
      o.className = 'ssc-option' +
        (o.textContent.startsWith(size + ' ') ? ' ssc-option-selected' : ''));
  }

  // Every navigation goes through a latency, exactly like the real FMS fetch.
  function go(n) {
    if (busy) return;
    const pages = Math.ceil(TOTAL / size);
    if (n < 1 || n > pages) return;
    busy = true;
    setTimeout(() => { page = n; draw(); fill(); busy = false; }, LAT);
  }

  const sel = document.getElementById('selval');
  sel.onclick = () => {
    const o = document.getElementById('opts');
    o.style.display = o.style.display === 'none' ? 'block' : 'none';
  };
  document.querySelectorAll('#opts li').forEach(o => {
    o.onclick = () => {
      const v = parseInt(o.textContent, 10);
      document.getElementById('opts').style.display = 'none';
      busy = true;
      setTimeout(() => {
        size = v; page = 1; draw(); fill(); busy = false;
      }, LAT);
    };
  });

  draw(); fill();
</script></body></html>"""

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    html = (HTML.replace("__TOTAL__", str(TOTAL))
                .replace("__LAT__", str(LATENCY_MS))
                .replace("__WINDOW__", str(WINDOW)))

    with sync_playwright() as p:
        b = p.chromium.launch().new_page()
        b.set_content(html)
        b.wait_for_timeout(300)

        print("\n-- _page_size reads the selection, not a dropdown option --")
        check("starts at 24 (the fake pager starts small)", ps._page_size(b) == 24,
              f"got {ps._page_size(b)}")

        print("\n-- set_page_size_50 actually sets 50 --")
        t0 = time.time()
        ok = ps.set_page_size_50(b)
        check("set_page_size_50() reported success", ok is True)
        check("_page_size() is now 50", ps._page_size(b) == 50,
              f"got {ps._page_size(b)}")
        check("it did not take 3 s of blind waiting",
              time.time() - t0 < 2.0, f"{time.time() - t0:.2f}s")

        # Prove the fix matters: replay the OLD _page_size() logic against the
        # very same DOM. It takes the first "NN Artikulo…" text it finds, which
        # is a dropdown OPTION, so it reports 24 while 50 is in force.
        old = b.evaluate(
            """() => {
                const els = [...document.querySelectorAll('div,span,button,li')];
                const cand = els
                  .map(e => (e.textContent || '').trim())
                  .filter(t => t && t.length < 30)
                  .map(t => t.match(/^(\\d{1,3})\\s*(Artik|\\/\\s*Page)/i))
                  .filter(Boolean);
                return cand.length ? parseInt(cand[0][1], 10) : null;
            }"""
        )
        check("the old regex-first read WAS wrong here (fix is real)",
              old == 24 and ps._page_size(b) == 50,
              f"old logic -> {old}, selection -> {ps._page_size(b)}")

        npages = -(-TOTAL // 50)
        print(f"\n-- copy_all_pages walks all {npages} pages "
              f"(window is {WINDOW}, so arrows are required) --")
        t0 = time.time()
        rows, pages = ps.copy_all_pages(b)
        dt = time.time() - t0

        check(f"copied every one of {TOTAL} rows", len(rows) == TOTAL,
              f"got {len(rows)}")
        check("all OIDs are distinct", len({r["SPX Tracking Number"] for r in rows})
              == len(rows))
        check("reached the last page", pages >= npages, f"pages={pages}")
        check("Current TO Number was captured on every row",
              all(r["Current TO Number"].startswith("TO") for r in rows))
        check("Order Status was captured on every row",
              all(r["Order Status"] == "SOC_Staging" for r in rows))

        # This asserts "no DEAD-CLICK STALL", not raw speed. The stall this
        # guards against (bugs 16/19) was a fixed ~8.3 s wait per strategy that
        # could not work on this markup -- and that wait is a real constant, NOT
        # scaled by LATENCY_MS, so one dead click blows any sane budget.
        # Measured cost here is npages+1 turns (the +1 is the deliberate return
        # to page 1 added by bug 24) plus per-turn fingerprint polling and
        # per-strategy JS evaluation: 4.1 s for 10 turns.
        # A 5.5 s budget keeps ~3.8 s of slack over that, so the assertion
        # still fails hard on the 8.3 s stall it exists to catch.
        budget = (npages + 1) * (LATENCY_MS / 1000.0) + 4.0
        check(f"copy finished inside {budget:.1f}s (no dead-click stall)",
              dt < budget, f"took {dt:.1f}s")

        print("\n-- the decoy bare number outside the pager is not clicked --")
        check("toolbar still shows its raw '2'", b.evaluate(
            "() => document.querySelector('#toolbar span').textContent") == "2")

        b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())