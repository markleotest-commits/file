"""Phase 8 — SOC Packed -> FMS batch search -> COPY the results table (no export)
-> dock attribution -> WPS review sheet + MC OID+TO QR Generator.

HOW DOCKS ARE TRACED (the answer to "which OID is from what dock"):
  * at phase 1 the bot read pending OIDs PER DAC TAB; refresh_pending_file saved
    that as  pending_tab_map.txt  (OID<TAB>tab). That map IS the dock of each OID.
  * the copied FMS table adds the missing piece: the Current TO Number per OID.
  * Destination Station is only a FALLBACK dock for OIDs missing from the map
    (resolved through MC's hub->DC table).

Pipeline (matches the manual flow, minus the hand-copying):
  batch search -> copy ALL pages of the results table (50/page)
  -> build ONE list where every line is "OID TO dock" (3rd column = dock);
     ALL copied rows with a TO are included (even ones that moved on to
     SOC_LHPacking etc. while searching) — the live status split is only
     printed to the console
  -> single paste into the MC OID+TO QR Generator (MC_V2 groups by the 3rd
     column: dock section headers + subtle per-card badge; plain "OID TO"
     lines and legacy "=== DOCK ===" headers still parse fine too).
  This step writes NO files (no TSV, no WPS sheet, no noto txt) — the QR
  generator paste is the only artifact.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import cli
from lib.cdp import attach as _cdp_attach

CDP = "http://127.0.0.1:9222"

WANTED = [
    "SPX Tracking Number",
    "Current TO Number",
    "Order SN",
    "Order Direction",
    "Destination Station",
    "On-Hold Reason",
    "High Value",
    "Order Status",
]

# Used ONLY for the informational console split (still packed vs moved on).
# The QR paste includes every copied row with a TO, regardless of status.
PACKED_STATUSES = ("SOC_PACKED", "SOC_PACKING", "SOC_STAGING")

MAX_PAGES = 250


# ------------------------------------------------------------------ inputs

def read_packed_oids_file() -> list[str]:
    p = ROOT / "packed_oids.txt"
    if not p.exists():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        t = ln.strip().split()[0] if ln.strip() else ""
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def read_tab_map() -> dict[str, str]:
    """OID -> source DAC tab, written by phase 1 (refresh_pending_file)."""
    p = ROOT / "pending_tab_map.txt"
    m: dict[str, str] = {}
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "\t" in ln:
                a, b = ln.split("\t", 1)
                m[a.strip().upper()] = b.strip()
    return m


# ------------------------------------------------------------------ table copy

def _table_js() -> str:
    """FMS splits the header and body into SEPARATE <table> elements
    (Element-UI style) — find the header table by its column names, then take
    the body rows from the matching body table in the same component."""
    return """() => {
        const tables = [...document.querySelectorAll('table')];
        const thOf = t => [...t.querySelectorAll('thead th, tr:first-child th')];
        const headT = tables.find(t =>
            thOf(t).some(th => th.textContent.includes('SPX Tracking Number')));
        if (!headT) return null;
        const headers = thOf(headT).map(th => th.innerText.trim());
        const withTds = tr => tr.querySelector('td');
        const rootEl = headT.closest('.el-table') ||
                       (headT.parentElement && headT.parentElement.parentElement) ||
                       document.documentElement;
        let bodyRows = [...rootEl.querySelectorAll('tbody tr')].filter(withTds);
        if (!bodyRows.length) {
          // header/body are separate tables: take the next table that has rows
          const after = tables.slice(tables.indexOf(headT) + 1).concat(
                        tables.slice(0, tables.indexOf(headT)));
          for (const t of after) {
            const r = [...t.querySelectorAll('tbody tr')].filter(withTds);
            if (r.length) { bodyRows = r; break; }
          }
        }
        const rows = bodyRows.map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim()));
        return { headers: headers, rows: rows };
    }"""


def _wait_size(page, want: int, ms: int = 4000) -> bool:
    """React the instant the pager control shows `want`/page (early exit)."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        if _page_size(page) == want:
            return True
        page.wait_for_timeout(150)
    return False


