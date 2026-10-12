
"""Read pending T.O.s from one DAC tab. Blanks in the middle do not stop us."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import gspread
from gspread.utils import rowcol_to_a1

TO_RE = re.compile(r"(?=.*\d)[A-Za-z0-9]{8,}$")


from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def is_real_to(value: str) -> bool:
    return bool(TO_RE.fullmatch(value.strip()))


def sheet_title_for(when, prefix: str = "DAC OFFLINE") -> str:
    """'DAC OFFLINE OCTOBER 1 2026' for a date or datetime.

    The day is NOT zero-padded -- that is the shape the team actually names
    files, and _title_date() parses it back. today_sheet_title() is just this
    with today's date, so there is exactly one place that builds the title.
    """
    return f"{prefix} {when.strftime('%B').upper()} {when.day} {when.year}"


def today_sheet_title(prefix: str = "DAC OFFLINE") -> str:
    return sheet_title_for(datetime.now(), prefix)


def parse_sheet_date(value) -> datetime | None:
    """'2026-10-01', '2026/10/01' or '20261001' -> datetime. None if unusable.

    Anything unparseable is a mistake worth reporting, not a silent "today", so
    this raises on junk rather than defaulting.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d %B %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Cannot read {s!r} as a date. Use YYYY-MM-DD, e.g. 2026-10-01."
    )


def connect(credentials_file: Path):
    if not Path(credentials_file).is_file():
        raise FileNotFoundError(
            f"Missing {credentials_file}. Finish STEP 4 (credentials.json)."
        )
    return gspread.service_account(filename=str(credentials_file))


def _auth_fail(e) -> None:
    """Turn Google's cryptic auth errors into the fix-it ladder."""
    s = str(e)
    if "invalid_grant" in s or "JWT" in s or "RefreshError" in type(e).__name__:
        raise SystemExit(
            "Google rejected credentials.json (invalid_grant / Invalid JWT Signature).\n"
            "  The private key signed fine, but Google no longer recognises it:\n"
            "  either the key was revoked/deleted in Google Cloud, or this folder has\n"
            "  a different/corrupted credentials.json (common after folder moves).\n"
            "  FIX A: copy the known-good credentials.json from your working folder.\n"
            "  FIX B: Cloud Console > IAM & Admin > Service Accounts > the account >\n"
            "         Keys > Add Key > JSON > save as credentials.json.\n"
            "  Check which account:  py -c \"import json;d=json.load(open('credentials.json'));print(d['client_email'],d['private_key_id'])\""
        )


def _drive_files_named(gc, title: str) -> list[dict]:
    """All live spreadsheets with this exact name, newest-created first.

    gspread's open(title) returns whichever file Drive lists first — with a
    recreated/copied daily sheet (two files, same name) that can be the OLD
    one. So we query Drive ourselves and sort by createdTime desc.
    """
    return _drive_files_query(gc, f'name="{title}"')


def _drive_files_prefixed(gc, prefix: str) -> list[dict]:
    """All live spreadsheets whose name contains the prefix, newest first."""
    return _drive_files_query(gc, f'name contains "{prefix}"')


def _drive_files_query(gc, name_q: str) -> list[dict]:
    """Every live spreadsheet matching `name_q`, following all result pages.

    pageSize is 100. The daily sheets plus the pre-created future ones outgrew
    one page, and a live file that was created long ago (the team reuses and
    renames files) sat on page 2+ -- so "the latest date" could drop out of the
    listing. Follow nextPageToken instead of trusting the first page.
    """
    url = "https://www.googleapis.com/drive/v3/files"
    base = {
        "q": f'{name_q} and mimeType="application/vnd.google-apps.spreadsheet" '
             "and trashed=false",
        "fields": "nextPageToken,files(id,name,createdTime,modifiedTime)",
        "orderBy": "createdTime desc",
        "pageSize": 100,
    }
    out: list[dict] = []
    token: str | None = None
    for _ in range(20):  # hard cap: 2000 files
        params = dict(base)
        if token:
            params["pageToken"] = token
        resp = None
        for getter in (
            lambda: gc.request("get", url, params=params),
            lambda: gc.http_client.request("get", url, params=params),
        ):
            try:
                resp = getter()
                break
            except Exception:
                continue
        if resp is None:
            break
        data = resp.json() or {}
        out.extend(data.get("files", []) or [])
        token = data.get("nextPageToken")
        if not token:
            break
    return out


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}


