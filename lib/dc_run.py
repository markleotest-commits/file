"""Per-DC scoping: which dock tabs belong to a DC, and that DC's pending T.O.s.

A run is ONE DC, start to finish. That is the whole point.

MC decides MISROUTE purely from Destination Station vs the DC selected in the
sidebar, so a batch containing both DAVAO and GENSAN parcels can never give a
correct answer for both at once. run_mc.py never had this problem because it
only ever handled a single DC. So we scope the run at the *first* step -- the
sheets read -- instead of trying to unpick a mixed batch at the end.

    DAVAO DC  <- DAVAO DAC 1..4
    GENSAN DC <- GENSAN DAC 5..7
"""

from __future__ import annotations

from datetime import datetime

from lib import cli
from lib.pending_tos import (
    connect,
    derive_dc,
    exclude_validated,
    fetch_scan_grids,
    list_dated_sheets,
    list_tab_titles,
    match_real_title,
    merge_pending,
    open_dated_sheet,
    open_named_tab,
    open_today_sheet,
    parse_sheet_date,
    pending_tos_from_config_ws,
    today_sheet_title,
)
from lib.state import PendingTO, RunState, generate_run_id

# Dock tab -> DC, for when config does not list the tabs explicitly.
_PREFIX_RULES = (
    ("DAVAO", "DAVAO DC"),
    ("GENSAN", "GENSAN DC"),
)


def all_dcs(config) -> list[str]:
    """Every DC reachable from config.SOURCE_TABS (or by tab-name prefix)."""
    tabs = getattr(config, "SOURCE_TABS", None) or []
    dcs: list[str] = []
    for t in tabs:
        d = derive_dc(t)
        if d and d not in dcs:
            dcs.append(d)
    if dcs:
        return dcs
    return [dc for _, dc in _PREFIX_RULES]


def tabs_for_dc(config, dc: str) -> list[str]:
    """The dock tabs that feed one DC."""
    target = derive_dc(dc) or dc
    tabs = getattr(config, "SOURCE_TABS", None) or []
    hits = [t for t in tabs if derive_dc(t) == target]
    if hits:
        return hits
    # No explicit list configured: fall back to tab names on today's sheet.
    for prefix, name in _PREFIX_RULES:
        if name == target:
            return [prefix]
    return []


def open_sheet(config, sheet_date=None):
    """Open the DAC OFFLINE spreadsheet for `sheet_date` (None = today).

    ``sheet_date`` may be a date, a datetime, or an "YYYY-MM-DD" string; see
    lib.pending_tos.parse_sheet_date().
    """
    gc = connect(config.CREDENTIALS_FILE)
    return open_dated_sheet(
        gc, getattr(config, "SHEET_ID", ""), config.SHEET_TITLE_PREFIX,
        parse_sheet_date(sheet_date),
    )


def dated_sheets(config) -> list[dict]:
    """Every dated DAC OFFLINE sheet on Drive, newest first. Feeds the UI picker.

    Returns [] if Drive or the credentials are unavailable, so the page shows an
    empty date list plus AUTO rather than the launcher failing to start.
    """
    try:
        gc = connect(config.CREDENTIALS_FILE)
        return list_dated_sheets(gc, config.SHEET_TITLE_PREFIX)
    except Exception as e:
        warn("Could not list dated sheets:", str(e).splitlines()[0][:80])
        return []


def pending_for_dc(config, dc: str, sheet_date=None,
                   sh=None) -> tuple[list[str], list[dict]]:
    """Read this DC's pending T.O.s. Returns (tab_titles, pending dicts).

    ``sh`` lets the caller pass a spreadsheet it already opened, so a run opens
    the chosen date once instead of three times.
    """
    want = tabs_for_dc(config, dc)
    if not want:
        raise SystemExit(f"No dock tab configured for {dc}. Set config.SOURCE_TABS.")

    if sh is None:
        sh = open_sheet(config, sheet_date)
    # Batched (bug 41): ONE worksheets() for matching, ONE values_batch_get
    # for all tab reads. The old code called list_tab_titles() (= a full
    # worksheets() round trip) once per wanted name, then sh.worksheet() +
    # get_values() per tab — ~15 API calls for this DC's tabs alone.
    metas = sh.worksheets()
    titles = [ws.title for ws in metas]
    picked: list[str] = []
    for name in want:
        real = match_real_title(name, titles)
        if real is None:
            cli.warn(f"Tab not found: {name!r} — skipped")
            continue
        picked.append(real)

    if not picked:
        raise SystemExit(
            f"None of {want} exist in {sh.title!r}.\n"
            f"Available: {', '.join(titles)}"
        )

    _real, grids, _dump = fetch_scan_grids(
        sh, picked, config, dump_tab=None, metas=metas)
    out = merge_pending(_real, grids, config)
    return picked, out


def build_state(config, dc: str, revalidate: bool = False,
                sheet_date=None) -> RunState:
    """Fresh per-DC state: this DC's tabs, this DC's pending T.O.s.

    ``revalidate=True`` ignores the "already in DUMP ALL" filter, so a DC can
    be validated again. That is what you want after a failed or partial run:
    the T.O.s are still blank on the source tab, so the normal read finds
    almost nothing and the run looks like it has no work to do.

    ``sheet_date`` picks which day's 'DAC OFFLINE …' file to read. None means
    today. The sheet is opened ONCE and reused for both the dock tabs and the
    DUMP ALL filter, so the read and the filter can never disagree about which
    day is being worked on.
    """
    cli.section(f"Read pending T.O.s for {dc}"
                + (f"  ({parse_sheet_date(sheet_date):%d %B %Y})"
                   if parse_sheet_date(sheet_date) else ""))
    sh = open_sheet(config, sheet_date)
    picked, pending = pending_for_dc(config, dc, sh=sh)
    if not pending:
        raise SystemExit(f"{dc} has no pending T.O.s. Nothing to do.")

    if revalidate:
        cli.warn("Re-validating: ignoring the 'already in DUMP ALL' filter.")
        excluded: list[str] = []
    else:
        pending, excluded = exclude_validated(pending, sh, config)

    objs = [PendingTO(to=p["to"], row=p["row"], col=p["to_col"], tab=p["tab"])
            for p in pending]
    return RunState(
        run_id=generate_run_id(objs),
        sheet_title=sh.title,
        source_tabs=picked,
        pending=objs,
        excluded_validated=excluded,
        created_at=datetime.now().isoformat(),
        dc=derive_dc(dc) or dc,
    )


def write_dock_map(state: RunState) -> None:
    """pending_tab_map.txt — which dock each T.O. came from.

    Only informational now that a run is single-DC, but packed_scrape and any
    later cross-run analysis still read it.
    """
    from pathlib import Path

    dest = Path(__file__).resolve().parent.parent / "pending_tab_map.txt"
    dest.write_text(
        "\n".join(f"{p.to}\t{p.tab}" for p in state.pending), encoding="utf-8"
    )
    cli.kv("Dock map", f"{len(state.pending)} entries -> {dest.name}")
