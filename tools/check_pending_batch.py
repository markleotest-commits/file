"""Batched pending scan must parse IDENTICALLY to the old per-tab loop.

The sheet is edited live by the team (measured: +12 TOs in 3 s), so comparing
two full scans minutes apart can never be exact. This guard instead:
  1. fetches every grid ONCE via the batch path,
  2. parses each grid with the NEW parser (_pending_from_grid) and with a
     copy of the OLD inline loop over the SAME grid object — asserts identical,
  3. times OLD (per-tab open+read) vs NEW (one batch) live for the speedup
     report only (counts may differ: live sheet, see above).

    py tools\\check_pending_batch.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def old_parse(grid, tab_title, first_to_col, block_width, data_start_row):
    """The pre-batch inline loop (pending_tos_from_tab body), over one grid."""
    from lib.pending_tos import is_real_to
    from gspread.utils import rowcol_to_a1

    def _col(col_1based):
        idx = col_1based - 1
        return [(row[idx].strip() if idx < len(row) else "") for row in grid]

    pending, seen = [], set()
    to_col = first_to_col
    last_col = 50  # old bound: ws.col_count (AX) on these tabs
    while to_col <= last_col:
        tos = _col(to_col)
        statuses = _col(to_col + 1)
        col_letter = rowcol_to_a1(1, to_col).rstrip("1")
        for offset, to in enumerate(tos):
            if not to or not is_real_to(to):
                continue
            status = statuses[offset] if offset < len(statuses) else ""
            if status or to in seen:
                continue
            seen.add(to)
            pending.append({"to": to, "row": data_start_row + offset,
                            "to_col": col_letter, "tab": tab_title})
        to_col += block_width
    return pending


def main() -> int:
    import config
    from lib.dc_run import open_sheet
    from lib.pending_tos import (
        _pending_from_grid,
        exclude_validated,
        fetch_scan_grids,
        merge_pending,
        open_named_tab,
        pending_tos_from_config_ws,
        resolve_source_tabs,
        validated_tos_in_dump,
    )

    sh = open_sheet(config, None)
    print(f"Sheet: {sh.title}")
    dump_tab = getattr(config, "DUMP_ALL_TAB", "DUMP ALL")
    first_to_col = getattr(config, "FIRST_TO_COL", 2)
    block_width = getattr(config, "BLOCK_WIDTH", 5)
    data_start_row = getattr(config, "DATA_START_ROW", 6)

    print("\n-- fetch once via the batch path --")
    t0 = time.time()
    metas = sh.worksheets()
    want = resolve_source_tabs(
        sh, config, titles=[ws.title for ws in metas])
    real_map, grids, dump_grid = fetch_scan_grids(
        sh, want, config, dump_tab=dump_tab, metas=metas)
    t_fetch = time.time() - t0
    print(f"  {len(grids)} tab grids + dump in {t_fetch:.1f}s")

    print("\n-- parser parity on IDENTICAL grids --")
    key = lambda d: (d["to"], d["row"], d["to_col"], d["tab"])
    all_same = True
    for _w, real in real_map.items():
        grid = grids.get(real, [])
        new_items = _pending_from_grid(
            grid, real, first_to_col=first_to_col,
            block_width=block_width, data_start_row=data_start_row)
        old_items = old_parse(
            grid, real, first_to_col, block_width, data_start_row)
        same = sorted(map(key, old_items)) == sorted(map(key, new_items))
        all_same = all_same and same
        if not same:
            print(f"  DIFF in {real}: old={len(old_items)} new={len(new_items)}")
    check("new parser == old loop on every grid", all_same)

    print("\n-- live speedup (counts may differ: the sheet is being edited) --")
    t0 = time.time()
    for title in want:
        ws = open_named_tab(sh, title)
        pending_tos_from_config_ws(ws, config)
    validated_tos_in_dump(sh, dump_tab)
    t_old = time.time() - t0
    t0 = time.time()
    metas = sh.worksheets()
    want2 = resolve_source_tabs(
        sh, config, titles=[ws.title for ws in metas])
    r2, g2, d2 = fetch_scan_grids(
        sh, want2, config, dump_tab=dump_tab, metas=metas)
    merge_pending(r2, g2, config)
    validated_tos_in_dump(sh, dump_tab, grid=d2)
    t_new = time.time() - t0
    print(f"  OLD {t_old:.1f}s  vs  NEW {t_new:.1f}s "
          f"({t_old / max(t_new, 0.01):.1f}x faster)")

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())