def _title_date(name: str):
    """(year, month, day) parsed from a 'DAC OFFLINE MONTH D YYYY' title."""
    m = re.search(
        r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
        r"OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})\s+(\d{4})",
        (name or "").upper())
    if not m:
        return None
    return (int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def _norm_title(name: str) -> str:
    """Title comparison key: upper-case with runs of whitespace collapsed.

    The team's filenames carry stray spaces -- live: "DAC OFFLINE OCTOBER 12
    2026" had TWO spaces before the year. list_dated_sheets() parses that date
    with a tolerant regex, so the picker showed that day, but an exact-name
    open missed the file and told the operator to share a file already shared.
    """
    return " ".join((name or "").split()).upper()


def _same_title(a: str, b: str) -> bool:
    return _norm_title(a) == _norm_title(b)


def _pick_sheet_file(files: list[dict], title: str):
    """Choose the LIVE daily sheet. createdTime is meaningless here (the team
    renames/reuses files — a 'SEPTEMBER 13' file created Sep 8 was observed).
    Order of trust: exact today-title → newest title-date not in the future
    → most recently MODIFIED file."""
    if not files:
        return None
    now = datetime.now()
    tkey = (now.year, now.month, now.day)
    exact = [f for f in files if _same_title(f.get("name") or "", title)]
    if exact:  # today's file exists — the most recently edited copy wins
        exact.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
        return exact[0]
    dated = [(f, _title_date(f.get("name") or "")) for f in files]
    past = [(f, d) for f, d in dated if d and d <= tkey]
    if past:  # newest title-date ≤ today; ties broken by last modification
        past.sort(key=lambda fd: (fd[0].get("modifiedTime") or "", fd[1]),
                  reverse=True)
        return past[0][0]
    future = [(f, d) for f, d in dated if d]
    if future:  # only future-dated files on Drive — nearest one is the best guess
        future.sort(key=lambda fd: fd[1])
        warn("No past-dated sheet on Drive — using nearest upcoming title.")
        return future[0][0]
    files.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
    return files[0]


def _open_exact(gc, title: str):
    """Open one spreadsheet by exact title. None when it does not exist."""
    try:
        return gc.open(title)
    except gspread.SpreadsheetNotFound:
        return None
    except Exception as e:
        _auth_fail(e)
        raise


def list_dated_sheets(gc, prefix: str = "DAC OFFLINE") -> list[dict]:
    """Every dated 'DAC OFFLINE <MONTH D YYYY>' spreadsheet on Drive.

    Newest date first, for the UI's date picker. One row per title-date: when
    Drive holds several files with the same title-date (the team recreates and
    copies these), the most recently modified one wins -- the same rule
    _pick_sheet_file() uses.

    Returns [] when Drive cannot be reached, so a picker that fails to fill is
    a visible empty list rather than a crash.
    """
    try:
        files = _drive_files_prefixed(gc, prefix)
    except Exception:
        return []
    best: dict[tuple, dict] = {}
    for f in files:
        d = _title_date(f.get("name") or "")
        if not d:
            continue
        row = {
            "date": f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}",
            "title": f.get("name") or "",
            "id": f.get("id") or "",
            "modified": (f.get("modifiedTime") or "")[:16].replace("T", " "),
        }
        cur = best.get(d)
        if cur is None or row["modified"] > cur["modified"]:
            best[d] = row
    return sorted(best.values(), key=lambda r: r["date"], reverse=True)


