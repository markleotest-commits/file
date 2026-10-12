"""Offline check of lib/trace_oid.py — the pure helpers, no Google calls.

Feeds synthetic value grids (same shape as fetch_scan_grids: rows start at
DATA_START_ROW) through normalize / match_cell / window_dates / scan_grid /
scan_dump. Run:  py tools\\check_trace_oid.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.trace_oid import (  # noqa: E402
    match_cell,
    normalize,
    scan_dump,
    scan_grid,
    window_dates,
)

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


print("-- normalize / match_cell --")
check("normalize strips space and case", normalize("  ph26 1001\t") == "PH261001")
check("exact match", match_cell("PH26100123", "PH26100123") == "exact")
check("exact match is case-insensitive", match_cell("PH26100123", "ph26100123") == "exact")
check("sheet value containing the query -> partial",
      match_cell("PH26100123", "SPEPH26100123") == "partial")
check("query containing the sheet value -> partial",
      match_cell("SPEPH26100123", "PH26100123") == "partial")
check("a header never matches", match_cell("PH26100123", "T.O. NUMBER") is None)
check("a different T.O. never matches", match_cell("PH26100123", "PH99999999") is None)
check("empty cell never matches", match_cell("PH26100123", "   ") is None)
check("empty query never matches", match_cell("", "PH26100123") is None)
check("a short cell cannot be a partial hit",
      match_cell("SPEPH26100123", "PH1") is None)

print("\n-- window_dates --")
rows = [
    {"date": "2026-10-31", "title": "DAC OFFLINE OCTOBER 31 2026"},  # pre-created future
    {"date": "2026-10-08", "title": "DAC OFFLINE OCTOBER 8 2026"},   # today
    {"date": "2026-10-01", "title": "DAC OFFLINE OCTOBER 1 2026"},
    {"date": "2026-09-30", "title": "DAC OFFLINE SEPTEMBER 30 2026"},
    {"date": "2026-09-10", "title": "DAC OFFLINE SEPTEMBER 10 2026"},
    {"date": "2026-09-09", "title": "DAC OFFLINE SEPTEMBER 9 2026"},   # day 30
    {"date": "2026-09-08", "title": "DAC OFFLINE SEPTEMBER 8 2026"},   # day 31 - outside
]
w = window_dates(rows, 30, today=date(2026, 10, 8))
check("the pre-created FUTURE sheet is dropped",
      all(r["date"] <= "2026-10-08" for r in w))
check("today is included", any(r["date"] == "2026-10-08" for r in w))
check("the 30th day back is inside, the 31st is not",
      any(r["date"] == "2026-09-09" for r in w) and not any(r["date"] == "2026-09-08" for r in w),
      str([r["date"] for r in w]))
check("newest first, order kept",
      [r["date"] for r in w] == ["2026-10-08", "2026-10-01", "2026-09-30",
                                 "2026-09-10", "2026-09-09"])
check("a 10-day window runs 2026-09-29 .. 2026-10-08",
      len(window_dates(rows, 10, today=date(2026, 10, 8))) == 3)
check("sheets outside any window vanish", window_dates(rows[:1] , 30, date(2026, 10, 8)) == [])

print("\n-- scan_grid --")
# Same shape fetch_scan_grids hands to the pending parser: row 1 of this grid
# is sheet row 6, and the grid spans every column of the tab (A..H here), so
# both TO blocks B and G are in range. Blocks: B=TO C=status ... G=TO H=status.
grid = [
    ["", "PH26100123", "MISROUTE", "", "", "", "", ""],   # sheet row 6, col B
    ["", "", "", "", "", "", "", ""],                     # row 7 blank
    ["", "TO20269999", "", "", "", "", "", ""],           # row 8, pending
    ["", "not a to", "", "", "", "", "", ""],             # row 9, junk
    ["", "", "", "", "", "", "SPEPH26100123", ""],        # row 10, col G
]
hits = scan_grid(grid, "PH26100123")
check("both blocks are walked", len(hits) == 2, str(hits))
check("a FILLED status still counts (it was scanned)",
      any(h["to"] == "PH26100123" and h["status"] == "MISROUTE" for h in hits))
check("rows are sheet rows, not grid offsets",
      {h["row"] for h in hits} == {6, 10}, str([h["row"] for h in hits]))
check("column letters follow the block layout",
      {h["to_col"] for h in hits} == {"B", "G"}, str([h["to_col"] for h in hits]))
check("the value that only CONTAINS the query is a partial match",
      any(h["to"] == "SPEPH26100123" and h["match"] == "partial" for h in hits))
pend = scan_grid(grid, "TO20269999")
check("a blank status is reported as pending",
      len(pend) == 1 and pend[0]["status"] == "" and pend[0]["row"] == 8,
      str(pend))
check("an empty grid is not an error", scan_grid([], "PH26100123") == [])
check("a short grid (ragged rows) does not crash",
      scan_grid([["", "PH26100123"]], "PH26100123")[0]["row"] == 6)

print("\n-- scan_dump --")
dump = [
    ["PH26100123", "MISROUTE", "wrong dock"],
    ["PH99999999", "OK", "clean"],
    ["TO20269999", ""],            # status present, remarks missing -> incomplete
]
d = scan_dump(dump, "PH26100123")
check("found by col A", len(d) == 1 and d[0]["row"] == 1, str(d))
check("status and remarks come along",
      d[0]["status"] == "MISROUTE" and d[0]["remarks"] == "wrong dock")
check("a miss is empty", scan_dump(dump, "PH00000000") == [])
check("no DUMP ALL tab is not an error", scan_dump(None, "PH26100123") == [])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
