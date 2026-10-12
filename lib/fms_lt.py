"""FMS Linehaul-Trip operations — every action carries a PROOF.

Ported from the old project's best selectors (V16) but rebuilt around one rule the
old bot never followed: *nothing counts as done until it is verified* —
filled input reads back the LT, Search waits for the actual row, View is
proven by the URL/tab change, Runsheet by its URL + Qty box, and every
runsheet hub-tab switch must change the qty before it counts.
"""

from __future__ import annotations

import re
import time

from lib import cli

TRIPS_URL = "https://spx.shopee.ph/#/hubLinehaulTrips/trip"


# ---------------------------------------------------------------- helpers

def _bbox_click(page, js: str, arg=None) -> bool:
    """Evaluate to get a bounding box, then real-mouse-click its center."""
    try:
        pt = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    except Exception:
        return False
    if not pt:
        return False
    page.mouse.click(pt["x"], pt["y"])
    return True


def on_login_page(page) -> bool:
    return "authenticate/login" in (page.url or "")


def goto_trips(page, timeout: int = 60) -> None:
    if "hubLinehaulTrips/trip" not in (page.url or ""):
        page.goto(TRIPS_URL, wait_until="domcontentloaded")
    cli.info("Waiting for the trips list (Handedover tab area)...")
    page.get_by_text("Handedover").first.wait_for(timeout=timeout * 1000)


# ---------------------------------------------------------------- handover

def ensure_tab(page, name: str, timeout: int = 10) -> bool:
    """Activate a trips tab (Handedover / Ended). The LH Trip Number input
    exists on several tabs, so proof = we clicked the tab and the filter
    form is ready afterwards."""
    clicked = _bbox_click(page, """(name) => {
        const els = [...document.querySelectorAll('div,span,a,li')];
        const t = els.find(e => (e.innerText || '').trim() === name
                          && e.children.length === 0);
        if (!t) return null;
        const r = t.getBoundingClientRect();
        return r.width ? { x: r.x + r.width / 2, y: r.y + r.height / 2 } : null;
    }""", name)
    if not clicked:
        try:
            page.get_by_text(name, exact=True).first.click(timeout=4000)
            clicked = True
        except Exception:
            cli.warn(f"{name} tab not found.")
            return False
    return wait_filter_ready(page, timeout)


# ---------------------------------------------------------------- fill

# Find the LH Trip NUMBER input regardless of form markup: geometric match
# (input sitting right of the exact 'LH Trip Number' leaf label, same row),
# then ant-form-item text match, then rightmost 'pasok' placeholder.
FINDER_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const labels = [...document.querySelectorAll('*')].filter(e =>
        !e.children.length && (e.innerText || '').trim() === 'LH Trip Number' && vis(e));
    const inputs = [...document.querySelectorAll('input')].filter(i =>
        (i.type === 'text' || !i.type) && vis(i));
    if (labels.length) {
        const lr = labels[0].getBoundingClientRect();
        let best = null, bd = 1e9;
        for (const i of inputs) {
            const r = i.getBoundingClientRect();
            if (Math.abs(r.y + r.height / 2 - (lr.y + lr.height / 2)) > 25) continue;
            const d = Math.abs(r.x - lr.right);
            if (d < bd) { bd = d; best = i; }
        }
        if (best) return best;
    }
    for (const it of [...document.querySelectorAll('.ant-form-item')]) {
        const t = it.textContent || '';
        if (/LH\\s*Trip\\s*Number/i.test(t) && !/LH\\s*Trip\\s*Name/i.test(t)) {
            const c = it.querySelector('input');
            if (c) return c;
        }
    }
    const cand = inputs.filter(e => /pasok|Please Input/i.test(e.placeholder || ''))
        .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
    return cand.length ? cand[cand.length - 1] : null;
}"""


def _lt_input_handle(page):
    try:
        h = page.evaluate_handle(FINDER_JS)
        return h.as_element()
    except Exception:
        return None


def wait_filter_ready(page, timeout: int = 15) -> bool:
    """PROOF that the Handedover filter form actually rendered."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _lt_input_handle(page):
            return True
        time.sleep(0.4)
    cli.warn("LH Trip Number input never appeared.")
    return False