def open_dated_sheet(gc, sheet_id: str, prefix: str, sheet_date=None):
    """Open the DAC OFFLINE spreadsheet for `sheet_date`. None = today.

    With an explicit date, ONLY that day's file is acceptable. open_today_sheet()
    deliberately falls back to "newest title-date <= today", which is right for an
    unattended run at 6am but catastrophic here: a mis-picked date would read
    another day's pending T.O.s and then write DUMP ALL into that other day.
    So a named date either opens or raises, naming what IS available.
    """
    if sheet_date is None:
        return open_today_sheet(gc, sheet_id, prefix)

    when = parse_sheet_date(sheet_date)
    title = sheet_title_for(when, prefix)
    try:
        files = _drive_files_prefixed(gc, prefix)
    except Exception:
        files = []

    # Match the requested DATE, not the title string. The team's filenames
    # carry stray spaces ("OCTOBER 12  2026"); list_dated_sheets() reads that
    # date fine, so the picker listed the day, but the old exact-name open
    # missed the file and said to share a file that WAS shared. Reuse the same
    # tolerant date match the picker uses, then open the newest edit.
    want = (when.year, when.month, when.day)
    hits = [f for f in files if _title_date(f.get("name") or "") == want]
    if not hits:  # a name that only differs by spacing/case still counts
        hits = [f for f in files if _same_title(f.get("name") or "", title)]
    if hits:
        hits.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
        pick = hits[0]
        mod = (pick.get("modifiedTime") or "")[:16].replace("T", " ")
        info(f'Opened "{pick["name"]}" for the selected date (modified {mod}).')
        try:
            return gc.open_by_key(pick["id"])
        except Exception as e:
            _auth_fail(e)
            warn("Open by id failed - falling back to title lookup.")

    sh = _open_exact(gc, title)
    if sh is not None:
        ok("Opened by title:", sh.title)
        return sh

    dated = list_dated_sheets(gc, prefix)
    have = ", ".join(r["date"] for r in dated) or "(none shared with the bot)"
    raise FileNotFoundError(
        f'No spreadsheet named "{title}" is available to the bot.\n'
        f"  Dates on Drive: {have}\n"
        f"  Pick one of those on the page, or share that file with the bot email."
    )


def open_today_sheet(gc, sheet_id: str, prefix: str):
    """Open the LIVE daily sheet: all 'DAC OFFLINE …' files on Drive are
    ranked (exact today title → newest title-date ≤ today → last modified),
    then SHEET_ID fallback with the old-file warning."""
    title = today_sheet_title(prefix)
    try:
        files = _drive_files_prefixed(gc, prefix)
    except Exception:
        files = []
    pick = _pick_sheet_file(files, title)
    if pick:
        d = _title_date(pick.get("name") or "")
        mod = (pick.get("modifiedTime") or "")[:16].replace("T", " ")
        info(f'Picked "{pick["name"]}" (title-date {d or "unknown"}, '
             f'modified {mod}, looking for "{title}") out of '
             f"{len(files)} '{prefix} …' file(s).")
        try:
            sh = gc.open_by_key(pick["id"])
            ok("Opened latest sheet:", sh.title)
            return sh
        except Exception as e:
            _auth_fail(e)
            warn("Newest-file open failed — falling back to title lookup.")
    try:
        sh = gc.open(title)
        ok("Opened by today's title:", sh.title)
        return sh
    except gspread.SpreadsheetNotFound:
        pass
    except Exception as e:
        _auth_fail(e)
        raise
    if sheet_id:
        warn("WARNING: today's file not found by name. Using SHEET_ID (may be OLD).")
        info("Clear SHEET_ID in config.py so the bot follows the daily DAC OFFLINE file.")
        try:
            return gc.open_by_key(sheet_id)
        except Exception as e:
            _auth_fail(e)
            raise
    raise FileNotFoundError(
        f'Could not open spreadsheet named "{title}". '
        "Share today's file (or its Drive folder) with the bot email."
    )


def _col_values(grid: list[list[str]], col_1based: int) -> list[str]:
    idx = col_1based - 1
    out = []
    for row in grid:
        out.append(row[idx].strip() if idx < len(row) else "")
    return out