def _wait_option_50(page, ms: int = 2000) -> bool:
    """Early-exit wait for the '50' leaf to exist (dropdown actually open)."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        try:
            if page.evaluate(
                "() => [...document.querySelectorAll('li,div,span,option')]"
                ".some(e => e.children.length === 0 && "
                "/^50(\\D|$)/i.test((e.textContent || '').trim()))"
            ):
                return True
        except Exception:
            return False
        page.wait_for_timeout(120)
    return False


def _dismiss_size_menu(page) -> None:
    """Close the '… Artikulo / Pahina' options menu if it is hanging open.

    A leftover open menu sits OVER the pager numbers/arrows (seen live in a
    screenshot: the 24/40/50 list covering the pager while "page 14 did not
    land" retried) and swallows the clicks _goto_page needs — the turn then
    never lands. Escape closes an ssc-select menu without touching anything
    else. Cheap and safe to call before page turns.
    """
    try:
        opened = page.evaluate(
            """() => {
            const opts = [...document.querySelectorAll('li.ssc-option')];
            return opts.some(e => e.offsetParent !== null);
        }""")
        if opened:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
    except Exception:
        pass


def set_page_size_50(page, quiet: bool = False) -> bool:
    """The '… Artikulo / Pahina' pager-size control -> 50 per page.

    FMS markup (measured, tools/probe_pager.py):
        <div class="ssc-pagination">
          <span class="total-container">Total 676 …</span>
          <span class="pager-prev pager-step">…</span>
          <ul class="pager"><li class="pager-item">1</li>
              <li class="pager-item active">2</li></ul>
          <span class="pager-next pager-step">…</span>
          <span class="ssc-select-single-value">50 Artikulo / Pahina</span>
          <ul><li class="ssc-option">24 Artikulo / Pahina</li>
              <li class="ssc-option ssc-option-selected">50 …</li></ul>

    The old version clicked a bare "24 Artikulo / Pahina" span (a dropdown
    OPTION, not the trigger) so the list never opened and 50 was never picked.
    The pager stayed at 24, which doubled the number of page turns -- at ~4.6 s
    a turn that is the single largest cost in the whole copy.

    Clicks the real trigger, picks the real option, then VERIFIES via
    _page_size(); success is only reported when the pager actually says 50.

    NOTE (bug 33): FMS paginates a result set WHEN THE SEARCH RUNS. Changing
    the control after results arrived updates the dropdown for the NEXT search
    but does not re-paginate the current table — the dropdown then lies about
    the live rows. Call this BEFORE the batch search when possible. quiet=True
    is for that pre-search attempt, where the results pager does not exist
    yet and a failure is expected, not worth a warning.
    """
    # 1) real <select>, if this build ever uses one
    try:
        for sel in page.locator("select").all():
            opts = [o.inner_text().strip() for o in sel.locator("option").all()]
            hit = next((o for o in opts if o == "50" or o.startswith("50 ")), None)
            if hit:
                sel.select_option(label=hit)
                if _wait_size(page, 50) or _page_size(page) == 50:
                    cli.ok("Pager set to 50/page (select)")
                    return True
    except Exception:
        pass

    # 2) ssc-select: click the trigger (.ssc-select-single-value), then the
    #    li.ssc-option that starts with "50"
    for attempt in range(2):
        try:
            opened = page.evaluate(
                """() => {
                const trig = document.querySelector(
                    '.ssc-pagination .ssc-select-single-value')
                    || document.querySelector('.ssc-select-single-value')
                    || document.querySelector('[class*="pagination"] [class*="select"]');
                if (!trig) return false;
                trig.click();
                return true;
            }"""
            )
            if not opened:
                break
            _wait_option_50(page)
            picked = page.evaluate(
                """() => {
                const opts = [...document.querySelectorAll(
                    'li.ssc-option, .ssc-dropdown li, [class*="option"]')];
                const el = opts.find(e => /^50(\\D|$)/i.test((e.textContent || '').trim()));
                if (!el) return false;
                el.click();
                return true;
            }"""
            )
            if picked and (_wait_size(page, 50) or _page_size(page) == 50):
                _dismiss_size_menu(page)
                cli.ok("Pager set to 50/page (dropdown)")
                return True
        except Exception:
            pass
        page.wait_for_timeout(200)

    _dismiss_size_menu(page)
    size = _page_size(page)
    if not quiet:
        cli.warn(f"Could not set 50/page — pager still at {size}/page, so the copy "
                 f"will need ~{2}x the page turns.")
    return False


def _table_total(page):
    """Row count reported by the pager, e.g. "Total 289 (Display Max …)"."""
    try:
        return page.evaluate(
            """() => {
            const m = (document.body.innerText || '')
              .match(/Total\\s+([0-9][0-9,]*)\\s*\\(Display/i);
            return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
        }"""
        )
    except Exception:
        return None


def _page_size(page) -> int:
    """Rows per page currently shown by the pager.

    Must read the pager's *selection*, not a dropdown option. The control is
    three option spans ("24 Artikulo / Pahina", "40 ...", "50 ...") plus one
    `.ssc-select-single-value` span holding the current choice. Matching the
    first option-shaped text in the document reported 24 while 50 rows were
    actually on screen, which made copy_all_pages() wait 15x200 ms for a page
    size that was already set and then compute 11 pages instead of 5.
    """
    try:
        v = page.evaluate(
            """() => {
                const sel = document.querySelector('.ssc-select-single-value')
                    || document.querySelector(
                         '[class*="pagination"] [class*="select-single-value"]')
                    || document.querySelector('[class*="select-single-value"]');
                if (sel) {
                    const m = (sel.textContent || '').trim().match(/^(\\d{1,4})/);
                    if (m) return parseInt(m[1], 10);
                }
                return null;
            }"""
        )
    except Exception:
        v = None
    if v:
        return int(v)
    # Fallback for a pager that does not use the ssc-select markup.
    try:
        v = page.evaluate(
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
    except Exception:
        v = None
    return int(v) if v else 50


POLL_MS = 50          # between cheap polls while waiting for the table
TURN_WAIT = 9.0       # longest wait for one page turn to deliver new rows
SETTLE_WAIT = 3.0     # normal re-settle per page: chunk 1 arrives instantly,
                      # so 3 s proves full-or-short fast (bug 42 — the 9 s
                      # mid-walk stall is gone; shorts are re-read at the end)
REVISIT_WAIT = 9.0    # last-chance re-read of short pages: covers the ~4.5 s
                      # throttle with headroom (bug 39's budget lives here now,
                      # where it only ever runs on pages known to be short)


def _fingerprint_js() -> str:
    """Cheap identity for "which page is on screen right now".

    Uses the SAME table-finding logic as _table_js() - it has to select the same
    rows or the comparison is meaningless - but reads only 4 cells with
    textContent (no layout, no innerText) instead of every cell. Measured 4 ms
    vs 15 ms on a 50-row page, and it is what the tight poll loops call.
    """
    return """() => {
        const tables = [...document.querySelectorAll('table')];
        const thOf = t => [...t.querySelectorAll('thead th, tr:first-child th')];
        const headT = tables.find(t =>
            thOf(t).some(th => th.textContent.includes('SPX Tracking Number')));
        if (!headT) return '';
        const withTds = tr => tr.querySelector('td');
        const rootEl = headT.closest('.el-table') ||
                       (headT.parentElement && headT.parentElement.parentElement) ||
                       document.documentElement;
        let bodyRows = [...rootEl.querySelectorAll('tbody tr')].filter(withTds);
        if (!bodyRows.length) {
          const after = tables.slice(tables.indexOf(headT) + 1).concat(
                        tables.slice(0, tables.indexOf(headT)));
          for (const t of after) {
            const r = [...t.querySelectorAll('tbody tr')].filter(withTds);
            if (r.length) { bodyRows = r; break; }
          }
        }
        if (!bodyRows.length) return '';
        const id = tr => {
            const tds = tr.querySelectorAll('td');
            let s = '';
            for (let i = 0; i < 2 && i < tds.length; i++)
                s += '|' + (tds[i].textContent || '').trim().slice(0, 18);
            return s;
        };
        return bodyRows.length + '#' + id(bodyRows[0]) + '#'
             + id(bodyRows[bodyRows.length - 1]);
    }"""


def _fingerprint(page) -> str:
    try:
        return page.evaluate(_fingerprint_js()) or ""
    except Exception:
        return ""


def _rows_key(rows) -> set:
    """Identity set of a page's rows (blank spacer rows dropped).

    Two different FMS pages are DISJOINT slices of one result set, so a real
    page turn shares NO row with the page before it. A half-rendered page
    still shows the previous page's bottom rows outright, so its set overlaps
    the previous page's heavily — that is the whole signal.
    """
    return {tuple(r) for r in rows if r and any(r)}


def _stale_tail(data, prev_rows) -> bool:
    """True when a freshly-read page still carries the previous page's rows.

    FMS swaps only the TOP of a cold table: the bottom keeps the previous
    page's rows verbatim until the ~4.5 s server throttle delivers the rest.
    So the row COUNT already looks full while the content is half stale — a
    chimera. The live log showed page 2 as ``50/50 rows, +5 new`` (45 rows
    were still page 1); that count-full shape slipped past both the settle
    check (``got >= expected``) and the SHORT/revisit check (``got < started``),
    so its rows were never read. Those stale rows are byte-identical to the
    previous page's, hence the overlap test.

    A threshold of ~a fifth of the page ignores a coincidental duplicate or a
    constant non-data/footer row, while real chimeras carry dozens. This does
    NOT look at the table's last row (an earlier attempt did, and HUNG when
    that row did not move on the live table), so an edge/footer row that stays
    put cannot stall a turn.
    """
    if not prev_rows:
        return False
    rows = [tuple(r) for r in data.get("rows", []) if r and any(r)]
    if not rows:
        return False
    dup = sum(1 for r in rows if r in prev_rows)
    return dup >= max(3, len(rows) // 5)


def _read_table_stable(page, tries: int = 10):
    """Read the results table once it has stopped changing.

    Same safety contract as before - never return a half-rendered page - but the
    settling is done on the cheap fingerprint instead of two full 15 ms
    innerText sweeps 300 ms apart. Wait for the fingerprint to repeat, take ONE
    full read, then re-check the fingerprint: if the table moved while we were
    reading it, throw the read away and start over. That is strictly stronger
    than the old "two equal reads", because the expensive read is now proven
    not to straddle a change.
    """
    prev = None
    for _ in range(tries):
        fp = _fingerprint(page)
        if not fp:
            prev = None            # empty / still loading
            page.wait_for_timeout(POLL_MS)
            continue
        if fp != prev:
            prev = fp
            page.wait_for_timeout(POLL_MS)
            continue
        # Fingerprint repeated: read for real.
        data = page.evaluate(_table_js())
        if not data or not data["rows"]:
            prev = None
            page.wait_for_timeout(POLL_MS)
            continue
        if _fingerprint(page) == fp:
            return data             # table held still through the read
        prev = None                # it moved mid-read; go round again
    return page.evaluate(_table_js())


def _sig(data) -> tuple:
    return tuple(tuple(r) for r in data["rows"])


def _click_next_sel(page) -> bool:
    for sel in (
        "button.btn-next:not([disabled])",
        ".btn-next:not([disabled])",
        "li.btn-next:not(.disabled) button",
        "button[aria-label='next']:not([disabled])",
    ):
        loc = page.locator(sel).first
        if loc.count():
            try:
                loc.click(timeout=2000)
                return True
            except Exception:
                continue
    return False


def _click_next_js(page, want: int, arrows_only: bool = False) -> bool:
    return bool(
        page.evaluate(
            """(arg) => {
            const want = String(arg.want), arrowsOnly = arg.arrows;
            const dis = e => e.disabled ||
              /disabled/i.test(String(e.className)) ||
              (e.getAttribute && e.getAttribute('aria-disabled') === 'true');
            const txt = e => (e.textContent || '').trim();
            const els = [...document.querySelectorAll('button, li, a, span, div')];
            let el = null;
            if (!arrowsOnly) {
              el = els.find(e => txt(e) === want && !dis(e) && e.children.length === 0 &&
                e.closest && e.closest('[class*="pag"], [class*="pager"], [class*="pagination"]'));
              if (!el) el = els.find(e => txt(e) === want && !dis(e) &&
                e.children.length === 0 &&
                (e.tagName === 'BUTTON' || e.tagName === 'LI' || e.tagName === 'SPAN'));
            }
            if (!el) el = els.find(e =>
              (txt(e) === '›' || txt(e) === '»' || txt(e) === '>') && !dis(e));
            if (!el) el = els.find(e => /btn-next|next/i.test(String(e.className)) && !dis(e));
            if (!el) return false;
            el.click();
            return true;
        }""",
            {"want": want, "arrows": arrows_only},
        )
    )


def _click_next_pw(page, want: int) -> bool:
    for sel in (f"button:text-is('{want}')", f"li:text-is('{want}')"):
        loc = page.locator(sel).first
        if loc.count():
            try:
                loc.click(timeout=2000)
                return True
            except Exception:
                continue
    return False


def _click_next_arrow_js(page) -> bool:
    """Click the pager's own "next page" arrow (`.pager-next`), via JS click.

    Needed because the pager only renders a window of page numbers (with 676
    rows it showed just 1-5), so clicking "page 8" by number is impossible once
    you are past the window. _click_next_sel() looked for `.btn-next`, which
    this markup does not use, so on a large batch the copy stopped dead at the
    edge of the number window.

    A JS click is used rather than a real mouse click: measured on this pager a
    JS click turned a page in 3.57 s, while page.mouse.click took 7-9 s on the
    same element.
    """
    return bool(
        page.evaluate(
            """() => {
            const dis = e => e.disabled
                || /disabled/i.test(String(e.className))
                || (e.getAttribute && e.getAttribute('aria-disabled') === 'true');
            const cands = [...document.querySelectorAll(
                '.ssc-pagination .pager-next, [class*="pagination"] .pager-next, '
                + '.pager-next, .btn-next')];
            const el = cands.find(e => !dis(e));
            if (!el) return false;
            el.click();
            return true;
        }"""
        )
    )


def _click_prev_arrow_js(page) -> bool:
    """Click the pager's "previous page" arrow (`.pager-prev`), via JS click.

    Needed to walk BACKWARDS. The pager renders only a window of page numbers,
    so once a previous run has left it on the last page, the number 1 may not
    be rendered at all and clicking by number is impossible -- the only way home
    is the previous arrow, one page at a time. Same JS-click rationale as
    _click_next_arrow_js: measured 3.3 s per turn vs 7-9 s for a real mouse
    click.
    """
    return bool(
        page.evaluate(
            """() => {
            const dis = e => e.disabled
                || /disabled/i.test(String(e.className))
                || (e.getAttribute && e.getAttribute('aria-disabled') === 'true');
            const cands = [...document.querySelectorAll(
                '.ssc-pagination .pager-prev, [class*="pagination"] .pager-prev, '
                + '.pager-prev, .btn-prev')];
            const el = cands.find(e => !dis(e));
            if (!el) return false;
            el.click();
            return true;
        }"""
        )
    )


def goto_first_page(page, tries: int = 12) -> int | None:
    """Put the pager back on page 1. Returns the page it ended on.

    MUST be called before copy_all_pages()'s walk. The previous run leaves the
    pager on the LAST page, and a windowed pager may not render the number 1 --
    so without this a new run starts by reading the last page's rows.

    Tries the page number first (one jump when 1 is rendered), then steps back
    with the previous arrow.
    """
    landed, _ = _goto_page(page, 1)
    if landed:
        return _active_page(page)
    for _ in range(tries):
        act = _active_page(page)
        if act is None or act <= 1:
            break
        if not _click_prev_arrow_js(page):
            break
        t0 = time.time()
        before = _fingerprint(page)
        moved = False
        while (time.time() - t0) < TURN_WAIT:
            fp = _fingerprint(page)
            if fp and fp != before:
                moved = True
                break
            page.wait_for_timeout(POLL_MS)
        if not moved:
            break
    return _active_page(page)


def _click_next_num_pos(page, want: int) -> bool:
    """Click the pager's own page-number button with a real mouse click.

    MUST stay scoped to the pager container. Unscoped, this searched every
    button/li/a/span/div in the document for text == str(want), matched an
    unrelated element, and returned True anyway. _goto_page() trusted that and
    waited the full 8 s for the table to change on EVERY page turn - measured
    8.3 s of a 12.4 s page turn, about 66% of the entire copy.
    """
    try:
        pt = page.evaluate(
            """(want) => {
            const txt = e => (e.textContent || '').trim();
            const cands = [...document.querySelectorAll(
                '.ssc-pagination .pager-item, ' +
                '[class*="pagination"] .pager-item, .pager-item')];
            const el = cands.find(e => txt(e) === String(want));
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (!r || !r.width || !r.height) return null;
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        }""",
            want,
        )
    except Exception:
        return False
    if not pt:
        return False
    try:
        page.mouse.click(pt["x"], pt["y"])
    except Exception:
        return False
    return True


def _click_next_pos(page, dx: int) -> bool:
    """Real mouse click on the '>' arrow that sits just LEFT of the
    'NN Artikulo…' page-size control (pager layout: < 1 2 3 > [NN Artikulo…]).
    Works no matter what DOM/tag the arrow is (svg icons have no text)."""
    try:
        ctl = page.get_by_text(re.compile(r"Artikulo|/\s*Page", re.I)).first
        box = ctl.bounding_box(timeout=2000)
    except Exception:
        return False
    if not box:
        return False
    page.mouse.click(box["x"] - dx, box["y"] + box["height"] / 2)
    return True


def _active_page(page):
    """Which page number the pager currently highlights, or None.

    Read the pager's own marker FIRST: `.ssc-pagination .pager-item.active`.
    The old version scanned every bare-number button/li/a/span in the document
    and guessed by font weight or colour, which returned 6 on a 3-page pager --
    enough to make _goto_page() reject a page it had actually reached, or to
    think a click had landed when it had not.

    The page-size options are `li.ssc-option` and the chosen one carries
    `ssc-option-selected`, so they must be excluded or "50" looks selected.
    """
    try:
        v = page.evaluate(
            """() => {
                const el = document.querySelector(
                    '.ssc-pagination .pager-item.active, '
                    + '[class*="pagination"] .pager-item.active');
                if (el) {
                    const m = (el.textContent || '').trim().match(/^(\\d{1,4})$/);
                    if (m) return parseInt(m[1], 10);
                }
                return null;
            }"""
        )
    except Exception:
        v = None
    if v:
        return int(v)
    # Fallback for a pager without the ssc markup. Ignore the page-size options.
    try:
        return page.evaluate(
            """() => {
            const leaves = [...document.querySelectorAll('button, li, a, span')]
              .filter(e => /^\\d{1,4}$/.test((e.textContent || '').trim())
                           && e.children.length === 0
                           && !/option|select/i.test(String(e.className || '')));
            if (!leaves.length) return null;
            const info = leaves.map(e => {
              const st = getComputedStyle(e);
              return { n: parseInt((e.textContent || '').trim(), 10),
                       cls: String(e.className || ''),
                       w: parseInt(st.fontWeight) || 400,
                       c: st.color };
            });
            let a = info.find(i => /active|current|selected/i.test(i.cls));
            if (!a) a = info.find(i => i.w >= 600);
            if (!a && info.length > 1) {
              const cnt = {};
              info.forEach(i => { cnt[i.c] = (cnt[i.c] || 0) + 1; });
              a = info.find(i => cnt[i.c] === 1);
            }
            return a ? a.n : null;
        }"""
        )
    except Exception:
        return None


def _goto_page(page, n: int, prev_sig=None, turn_wait: float | None = None):
    """Land on page n and PROVE it by reading: the table fingerprint must change
    from the page currently on screen, the read must be stable, and the pager's
    highlighted number must equal n.

    Measured on the live FMS pager (see tools/probe_strategies.py):
      * a page turn takes 4.3-4.8 s. That is FMS server-side paging behind an
        `application.request_delay_fms` throttle, so TURN_WAIT is 9 s to leave
        headroom on a slow turn.
      * the table fingerprint is the only reliable "it moved" signal. The
        pager's highlighted number does NOT flip when the click lands - it flips
        when the rows arrive, so it cannot be used to tell a live turn from a
        dead click.
      * strategies that cannot work on this markup (_click_next_sel,
        _click_next_pos) return False in ~60 ms without clicking, so they cost
        nothing. That, plus scoping _click_next_num_pos to the pager, is what
        removed the 8.3 s dead click that used to happen on every page.

    Returns (landed, data).
    """
    act = _active_page(page)
    before_fp = _fingerprint(page)
    if act == n:
        # Already on the wanted number — but a pager flips to n while the
        # bottom of the table can still be page n-1. Wait out that half-render
        # (bounded); hand back whatever we have so the walk can settle it.
        t0 = time.time()
        tw = turn_wait or TURN_WAIT
        while True:
            data = _read_table_stable(page)
            if data and data.get("rows"):
                if not _stale_tail(data, prev_sig):
                    return True, data
                if (time.time() - t0) >= tw:
                    return True, data
            if (time.time() - t0) >= tw:
                break
            page.wait_for_timeout(200)
    next_ok = act is not None and n == act + 1
    # Order matters. Measured on the live pager (tools/trace_goto.py):
    #   _click_next_js by number  -> page turned in 3.57 s   <- fastest
    #   _click_next_pw  locator   -> 4.7 s
    #   _click_next_num_pos       -> 7-9 s, and sometimes not at all
    # so the real-mouse-click strategy goes last, and the arrow clicker is in
    # the list because the number window runs out on large batches.
    strats = [
        lambda: _click_next_js(page, n, False),
        lambda: _click_next_pw(page, n),
    ]
    if next_ok:
        strats += [
            lambda: _click_next_arrow_js(page),
            lambda: _click_next_sel(page),
            lambda: _click_next_pos(page, 12),
            lambda: _click_next_pos(page, 20),
            lambda: _click_next_pos(page, 30),
        ]
    strats.append(lambda: _click_next_num_pos(page, n))
    for strat in strats:
        try:
            if not strat():
                continue
        except Exception:
            continue
        # Page turns load asynchronously - the OLD rows stay on screen until the
        # new page arrives. Poll the cheap fingerprint until the table actually
        # changes; a read taken too early would just see the previous page.
        # turn_wait caps one attempt (bug 40: the walk's single retry passes 3
        # s — a background-console operator rule; attempt 1 keeps the full
        # TURN_WAIT because healthy turns need 3.5 s+).
        t0 = time.time()
        tw = turn_wait or TURN_WAIT
        moved = False
        data = None
        while (time.time() - t0) < tw:
            fp = _fingerprint(page)
            # Must be non-empty: while the table is loading, _fingerprint()
            # returns "" (no rows yet), which is *different* from before_fp and
            # would otherwise read as a successful turn.
            if not fp or fp == before_fp:
                page.wait_for_timeout(POLL_MS)
                continue
            moved = True
            best = _read_table_stable(page)
            if not best or not best.get("rows"):
                page.wait_for_timeout(POLL_MS)
                continue
            data = best
            if not _stale_tail(best, prev_sig):
                break             # a real, whole page — done
            # Half-rendered: the bottom is still the previous page's rows
            # (a count-full chimera). Keep waiting for the throttled rest
            # instead of recording it — this is the "page 2 copies only half"
            # bug. Bounded by tw, so a table that never finishes still hands
            # the read back and the walk's short/revisit path recovers it.
            page.wait_for_timeout(200)
        if not moved or data is None:
            continue  # this click never produced rows — next strategy
        act2 = _active_page(page)
        if act2 is not None and act2 != n:
            continue  # landed on the wrong page
        return True, data
    return False, None


def _settle_page(page, data, expected_rows, budget_s):
    """Re-read until the page holds its expected rows (bounded).

    Returns (data, got). Full pages return at once; cold pages wait up to
    budget_s for the throttled chunk. Callers decide what a shortfall means:
    the main walk notes it and moves on in 3 s (bug 42), the end revisit
    waits it out with REVISIT_WAIT.
    """
    t_read = time.time()
    while expected_rows > 0 and (time.time() - t_read) < budget_s:
        got = sum(1 for c in data["rows"] if c and any(c))
        if got >= expected_rows:
            break
        page.wait_for_timeout(450)
        fresh = _read_table_stable(page)
        if fresh and fresh.get("rows"):
            data = fresh
    got = sum(1 for c in data["rows"] if c and any(c))
    return data, got


def copy_all_pages(page, expect_oids: list[str] | None = None) -> tuple[list[dict], int]:
    """Copy the batch-search results table, page by page. No Export click.

    Walks pages from 1, waits for each page to finish rendering, verifies
    every page turn actually landed (else retries other click strategies),
    and keeps every row no matter what its live Order Status is.

    ``expect_oids`` scopes the walk to the batch that was pasted (the packed
    OIDs). FMS batch search returns ALL orders for those OIDs regardless of
    status — measured: 193 packed OIDs -> 563 rows over 12 pages — so the
    walk stops as soon as every pasted OID has been found (or every row the
    pager reports has been read). A packed batch sitting in the first ~4
    pages is therefore copied in ~4 pages, not 12.

    A walk that stops early (page turn failed) while pasted OIDs are still
    missing is a FAILURE, not a warning: a short paste must never reach the
    QR generator silently. This is what the 140-vs-193 bug was — page 5
    would not turn and the operator only noticed by comparing counts.
    """
    mp: dict[str, int] = {}
    out: list[dict] = []
    seen: set[str] = set()
    pages = 0
    t_start = time.time()
    expected = {str(o).strip().upper() for o in expect_oids} if expect_oids else None
    raw_rows = 0
    truncated = False
    scope_announced = False
    skipped: list[int] = []  # pages that would not turn; walked past, not dropped
    consec_fail = 0  # circuit breaker: 3 stuck pages in a row = dead pager
    pager_dead = False  # set by the breaker; skips the end revisit (no turns)
    short_pages: list[int] = []  # settled short; re-read at the end (bug 42)
    page_rows: dict[int, set] = {}  # each page's rows, to spot a stale tail
    prev_rows: set | None = None    # rows of the page read just before this one

    # the 50/page switch may still be applying when we get here - wait until
    # the pager control actually shows 50 (or give up after ~3 s and use
    # whatever size the site settled on). _page_size() reads the pager's
    # selected value, so this exits at once when 50 is already chosen.
    size = _page_size(page)
    for _ in range(15):
        if size == 50:
            break
        page.wait_for_timeout(200)
        size = _page_size(page)
    total = _table_total(page)
    last = -(-total // size) if total else None  # ceil(total / size)
    # Real rows-per-page, learned from page 1 when the dropdown lies (bug 33).
    # Seeded from the control; the per-iteration line below must NOT reset it
    # once page 1 has taught us better (it used to wipe the learn every page).
    eff_size = size if size and size > 1 else 0
    if total:
        msg = (f"Pager analysis: {total} row(s) at {size}/page -> {last} "
               f"page(s). Copying pages 1..{last} in order, every row, "
               f"whatever its status.")
        if expected and size and size > 1:
            scope = -(-len(expected) // size)
            if scope < last:
                msg += (f" Packed-batch scope: {len(expected)} pasted OID(s) "
                        f"-> a normal batch ends around page {scope}; past "
                        f"that, missing OIDs keep the walk going.")
        cli.info(msg)
    else:
        cli.info("Pager total not readable — copying page by page until the "
                 "pager stops turning.")
    # The pager is left on the LAST page by the previous run. A windowed pager
    # may not render the number 1 at all, so going forward from there is
    # impossible and the walk would begin by reading the last page. Reset first.
    start_page = _active_page(page)
    if start_page is not None and start_page > 1:
        cli.info(f"Pager is on page {start_page} (left over from a previous "
                 f"run) — returning to page 1 before copying.")
        at = goto_first_page(page)
        if at is not None and at > 1:
            raise RuntimeError(
                f"Could not return the FMS results pager to page 1 — it is on "
                f"page {at}. Copying now would silently read the wrong page. "
                f"Click page 1 in FMS by hand, then re-run this step."
            )
        cli.ok(f"Back on page {_active_page(page)}.")

    n = 1
    while n <= MAX_PAGES:
        landed, data = _goto_page(page, n, prev_sig=prev_rows)
        # A live page turn can stall once on the FMS throttle (4.3-4.8 s a
        # turn), so attempt 1 keeps the full TURN_WAIT — healthy turns need
        # 3.5 s+, and cutting attempt 1 short would skip healthy pages. But
        # the old two extra FULL passes made one stuck page burn ~25-40 s.
        # Bug 40 (operator rule: "if it cant proceed past 3s then proceed"):
        # exactly ONE retry, capped at 3 s, then skip-and-continue decides.
        # _goto_page() already retries several click strategies internally.
        if not landed:
            cli.muted(f"  page {n} did not land — one 3 s retry, then "
                      f"moving on...")
            _dismiss_size_menu(page)
            page.wait_for_timeout(900)
            landed, data = _goto_page(page, n, prev_sig=prev_rows,
                                      turn_wait=3.0)
        if not landed:
            # NEVER accept the page that happens to be on screen as page 1.
            # This used to do exactly that ("copying the current page as page
            # 1"), so a run that started on the pager's LAST page silently
            # copied that page's few rows and called it a full result set.
            if n == 1:
                raise RuntimeError(
                    f"Could not get the FMS results pager back to page 1 "
                    f"(it is on page {_active_page(page)}), so the copy would "
                    f"start from the wrong page and quietly return only the "
                    f"last page's rows. Re-run one batch search in FMS by hand "
                    f"to reset the pager, then re-run this step."
                )
            # The result set can SHRINK mid-walk (seen live: analysis said 751
            # rows, the footer said 636 by page 13 — FMS re-settles volatile
            # sets). A page that no longer exists can never "land", and
            # retrying it is pointless: re-read the live total, and if every
            # readable row is already copied, the walk is COMPLETE, not
            # truncated. Only a genuinely unreachable page keeps the
            # truncated flag, so the guard below still raises on real stalls.
            live_total = _table_total(page)
            live_pages = (-(-live_total // eff_size)
                          if live_total and eff_size else None)
            if live_total is not None and raw_rows >= live_total:
                cli.ok(f"Page {n} will not turn, but {raw_rows} row(s) read "
                       f"already cover the live total ({live_total}) — the "
                       f"result set shrank mid-walk. Copy is complete.")
                break
            if live_pages is not None and n > live_pages:
                cli.ok(f"Page {n} will not turn — the live result set now "
                       f"holds only {live_pages} page(s) ({live_total} rows). "
                       f"Copy is complete.")
                break
            # Skip-and-continue (bug 38): one stuck page must not kill the hunt
            # for OIDs living on later pages (374-run: 373/374 found by page 8,
            # page 9 would not turn, step died instead of walking 9-19). Skip
            # it, walk on, and let the completeness guard decide at the end:
            # all packed found -> the skipped pages held nothing needed;
            # still missing -> RAISE naming the skipped pages (the missing
            # OIDs may be on them — never a silent short paste). Three stuck
            # pages in a row means the pager itself is dead, not one bad
            # turn, so stop and let the guard raise.
            skipped.append(n)
            consec_fail += 1
            if consec_fail >= 3:
                truncated = True
                pager_dead = True
                cli.warn(f"Pages {skipped[-3:]} would not turn in a row — the "
                         f"pager looks dead. Stopping at {len(out)} row(s) "
                         f"({raw_rows} read).")
                break
            cli.warn(f"Could not reach page {n} — skipping it for now and "
                     f"continuing (skipped so far: {skipped}).")
            n += 1
            continue
        if data is None:
            if pages == 0:
                cli.warn("Results table not found on the page.")
                return [], 0
            truncated = True
            break
        if not mp:
            for i, h in enumerate(data["headers"]):
                if h in WANTED and h not in mp:
                    mp[h] = i

        # Bug 33/34 (+39/42): FMS renders a COLD page in two chunks — the first
        # ~24 rows arrive instantly, the rest after the ~4.5 s server throttle.
        # _read_table_stable() can accept the page after chunk 1, so the copy
        # must re-settle until the page reaches its expected size. The normal
        # settle is 3 s (bug 42: chunk 1 proves full-or-short fast — the old
        # 9 s stall per cold page is gone); pages that settle short are noted
        # and re-read at the END with REVISIT_WAIT, when the table is warm.
        # eff_size holds the REAL rows-per-page (page 1's post-settle count
        # when it falls far short of the dropdown), so a genuinely smaller
        # pager is not re-tolled on every page.
        if not eff_size:
            eff_size = size if size and size > 1 else 0
        if total and eff_size:
            expected_rows = min(eff_size, total - (n - 1) * eff_size)
        else:
            expected_rows = eff_size
        data, got = _settle_page(page, data, expected_rows, SETTLE_WAIT)
        # A count-FULL table can still be half-rendered: FMS left the previous
        # page's rows in the bottom while the top already shows page n. Those
        # rows are identical to the ones we just read, so compare against the
        # previous page. _goto_page() already waited for a clean read; if its
        # budget ran out first, note it here so the end revisit recovers it
        # (bug 42's mechanism, extended to count-FULL pages).
        stale_now = _stale_tail(data, prev_rows)
        page_rows[n] = _rows_key(data["rows"])
        prev_rows = page_rows[n]

        new = 0
        for cells in data["rows"]:
            if not cells or all(not c for c in cells):
                continue  # blank spacer row
            rec = {w: (cells[mp[w]] if w in mp and mp[w] < len(cells) else "") for w in WANTED}
            raw_rows += 1
            oid = rec["SPX Tracking Number"]
            if oid and oid not in seen:
                seen.add(oid)
                out.append(rec)
                new += 1
        pages += 1
        consec_fail = 0  # a landed page proves the pager is alive
        # (bug 48) The total read BEFORE page 1 is unreliable: FMS can still be
        # re-rendering the PREVIOUS search's footer, so _table_total() returns a
        # transitional value (live: 95 pasted OIDs -> first read said 254 -> the
        # walk planned 6 pages that do not exist and flagged the real last page
        # "SHORT 45/50"). Re-read it on every landed page, adopt the settled
        # value, and recompute THIS page's expected size against it, so the
        # SHORT check and the "cover the total" stop use the truth, not a stale
        # number.
        live_total = _table_total(page)
        if live_total and live_total != total:
            cli.info(f"  pager total corrected {total} -> {live_total} "
                     f"(the first read caught FMS mid-re-render) — replanning.")
            total = live_total
            last = -(-total // size) if total else None
        if total and eff_size:
            expected_rows = max(0, min(eff_size, total - (n - 1) * eff_size))
        # Bug 39: always show ROWS READ alongside new-distinct. "+35 new" used
        # to hide whether page 2 held 50 rows with 15 duplicates (healthy) or
        # 35 rows with 15 rows never read (the missing OIDs at the end). A
        # page that settles short is noted (muted, one line) and the walk
        # moves on at once — the revisit pass at the end recovers its rows.
        hdr = f"  page {n}: {got}/{expected_rows} rows, +{new} new, total copied {len(out)}"
        if total:
            hdr += f" / {total}"
        if expected is not None:
            pf = sum(1 for o in expected if o in {x.upper() for x in seen})
            hdr += f" of {len(expected)} pasted | {pf}/{len(expected)} packed found"
        hdr += f"  [{time.time() - t_start:.1f}s]"
        cli.info(hdr)
        if expected_rows > 0 and got < expected_rows:
            short_pages.append(n)
            cli.muted(f"  page {n}: SHORT {got}/{expected_rows} — moving on, "
                      f"will re-read at the end.")
        elif stale_now:
            short_pages.append(n)
            cli.muted(f"  page {n}: half-rendered (still showing page "
                      f"{n - 1}'s rows) — moving on, will re-read at the end.")
        # Bug 33: the size dropdown and the real table can disagree. FMS
        # paginates a result set at search time, so if the control was
        # changed after the search the dropdown says 50 while the table
        # really shows ~24 rows. Learn the real size from page 1 (AFTER its
        # re-settle window, so a slow cold page is not misread) — the rest
        # of the walk then plans against reality, and the operator hears it
        # on page 1, not after page 14.
        if (pages == 1 and eff_size and got and got < eff_size * 0.7):
            eff_size = max(got, 1)
            cli.warn(
                f"Pager dropdown says {size}/page but page 1 settled at "
                f"only {got} row(s) — this result set is paginated at "
                f"~{eff_size}/page (bug 33). Copying at ~{eff_size}/page; "
                f"expect ~{-(-total // eff_size) if total else 0} page turns."
            )
        if total and raw_rows >= total:
            cli.ok("Copied every row the pager reports.")
            break
        if expected is not None and not (expected - {x.upper() for x in seen}):
            cli.ok(f"All {len(expected)} pasted OID(s) found after page {n} — "
                   f"stopping early.")
            break
        # Packed-batch scope CHECKPOINT (bug 36, revised after the 636-run): FMS
        # batch search returns ALL-status rows for the pasted OIDs, so the row
        # total can imply far more pages than the packed count (257 packed ->
        # 2250 rows / 45 pages). ceil(packed / per-page) is where a NORMAL
        # batch ends — but packed OIDs can genuinely live past it (636 packed
        # -> 751 rows at ~1.2 rows/OID: 44 packed OIDs were STILL missing at
        # page 13, the scope, with their rows on pages 14-16). A hard stop
        # here would short-paste exactly like bug 33, so the scope is a
        # REPORT, not a stop: all found -> done (same as the early-stop
        # above); still missing -> warn loudly and KEEP WALKING, because their
        # rows may be on later pages. Announced once.
        if (expected and eff_size and not scope_announced
                and n >= -(-len(expected) // eff_size)):
            scope_announced = True
            scope = -(-len(expected) // eff_size)
            still_missing = sorted(expected - {x.upper() for x in seen})
            if still_missing:
                cli.warn(
                    f"Past scope (page {n} = ceil({len(expected)}/{eff_size})): "
                    f"{len(still_missing)} still missing, e.g. "
                    f"{still_missing[0]} — walking on.")
            else:
                cli.ok(f"All {len(expected)} pasted OID(s) found within the "
                       f"packed-batch scope (page {n} of "
                       f"ceil({len(expected)}/{eff_size})).")
        # NOTE: no early stop at the computed "last page" — page sizes can
        # shift mid-run, so we trust only the Total, every pasted OID being
        # found, and the pager itself. The loop ends when the Total is
        # reached, every pasted OID is found, the pager dies (3 stuck pages in
        # a row), or at MAX_PAGES. Single stuck pages are SKIPPED past, not
        # stopped at (bug 38) — the guard below accounts for them.
        n += 1
    else:
        # The while-condition ran out (MAX_PAGES) without a break — the pager
        # kept turning pages but the result set never completed. Same hazard
        # as a page-turn failure: the paste must not proceed short.
        truncated = True
        cli.warn(f"MAX_PAGES ({MAX_PAGES}) reached — stopping at "
                 f"{len(out)} row(s) ({raw_rows} read).")
    cli.ok(f"Copied {len(out)} row(s) from {pages} page(s) "
           f"in {time.time() - t_start:.1f}s.")
    # Revisit pass (bug 42): pages that settled short are re-read NOW, when
    # the table is warm — but only if OIDs are still missing (nothing missing
    # = nothing to recover, skip it and stay fast). A re-read that lands
    # merges its rows with the full REVISIT_WAIT; one that will not turn
    # joins `skipped` for the guard below. Fully automatic: the walk never
    # sits 9 s on a cold page mid-run anymore.
    if expected is not None:
        missing_now = sorted(expected - {x.upper() for x in seen})
    else:
        missing_now = []
    if missing_now and short_pages and not pager_dead:
        cli.info(f"Re-reading {len(short_pages)} short page(s) "
                 f"{short_pages} — {len(missing_now)} pasted OID(s) still "
                 f"missing.")
        live = _table_total(page) or total
        for sn in list(dict.fromkeys(short_pages)):
            exp2 = (min(eff_size, live - (sn - 1) * eff_size)
                    if live and eff_size else eff_size)
            landed2, data2 = _goto_page(page, sn,
                                        prev_sig=page_rows.get(sn - 1))
            if not landed2 or not data2 or not data2.get("rows"):
                if sn not in skipped:
                    skipped.append(sn)
                cli.warn(f"Could not re-read page {sn} — left in skipped "
                         f"{skipped}.")
                continue
            data2, got2 = _settle_page(page, data2, exp2, REVISIT_WAIT)
            page_rows[sn] = _rows_key(data2["rows"])
            add = 0
            for cells in data2["rows"]:
                if not cells or all(not c for c in cells):
                    continue
                rec = {w: (cells[mp[w]] if w in mp and mp[w] < len(cells) else "")
                       for w in WANTED}
                raw_rows += 1
                oid = rec["SPX Tracking Number"]
                if oid and oid not in seen:
                    seen.add(oid)
                    out.append(rec)
                    add += 1
            cli.info(f"  re-read page {sn}: {got2}/{exp2} rows, +{add} new "
                     f"[{time.time() - t_start:.1f}s]")
    # Over-read guard (bug 50): the pager's Total is the size of THIS search's
    # result set, so the number of DISTINCT rows copied can never exceed it. If
    # it does, the walk read a table that does not belong to this search (a
    # stale/mixed table), and pasting it ships strangers to the QR generator.
    # Live failure this encodes: a 63-row packed search copied 96 distinct OIDs
    # because the walk started against the PREVIOUS search's table. A long paste
    # is as wrong as a short one, so abort instead of pasting.
    if total and len(out) > total:
        raise RuntimeError(
            f"Copy OVERRAN the search: {len(out)} distinct OID(s) read but the "
            f"pager total says {total} ({len(out) - total} extra). The results "
            f"table was stale or changed mid-walk, so this paste would be "
            f"wrong. Re-run this step."
        )
    # Completeness guard: a walk that ended while pasted OIDs are still
    # missing must stop the step, never flow a short paste into the QR
    # generator. (This is the 140-vs-193 bug: page 5 would not turn, the copy
    # warned and carried on, and the QR generator never knew it was short.)
    if expected is not None:
        missing = sorted({str(o).strip().upper() for o in expected} - {x.upper() for x in seen})
        if missing:
            if skipped:
                raise RuntimeError(
                    f"Copy INCOMPLETE: {len(missing)} of {len(expected)} "
                    f"pasted OID(s) still missing (e.g. {missing[0]}) and "
                    f"page(s) {skipped} could not be read — the missing OIDs "
                    f"may be on those pages. A short paste was about to reach "
                    f"the QR generator, so the step aborts instead. Re-run "
                    f"this step."
                )
            if truncated:
                raise RuntimeError(
                    f"Copy INCOMPLETE: the pager stopped before {len(missing)} "
                    f"of {len(expected)} pasted OID(s) were found "
                    f"({len(out)} copied, {raw_rows} rows read; e.g. "
                    f"{missing[0]}). A short paste was about to reach the QR "
                    f"generator, so the step aborts instead. Re-run this step."
                )
            cli.warn(f"{len(missing)} pasted OID(s) never appeared in the "
                     f"search results (e.g. {missing[0]}) — they cannot get a "
                     f"QR code. Check FMS and re-run.")
        elif skipped:
            cli.ok(f"All {len(expected)} pasted OID(s) found — skipped page(s) "
                   f"{skipped} held nothing missing.")
    # Paging clicks the pager and the page-size control, which sit ~2297px down
    # inside div.ssc-layout-item.ssc-layout-item-direction-bottom. Leave the
    # view back at the top so the toolbar (and Export) is where a person
    # expects it instead of the page ending up pinned at the bottom.
    try:
        from fms_batch_export import scroll_fms_to_top
        moved = scroll_fms_to_top(page)
        if moved:
            cli.muted(f"  reset {moved} scrolled container(s) to the top")
    except Exception:
        pass
    # Leave the pager on page 1 too. A windowed pager will not render the
    # number 1 once you are deep in the result set, so ending on the last page
    # makes the NEXT run unable to get home in one jump.
    end_page = _active_page(page)
    if end_page is not None and end_page > 1:
        try:
            at = goto_first_page(page)
            cli.muted(f"  left the pager on page {at}")
        except Exception as e:
            cli.warn(f"Could not return the pager to page 1 "
                     f"({str(e).splitlines()[0][:60]}) — the next run will "
                     f"reset it before copying.")
    return out, pages


# ------------------------------------------------------------------ docks

def resolve_docks(page_mc, stations: list[str]) -> list[str]:
    """Fallback only: Destination Station -> DC via MC's hub->DC table."""
    try:
        return page_mc.evaluate(
            """(sts) => sts.map(s => {
                if (!s || s === '-') return '';
                if (typeof KNOWN_DCS !== 'undefined' && KNOWN_DCS.includes(s)) return s;
                if (s.toUpperCase().endsWith(' DC')) return s;
                try {
                    if (typeof getHubMap === 'function' && typeof resolveHubDC === 'function')
                        return resolveHubDC(s, getHubMap()) || '';
                } catch (e) {}
                return '';
            })""",
            stations,
        )
    except Exception as e:
        cli.warn("MC dock resolution unavailable (" + str(e).splitlines()[0] + ")")
        return ["" for _ in stations]


def dock_key(name: str):
    m = re.search(r"(\d+)\s*$", name or "")
    base = re.sub(r"\d+\s*$", "", name or "").strip()
    return (base, int(m.group(1)) if m else 999)


# ------------------------------------------------------------------ QR gen

def load_qr_generator(page_mc, text: str) -> bool:
    """Open the OID+TO QR Generator in MC and load the dock-sectioned list.

    Returns True only when the generator's #qrInput textarea was found and
    filled — the caller uses that to distinguish "loaded" from a silent
    no-op (e.g. the MC page without the generator open), which used to finish
    the step while the QR generator still held the last run's data (bug 35).
    """
    page_mc.bring_to_front()
    try:
        filled = page_mc.evaluate(
            """(t) => {
                if (typeof openQRGen_OidTo === 'function') openQRGen_OidTo();
                const ta = document.getElementById('qrInput');
                if (!ta) return false;
                ta.value = t;
                if (typeof qrRenderNow === 'function') qrRenderNow();
                return true;
            }""",
            text,
        )
    except Exception:
        return False
    return bool(filled)


# ------------------------------------------------------------------ main step

def run_packed_step(context, page_fms, page_mc=None, timeout: int = 180,
                    oids: list[str] | None = None) -> dict | None:
    from fms_batch_export import (
        paste_batch,
        submit_and_wait_results,
        wait_logged_in,
    )

    if not oids:
        if page_mc is not None:
            try:
                from lib.mc_drive import read_packed_oids
                oids = read_packed_oids(page_mc)
            except Exception as e:
                cli.warn("Could not read MC Packed tab: " + str(e).splitlines()[0])
                oids = []
        if not oids:
            oids = read_packed_oids_file()
    if not oids:
        cli.ok("No SOC Packed OIDs (MC Packed tab empty, no packed_oids.txt) — step skipped.")
        return None

    cli.ok(f"{len(oids)} SOC Packed OID(s) to re-search in FMS.")
    page_fms.bring_to_front()
    wait_logged_in(page_fms, max(timeout, 300))

    # FMS paginates a result set WHEN THE SEARCH RUNS. The '… Artikulo /
    # Pahina' control must already say 50 BEFORE the search, or the results
    # come back at the size a previous session left behind (default 24) —
    # switching afterwards only changes the dropdown, not the live table, and
    # the copy then reads ~24-row pages while _page_size() claims 50 (bug 33).
    # Best effort here: the results pager may not exist yet, so quiet. The
    # call right after the results banner verifies and catches the case where
    # a fresh session was at 24 and the pre-search attempt could not apply.
    set_page_size_50(page_fms, quiet=True)

    paste_batch(page_fms, oids, 3000)
    with cli.activity("FMS is searching the packed batch") as act:
        submit_and_wait_results(page_fms, timeout)
        act.stop("Results banner visible")

    set_page_size_50(page_fms)
    cli.info("Copying the results table (no export)...")
    rows, pages = copy_all_pages(page_fms, expect_oids=oids)
    if not rows:
        cli.warn("Nothing copied from the batch-search table.")
        return None
    cli.ok(f"Copied {len(rows)} row(s) over {pages} page(s).")

    # ---- dock attribution: phase-1 source tab first, destination as fallback
    tab_map = read_tab_map()
    if tab_map:
        cli.ok(f"Dock map loaded: {len(tab_map)} OID(s) from phase-1 tabs.")
    else:
        cli.warn("pending_tab_map.txt missing — using Destination Station as dock.")
    fallback = (
        resolve_docks(page_mc, [r["Destination Station"] for r in rows])
        if page_mc is not None else ["" for _ in rows]
    )
    n_tab = n_dest = 0
    for r, fb in zip(rows, fallback):
        dock = tab_map.get(r["SPX Tracking Number"].upper())
        if dock:
            n_tab += 1
        else:
            dock = fb or (r["Destination Station"] or "unknown")
            n_dest += 1
        r["_dock"] = dock
    cli.info(f"  docks from phase-1 tabs: {n_tab} · fallback (destination): {n_dest}")

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["_dock"], []).append(r)
    order = sorted(groups, key=dock_key)

    # ---- one dock-sectioned list -> single paste into the QR generator ----
    # (no files on disk: the QR paste is the only artifact of this step)
    gen_lines: list[str] = []
    n_moved = 0
    for dock in order:
        recs = groups[dock]
        still = [r for r in recs
                 if (r["Order Status"] or "").upper().startswith(PACKED_STATUSES)]
        moved = [r for r in recs
                 if not (r["Order Status"] or "").upper().startswith(PACKED_STATUSES)]
        n_moved += len(moved)
        # EVERY copied row with a TO goes into the QR paste — status changes
        # (SOC_PACKED -> SOC_LHPacking etc.) are informational only, never an
        # exclusion.
        with_to = [r for r in recs if r["Current TO Number"] not in ("", "-")]
        no_to = [r for r in recs if r["Current TO Number"] in ("", "-")]
        cli.info(f"  {dock}: {len(recs)} copied | {len(still)} still packed | "
                 f"{len(moved)} moved on | {len(with_to)} with TO | "
                 f"{len(no_to)} need TO")
        for r in with_to:
            gen_lines.append(
                f"{r['SPX Tracking Number']} {r['Current TO Number']} {dock}")
    if n_moved:
        cli.info(f"{n_moved} OID(s) changed status while the bot was searching "
                 "(now LH/other) — still INCLUDED in the QR paste.")

    # ---- single paste into the MC OID+TO QR Generator (dock sections) ----
    gen_text = "\n".join(gen_lines) + "\n"
    from lib.notify import send_seatalk
    docks_txt = ", ".join(f"{d}×{len(groups[d])}" for d in order[:5])
    notify = (f"SHEET-BOT · SOC Packed → QR generator\n"
              f"{len(rows)} OID(s) over {pages} page(s) · "
              f"{len(order)} dock(s): {docks_txt}")
    if page_mc is not None and gen_text.strip():
        try:
            qr_loaded = load_qr_generator(page_mc, gen_text)
        except Exception as e:
            cli.warn("QR generator load failed: " + str(e).splitlines()[0])
            qr_loaded = False
        if qr_loaded:
            cli.ok(f"QR generator loaded: {len(order)} dock(s) tagged per line "
                   "in ONE paste (MC shows section headers + badges).")
        else:
            cli.warn("QR generator did NOT load — the MC page is missing the "
                     "generator textarea (#qrInput). Paste manually:")
            for ln in gen_lines[:20]:
                cli.muted("  " + ln)
    else:
        cli.info("No MC page attached — paste this into the OID+TO QR Generator:")
        for ln in gen_lines[:20]:
            cli.muted("  " + ln)
    try:
        send_seatalk(notify)
    except Exception as e:
        cli.warn("Seatalk notify failed (step itself is done): "
                 + str(e).splitlines()[0])

    return {"rows": len(rows), "pages": pages,
            "docks": {d: len(groups[d]) for d in order}}


def main() -> int:
    from fms_batch_export import CDP, pick_page
    from playwright.sync_api import sync_playwright

    url = "https://spx.shopee.ph/#/orderTracking"
    with sync_playwright() as p:
        browser = _cdp_attach(p, CDP, log=None)   # see lib/cdp.py (bug 31)
        context = browser.contexts[0]
        page_fms = pick_page(context, url)
        page_mc = None
        try:
            from lib.mc_drive import _find_mc_page, MC_HTML
            page_mc = _find_mc_page(context, MC_HTML)
        except Exception:
            page_mc = None
        run_packed_step(context, page_fms, page_mc)
    return 0


if __name__ == "__main__":
    cli.entry("packed_scrape", "SOC Packed -> batch search -> copy table -> docks -> QR gen")(main)()
