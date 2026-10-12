"""Packed-scoped copy: stop at the pasted guard set, never ship a short paste.

copy_all_pages() walks FMS batch-search results from page 1. FMS returns ALL
orders for the pasted OIDs, whatever their status (measured: 193 packed OIDs
-> 563 rows over 12 pages), so the walk is now scoped with expect_oids=:

  * stop as soon as every pasted OID has been found (a packed batch sitting in
    the first ~4 pages is copied in ~4 pages, not 12);
  * the packed-batch scope (ceil(packed / per-page)) is a CHECKPOINT, not a
    stop: packed OIDs can genuinely live past it (636 packed -> 751 rows at
    ~1.2 rows/OID had 44 still missing at page 13), so missing OIDs at the
    scope warn loudly and the walk CONTINUES;
  * RAISE if the walk ends short (page turn failed / MAX_PAGES) while pasted
    OIDs are still missing - the 140-vs-193 bug was exactly that: page 5 would
    not turn, the copy warned and carried on, and a short paste reached the
    QR generator unnoticed.

The fake pager below is the one from check_pager_reset.py (9 pages, 50/page)
plus a __blockAfter hook so a page turn can be made to fail deterministically.

Run with:  py tools\\check_packed_scoping.py
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
WINDOW = 5         # wide enough that page 5 is rendered early on

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
  let size = 24, page = 1, busy = false, blockAfter = 0, blockPages = [];
  let staleTotal = 0, staleTurns = 0, turns = 0, attempted = [];
  let chunkDelay = 0;   // test hook: second chunk arrives this many ms late
  let hybridK = 0;      // test hook: top-K new rows + stale tail on first paint
  let prevStart = 0;    // start index of the rows currently on screen
  let constLast = '';   // test hook: a row appended to every page (e.g. footer)
  const oid = i => 'PH' + (100000000000 + i);
  function draw() {
    document.getElementById('total').textContent =
      (staleTotal && turns < staleTurns) ? staleTotal : TOTAL;
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
  function rowHtml(i) {
    return '<tr><td>' + oid(i) + '</td><td>TO20261002T' + i + '</td><td>' + i +
           '</td><td>Forward</td><td>Davao Hub</td><td>-</td><td>No</td>' +
           '<td>SOC_Staging</td><td>View</td></tr>';
  }
  function fill() {
    const start = (page - 1) * size;
    const end = Math.min(start + size, TOTAL);
    const rows = document.getElementById('rows');
    if (hybridK > 0 && start > 0 && hybridK < size) {
      // REAL FMS shape (bug 43): a virtualized table swaps only the TOP chunk
      // on the first tick; the bottom of the table still shows the PREVIOUS
      // page's rows, so the row COUNT is already full even though the content
      // is half stale. The throttled rest replaces the whole table later.
      const k = Math.min(hybridK, end - start);
      let first = '';
      for (let i = start; i < start + k; i++)
        first += rowHtml(i);
      // The untouched BOTTOM of the table still shows the previous page's
      // rows in place, so the LAST row is the previous page's last row — that
      // is exactly what a "did it really turn?" check must notice.
      for (let i = prevStart + k; i < Math.min(prevStart + size, TOTAL); i++)
        first += rowHtml(i);
      rows.innerHTML = first;
      setTimeout(() => {
        let full = '';
        for (let i = start; i < end; i++)
          full += rowHtml(i);
        rows.innerHTML = full;
      }, chunkDelay);
    } else {
      // bug-34 shape: first ~24 rows instantly, the rest after the throttle.
      const split = chunkDelay > 0 ? Math.min(start + 24, end) : end;
      let h = '';
      for (let i = start; i < split; i++)
        h += rowHtml(i);
      rows.innerHTML = h;
      if (split < end) {
        const a = split, b = end;
        setTimeout(() => {
          let h2 = '';
          for (let i = a; i < b; i++)
            h2 += rowHtml(i);
          document.getElementById('rows').innerHTML += h2;
        }, chunkDelay);
      }
    }
    if (constLast)
      document.getElementById('rows').innerHTML += constLast;
    document.getElementById('selval').textContent = size + ' Artikulo / Pahina';
    document.querySelectorAll('#opts li').forEach(o => {
      o.className = 'ssc-option' +
        (o.textContent.startsWith(size + ' ') ? ' ssc-option-selected' : '');
    });
  }
  function go(n) {
    attempted.push(n);                         // test hook: phantom-page probe
    if (busy) return;
    const pages = Math.ceil(TOTAL / size);
    if (n < 1 || n > pages) return;
    if (blockAfter && n > blockAfter) return;   // test hook: freeze the pager
    if (blockPages.includes(n)) return;        // test hook: freeze one page
    busy = true;
    setTimeout(() => { prevStart = (page - 1) * size;
                       if (n !== page) turns++;
                       page = n; draw(); fill(); busy = false; }, LAT);
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
      setTimeout(() => { size = v; page = 1; prevStart = 0;
                         draw(); fill(); busy = false; }, LAT);
    };
  });
  draw(); fill();
  window.__blockAfter = v => { blockAfter = v; };
  window.__blockPages = arr => { blockPages = arr || []; };
  window.__chunkDelay = ms => { chunkDelay = ms || 0; };
  window.__hybridChunk = k => { hybridK = k || 0; };
  window.__constLast = html => { constLast = html || ''; };
  window.__jumpTo = n => { prevStart = (page - 1) * size;
                           page = n; draw(); fill(); };
  window.__attempted = () => attempted.slice();
  window.__resetAttempted = () => { attempted = []; };
  window.__staleTotal = (v, afterTurns) => {
    staleTotal = v || 0; staleTurns = afterTurns || 0; turns = 0;
    draw(); fill();
  };
</script></body></html>"""

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def oid_at(i: int) -> str:
    return "PH" + str(100000000000 + i)