def _pending_from_grid(
    grid: list[list[str]],
    tab_title: str,
    first_to_col: int = 2,
    block_width: int = 5,
    data_start_row: int = 6,
) -> list[dict]:
    """Pending T.O.s from an already-fetched value grid (no API calls).

    Same parse as pending_tos_from_tab(); the grid is rows of cell strings
    starting at row 1 (only rows >= data_start_row are scanned).
    """
    pending: list[dict] = []
    seen: set[str] = set()
    last_col = max((len(r) for r in grid), default=first_to_col)
    to_col = first_to_col

    def _col(col_1based: int) -> list[str]:
        idx = col_1based - 1
        return [
            (row[idx].strip() if idx < len(row) else "")
            for row in grid
        ]

    while to_col <= last_col:
        status_col = to_col + 1
        tos = _col(to_col)
        statuses = _col(status_col)
        col_letter = rowcol_to_a1(1, to_col).rstrip("1")

        for offset, to in enumerate(tos):
            if not to or not is_real_to(to):
                continue
            status = statuses[offset] if offset < len(statuses) else ""
            if status:
                continue
            if to in seen:
                continue
            seen.add(to)
            pending.append(
                {
                    "to": to,
                    "row": data_start_row + offset,
                    "to_col": col_letter,
                    "tab": tab_title,
                }
            )

        to_col += block_width

    return pending


def pending_tos_from_tab(
    ws,
    first_to_col: int = 2,
    block_width: int = 5,
    data_start_row: int = 6,
) -> list[dict]:
    last_row = max(ws.row_count, data_start_row)
    last_col = max(ws.col_count, first_to_col)
    a1 = f"{rowcol_to_a1(data_start_row, 1)}:{rowcol_to_a1(last_row, last_col)}"
    grid = ws.get_values(a1)
    return _pending_from_grid(
        grid, ws.title,
        first_to_col=first_to_col,
        block_width=block_width,
        data_start_row=data_start_row,
    )


def _norm(s: str) -> str:
    """Whitespace/case-insensitive compare key (DAC tab names drift:
    'DAVAO DAC 4' one day, 'DAVAO DAC4' the next)."""
    return re.sub(r"\s+", "", s or "").lower()


def list_tab_titles(sh) -> list[str]:
    return [ws.title for ws in sh.worksheets()]


def derive_dc(tab_title: str) -> str | None:
    """'DAVAO DAC 2' -> 'DAVAO DC'; 'GENSAN DAC' -> 'GENSAN DC'.
    The pending scan already knows where the batch belongs."""
    t = (tab_title or "").upper().strip()
    if not t:
        return None
    # trailing "DAC"/"DAC 2"/"DAC5" -> "DC" (GENSAN DAC5 -> GENSAN DC)
    t = re.sub(r"\bDAC(\s*\d+)?$", "DC", t).strip()
    t = re.sub(r"\s*\d+$", "", t).strip()
    return t or None


def derive_dcs_from_tabs(titles: list[str]) -> list[str]:
    """Distinct DCs of the scanned source tabs, most-frequent first."""
    votes: dict[str, int] = {}
    for t in titles or []:
        d = derive_dc(t)
        if d:
            votes[d] = votes.get(d, 0) + 1
    return [k for k, _ in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))]


def derive_dc_from_tabs(titles: list[str]) -> str | None:
    """Majority DC of the scanned source tabs."""
    d = derive_dcs_from_tabs(titles)
    return d[0] if d else None