def _setter_fill(page, lt: str) -> bool:
    """Last-resort: React native setter + input/change events, read-back proof."""
    try:
        ok = page.evaluate(
            "(lt) => { const setter = Object.getOwnPropertyDescriptor("
            "HTMLInputElement.prototype, 'value').set;"
            " const inp = (" + FINDER_JS + ")();"
            " if (!inp) return false;"
            " setter.call(inp, lt);"
            " inp.dispatchEvent(new Event('input', { bubbles: true }));"
            " inp.dispatchEvent(new Event('change', { bubbles: true }));"
            " return true; }",
            lt,
        )
    except Exception as e:
        cli.warn("setter fill evaluate failed: " + str(e).splitlines()[0])
        return False
    if not ok:
        return False
    val = ""
    try:
        val = page.evaluate("() => { const i = (" + FINDER_JS + ")();"
                            " return i ? (i.value || '') : ''; }")
    except Exception:
        return False
    return (val or "").strip() == lt


def fill_lt(page, lt: str) -> bool:
    """Fill the LH Trip NUMBER input and PROVE it by reading the value back.
    Trusted events first (Playwright fill / real typing) so React state updates;
    setter trick only as fallback. Two attempts (form may still be rendering)."""
    for _ in (1, 2):
        el = _lt_input_handle(page)
        if el is not None:
            try:
                el.fill(lt, timeout=4000)
                if (el.input_value() or "").strip() == lt:
                    return True
            except Exception as e:
                cli.warn("fill() failed: " + str(e).splitlines()[0])
            try:
                el.click(timeout=3000)
                page.keyboard.press("Control+A")
                page.keyboard.type(lt, delay=25)
                if (el.input_value() or "").strip() == lt:
                    return True
            except Exception:
                pass
        if _setter_fill(page, lt):
            return True
        time.sleep(0.8)
    return False


# ---------------------------------------------------------------- search

_STATE_PRIO = {"rows": 3, "loading": 2, "empty": 1, "banner": 1, "stale": 1, "": 0}


def _search_state(page, lt: str) -> tuple[str, dict]:
    """Ask EVERY frame for the search outcome; keep the strongest signal.
    banner_lt = the LT the results banner names (search proof); 'stale' when
    the banner names a DIFFERENT LT (our Search click didn't take)."""
    best, diag = "", {"frames": 0, "tables": 0, "tds": 0, "first": "",
                      "banner_lt": ""}
    for fr in page.frames:
        try:
            r = fr.evaluate(
                """(lt) => {
                const body = document.body.innerText || '';
                let st = '';
                const tds = [...document.querySelectorAll('td')];
                for (const td of tds)
                    if ((td.innerText || '').includes(lt)) { st = 'rows'; break; }
                if (!st) {
                    const rows = [...document.querySelectorAll('table tbody tr')]
                        .filter(r => r.querySelector('td'));
                    for (const r of rows)
                        if ((r.innerText || '').includes(lt)) { st = 'rows'; break; }
                }
                const bm = body.match(
                    /Showing all results for LT number\\s*"?\\s*([A-Z0-9]{6,})"?/i);
                const banner_lt = bm ? bm[1] : '';
                if (!st && document.querySelector(
                        '.ant-spin-spinning,.ant-table-loading,.ant-spin-blur'))
                    st = 'loading';
                if (!st && banner_lt && banner_lt !== lt) st = 'stale';
                if (!st && /Walang\\s+data|No\\s+data/i.test(body)) st = 'empty';
                if (!st && banner_lt === lt) st = 'banner';
                const firstTd = tds.find(td => (td.innerText || '').trim());
                return { st, banner_lt,
                         tables: document.querySelectorAll('table').length,
                         tds: tds.length,
                         first: firstTd ? (firstTd.innerText || '')
                                 .replace(/\\s+/g, ' ').slice(0, 80) : '' };
            }""",
                lt,
            )
        except Exception:
            continue
        diag["frames"] += 1
        diag["tables"] = max(diag["tables"], r["tables"])
        diag["tds"] = max(diag["tds"], r["tds"])
        if r["first"]:
            diag["first"] = r["first"]
        if r["banner_lt"]:
            diag["banner_lt"] = r["banner_lt"]
        if _STATE_PRIO.get(r["st"], 0) > _STATE_PRIO.get(best, 0):
            best = r["st"]
    return best, diag


