
"""Drive Misroute Checker (MC V2) in the same debug Chrome as FMS."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MC_HTML = ROOT / "mc" / "MC_V2.html"
CDP = "http://127.0.0.1:9222"


def _mc_url() -> str:
    """The Vercel MC site URL (with basic-auth userinfo), or "" for local."""
    try:
        import config
        return (getattr(config, "MC_URL", "") or "").strip()
    except Exception:
        return ""


def _mc_host(url: str) -> str:
    """Hostname of the MC site, for tab matching (never logs credentials)."""
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _mc_desc(url: str) -> str:
    """MC site description safe for logs (credentials stripped)."""
    host = _mc_host(url)
    if not host:
        return url
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        return f"{parts.scheme}://{host}{parts.path or ''}"
    except Exception:
        return host


from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def latest_fms_xlsx(root: Path | None = None) -> Path:
    from lib.wait_file import download_watch_folders

    root = root or ROOT
    cands: list[Path] = []
    for folder in download_watch_folders(root):
        if not folder.is_dir():
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if not p.is_file() or p.stat().st_size < 20_000:
                    continue
            except OSError:
                continue
            name = p.name.lower()
            if name.startswith("fms_reception_") and name.endswith(".xlsx"):
                cands.append(p)
    if not cands:
        raise SystemExit(
            "No FMS .xlsx found. Run:  py run_fms.py\n"
            "WPS must open FMS_Reception_*.xlsx (not the jammed .csv)."
        )
    pick = max(cands, key=lambda p: p.stat().st_mtime)
    info("FMS xlsx:", pick, f"({pick.stat().st_size} bytes)")
    return pick


def _find_mc_page(context, html: Path):
    """Return an already-open MC tab in this Chrome, or None.
    Marker-based first (lib.tabs), legacy filename scan as fallback.
    When config.MC_URL is set (Vercel site), matches by site host instead —
    the local filename never appears in a https:// URL."""
    from lib import tabs
    url = _mc_url()
    hints = ((_mc_host(url),) if url and _mc_host(url)
             else (html.name.lower(),))
    try:
        p = tabs.get(context, "mc", hints)
        if p is not None:
            return p
    except Exception:
        pass
    try:
        pages = context.pages
    except Exception:
        return None
    if url and _mc_host(url):
        needle = _mc_host(url)  # e.g. "sheet-mc.vercel.app"
    else:
        needle = html.name.lower()  # e.g. "mc_v1.9.html"
    for p in pages:
        try:
            purl = (p.url or "").lower()
        except Exception:
            continue
        if needle in purl:
            return p
    return None


def open_mc_page(context, html: Path | None = None):
    url = _mc_url()
    if url:
        return _open_mc_url(context, url)
    html = html or MC_HTML
    if not html.is_file():
        raise SystemExit(f"Missing {html}")

    # REUSE an existing MC tab if there is one — don't stack more MC tabs in
    # Chrome (each extra tab slows the profile down).
    page = _find_mc_page(context, html)
    if page is not None:
        info("Reusing open Misroute Checker tab (not opening a new one):", html.name)
        # RELOAD only when the live tab is stale (missing current features) —
        # a reused tab kept the OLD MC_V2.html loaded (2026-09-17), but a
        # reload every run wastes seconds for no reason.
        try:
            modern = page.evaluate(
                "() => typeof detectDCFromInput === 'function' && "
                "!!document.getElementById('myDC2')")
        except Exception:
            modern = False
        if not modern:
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
    else:
        from lib import tabs
        page = context.new_page()
        tabs.register(page, "mc")  # tag BEFORE goto so the marker survives it
        page.goto(html.resolve().as_uri(), wait_until="domcontentloaded")
        ok("Opened Misroute Checker:", html.name)

    page.locator("#inputData").wait_for(timeout=15000)
    # Bring it to front and clear stale input/results before we paste new data.
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.evaluate("typeof clearAll === 'function' ? clearAll() : null")
    except Exception:
        pass
    return page