def resolve_source_tabs(sh, config, titles: list[str] | None = None) -> list[str]:
    """Which tab(s) to read pending T.O.s from.

    Priority:
      1. config.SOURCE_TABS (explicit list) if set/non-empty.
      2. config.SOURCE_TAB_PREFIX (auto-discover all tabs starting with the
         prefix, minus config.SOURCE_TAB_EXCLUDE). e.g. prefix "DAVAO DAC"
         picks DAVAO DAC 2,3,4,... and EXCLUDE removes DAVAO DAC 1.
      3. config.SOURCE_TAB (single tab, default) if present.
    """
    # 1) explicit list
    tabs = getattr(config, "SOURCE_TABS", None)
    if tabs:
        return list(tabs)

    # 2) prefix auto-discovery (spacing-insensitive: "DAVAO DAC" matches
    #    both "DAVAO DAC 4" and "DAVAO DAC4")
    prefix = getattr(config, "SOURCE_TAB_PREFIX", None)
    if prefix:
        nex = {_norm(x) for x in getattr(config, "SOURCE_TAB_EXCLUDE", [])}
        np = _norm(prefix)
        all_titles = titles if titles is not None else list_tab_titles(sh)
        matched = [
            t for t in all_titles
            if _norm(t) == np or _norm(t).startswith(np)
        ]
        if nex:
            matched = [t for t in matched if _norm(t) not in nex]
        if not matched:
            info(f'No tabs found starting with prefix "{prefix}".')
            info("Tabs in this file:")
            for n in all_titles:
                info(f"  - {n}")
            raise SystemExit(
                'Edit config.py  SOURCE_TAB_PREFIX  to the right prefix, '
                'or set SOURCE_TAB to one tab.'
            )
        return matched

    # 3) single tab
    return [config.SOURCE_TAB]


def pending_tos_from_config_ws(ws, config) -> list[dict]:
    return pending_tos_from_tab(
        ws,
        first_to_col=getattr(config, "FIRST_TO_COL", 2),
        block_width=getattr(config, "BLOCK_WIDTH", 5),
        data_start_row=getattr(config, "DATA_START_ROW", 6),
    )


def match_real_title(wanted: str, titles: list[str]) -> str | None:
    """Match a configured tab name to a real tab title with ZERO API calls.

    Same ladder as open_named_tab (exact -> case-insensitive ->
    spacing-insensitive), which re-lists worksheets() on every mismatch.
    """
    if not wanted:
        return None
    for t in titles:
        if t == wanted:
            return t
    wl = wanted.strip().lower()
    for t in titles:
        if t.strip().lower() == wl:
            info(f'Tab name differs only by case/spaces. Using "{t}"')
            return t
    wn = _norm(wanted)
    for t in titles:
        if _norm(t) == wn:
            info(f'Tab name differs by spacing/case. Using "{t}"')
            return t
    return None


def _q(title: str) -> str:
    return "'" + (title or "").replace("'", "''") + "'"


def fetch_scan_grids(sh, want_titles, config, dump_tab=None, metas=None):
    """Read every wanted tab (+ DUMP ALL) in TWO Google API calls.

    The old path did sh.worksheet(title) + ws.get_values() PER TAB (~1 s per
    open, measured — bug 41) plus a worksheets() re-list on every name
    mismatch. This lists worksheets once (titles + grid sizes) and pulls all
    value grids in one values_batch_get. Ranges start at data_start_row, so
    row math matches pending_tos_from_tab exactly.

    Returns (real_map, grids, dump_grid): real_map {wanted: real title}
    (unmatched wanteds warn + skip, as before), grids {real title: values},
    dump_grid the DUMP ALL!A:C values (None when dump_tab is falsy).
    """
    first_to_col = getattr(config, "FIRST_TO_COL", 2)
    data_start_row = getattr(config, "DATA_START_ROW", 6)
    if metas is None:
        metas = sh.worksheets()
    titles = [ws.title for ws in metas]
    by_title = {ws.title: ws for ws in metas}
    real_map: dict[str, str] = {}
    for want in want_titles:
        real = match_real_title(want, titles)
        if real is None:
            warn(f'Tab not found: "{want}" — skipped')
            continue
        real_map[want] = real
    ranges: list[str] = []
    keys: list[tuple] = []
    for _want, real in real_map.items():
        ws = by_title[real]
        last_row = max(ws.row_count, data_start_row)
        last_col = max(ws.col_count, first_to_col)
        ranges.append(
            f"{_q(real)}!{rowcol_to_a1(data_start_row, 1)}:"
            f"{rowcol_to_a1(last_row, last_col)}"
        )
        keys.append(("tab", real))
    if dump_tab:
        ranges.append(f"{_q(dump_tab)}!A:C")
        keys.append(("dump", dump_tab))
    grids: dict[str, list[list[str]]] = {}
    dump_grid: list[list[str]] | None = None
    if ranges:
        resp = sh.values_batch_get(ranges) or {}
        vrs = resp.get("valueRanges", []) if isinstance(resp, dict) else []
        for key, vr in zip(keys, vrs):
            vals = ((vr or {}).get("values", []) if isinstance(vr, dict)
                    else [])
            if key[0] == "dump":
                dump_grid = vals
            else:
                grids[key[1]] = vals
    return real_map, grids, dump_grid


