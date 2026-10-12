"""Trace one T.O. / OID (PH...) across the recent DAC OFFLINE sheets.

The date picker works on ONE day. This answers the other question --
"where has this T.O. already been scanned?" -- by walking every dated
'DAC OFFLINE ...' file on Drive from the present date back N days (a
calendar sweep) and looking for the value in the dock tabs' T.O. columns,
plus that day's DUMP ALL.

Read-only: Google Drive + Sheets only. No FMS, no Chrome, no writes.
Every call here belongs on the main thread (see the queue rationale in
lib/panel_server.py), so the panel enqueues it instead of running it in
the HTTP thread.

The pure helpers (normalize / match_cell / window_dates / scan_grid /
scan_dump) carry no API dependency, so tools/check_trace_oid.py can test
them offline on synthetic grids.
"""

from __future__ import annotations

from datetime import date, timedelta

from gspread.utils import rowcol_to_a1

from lib import cli
from lib.pending_tos import (
    connect,
    fetch_scan_grids,
    is_real_to,
    list_dated_sheets,
    resolve_source_tabs,
)


def normalize(value) -> str:
    """Compare key: upper-case with every whitespace removed."""
    return "".join(str(value or "").split()).upper()


def match_cell(query: str, cell: str) -> str | None:
    """'exact' | 'partial' | None -- how this cell matches the query.

    The sheet holds T.O.s in several shapes (PH26..., TO2026..., SPEPH...),
    so a value that merely CONTAINS the query still counts, reported as
    'partial'. Header rows can never match: the cell itself must look like a
    real T.O. (is_real_to), which is what keeps 'T.O. NUMBER' out of the
    results when the query is short.
    """
    if not query:
        return None
    raw = str(cell or "").strip()
    if not raw or not is_real_to(raw):
        return None
    v = normalize(raw)
    if v == query:
        return "exact"
    if query in v:                       # PH... typed, sheet holds SPEPH...
        return "partial"
    if len(query) >= 8 and v in query:   # SPEPH... typed, sheet holds PH...
        return "partial"
    return None


def window_dates(rows, days, today=None) -> list[dict]:
    """Dated sheets from `today` back `days` days (both ends inclusive),
    newest first. Drive holds pre-created FUTURE sheets, so anything after
    today is dropped -- the sweep starts at the present date."""
    today = today or date.today()
    cutoff = today - timedelta(days=max(1, int(days)) - 1)
    lo, hi = cutoff.isoformat(), today.isoformat()
    return [r for r in rows if r.get("date") and lo <= r["date"] <= hi]


def scan_grid(grid, query, first_to_col=2, block_width=5,
              data_start_row=6) -> list[dict]:
    """Every matching T.O. cell in the block columns, whatever its Status.

    Same block walk as pending_tos._pending_from_grid (B, G, L, ... with the
    Status one column to the right) but WITHOUT the pending filter: the
    question here is "was this scanned on that day?", and a FILLED Status is
    an answer too. `grid` starts at data_start_row, so row numbers match.
    """
    hits: list[dict] = []
    if not grid or not query:
        return hits
    last_col = max((len(r) for r in grid), default=first_to_col)
    to_col = first_to_col
    while to_col <= last_col:
        col_letter = rowcol_to_a1(1, to_col).rstrip("1")
        for offset, row in enumerate(grid):
            cell = row[to_col - 1] if to_col - 1 < len(row) else ""
            kind = match_cell(query, cell)
            if not kind:
                continue
            status = row[to_col] if to_col < len(row) else ""
            hits.append({
                "to": str(cell).strip(),
                "match": kind,
                "row": data_start_row + offset,
                "to_col": col_letter,
                "status": str(status or "").strip(),
            })
        to_col += block_width
    return hits


def scan_dump(dump_grid, query) -> list[dict]:
    """DUMP ALL rows for this T.O. (A = T.O., B = Status, C = Remarks)."""
    hits: list[dict] = []
    for i, row in enumerate(dump_grid or []):
        cell = row[0] if row else ""
        kind = match_cell(query, cell)
        if not kind:
            continue
        hits.append({
            "to": str(cell).strip(),
            "match": kind,
            "row": i + 1,
            "status": str(row[1] or "").strip() if len(row) > 1 else "",
            "remarks": str(row[2] or "").strip() if len(row) > 2 else "",
        })
    return hits


def _first_line(e: Exception) -> str:
    return (str(e).splitlines()[0] if str(e) else type(e).__name__)[:160]


def _quiet_match(want: str, titles: list[str]) -> str | None:
    """Configured tab name -> real tab title, WITHOUT the log line.

    match_real_title() announces every spacing mismatch ('DAVAO DAC 1' vs
    'DAVAO DAC1'), which is useful once in a run but is 7 lines per sheet
    across a 30-day sweep. Feeding fetch_scan_grids() titles it can match
    exactly keeps the sweep output to one line per day.
    """
    if not want:
        return None
    if want in titles:
        return want

    def norm(s: str) -> str:
        return "".join((s or "").split()).lower()

    nw = norm(want)
    for t in titles:
        if norm(t) == nw:
            return t
    return None