def _open_mc_url(context, url: str):
    """Open the Vercel MC site (basic-auth userinfo is in the URL, so Chrome
    answers the login itself — no prompt for the bot to get stuck on). Same
    reuse/reload/front/clear contract as the local-file path above."""
    from lib import tabs

    desc = _mc_desc(url)
    # REUSE an existing MC tab if there is one — don't stack more MC tabs in
    # Chrome (each extra tab slows the profile down).
    page = _find_mc_page(context, MC_HTML)
    if page is not None:
        info("Reusing open Misroute Checker tab (not opening a new one):", desc)
        # RELOAD only when the live tab is stale (missing current features).
        try:
            modern = page.evaluate(
                "() => typeof detectDCFromInput === 'function' && "
                "!!document.getElementById('myDC2')")
        except Exception:
            modern = False
        if not modern:
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
    else:
        page = context.new_page()
        tabs.register(page, "mc")  # tag BEFORE goto so the marker survives it
        page.goto(url, wait_until="domcontentloaded")
        ok("Opened Misroute Checker:", desc)

    page.locator("#inputData").wait_for(timeout=15000)
    # Bring it to front and clear stale input/results before we paste new data.
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.evaluate("typeof clearAll === 'function' ? clearAll() : null")
    except Exception:
        pass
    return page


def copy_reception_export(xlsx_path: Path, fast_mode: bool = False) -> str:
    """Ctrl+A twice in WPS/Excel, Copy, return clipboard (pasteable into MC)."""
    from lib.wps_copy import copy_from_wps

    text = copy_from_wps(xlsx_path, fast_mode=fast_mode)
    if "Order ID" not in text[:2000]:
        raise SystemExit("Copied text has no Order ID header. Do not paste into MC.")
    if "Calamba City" in text[:12000] and "Davao" not in text[:12000]:
        raise SystemExit(
            "This looks like the OLD Calamba reverse-logistics export. Stop."
        )
    ok("Copied Reception export from WPS:", len(text.splitlines()), "lines")
    return text


def paste_reception_into_mc(context, xlsx_path: Path, dc: str = "DAVAO DC",
                            dc2: str = ""):
    """The only post-WPS step: copy Reception export → paste MC → Run Validation."""
    text = copy_reception_export(xlsx_path)
    page = open_mc_page(context)
    info("Pasting Reception export into MC...")
    paste_and_validate(page, text, dc, dc2)
    return page


def _dismiss_mc_overlays(page) -> None:
    """Close any open MC modal (QR generator, settings, hub search...) so real
    clicks land. A leftover overlay from a previous run intercepts pointer
    events and made Run Validation time out (2026-09-13 crash)."""
    try:
        page.evaluate(
            """() => {
            for (const f of ['closeQRGen', 'closeSettings', 'closeHubSearch', 'closeKBHelp']) {
                try { if (typeof window[f] === 'function') window[f](); } catch (e) {}
            }
            document.querySelectorAll('.modal-overlay').forEach(m => { m.style.display = 'none'; });
        }"""
        )
    except Exception:
        pass