def merge_pending(real_map, grids, config) -> list[dict]:
    """Parse + dedup pending T.O.s from batched grids (no API calls)."""
    first_to_col = getattr(config, "FIRST_TO_COL", 2)
    block_width = getattr(config, "BLOCK_WIDTH", 5)
    data_start_row = getattr(config, "DATA_START_ROW", 6)
    out: list[dict] = []
    seen: set[str] = set()
    for _want, real in real_map.items():
        items = _pending_from_grid(
            grids.get(real, []), real,
            first_to_col=first_to_col,
            block_width=block_width,
            data_start_row=data_start_row,
        )
        info(f"  tab {real}: {len(items)} pending")
        for it in items:
            if it["to"] in seen:
                continue
            seen.add(it["to"])
            out.append(it)
    return out


def pending_tos_from_sheet(sh, config) -> list[dict]:
    """Read + merge pending T.O.s from every resolved source tab (dedup).

    Batched (bug 41): one worksheets() + one values_batch_get for all tabs,
    instead of sh.worksheet() + get_values() per tab.
    """
    metas = sh.worksheets()
    titles = [ws.title for ws in metas]
    want = resolve_source_tabs(sh, config, titles=titles)
    _real, grids, _dump = fetch_scan_grids(
        sh, want, config, dump_tab=None, metas=metas)
    return merge_pending(_real, grids, config)