def trace_oid(config, oid, days=30, progress=None) -> dict:
    """Sweep today -> today-(days-1) for `oid`. Returns a JSON-safe result.

    `progress(i, n, date, title)` is called before each sheet, so the panel
    can show "scanning 7/30 - 2026-10-02" while the main thread works.

    Result:
        oid, query, days, from, to, sheets_on_drive,
        checked_dates[], checked, dates_hit, hits[], errors[]
    A hit is {date, title, tab, row, to_col, to, status, match, dump} where
    status '' means still pending, tab None means it was in DUMP ALL only,
    and dump is that day's DUMP ALL row ({row, status, remarks}) or None.
    """
    q = normalize(oid)
    if not q:
        raise ValueError("No T.O. / OID given. Type a value like PH261001234567.")
    days = max(1, min(int(days or 30), 365))

    gc = connect(config.CREDENTIALS_FILE)
    prefix = getattr(config, "SHEET_TITLE_PREFIX", "DAC OFFLINE")
    all_rows = list_dated_sheets(gc, prefix)
    rows = window_dates(all_rows, days)
    today = date.today()
    cutoff = today - timedelta(days=days - 1)

    result = {
        "oid": str(oid).strip(),
        "query": q,
        "days": days,
        "from": cutoff.isoformat(),
        "to": today.isoformat(),
        "sheets_on_drive": len(all_rows),
        "checked_dates": [r["date"] for r in rows],
        "checked": 0,
        "dates_hit": 0,
        "hits": [],
        "errors": [],
    }
    if not rows:
        result["note"] = (
            f"No dated '{prefix} ...' sheet between {result['from']} and "
            f"{result['to']}. {len(all_rows)} dated file(s) on Drive."
        )
        return result

    first_to_col = getattr(config, "FIRST_TO_COL", 2)
    block_width = getattr(config, "BLOCK_WIDTH", 5)
    data_start_row = getattr(config, "DATA_START_ROW", 6)
    dump_tab = getattr(config, "DUMP_ALL_TAB", "DUMP ALL")

    for i, row in enumerate(rows, 1):
        if progress:
            try:
                progress(i, len(rows), row["date"], row["title"])
            except Exception:
                pass
        try:
            sh = gc.open_by_key(row["id"]) if row.get("id") else None
            if sh is None:
                raise FileNotFoundError("no Drive id for this file")
        except Exception as e:
            result["errors"].append({"date": row["date"],
                                     "title": row["title"],
                                     "error": _first_line(e)})
            cli.warn(f"Trace {row['date']}: cannot open {row['title']} — "
                     f"{_first_line(e)}")
            continue
        try:
            metas = sh.worksheets()
            titles = [w.title for w in metas]
            try:
                want = resolve_source_tabs(sh, config, titles=titles)
            except SystemExit as e:
                # No configured dock tab in this file: still check DUMP ALL
                # rather than losing the whole day.
                want = []
                result["errors"].append({"date": row["date"],
                                         "title": row["title"],
                                         "error": _first_line(e)})
            resolved = [(w, _quiet_match(w, titles)) for w in want]
            picked = [t for _w, t in resolved if t]
            for w, t in resolved:
                if t is None:
                    cli.warn(f'Trace {row["date"]}: tab not found: {w!r}')
            real_map, grids, dump_grid = fetch_scan_grids(
                sh, picked, config,
                dump_tab=dump_tab if dump_tab in titles else None,
                metas=metas,
            )
        except Exception as e:
            result["errors"].append({"date": row["date"],
                                     "title": row["title"],
                                     "error": _first_line(e)})
            cli.warn(f"Trace {row['date']}: read failed — {_first_line(e)}")
            continue

        result["checked"] += 1
        day_hits: list[dict] = []
        for _want, real in real_map.items():
            for h in scan_grid(grids.get(real, []), q, first_to_col,
                               block_width, data_start_row):
                h.update({"date": row["date"], "title": row["title"],
                          "tab": real})
                day_hits.append(h)
        dump_hits = scan_dump(dump_grid, q)
        if dump_hits and not day_hits:
            # Present in DUMP ALL but no longer on a dock tab that day.
            d = dump_hits[0]
            day_hits.append({"date": row["date"], "title": row["title"],
                             "tab": None, "to": d["to"], "match": d["match"],
                             "row": None, "to_col": None, "status": ""})
        if day_hits:
            d0 = dump_hits[0] if dump_hits else None
            for h in day_hits:
                h["dump"] = d0
            result["hits"].extend(day_hits)
            spots: list[str] = []
            for h in day_hits:
                spot = h["tab"] or "DUMP ALL"
                if h.get("row"):
                    spot += (f' r{h["row"]} ({h["to_col"]})'
                             if h.get("to_col") else f' r{h["row"]}')
                if spot not in spots:
                    spots.append(spot)
            cli.info(f"Trace {row['date']}: HIT in " + ", ".join(spots))
        else:
            cli.info(f"Trace {row['date']}: no match")

    result["dates_hit"] = len({h["date"] for h in result["hits"]})
    return result