def paste_and_validate(page, csv_text: str, dc: str, dc2: str = "") -> None:
    """Original: paste once, set both DCs, single validation pass (both DCs at once)."""
    _dismiss_mc_overlays(page)
    page.evaluate(
        """(t) => {
            const el = document.getElementById('inputData');
            el.value = t;
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        csv_text,
    )
    ok(f"Pasted {len(csv_text)} chars into MC")
    # Auto-switch to the DC the batch belongs to BEFORE validating —
    # validating under the wrong DC flags everything as misroute.
    # Priority: DC derived from the scanned pending tabs (authoritative —
    # the pending scan knows where the batch goes) > page-side hub vote.
    detected = None
    try:
        detected = page.evaluate(
            "typeof detectDCFromInput === 'function' ? detectDCFromInput() : null")
    except Exception:
        detected = None
    opts = []
    try:
        opts = page.evaluate(
            "() => [...document.querySelectorAll('#myDC option')]"
            ".map(o => o.value)")
    except Exception:
        opts = []
    cur = page.input_value("#myDC")
    target, why = None, ""
    if dc and dc in opts:
        target, why = dc, "from scanned pending tabs"
    elif detected and detected in opts:
        target, why = detected, "detected from batch hubs"
    if target and target != cur:
        page.select_option("#myDC", target)
        info(f"DC auto-switch: {cur} -> {target} ({why})")
    else:
        info("DC / SOC:", cur)
    # optional 2nd DC — validate both at once (MC treats both as "mine")
    try:
        if page.locator("#myDC2").count():
            want2 = dc2 if (dc2 and dc2 in opts and dc2 != target) else ""
            page.select_option("#myDC2", want2)
            if want2:
                info(f"2nd DC set: {want2} — validating both DCs at once")
    except Exception:
        pass
    page.get_by_role("button", name=re.compile(r"Run Validation", re.I)).click()
    try:
        page.locator("#statsBar").wait_for(state="visible", timeout=60000)
    except Exception:
        err = page.locator(".toast.t-error").first
        extra = err.inner_text() if err.count() else "no stats bar"
        raise RuntimeError("MC validation failed: " + extra)
    total = page.locator("#s-total").inner_text()
    lh = page.locator("#s-lh").inner_text()
    ok(f"MC done. Total={total}  LH={lh}")


def paste_and_validate_one_dc(page, csv_text: str, dc: str) -> list[dict]:
    """Validate a SINGLE DC against a chunk that contains ONLY that DC's rows.

    This is the correct unit of work. MC marks a parcel MISROUTE purely because
    its Destination Station resolves to a DC other than the selected one, so a
    mixed batch always produces cross-DC false positives. Feeding one DC its own
    rows makes every verdict final — nothing to reconcile afterwards.
    """
    _dismiss_mc_overlays(page)
    page.evaluate(
        """(t) => {
            const el = document.getElementById('inputData');
            el.value = t;
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        csv_text,
    )
    opts = []
    try:
        opts = page.evaluate(
            "() => [...document.querySelectorAll('#myDC option')].map(o => o.value)")
    except Exception:
        opts = []
    if dc not in opts:
        raise RuntimeError(f"{dc!r} is not a DC option in MC (has: {opts})")
    # Only the DC under test is selected — a 2nd DC would excuse its parcels.
    page.select_option("#myDC", dc)
    try:
        if page.locator("#myDC2").count():
            page.select_option("#myDC2", "")
    except Exception:
        pass
    page.evaluate("typeof runCheck === 'function' ? runCheck() : null")
    try:
        page.locator("#statsBar").wait_for(state="visible", timeout=60000)
    except Exception:
        err = page.locator(".toast.t-error").first
        extra = err.inner_text() if err.count() else "no stats bar"
        raise RuntimeError(f"MC validation failed for {dc}: " + extra)
    rows = read_mc_results(page)
    for r in rows:
        r["validation_dc"] = dc
    mis = sum(1 for r in rows if r.get("result", "").upper() == "MISROUTE")
    lh = sum(1 for r in rows if r.get("grp", "").lower() == "lh")
    ok(f"{dc}: {len(rows)} validated, {mis} misroute, {lh} SOC-LH")
    return rows


def switch_lh_tab(page) -> None:
    page.locator('.tab-btn[data-tab="lh"]').click()
    page.locator("#panel-lh").wait_for(timeout=10000)


def read_lh_rows(page) -> list[tuple[str, str, str]]:
    """LH tab columns: SPX Number, Status, Remarks (SUCCESSFULLY SCAN)."""
    rows = page.evaluate(
        """() => [...document.querySelectorAll('#tbody-lh tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim().replace(/\\n/g, ' '))
        )"""
    )
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for cells in rows:
        if len(cells) < 2:
            continue
        to = (cells[0] or "").split()[0] if cells[0] else ""
        if not to or to in seen:
            continue
        seen.add(to)
        status = cells[1] if len(cells) > 1 else ""
        remarks = cells[2] if len(cells) > 2 else "SUCCESSFULLY SCAN"
        out.append((to, status, remarks))
    info(f"LH rows: {len(out)}")
    if out:
        info("  first:", out[0][0], out[0][1], out[0][2])
    return out


# ══════════════════════════════════════════════════════════════════
#  MC NAVIGATION HELPERS (learn how to operate the MC tool)
#  Non-breaking additions. DC-switching / re-validation comes later.
# ══════════════════════════════════════════════════════════════════

STAT_SELECTORS = {
    "total": "#s-total",
    "mis": "#s-mis",
    "good": "#s-good",
    "lh": "#s-lh",
    "pack": "#s-pack",
    "hvi": "#s-hvi",
    "rate": "#s-rate",
}

VALID_TABS = ["mis", "mishvi", "lh", "pack", "soc", "hvir", "hvi"]


def read_packed_oids(page) -> list[str]:
    """OIDs currently shown in MC's Packed tab (SOC Packed / Packing / Staging)."""
    try:
        cells = page.evaluate(
            """() => [...document.querySelectorAll('#tbody-pack tr')].map(tr => {
                const td = tr.querySelector('td');
                return td ? td.innerText.trim().split(/\\s+/)[0] : '';
            })"""
        )
    except Exception as e:
        warn("Could not read MC Packed tab:", e)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for c in cells or []:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    info(f"MC Packed tab: {len(out)} OID(s)")
    return out


def mc_ready(page) -> bool:
    """True if MC page is loaded (input + a DC select exist)."""
    try:
        page.locator("#inputData").wait_for(timeout=5000)
        page.locator("#myDC").wait_for(timeout=3000)
        return True
    except Exception:
        return False


def read_mc_stats(page) -> dict[str, str]:
    """Read the stats bar. Empty strings if a stat is hidden."""
    out: dict[str, str] = {}
    for key, sel in STAT_SELECTORS.items():
        try:
            out[key] = (page.locator(sel).inner_text() or "").strip()
        except Exception:
            out[key] = ""
    return out


def switch_mc_tab(page, tab: str) -> None:
    """Switch MC to a validator tab ('mis','lh','pack','soc','hvir','hvi')."""
    if tab not in VALID_TABS:
        raise ValueError(f"Unknown MC tab: {tab!r}")
    page.locator(f'.tab-btn[data-tab="{tab}"]').first.click()
    page.locator(f'#panel-{tab}').wait_for(timeout=10000)


def count_tab_rows(page, tab: str) -> int:
    """How many rows MC currently reports for a tab (via its tab badge)."""
    try:
        txt = (page.locator(f"#tab-cnt-{tab}").inner_text() or "0").strip()
        return int(re.sub(r"\D", "", txt) or 0)
    except Exception:
        return 0


def lh_has_content(page) -> dict:
    """Check the LH tab: switch to it and report whether it has rows."""
    switch_mc_tab(page, "lh")
    rows = read_lh_rows(page)  # dedupes + logs count
    has = len(rows) > 0
    first = rows[0] if rows else None
    return {
        "has_content": has,
        "count": len(rows),
        "rows": rows,
        "first": first,
    }


def run_validation(page) -> dict:
    """Ensure a Run Validation happens and MC settles, then return stats.

    Idempotent: only clicks if data is present; safe to call after a paste.
    """
    try:
        # If input has content, clicking Run Validation (or the shortcut
        # Ctrl+Shift+R -> runCheck) computes fresh results.
        page.get_by_role("button", name=re.compile(r"Run Validation", re.I)).click()
    except Exception:
        # fall back to MC's internal runCheck via the keyboard shortcut handler
        try:
            page.evaluate("typeof runCheck === 'function' ? runCheck() : null")
        except Exception:
            pass
    try:
        page.locator("#statsBar").wait_for(state="visible", timeout=60000)
    except Exception:
        err = page.locator(".toast.t-error").first
        extra = err.inner_text() if err.count() else "no stats bar"
        raise RuntimeError("MC validation failed: " + extra)
    return read_mc_stats(page)


def navigate_and_check_lh(page, dc: str | None = None) -> dict:
    """Validate (optionally under a DC) then check the LH tab.

    Returns a report dict the caller can branch on:
      { stats, lh: {has_content, count, rows, first} }
    Follows: if there is data -> validate; then if LH has rows -> flag
    (continue) ; if not -> no LH content -> continue.
    """
    if not mc_ready(page):
        raise RuntimeError("MC page not ready (missing input or DC select).")

    # 1) (optional) set the DC before validating
    if dc:
        try:
            page.select_option("#myDC", dc)
            info("DC / SOC set to:", dc)
        except Exception as e:
            warn("Could not set DC:", e)

    # 2) make sure there's input data to validate
    has_input = page.evaluate(
        "() => (document.getElementById('inputData')?.value || '').trim().length > 20"
    )
    if not has_input:
        # no data -> nothing to validate / no LH content by definition
        return {
            "validated": False,
            "stats": read_mc_stats(page),
            "lh": {"has_content": False, "count": 0, "rows": [], "first": None},
        }

    # 3) validate (if there is data)
    stats = run_validation(page)

    # 4) then check the LH tab
    lh = lh_has_content(page)
    if lh["has_content"]:
        ok(f"=> LH HAS content ({lh['count']} rows) — flag & continue.")
    else:
        warn("=> LH is empty — no LH content — continue.")
    return {"validated": True, "stats": stats, "lh": lh}


# ══════════════════════════════════════════════════════════════════
#  CAPTURE MC RESULTS FOR DUMP ALL
#  Reads MC's in-memory `results` array (exact source of truth, no
#  clipboard/OS dependency) and builds the rows to write to DUMP ALL.
#  DUMP ALL columns: A=TO  B=Status  C=Remarks
#
#  Precedence (per order):
#    1) MISROUTED  ->  TO | MISROUTED | <dc it belongs to / actual hub>
#                      (a wrong-DC parcel must NOT also be marked scanned)
#    2) SOC LH grp ->  TO | <SOC_LH status> | <LH remark, e.g. SUCCESSFULLY SCAN>
#  Anything already on the sheet is preserved (write_rows only fills blanks
#  and appends new T.O.s).
# ══════════════════════════════════════════════════════════════════


def read_mc_results(page) -> list[dict]:
    """Return MC's raw validated rows as a list of dicts.

    Reads the module-level `results` array that runCheck() populates.
    Keys mirror the JS result objects:
      spx, rawSt (status), result (MISROUTE/GOOD), grp (lh/...),
      resolvedDC, actualHub, hub, hvi.
    """
    try:
        rows = page.evaluate(
            """() => {
                if (typeof results === 'undefined' || !Array.isArray(results)) return [];
                return results.map(function (r) {
                    return {
                        spx: (r.spx || '').trim(),
                        rawSt: (r.rawSt || '').trim(),
                        result: (r.result || '').toUpperCase(),
                        grp: (r.grp || '').toLowerCase(),
                        resolvedDC: (r.resolvedDC || '').trim(),
                        actualHub: (r.actualHub || '').trim(),
                        hub: (r.hub || '').trim(),
                        hvi: !!r.hvi
                    };
                });
            }"""
        )
        return rows or []
    except Exception as e:
        warn("Could not read MC results array:", e)
        return []


def build_dump_rows(page, lh_remark: str | None = None) -> list[tuple[str, str, str]]:
    """Build DUMP ALL rows from MC results with misroute > SOC-LH precedence.

    Returns a list of (to, status, remarks) tuples, deduplicated by TO.
    Misroute rows are emitted first; SOC-LH rows only for TOs that were not
    already emitted as misroutes.
    HVI misroutes are NEVER dumped — the user handles those manually (they
    stay visible in MC's HVI Misroute tab).
    """
    rows = read_mc_results(page)
    return build_dump_rows_from_results(rows, lh_remark, page)


def build_dump_rows_from_results(
    rows: list[dict],
    lh_remark: str | None = None,
    page = None,
) -> list[tuple[str, str, str]]:
    """Build DUMP ALL rows from pre-fetched MC results.

    Same logic as build_dump_rows but works with a results list instead of
    reading from the page.

    Every DC must have been validated against ONLY its own rows (see
    paste_and_validate_one_dc). There is deliberately no OID->DC remapping here:
    the results are already final, and any post-hoc guessing about which DC
    "owned" a row is what used to flag every GENSAN parcel as a misroute.

    Args:
        rows: result dicts from read_mc_results(), one group per validated DC
        lh_remark: Optional LH remark override
        page: Optional page reference to fetch the LH remark from MC

    Returns:
        List of (to, status, remarks) tuples for DUMP ALL
    """
    if not rows:
        info("No MC results available to capture.")
        return []

    # Determine LH remark
    if not lh_remark and page is not None:
        try:
            lh_remark = page.evaluate(
                "() => (typeof LS !== 'undefined' ? LS.getLH() : 'SUCCESSFULLY SCAN')"
            )
        except Exception:
            lh_remark = "SUCCESSFULLY SCAN"
    if not lh_remark:
        lh_remark = "SUCCESSFULLY SCAN"

    mis_rows: list[tuple[str, str, str]] = []
    lh_rows: list[tuple[str, str, str]] = []
    seen_mis: set[str] = set()
    hvi_skipped = 0

    # Pass 1: misroutes (result == MISROUTE, and not a RETURN* status).
    # HVI misroutes are skipped — the user handles those by hand.
    for r in rows:
        if r.get("result", "").upper() == "MISROUTE" and not r.get("rawSt", "").upper().startswith("RETURN"):
            if r.get("hvi"):
                hvi_skipped += 1
                continue
            to = r.get("spx", "").strip()
            if not to or to in seen_mis:
                continue
            seen_mis.add(to)
            # Remarks = the ACTUAL hub the order belongs to (not the DC).
            remarks = r.get("actualHub") or r.get("hub") or "MISROUTE"
            mis_rows.append((to, "MISROUTED", remarks))
    if hvi_skipped:
        info(f"HVI misroutes left for manual handling (NOT dumped): {hvi_skipped}")

    # Pass 2: SOC LH group, skipping TOs already flagged as misroutes.
    for r in rows:
        if r.get("grp", "").lower() == "lh":
            to = r.get("spx", "").strip()
            if not to or to in seen_mis:
                continue
            seen_mis.add(to)
            status = r.get("rawSt") or "SOC_LH"
            lh_rows.append((to, status, lh_remark))

    out = mis_rows + lh_rows
    info(
        f"Capture for DUMP ALL: {len(mis_rows)} misroute + "
        f"{len(lh_rows)} SOC-LH = {len(out)} row(s)"
    )
    if mis_rows:
        info("  misroute sample:", mis_rows[0][0], "->", mis_rows[0][2])
    if lh_rows:
        info("  SOC-LH sample:", lh_rows[0][0], "->", lh_rows[0][2])
    return out


