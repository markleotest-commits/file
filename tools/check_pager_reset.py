"""A copy must NEVER start from whatever page the pager was left on.

Reported symptom: after one SOC-packed session finished, the next session
"just copies the last row instead of starting from 1".

Root cause was two bugs stacked:

  1. copy_all_pages() walked forward from wherever the pager happened to be, and
     on failing to reach page 1 it did this:

         if not landed and n == 1:
             cli.warn("Could not confirm page 1 - copying the current page as
                      page 1.")
             data = _read_table_stable(page)      # <-- silently accepted it
             landed = bool(data)

     So the last page's handful of rows was recorded as "page 1" and the run
     reported success.

  2. _goto_page() could only go FORWARD, and only when the target number was
     rendered. The pager renders a window of ~5 numbers, so from the last page
     the number 1 is often not on screen and there was no way home at all --
     there was no previous-page clicker.

Fixed by goto_first_page() (number jump, else step back with .pager-prev),
called before the walk and again at the end, and by making a failure to reach
page 1 raise instead of proceeding.

The fake pager here has a 3-wide window over 9 pages, so from page 9 the number
1 is genuinely not rendered -- the exact situation that used to silently copy
one page.
Run with:  py tools\\check_pager_reset.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

import lib.packed_scrape as ps

TOTAL = 450        # 9 pages at 50/page
LATENCY_MS = 120
WINDOW = 3         # narrow enough that page 1 is NOT rendered from page 9

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  body { font: 12px sans-serif; margin: 6px; }
  table { border-collapse: collapse; }
  th, td { border: 1px solid #ccc; padding: 1px 3px; font-size: 11px; }
  .ssc-pagination { margin-top: 8px; display: flex; gap: 6px; align-items: center; }
  ul.pager { display: flex; list-style: none; gap: 3px; padding: 0; margin: 0; }
  li.pager-item { padding: 1px 5px; border: 1px solid #999; cursor: pointer; }
  li.pager-item.active { background: #333; color: #fff; }
  li.ssc-option { padding: 1px 5px; cursor: pointer; }
  li.ssc-option.ssc-option-selected { font-weight: 700; }
  .pager-step { padding: 1px 5px; border: 1px solid #999; cursor: pointer; }
  .pager-step-disabled { opacity: .4; }
</style></head><body>
  <div id="toolbar">Batch Search <span>2</span></div>
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
  function draw() {
    document.getElementById('total').textContent = TOTAL;
    const pages = Math.ceil(TOTAL / size);
    const a = Math.max(1, Math.min(page - 1, pages - WINDOW + 1));
    const bb = Math.min(pages, a + WINDOW - 1);
    let h = '';
    for (let n = a; n <= bb; n++)
      h += '<li class="pager-item' + (n === page ? ' active' : '') + '">' + n + '</li>';
    document.getElementById('pager').innerHTML = h;
    document.querySelectorAll('#pager li').forEach(li => {
      li.onclick = () => go(parseInt(li.textContent, 10));
    });
    const nx = document.getElementById('next');
    nx.className = 'pager-next pager-step' + (page >= pages ? ' pager-step-disabled' : '');
    nx.onclick = () => go(page + 1);
    const pv = document.getElementById('prev');
    pv.className = 'pager-prev pager-step' + (page <= 1 ? ' pager-step-disabled' : '');
    pv.onclick = () => go(page - 1);
  }
  function fill() {
    const start = (page - 1) * size;
    let h = '';
    for (let i = start; i < Math.min(start + size, TOTAL); i++)
      h += '<tr><td>' + oid(i) + '</td><td>TO20261002T' + i + '</td><td>' + i +
           '</td><td>Forward</td><td>Davao Hub</td><td>-</td><td>No</td>' +
           '<td>SOC_Staging</td><td>View</td></tr>';
    document.getElementById('rows').innerHTML = h;
    document.getElementById('selval').textContent = size + ' Artikulo / Pahina';
    document.querySelectorAll('#opts li').forEach(o => {
      o.className = 'ssc-option' +
        (o.textContent.startsWith(size + ' ') ? ' ssc-option-selected' : '');
    });
  }
  function go(n) {
    if (busy) return;
    const pages = Math.ceil(TOTAL / size);
    if (n < 1 || n > pages) return;
    busy = true;
    setTimeout(() => { page = n; draw(); fill(); busy = false; }, LAT);
  }
  document.getElementById('selval').onclick = () => {
    const o = document.getElementById('opts');
    o.style.display = o.style.display === 'none' ? 'block' : 'none';
  };
  document.querySelectorAll('#opts li').forEach(o => {
    o.onclick = () => {
      const v = parseInt(o.textContent, 10);
      document.getElementById('opts').style.display = 'none';
      busy = true;
      setTimeout(() => { size = v; page = 1; draw(); fill(); busy = false; }, LAT);
    };
  });
  draw(); fill();
  // test hook: jump straight to a page without going through the UI
  window.__jumpTo = n => { page = n; draw(); fill(); };
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

        print("\n-- setup: 50/page, then leave the pager on the LAST page --")
        ps.set_page_size_50(b)
        npages = -(-TOTAL // ps._page_size(b))
        b.evaluate(f"() => window.__jumpTo({npages})")
        b.wait_for_timeout(200)
        check("pager starts on the last page", ps._active_page(b) == npages,
              f"active={ps._active_page(b)} of {npages}")

        rendered = b.evaluate(
            "() => [...document.querySelectorAll('.ssc-pagination .pager-item')]"
            ".map(e => (e.textContent||'').trim())")
        check("page 1 is NOT rendered from here (the trap)",
              "1" not in rendered, f"rendered numbers = {rendered}")

        print("\n-- what the OLD fallback would have recorded --")
        # Old code, on failing to reach page 1, did:
        #     data = _read_table_stable(page)   # accepted whatever was shown
        # Replay exactly that here and show it is the LAST page's rows.
        d = ps._read_table_stable(b)
        shown = [r[0] for r in d["rows"]] if d and d.get("rows") else []
        last_oids = {"PH" + str(100000000000 + i) for i in range(TOTAL - 50, TOTAL)}
        first_oids = {"PH" + str(100000000000 + i) for i in range(50)}
        check("the old fallback would have read the LAST page's rows",
              bool(shown) and set(shown) == last_oids,
              f"{len(shown)} rows, first={shown[0] if shown else None}")
        check("none of page 1's rows were visible from here",
              not (set(shown) & first_oids),
              "so the old run reported success having copied 50 of 450 rows")

        print("\n-- goto_first_page() must get home anyway --")
        t0 = time.time()
        at = ps.goto_first_page(b)
        dt = time.time() - t0
        check("it returned to page 1", at == 1, f"ended on page {at} in {dt:.1f}s")

        print("\n-- and a full copy must get every row from a last-page start --")
        b.evaluate(f"() => window.__jumpTo({npages})")
        b.wait_for_timeout(200)
        check("reset to the last page again", ps._active_page(b) == npages)

        rows, pages = ps.copy_all_pages(b)
        check(f"copied all {TOTAL} rows", len(rows) == TOTAL, f"got {len(rows)}")
        check("all OIDs distinct",
              len({r["SPX Tracking Number"] for r in rows}) == len(rows))
        check("the FIRST result is present (proves it started at page 1)",
              any(r["SPX Tracking Number"] == "PH100000000000" for r in rows))
        check("the LAST result is present (proves it reached the end)",
              any(r["SPX Tracking Number"] == "PH" + str(100000000000 + TOTAL - 1)
                  for r in rows))
        check("it leaves the pager on page 1 for the next run",
              ps._active_page(b) == 1, f"ended on page {ps._active_page(b)}")

        print("\n-- a 1-page result must not need any of this --")
        b.evaluate("() => { window.__jumpTo(1); }")
        b.evaluate("() => { document.getElementById('total').textContent = '30'; }")
        rows2, _ = ps.copy_all_pages(b)
        check("single page still works", len(rows2) > 0, f"got {len(rows2)}")

        b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())