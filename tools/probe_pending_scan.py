"""Time the pending-T.O. scan stage by stage (READ-ONLY, writes nothing).

Walks the exact path refresh_pending_file() takes and reports where the
seconds go: Drive lookup, sheet open, worksheet listing, per-tab open+read,
DUMP ALL exclusion read.

    py tools\\probe_pending_scan.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gspread.utils import rowcol_to_a1


def main() -> int:
    import config
    from lib.pending_tos import (
        connect,
        list_tab_titles,
        open_named_tab,
        open_today_sheet,
        resolve_source_tabs,
    )

    t_all = time.time()

    t0 = time.time()
    gc = connect(config.CREDENTIALS_FILE)
    print(f"connect (auth):                          {time.time() - t0:6.2f}s")

    t0 = time.time()
    sh = open_today_sheet(gc, getattr(config, "SHEET_ID", ""),
                          config.SHEET_TITLE_PREFIX)
    print(f"open_today_sheet (Drive find + open):    {time.time() - t0:6.2f}s")
    print(f"  spreadsheet: {sh.title}")

    t0 = time.time()
    titles = list_tab_titles(sh)
    one_ws_list = time.time() - t0
    print(f"sh.worksheets() [{len(titles)} tabs]:               {one_ws_list:6.2f}s")

    t0 = time.time()
    picked = resolve_source_tabs(sh, config)
    print(f"resolve_source_tabs [{len(picked)} tabs]:         {time.time() - t0:6.2f}s")
    for t in picked:
        print(f"    - {t}")

    # NOTE (bug 41, fixed): pending_for_dc() used to call list_tab_titles()
    # once per wanted name (~14 worksheets() round trips). It now fetches
    # once + batch-reads. The line below shows what one call costs, for
    # reference against the old multiplication.
    n_want = len(getattr(config, "SOURCE_TABS", []) or [1])
    print(f"one worksheets() = {one_ws_list:.2f}s; the old code paid this "
          f"~{2 * n_want}x per scan")

    first_to_col = getattr(config, "FIRST_TO_COL", 2)
    data_start_row = getattr(config, "DATA_START_ROW", 6)
    total_cells = 0
    for title in picked:
        t0 = time.time()
        ws = open_named_tab(sh, title)
        t_open = time.time() - t0
        last_row = max(ws.row_count, data_start_row)
        last_col = max(ws.col_count, first_to_col)
        a1 = (f"{rowcol_to_a1(data_start_row, 1)}:"
              f"{rowcol_to_a1(last_row, last_col)}")
        t0 = time.time()
        grid = ws.get_values(a1)
        t_read = time.time() - t0
        cells = sum(len(r) for r in grid)
        total_cells += cells
        print(f"  tab {title}: open {t_open:5.2f}s + read {a1:>14s} "
              f"{len(grid):5d} rows / {cells:6d} cells in {t_read:5.2f}s")

    dump_tab = getattr(config, "DUMP_ALL_TAB", "DUMP ALL")
    t0 = time.time()
    try:
        ws = sh.worksheet(dump_tab)
        grid = ws.get_values("A:C")
        t_dump = time.time() - t0
        cells = sum(len(r) for r in grid)
        print(f"DUMP ALL read A:C: {len(grid)} rows / {cells} cells "
              f"in {t_dump:.2f}s")
    except Exception as e:
        print(f"DUMP ALL read failed: {str(e).splitlines()[0][:80]}")

    print(f"\nTOTAL scan time: {time.time() - t_all:.1f}s "
          f"({total_cells} tab cells + dump)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