def _click_search_btn(page) -> bool:
    try:
        page.locator("button").filter(has_text="Search").first.click(timeout=5000)
        return True
    except Exception:
        return _bbox_click(page, """() => {
            const b = [...document.querySelectorAll('button')]
                .find(b => (b.innerText || '').trim() === 'Search');
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        }""")


def click_search(page, lt: str) -> str:
    """Click Search (trusted) and wait for the outcome across ALL frames.
    The banner must name OUR LT, else the click didn't take -> re-click
    (max 2). Returns 'rows' | 'empty' | 'timeout'. Logs every state change;
    on a non-rows outcome dumps what the page actually shows."""
    if not _click_search_btn(page):
        return "timeout"
    t0 = time.time()
    banner_since = None
    empty_since = None
    stale_since = None
    reclicks = 0
    last = None
    diag = {}
    while time.time() - t0 < 25:
        st, diag = _search_state(page, lt)
        if st != last:
            cli.info(f"  search state: {st or '…'}"
                     + (f" (banner: {diag.get('banner_lt')})"
                        if diag.get("banner_lt") else ""))
            last = st
        if st == "rows":
            return "rows"
        if st == "loading":
            empty_since = None
            banner_since = None
            stale_since = None
        elif st == "stale":
            # results on screen belong to another query — our click didn't take
            if stale_since is None:
                stale_since = time.time()
            elif time.time() - stale_since > 3 and reclicks < 2:
                cli.info(f"  banner shows {diag.get('banner_lt')} — "
                         f"re-clicking Search for {lt}...")
                _click_search_btn(page)
                reclicks += 1
                stale_since = None
                empty_since = None
        elif st == "empty":
            if empty_since is None:
                empty_since = time.time()
            elif time.time() - empty_since > 4:
                if reclicks < 1:
                    cli.info(f"  'Walang data' stable — re-clicking Search "
                             f"once to confirm...")
                    _click_search_btn(page)
                    reclicks += 1
                    empty_since = time.time()
                else:
                    break
        elif st == "banner":
            if banner_since is None:
                banner_since = time.time()
            elif time.time() - banner_since > 6:
                break
        else:
            empty_since = None
        time.sleep(0.4)
    if last != "rows":
        cli.warn(f"  search diag: frames={diag.get('frames')} "
                 f"tables={diag.get('tables')} tds={diag.get('tds')} "
                 f"banner_lt={diag.get('banner_lt')} first='{diag.get('first')}'")
    return "empty" if last in ("empty", "banner") else "timeout"


# ---------------------------------------------------------------- view

