"""Live check of the date picker against real Google Drive.

The offline guards prove the date is threaded through the code. This proves the
part that can only be true or false against the real thing: that Drive really
holds dated 'DAC OFFLINE …' files, that they are discovered, that the chosen one
OPENS, and that a date with no file fails loudly instead of quietly falling back
to another day (which would read one day's T.O.s and stamp them into another).

Read-only. Nothing is written, and no dock tab is read.

Run with:  py tools\\verify_live_dates.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from lib import cli
from lib.dc_run import dated_sheets, open_sheet
from lib.pending_tos import (connect, open_dated_sheet, parse_sheet_date,
                             sheet_title_for)

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    prefix = config.SHEET_TITLE_PREFIX
    print(f"\n-- listing dated '{prefix} ...' files on Drive --")
    rows = dated_sheets(config)
    print(f"   found {len(rows)}")
    for r in rows[:10]:
        print(f"     {r['date']}  {r['title']}  (edited {r['modified']})")
    check("Drive holds at least one dated sheet", len(rows) > 0)

    if not rows:
        print("   nothing to verify further -- is the bot email still shared "
              "with the DAC OFFLINE folder?")
        return 1

    check("they come back newest first",
          [r["date"] for r in rows] == sorted((r["date"] for r in rows),
                                               reverse=True))
    check("every row has a date and a title",
          all(r["date"] and r["title"].startswith(prefix) for r in rows))
    check("no duplicate dates",
          len({r["date"] for r in rows}) == len(rows))

    print("\n-- today's picker still works (AUTO must not regress) --")
    try:
        sh = open_sheet(config)
        check("AUTO opens a spreadsheet", sh is not None, sh.title)
        check("  and it is a DAC OFFLINE file", sh.title.startswith(prefix),
              sh.title)
    except Exception as e:
        check("AUTO opens a spreadsheet", False, str(e).splitlines()[0][:70])

    print("\n-- a real discovered date must open THAT file --")
    target = rows[min(1, len(rows) - 1)]          # not today, so it is a real test
    want = sheet_title_for(parse_sheet_date(target["date"]), prefix)
    print(f"   asking for {target['date']} -> {want!r}")
    try:
        sh2 = open_sheet(config, target["date"])
        check("it opened", sh2 is not None, sh2.title)
        check("  and it is exactly the file for that date", sh2.title == want,
              f"opened {sh2.title!r}, wanted {want!r}")
    except Exception as e:
        check("it opened", False, str(e).splitlines()[0][:70])

    print("\n-- two different dates must be two different files --")
    if len(rows) >= 2:
        a, b = rows[0], rows[-1]
        try:
            ta = open_sheet(config, a["date"]).title
            tb = open_sheet(config, b["date"]).title
            check("picking different dates gives different sheets",
                  ta != tb, f"{ta!r} vs {tb!r}")
            check("  and each matches its own date",
                  ta == sheet_title_for(parse_sheet_date(a["date"]), prefix)
                  and tb == sheet_title_for(parse_sheet_date(b["date"]), prefix),
                  f"{ta!r} / {tb!r}")
        except Exception as e:
            check("picking different dates gives different sheets", False,
                  str(e).splitlines()[0][:70])

    print("\n-- a date with no file must FAIL, never fall back --")
    # 1999 is certainly not on Drive. This is the safety-critical case: if it
    # silently returned today's sheet, the run would read one day's T.O.s and
    # write them into another.
    gc = connect(config.CREDENTIALS_FILE)
    try:
        sh3 = open_dated_sheet(gc, getattr(config, "SHEET_ID", ""), prefix,
                               parse_sheet_date("1999-01-01"))
        check("a missing date raises instead of opening something", False,
              f"it opened {getattr(sh3, 'title', sh3)!r}")
    except FileNotFoundError as e:
        msg = str(e)
        check("a missing date raises instead of opening something", True)
        check("  and the message names what IS available",
              "Dates on Drive:" in msg, msg.splitlines()[-1][:76])
    except Exception as e:
        check("a missing date raises instead of opening something", False,
              f"{type(e).__name__}: {str(e)[:60]}")

    print("\n-- the dock tabs actually exist on the chosen day's file --")
    # Only read tab TITLES -- no pending data, nothing written.
    try:
        from lib.pending_tos import list_tab_titles
        for r in (rows[0], rows[-1]):
            s = open_sheet(config, r["date"])
            tabs = list_tab_titles(s)
            dacs = [t for t in tabs if t.strip().upper().startswith(("DAVAO", "GENSAN"))]
            check(f"{r['date']} has the dock tabs", len(dacs) > 0,
                  f"{len(dacs)} DAC tab(s) of {len(tabs)}")
    except Exception as e:
        check("dock tabs readable on the chosen days", False,
              str(e).splitlines()[0][:70])

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