def open_named_tab(sh, title: str):
    """Open a tab; if missing, try case-insensitive match, else list all tabs."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        pass
    want = (title or "").strip().lower()
    for ws in sh.worksheets():
        if ws.title.strip().lower() == want:
            info(f'Tab name differs only by case/spaces. Using "{ws.title}"')
            return ws
    nwant = _norm(title)
    for ws in sh.worksheets():
        if _norm(ws.title) == nwant:
            info(f'Tab name differs by spacing/case. Using "{ws.title}"')
            return ws
    warn(f'Tab not found: "{title}"')
    info("Tabs in this file:")
    for n in list_tab_titles(sh):
        info(f"  - {n}")
    info('Edit config.py  SOURCE_TAB  to one of those names (exact).')
    raise SystemExit(1)


def validated_tos_in_dump(sh, dump_tab: str, grid=None) -> set[str]:
    """TOs already validated = present in DUMP ALL with a Status (col B).

    A TO is considered done/validated when it has a Status (and normally a
    remark) in the DUMP ALL tab (that is the master the XLOOKUPs read). Any
    such TO must NOT be re-searched or re-dumped.
    """
    if grid is None:
        try:
            ws = sh.worksheet(dump_tab)
        except Exception:
            return set()
        try:
            grid = ws.get_values("A:C")
        except Exception:
            return set()
    validated: set[str] = set()
    for row in grid:
        if not row:
            continue
        to = (row[0] or "").strip() if len(row) > 0 else ""
        status = (row[1] or "").strip() if len(row) > 1 else ""
        remarks = (row[2] or "").strip() if len(row) > 2 else ""
        # Fully validated = real TO with BOTH a Status and a Remarks value.
        if to and status and remarks and is_real_to(to):
            validated.add(to)
    return validated


def incomplete_tos_in_dump(sh, dump_tab: str) -> list[dict]:
    """Rows in DUMP ALL that have a TO (col A) but are missing Status or Remarks.

    A blank B/C on a row that already has a TO = not fully validated yet.
    Returns [{row, to, status, remarks}, ...] in worksheet row order.
    """
    try:
        ws = sh.worksheet(dump_tab)
    except Exception:
        return []
    try:
        grid = ws.get_values("A:C")
    except Exception:
        return []
    out: list[dict] = []
    for i, row in enumerate(grid):
        if not row:
            continue
        to = (row[0] or "").strip() if len(row) > 0 else ""
        status = (row[1] or "").strip() if len(row) > 1 else ""
        remarks = (row[2] or "").strip() if len(row) > 2 else ""
        if to and is_real_to(to) and (not status or not remarks):
            out.append(
                {
                    "row": i + 1,
                    "to": to,
                    "status": status,
                    "remarks": remarks,
                    "missing": "status" if not status else ("remarks" if not remarks else ""),
                }
            )
    return out


def _should_skip_dumped(config) -> bool:
    return bool(getattr(config, "SKIP_ALREADY_DUMPED", True))


def exclude_validated(pending: list[dict], sh, config, dump_grid=None) -> tuple[list[dict], list[str]]:
    """Filter a merged pending list down to TOs not yet validated in DUMP ALL.

    Returns (kept_pending, excluded_tos).
    """
    if not _should_skip_dumped(config):
        return pending, []
    dump_tab = getattr(config, "DUMP_ALL_TAB", "DUMP ALL")
    validated = validated_tos_in_dump(sh, dump_tab, grid=dump_grid)
    if not validated:
        return pending, []
    kept = [p for p in pending if p["to"] not in validated]
    excluded = [p["to"] for p in pending if p["to"] in validated]
    removed = len(excluded)
    ok(
        f"Excluded {removed} T.O.(s) already validated in DUMP ALL "
        f"({dump_tab}) to avoid duplicates."
    )
    return kept, excluded


def refresh_pending_file(config, dest: Path) -> list[str]:
    """Always re-read today's sheet. Do not reuse yesterday's pending_tos.txt.

    Pending = TOs with blank status on the source tabs, MINUS any already
    validated in DUMP ALL (so we never re-search / re-dump them).
    """
    gc = connect(config.CREDENTIALS_FILE)
    sh = open_today_sheet(gc, getattr(config, "SHEET_ID", ""), config.SHEET_TITLE_PREFIX)
    info("Spreadsheet:", sh.title)

    # Batched (bug 41): resolve once, then ONE worksheets() + ONE
    # values_batch_get for all source tabs + DUMP ALL — instead of a
    # worksheet open + full-grid read PER TAB plus a full DUMP ALL re-read.
    metas = sh.worksheets()
    want = resolve_source_tabs(
        sh, config, titles=[ws.title for ws in metas])
    info("Reading source tab(s):", ", ".join(want))
    dump_tab = getattr(config, "DUMP_ALL_TAB", "DUMP ALL")
    real_map, grids, dump_grid = fetch_scan_grids(
        sh, want, config, dump_tab=dump_tab, metas=metas)

    merged = merge_pending(real_map, grids, config)
    pending, _excluded = exclude_validated(
        merged, sh, config, dump_grid=dump_grid)

    dest.write_text("\n".join(p["to"] for p in pending), encoding="utf-8")
    # Dock map: which source tab (DAC) each pending OID came from — used later
    # by the packed step to know which dock each OID belongs to.
    map_path = dest.with_name("pending_tab_map.txt")
    map_path.write_text(
        "".join(f"{p['to']}\t{p['tab']}\n" for p in pending), encoding="utf-8"
    )
    ok(f"Pending T.O.s to batch-search: {len(pending)}  → {dest}")
    info("Dock map (OID → source tab):", map_path.name)
    for item in pending[:8]:
        info(f"  {item['to']}   row {item['row']} col {item['to_col']} tab {item['tab']}")
    if len(pending) > 8:
        info(f"  ... {len(pending) - 8} more")
    return [p["to"] for p in pending]