def view_point(page, lt: str):
    """Coordinates of the View action for the LT row. Handles Ant Design's
    split table (main body + frozen right column with the View link)."""
    try:
        return page.evaluate(
            """(lt) => {
            const main = document.querySelector('.ant-table-content table tbody')
                    || document.querySelector('table tbody');
            if (!main) return null;
            const mRows = [...main.querySelectorAll('tr')].filter(r => r.querySelector('td'));
            const i = mRows.findIndex(r => (r.innerText || '').includes(lt));
            if (i < 0) return null;
            // same-row View first
            let v = [...mRows[i].querySelectorAll('a,span,button')]
                .find(e => (e.innerText || '').trim() === 'View');
            if (!v) {
                // frozen right column: correlate by row index
                const fx = document.querySelector('.ant-table-fixed-right table tbody');
                if (fx) {
                    const fRows = [...fx.querySelectorAll('tr')].filter(r => r.querySelector('td'));
                    v = [...(fRows[i] || fRows[0] || { querySelectorAll: () => [] })
                        .querySelectorAll('a,span,button')]
                        .find(e => (e.innerText || '').trim() === 'View');
                }
            }
            if (!v) return null;
            const r = v.getBoundingClientRect();
            return r.width ? { x: r.x + r.width / 2, y: r.y + r.height / 2 } : null;
        }""",
            lt,
        )
    except Exception:
        return None