def row_index(row) -> int:
    return int(row["SPX Tracking Number"][3:]) - 100000000000


def main() -> int:
    html = (HTML.replace("__TOTAL__", str(TOTAL))
                .replace("__LAT__", str(LATENCY_MS))
                .replace("__WINDOW__", str(WINDOW)))
    # The fake turns pages in ~120 ms; no need for the 9 s live-page budget.
    # NOTE: TURN_WAIT is in SECONDS (default 9.0). Earlier drafts of this tool
    # set it to 2000 — i.e. 2000 s — so a blocked page genuinely waited half
    # an hour before giving up. Use floats here.
    ps.TURN_WAIT = 2.0
    ps.POLL_MS = 50

    with sync_playwright() as p:
        b = p.chromium.launch().new_page()
        b.set_content(html)
        b.wait_for_timeout(300)
        ps.set_page_size_50(b)

        print("\n-- A: a packed subset in pages 1-4 must stop the walk early --")
        expect_a = [oid_at(i) for i in range(193)]
        rows_a, pages_a = ps.copy_all_pages(b, expect_oids=expect_a)
        got_a = {r["SPX Tracking Number"] for r in rows_a}
        check("stopped after 4 pages, not all 9",
              pages_a == 4, f"pages used = {pages_a}")
        check("every pasted OID was found", set(expect_a) <= got_a,
              f"{len(set(expect_a) - got_a)} missing")
        check("copied only rows from the pages it walked", pages_a == 4,
              f"rows={len(rows_a)}, first={sorted(row_index(r) for r in rows_a)[:3]}")

        print("\n-- B: a page turn that fails mid-walk with OIDs missing must RAISE --")
        b.evaluate("() => window.__blockAfter(4)")   # page 5 turns refuse to land
        expect_b = [oid_at(i) for i in range(210)]   # 10 rows live on page 5
        try:
            ps.copy_all_pages(b, expect_oids=expect_b)
            check("truncated walk raised RuntimeError", False, "no exception!")
        except RuntimeError as e:
            check("truncated walk raised RuntimeError", True)
            check("error names the shortfall",
                  "Copy INCOMPLETE" in str(e) and "10 of 210" in str(e),
                  str(e).splitlines()[0][:110])
        b.evaluate("() => window.__blockAfter(0)")

        print("\n-- C: complete walk with the full set still succeeds --")
        rows_c, pages_c = ps.copy_all_pages(b, expect_oids=[oid_at(i) for i in range(TOTAL)])
        check("copied all 450 rows", len(rows_c) == TOTAL, f"got {len(rows_c)}")
        check("walked all 9 pages", pages_c == 9, f"pages used = {pages_c}")

        print("\n-- D: an OID that never appears must WARN after a full walk, not raise --")
        expect_d = [oid_at(i) for i in range(193)] + ["PH999999999999"]
        rows_d, pages_d = ps.copy_all_pages(b, expect_oids=expect_d)
        got_d = {r["SPX Tracking Number"] for r in rows_d}
        check("walked all 9 pages (the scope is a checkpoint, not a stop)",
              pages_d == 9, f"pages used = {pages_d}")
        check("returns every row the pager reports",
              len(rows_d) == TOTAL, f"got {len(rows_d)}")
        check("phantom OID is not silently invented",
              "PH999999999999" not in got_d)

        print("\n-- G: packed OIDs living PAST the scope keep the walk going --")
        expect_g = [oid_at(i) for i in range(200, 260)]  # pages 5-6 of 9
        rows_g, pages_g = ps.copy_all_pages(b, expect_oids=expect_g)
        got_g = {r["SPX Tracking Number"] for r in rows_g}
        check("did not stop at the 2-page scope",
              pages_g > 2, f"pages used = {pages_g}")
        check("stopped once every packed OID was found (page 6)",
              pages_g == 6, f"pages used = {pages_g}")
        check("every packed OID was found", set(expect_g) <= got_g,
              f"{len(set(expect_g) - got_g)} missing")

        print("\n-- F: 2250 all-status rows for 257 packed -> 6 pages, not 45 --")
        html2 = (HTML.replace("__TOTAL__", "2250")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b2 = p.chromium.launch().new_page()
        b2.set_content(html2)
        b2.wait_for_timeout(300)
        ps.set_page_size_50(b2)
        expect_f = [oid_at(i) for i in range(257)]
        rows_f, pages_f = ps.copy_all_pages(b2, expect_oids=expect_f)
        got_f = {r["SPX Tracking Number"] for r in rows_f}
        check("stopped at the 6-page packed scope, not 45",
              pages_f == 6, f"pages used = {pages_f}")
        check("every packed OID was found", set(expect_f) <= got_f,
              f"{len(set(expect_f) - got_f)} missing")
        check("did not walk the whole result set",
              pages_f < 45, f"pages used = {pages_f}")
        b2.close()

        print("\n-- J: throttled two-chunk pages are read WHOLE, not as chunk 1 --")
        html3 = (HTML.replace("__TOTAL__", "450")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b3 = p.chromium.launch().new_page()
        b3.set_content(html3)
        b3.wait_for_timeout(300)
        ps.set_page_size_50(b3)
        # Second chunk lands 2.5 s after the first: the old 1400 ms budget
        # recorded ~24-row pages here (bug 39); the uniform budget must wait.
        b3.evaluate("() => window.__chunkDelay(2500)")
        rows_j, pages_j = ps.copy_all_pages(
            b3, expect_oids=[oid_at(i) for i in range(TOTAL)])
        check("all 450 rows read despite throttled chunks",
              len(rows_j) == TOTAL, f"got {len(rows_j)}")
        check("walked all 9 pages", pages_j == 9, f"pages used = {pages_j}")
        b3.close()

        print("\n-- K: chunks SLOWER than the settle are recovered by revisit --")
        html4 = (HTML.replace("__TOTAL__", "100")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b4 = p.chromium.launch().new_page()
        b4.set_content(html4)
        b4.wait_for_timeout(300)
        ps.set_page_size_50(b4)
        # Second chunk lands 5 s after the first: the 3 s settle records
        # SHORT pages and moves on; the end revisit (9 s budget) recovers.
        b4.evaluate("() => window.__chunkDelay(5000)")
        expect_k = [oid_at(i) for i in range(100)]
        rows_k, pages_k = ps.copy_all_pages(b4, expect_oids=expect_k)
        got_k = {r["SPX Tracking Number"] for r in rows_k}
        check("all 100 rows recovered via revisit", len(rows_k) == 100,
              f"got {len(rows_k)}")
        check("every packed OID was found", set(expect_k) <= got_k,
              f"{len(set(expect_k) - got_k)} missing")
        b4.close()

        print("\n-- L: half-rendered (chimera) pages must not lose their tail --")
        html5 = (HTML.replace("__TOTAL__", "450")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b5 = p.chromium.launch().new_page()
        b5.set_content(html5)
        b5.wait_for_timeout(300)
        ps.set_page_size_50(b5)
        # REAL FMS shape (bug 43): each cold turn paints the top ~24 NEW rows
        # but keeps the PREVIOUS page's rows underneath until the throttled
        # chunk lands — the table still holds 50 rows, so the row COUNT looks
        # complete. The old "fingerprint changed" test accepted that chimera
        # (live: page 2 = 5 new + 45 stale) and silently dropped the tail,
        # which is the "page 2 copies only half" bug. Needs the full turn
        # budget so the second chunk can land inside one attempt.
        ps.TURN_WAIT = 5.0
        b5.evaluate("() => { window.__hybridChunk(24); window.__chunkDelay(2000); }")
        rows_l, pages_l = ps.copy_all_pages(
            b5, expect_oids=[oid_at(i) for i in range(TOTAL)])
        got_l = {r["SPX Tracking Number"] for r in rows_l}
        check("chimera tails were read (no rows lost)", len(rows_l) == TOTAL,
              f"got {len(rows_l)}")
        check("every row present exactly once", len(got_l) == TOTAL,
              f"distinct={len(got_l)}")
        check("walked all 9 pages", pages_l == 9, f"pages used = {pages_l}")
        b5.close()
        ps.TURN_WAIT = 2.0

        print("\n-- M: a constant last row (footer) must not stall a turn --")
        html6 = (HTML.replace("__TOTAL__", "450")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b6 = p.chromium.launch().new_page()
        b6.set_content(html6)
        b6.wait_for_timeout(300)
        ps.set_page_size_50(b6)
        # The table ends with a row that is IDENTICAL on every page. An earlier
        # fix gated "did the turn finish?" on the last row moving and HUNG here
        # (live: the walk never got past page 1). The overlap test ignores a
        # single constant row, so the walk must still finish.
        # An all-empty trailing row: FMS-style tbody filler. Its first two
        # cells are blank on EVERY page, so the abandoned last-row turn test
        # saw an unchanged edge and hung. The overlap test drops blank rows.
        footer = "<tr>" + "<td></td>" * 9 + "</tr>"
        b6.evaluate("(html) => window.__constLast(html)", footer)
        ps.TURN_WAIT = 2.0
        t_m = time.time()
        rows_m, pages_m = ps.copy_all_pages(
            b6, expect_oids=[oid_at(i) for i in range(TOTAL)])
        got_m = {r["SPX Tracking Number"] for r in rows_m}
        check("every data row read past a constant footer",
              {oid_at(i) for i in range(TOTAL)} <= got_m,
              f"missing {TOTAL - len(got_m)}")
        check("walked all 9 pages with a constant footer", pages_m == 9,
              f"pages used = {pages_m}")
        check("did not stall on the constant last row",
              time.time() - t_m < 30, f"took {time.time() - t_m:.1f}s")
        b6.close()

        print("\n-- H: one stuck page is SKIPPED and the hunt continues --")
        b.evaluate("() => window.__blockPages([5])")  # page 5 never lands
        expect_h = [oid_at(i) for i in range(250, 260)]  # all live on page 6
        rows_h, pages_h = ps.copy_all_pages(b, expect_oids=expect_h)
        got_h = {r["SPX Tracking Number"] for r in rows_h}
        check("walked past the stuck page to page 6 (5 pages read)",
              pages_h == 5, f"pages read = {pages_h}")
        check("every packed OID was found anyway", set(expect_h) <= got_h,
              f"{len(set(expect_h) - got_h)} missing")
        b.evaluate("() => { window.__blockPages([]); window.__jumpTo(1); }")

        print("\n-- I: missing OIDs on a skipped page must RAISE naming it --")
        b.evaluate("() => window.__blockPages([5])")
        expect_i = [oid_at(i) for i in range(200, 250)]  # all live on page 5
        try:
            ps.copy_all_pages(b, expect_oids=expect_i)
            check("skipped-page shortfall raised RuntimeError", False,
                  "no exception!")
        except RuntimeError as e:
            check("skipped-page shortfall raised RuntimeError", True)
            check("error names the skipped page",
                  "Copy INCOMPLETE" in str(e) and "[5]" in str(e),
                  str(e).splitlines()[0][:120])
        b.evaluate("() => { window.__blockPages([]); window.__jumpTo(1); }")

        print("\n-- E: no expect_oids -> previous all-pages behavior --")
        rows_e, pages_e = ps.copy_all_pages(b)
        check("copied 450 rows without scoping", len(rows_e) == TOTAL,
              f"got {len(rows_e)}")
        check("left the pager on page 1",
              ps._active_page(b) == 1, f"active={ps._active_page(b)}")

        print("\n-- N: a stale first total must self-correct, never chase "
              "phantom pages (bug 48) --")
        # Live failure: 95 pasted OIDs, but the pager footer still showed the
        # PREVIOUS search's total (254) when the walk read it, so the walk
        # planned 6 pages, flagged the real last page "SHORT 45/50", probed a
        # page 3 that does not exist, and only reconciled to the truth ~12 s
        # later (43 s total). Reproduce: 95 real rows (2 pages) + 27 phantoms
        # that never appear, with the footer lying (500) until the first turn.
        html7 = (HTML.replace("__TOTAL__", "95")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b7 = p.chromium.launch().new_page()
        b7.set_content(html7)
        b7.wait_for_timeout(300)
        ps.set_page_size_50(b7)
        b7.evaluate(
            "() => { window.__resetAttempted(); window.__staleTotal(500, 1); }")
        expect_n = [oid_at(i) for i in range(95)] + \
                   ["PH8%011d" % i for i in range(27)]
        raised = None
        try:
            rows_n, pages_n = ps.copy_all_pages(b7, expect_oids=expect_n)
        except RuntimeError as e:
            rows_n, pages_n, raised = [], 0, e
        got_n = {r["SPX Tracking Number"] for r in rows_n}
        attempted_n = b7.evaluate("() => window.__attempted()")
        check("stale total did not abort the walk", raised is None,
              str(raised).splitlines()[0][:100] if raised else "")
        check("read both real pages and stopped", pages_n == 2,
              f"pages used = {pages_n}")
        check("never probed a page the result set does not have",
              (max(attempted_n) if attempted_n else 0) <= 2,
              f"turned/attempted {attempted_n}")
        check("every real row read",
              {oid_at(i) for i in range(95)} <= got_n,
              f"{95 - len(got_n)} distinct rows")
        check("the 27 never-appeared OIDs were not invented",
              not any(o.startswith("PH8") for o in got_n))
        b7.close()

        print("\n-- O: a paste that reads MORE rows than the pager total must "
              "RAISE (bug 50) --")
        # Live failure: a 63-row packed search copied 96 distinct OIDs because
        # the walk started against the PREVIOUS search's table. Here the footer
        # understates (20) while 95 real rows exist, so the walk reads 50 rows;
        # 50 > 20 must abort, never paste strangers.
        html8 = (HTML.replace("__TOTAL__", "95")
                     .replace("__LAT__", str(LATENCY_MS))
                     .replace("__WINDOW__", str(WINDOW)))
        b8 = p.chromium.launch().new_page()
        b8.set_content(html8)
        b8.wait_for_timeout(300)
        ps.set_page_size_50(b8)
        b8.evaluate("() => window.__staleTotal(20, 999999)")
        raised8 = None
        try:
            ps.copy_all_pages(b8, expect_oids=[oid_at(i) for i in range(95)])
        except RuntimeError as e:
            raised8 = e
        check("over-read raised RuntimeError", raised8 is not None)
        check("error names the over-run",
              raised8 is not None and "OVERRAN" in str(raised8)
              and "20" in str(raised8),
              str(raised8).splitlines()[0][:110] if raised8 else "")
        b8.close()

        b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())