def _view_proof(page, context, before: int, timeout: int):
    """PROOF a View click worked: new tab on /view, same-tab URL change, or the
    detail page's 'View Runsheet' button visible."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if len(context.pages) > before:
                np = context.pages[-1]
                if "hubLinehaulTrips/view" in (np.url or ""):
                    np.bring_to_front()
                    return np
            if "hubLinehaulTrips/view" in (page.url or ""):
                return page
            if page.evaluate("() => (document.body.innerText || '')"
                             ".includes('View Runsheet')"):
                return page
        except Exception:
            pass
        time.sleep(0.3)
    return None


def click_view(page, context, lt: str):
    """Click the LT row's View action; PROOF = detail page opens.
    Frame-aware strategies: same-row View (antd v4 fixed cells live in the
    same tr), frozen-right Action column correlated by row index, raw bbox
    mouse click. Playwright locator clicks handle iframe coordinates."""
    before = len(context.pages)
    frames = list(page.frames)
    # 0) direct href navigation — View is an <a>; most reliable of all
    try:
        href = page.evaluate(
            """(lt) => {
            const rows = [...document.querySelectorAll('tr')]
                .filter(r => (r.innerText || '').includes(lt));
            for (const row of rows) {
                const a = row.querySelector('a[href*="view"], a[href*="detail"]');
                if (a) return a.getAttribute('href');
            }
            const allA = [...document.querySelectorAll('table a')]
                .filter(a => (a.innerText || '').trim() === 'View');
            return allA.length === 1 ? allA[0].getAttribute('href') : null;
        }""",
            lt,
        )
    except Exception:
        href = None
    if href:
        target = href if href.startswith("http") else "https://spx.shopee.ph" + href
        cli.info(f"  navigating to View URL for {lt}: {target}")
        try:
            page.goto(target, wait_until="domcontentloaded")
            d = _view_proof(page, context, before, 8)
            if d:
                return d
            page.goto(TRIPS_URL, wait_until="domcontentloaded")
        except Exception as e:
            cli.warn("href nav failed: " + str(e).splitlines()[0])
    # 1) View inside the row that contains the LT (any frame)
    for fr in frames:
        try:
            row = fr.locator("tr").filter(has_text=lt).first
            if not row.count():
                continue
            v = row.get_by_text("View", exact=True).first
            if v.count():
                cli.info(f"  clicking View for {lt} (same-row)...")
                v.click(timeout=4000)
                d = _view_proof(page, context, before, 8)
                if d:
                    return d
        except Exception as e:
            cli.warn("same-row View click failed: " + str(e).splitlines()[0])
    # 2) frozen right Action column — correlate by row index (any frame)
    for fr in frames:
        try:
            idx = fr.evaluate(
                """(lt) => {
                const main = document.querySelector('.ant-table-content table tbody')
                        || document.querySelector('table tbody');
                if (!main) return null;
                const rows = [...main.querySelectorAll('tr')]
                    .filter(r => r.querySelector('td'));
                const i = rows.findIndex(r => (r.innerText || '').includes(lt));
                return i < 0 ? null : i;
            }""",
                lt,
            )
        except Exception:
            idx = None
        if idx is None:
            continue
        try:
            fx = (fr.locator(".ant-table-fixed-right table tbody tr")
                  .nth(idx).get_by_text("View", exact=True).first)
            if fx.count():
                cli.info(f"  clicking View for {lt} (fixed-right column)...")
                fx.click(timeout=4000)
                d = _view_proof(page, context, before, 8)
                if d:
                    return d
        except Exception as e:
            cli.warn("fixed-right View click failed: " + str(e).splitlines()[0])
    # 3) raw bbox mouse click
    pt = view_point(page, lt)
    if pt:
        cli.info(f"  clicking View for {lt} (mouse)...")
        page.mouse.click(pt["x"], pt["y"])
        d = _view_proof(page, context, before, 8)
        if d:
            return d
    cli.warn(f"No View click succeeded for {lt}.")
    return None


# ---------------------------------------------------------------- runsheet

def open_runsheet(page, context):
    """Click View Runsheet; PROOF = URL contains /runsheet (same or new tab)."""
    before = len(context.pages)
    try:
        btn = page.locator("button").filter(has_text="View Runsheet").first
        btn.click(timeout=8000)
    except Exception:
        _bbox_click(page, """() => {
            const b = [...document.querySelectorAll('button')]
                .find(b => (b.innerText || '').includes('View Runsheet'));
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        }""")
    t0 = time.time()
    while time.time() - t0 < 12:
        try:
            if len(context.pages) > before:
                np = context.pages[-1]
                if "/runsheet" in (np.url or ""):
                    np.bring_to_front()
                    return np
            if "/runsheet" in (page.url or ""):
                return page
        except Exception:
            pass
        time.sleep(0.4)
    cli.warn("View Runsheet did not lead to a /runsheet page.")
    return None


def _content_frame(page):
    """The frame that actually renders the runsheet (micro-frontends may
    embed it in an iframe); None until the content exists."""
    for fr in page.frames:
        try:
            if fr.evaluate("() => (document.body.innerText || '')"
                           ".includes('Qty of Parcel')"):
                return fr
        except Exception:
            continue
    return None


def _read_active_qty(fr):
    try:
        return fr.evaluate(
            """() => {
            const body = (document.body.innerText || '') + '\\n' + (document.body.textContent || '');
            const t = body.match(/Qty\\s+of\\s+TO\\D*(\\d+)/i);
            const p = body.match(/Qty\\s+of\\s+Parcel\\D*(\\d+)/i);
            return { to: t ? parseInt(t[1], 10) : 0,
                     parcel: p ? parseInt(p[1], 10) : 0 };
        }"""
        )
    except Exception:
        return {"to": 0, "parcel": 0}


def _hub_tabs(fr) -> list[str]:
    try:
        return fr.evaluate(
            """() => {
            const out = [];
            for (const sel of ['.ssc-tabs-tab', '.ant-tabs-tab', '[role="tab"]']) {
                for (const e of document.querySelectorAll(sel)) {
                    const t = (e.innerText || '').trim();
                    if (t && t.length < 60 && /Hub|SOC|Parking/.test(t)) out.push(t);
                }
                if (out.length) break;
            }
            if (!out.length) {
                // generic: leaf labels near the top of the page
                for (const e of document.querySelectorAll('*')) {
                    if (e.children.length) continue;
                    const t = (e.innerText || '').trim();
                    if (!t || t.length > 40 || !/^(SOC\\s*\\d+|.*Hub|.*Parking)$/.test(t))
                        continue;
                    const r = e.getBoundingClientRect();
                    if (r.width && r.y < 260) out.push(t);
                }
            }
            return [...new Set(out)];
        }"""
        )
    except Exception:
        return []


def _click_tab_fr(fr, tab: str) -> bool:
    """Locator click inside whatever frame renders the tabs."""
    for sel in (".ssc-tabs-tab", ".ant-tabs-tab", '[role="tab"]'):
        try:
            els = fr.locator(sel)
            for i in range(els.count()):
                e = els.nth(i)
                try:
                    if (e.inner_text(timeout=500) or "").strip() == tab:
                        e.click(timeout=3000)
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    # last resort: exact-text DOM click on the first match (tab label comes
    # before table cells in DOM order)
    try:
        fr.evaluate(
            """(name) => {
            const els = [...document.querySelectorAll('*')].filter(e =>
                !e.children.length && (e.innerText || '').trim() === name);
            if (els.length) { els[0].click(); return true; }
            return false;
        }""",
            tab,
        )
        return True
    except Exception:
        return False


def read_runsheet(page) -> dict:
    """Extract per-origin-hub legs from the runsheet.

    Each hub tab shows its own Qty of TO / Qty of Parcel; switching tabs must
    CHANGE the qty before the read counts (same proof as the FMS pager fix).
    Destination hub = next hub in the Trip Route.
    """
    fr = None
    t0 = time.time()
    while time.time() - t0 < 15 and fr is None:
        fr = _content_frame(page)
        if fr is None:
            time.sleep(0.4)
    if fr is None:
        cli.warn("Runsheet content ('Qty of Parcel') never rendered — "
                 "reading nothing rather than guessing.")
        return {"route": "", "legs": []}
    cli.info("  runsheet rendered — reading legs...")
    try:
        route = fr.evaluate(
            """() => {
            const body = (document.body.innerText || '') + '\\n' + (document.body.textContent || '');
            const m = body.match(/Trip\\s+Route[^\\n]*\\n?\\s*([^\\n>]+(?:>\\s*[^\\n>]+)*)/i);
            return m ? m[1].trim() : '';
        }"""
        ) or ""
    except Exception:
        route = ""
    route_hubs = [h.strip() for h in route.split(">") if h.strip()]

    tabs = _hub_tabs(fr)
    cli.info(f"  route: {route!r} · hub tabs: {tabs}")
    legs: list[dict] = []
    if not tabs:
        q = _read_active_qty(fr)
        if q["parcel"] > 0 and route_hubs:
            legs.append({"origin": route_hubs[0],
                         "dest": route_hubs[1] if len(route_hubs) > 1 else "",
                         "qty_to": q["to"], "qty_parcel": q["parcel"]})
        else:
            cli.warn(f"  runsheet snapshot: no tabs, route={route!r} qty={q}")
        return {"route": route, "legs": legs}

    prev_qty = None
    for i, tab in enumerate(tabs):
        if not _click_tab_fr(fr, tab):
            cli.warn(f"Runsheet tab '{tab}' not clickable — skipping leg.")
            continue
        # wait for the qty to actually change (tab content loads async)
        t0 = time.time()
        q = _read_active_qty(fr)
        while time.time() - t0 < 6:
            if q["parcel"] and (prev_qty is None or q["parcel"] != prev_qty):
                break
            time.sleep(0.3)
            q = _read_active_qty(fr)
        prev_qty = q["parcel"]
        dest = route_hubs[i + 1] if i + 1 < len(route_hubs) else ""
        legs.append({"origin": tab, "dest": dest,
                     "qty_to": q["to"], "qty_parcel": q["parcel"]})
    if not legs:
        cli.warn(f"  runsheet snapshot: route={route!r} tabs={tabs} "
                 f"qty={_read_active_qty(fr)}")
    return {"route": route, "legs": legs}


def _click_tab(page, tab: str) -> bool:
    return _bbox_click(page, """(name) => {
        for (const sel of ['.ssc-tabs-tab', '.ant-tabs-tab']) {
            const e = [...document.querySelectorAll(sel)]
                .find(e => (e.innerText || '').trim() === name);
            if (e) { const r = e.getBoundingClientRect();
                     if (r.width) return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; }
        }
        return null;
    }""", tab)
