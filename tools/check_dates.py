"""The date the page picks must reach EVERY sheet access, especially DUMP ALL.

Reported ask: "make it so the ui has a selector of which date the bot should
validate". A date selector is only safe if the chosen day governs the whole run.

The dangerous one is phase 8. run_dc.py used to re-derive the sheet there:

    sh = open_today_sheet(gc, config.SHEET_ID, config.SHEET_TITLE_PREFIX)

so a --date run would read DAVAO's pending T.O.s from one day's sheet and then
stamp the results into TODAY's DUMP ALL. Silently wrong, and it corrupts a real
sheet. So two things are asserted below: the date is threaded into phase 8, and
the run REFUSES to write when phase 1 and phase 8 disagree.

Also checked: title building round-trips through the parser (a non-padded day
like "OCTOBER 1 2026"), the picker cannot select a date that is not on Drive,
and a selection that disappears on refresh falls back to AUTO instead of sticking.

Run with:  py tools\\check_dates.py
"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import panel_server, pending_tos

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    PREFIX = "DAC OFFLINE"

    print("-- the title the team actually uses must round-trip --")
    for d, want in (
        (datetime(2026, 10, 1), "DAC OFFLINE OCTOBER 1 2026"),
        (datetime(2026, 12, 31), "DAC OFFLINE DECEMBER 31 2026"),
        (datetime(2026, 1, 5), "DAC OFFLINE JANUARY 5 2026"),
    ):
        got = pending_tos.sheet_title_for(d, PREFIX)
        check(f"{d:%Y-%m-%d} -> {want!r}", got == want, f"got {got!r}")
        back = pending_tos._title_date(got)
        check(f"  and it parses back to {d:%Y-%m-%d}",
              back == (d.year, d.month, d.day), f"got {back}")

    check("today_sheet_title() is just sheet_title_for(today)",
          pending_tos.today_sheet_title(PREFIX)
          == pending_tos.sheet_title_for(datetime.now(), PREFIX))

    print("\n-- dates from the page must parse, junk must not --")
    for good in ("2026-10-01", "2026/10/01", "20261001", datetime(2026, 10, 1)):
        check(f"{good!r} parses", pending_tos.parse_sheet_date(good) is not None)
    check("None means 'today', not an error",
          pending_tos.parse_sheet_date(None) is None)
    check("'' means 'today', not an error",
          pending_tos.parse_sheet_date("") is None)
    for bad in ("01-10-2026", "october", "2026-13-45", "next tuesday"):
        try:
            pending_tos.parse_sheet_date(bad)
            check(f"{bad!r} is rejected", False, "it was accepted")
        except ValueError:
            check(f"{bad!r} is rejected", True)

    print("\n-- the date must be threaded from the page down to run_one_dc --")
    psrc = src("lib/panel_server.py")
    check("/api/run reads a date from the body", '"date": sheet_date or "auto"' in psrc)
    check("  and puts it on the job", '"date": sheet_date}' in psrc)
    check("'auto'/'today' normalise to AUTO, not a literal date",
          'sheet_date.lower() in ("auto", "today")' in psrc)
    check("/api/date applies the pick in the RESPONSE, not the queue",
          "try_set_date(" in psrc)
    check("  answering 200 when accepted and 400 when refused",
          'self._json(200, {"ok": True' in psrc
          and 'self._json(400, {"ok": False' in psrc)
    check("  and it does NOT queue the pick as a job",
          '{"kind": "date"' not in psrc,
          "queueing it is what let the page show one date and send another")
    check("  only the Google title lookup is queued",
          '{"kind": "sheet"' in psrc)
    check("'auto'/'today' are normalised inside try_set_date, so no caller "
          "can skip it",
          'want.lower() in ("auto", "today")' in psrc)

    vsrc = src("validate_ui.py")
    check("the launcher refreshes the title for the job's date",
          'job.get("kind") == "sheet"' in vsrc)
    check("  and hands the run's date to run_one_dc", "sheet_date=sheet_date" in vsrc)

    rdsrc = src("run_dc.py")
    check("run_one_dc accepts sheet_date",
          "sheet_date=None) -> RunState" in rdsrc)
    check("  and forwards it to build_state", "sheet_date=sheet_date)" in rdsrc)

    print("\n-- the one that matters: phase 8 must use the same date --")
    check("phase 8 no longer calls open_today_sheet",
          "open_today_sheet(gc," not in rdsrc,
          "it re-derived today and would write the wrong day")
    check("phase 8 opens the chosen date", "open_dated_sheet(gc," in rdsrc)
    check("phase 8 refuses when the two sheets disagree",
          "Refusing to write" in rdsrc and "state.sheet_title" in rdsrc)
    check("  and it compares the opened title, not just the request",
          "sh.title != state.sheet_title" in rdsrc)

    print("\n-- build_state must read and filter on the SAME sheet --")
    import ast

    dcs = src("lib/dc_run.py")
    check("build_state takes sheet_date", "sheet_date=None) -> RunState" in dcs)
    check("  it opens the sheet ONCE", "sh = open_sheet(config, sheet_date)" in dcs)
    check("  and passes that handle down", "pending_for_dc(config, dc, sh=sh)" in dcs)
    check("  the DUMP ALL filter uses that same handle",
          "exclude_validated(pending, sh, config)" in dcs)

    # Count opens INSIDE build_state only. pending_for_dc keeps its own
    # `if sh is None: open_sheet(...)` so direct callers still work, so
    # counting the whole file would be wrong.
    def body_of(path: str, fname: str) -> str:
        tree = ast.parse(src(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fname:
                return ast.get_source_segment(src(path), node) or ""
        return ""

    bs = body_of("lib/dc_run.py", "build_state")
    check("build_state opens the sheet exactly once",
          bs.count("open_sheet(") == 1, f"{bs.count('open_sheet(')} opens in build_state")
    check("  and it happens before the dock tabs are read",
          bs.index("open_sheet(") < bs.index("pending_for_dc("))

    print("\n-- --date on the command line --")
    check("run_dc.py has --date", '"--date"' in rdsrc)
    check("  and validates it before doing anything", "parse_sheet_date(args.date)" in rdsrc)

    print("\n-- an explicit date must never silently fall back to another day --")
    ps = src("lib/pending_tos.py")
    check("open_dated_sheet with no date defers to today's picker",
          "if sheet_date is None:\n        return open_today_sheet(" in ps)
    check("  with a date it opens that exact title", "open_dated_sheet" in ps
          and "sheet_title_for(when, prefix)" in ps)
    check("  and raises rather than picking a different day",
          "is available to the bot" in ps and "Dates on Drive:" in ps)

    print("\n-- the page cannot request a date that is not there --")
    st = panel_server.PanelState()
    st.set_dates([{"date": "2026-10-01", "title": "DAC OFFLINE OCTOBER 1 2026",
                   "modified": "2026-10-01 09:00"}])
    st.sheet_date = "2026-10-01"
    d = st.to_dict()
    check("the page is told the date list", len(d["dates"]) == 1, str(d["dates"]))
    check("  and the current selection", d["sheet_date"] == "2026-10-01")
    check("AUTO is the default", panel_server.PanelState().to_dict()["sheet_date"] == "")
    check("AUTO reads as 'AUTO (today)'",
          panel_server.PanelState().date_label() == "AUTO (today)")

    ok_, msg = st.try_set_date("2026-10-01")
    check("a date that IS on Drive is accepted", ok_ and st.sheet_date == "2026-10-01", msg)
    ok_, msg = st.try_set_date("1999-01-01")
    check("a date that is NOT on Drive is refused", not ok_, "")
    check("  and it says which dates are available", "Available:" in msg, msg[:70])
    check("  and it leaves the previous selection untouched",
          st.sheet_date == "2026-10-01", st.sheet_date)
    ok_, msg = st.try_set_date("auto")
    check("'auto' is accepted and clears the date", ok_ and st.sheet_date == "", msg)

    # A Drive outage must not make the picker useless: with no list we cannot
    # say a date is bogus, so accept it and let the main thread re-check.
    blind = panel_server.PanelState()
    ok_, _ = blind.try_set_date("2026-10-01")
    check("with Drive unlistable the pick is accepted, not refused everything",
          ok_ and blind.sheet_date == "2026-10-01")

    print("\n-- a date that vanishes must fall back to AUTO, not stick --")
    st.set_dates([{"date": "2026-10-02", "title": "DAC OFFLINE OCTOBER 2 2026",
                   "modified": "2026-10-02 09:00"}])
    check("the stale selection was dropped", st.to_dict()["sheet_date"] == "",
          f"still {st.to_dict()['sheet_date']!r}")
    check("and it now reads as AUTO", st.date_label() == "AUTO (today)")

    print("\n-- the page's HTML must actually send the date --")
    html = src("mc/validate_ui.html")
    check("there is a date <select>", 'id="dates"' in html)
    check("changing it posts to /api/date", "/api/date" in html)
    check("there is a refresh control", "refreshDates" in html)
    check("START sends the chosen date",
          "revalidate: false, date: chosenDate()" in html)
    check("RE-VALIDATE sends it too",
          "revalidate: true, date: chosenDate()" in html)
    check("the render signature includes the date, so the page updates",
          "s.sheet_date, s.dates" in html)
    check("the dropdown is not rebuilt while it has focus",
          "document.activeElement !== sel" in html)
    check("the result line shows the sheet it wrote to",
          "d.result.sheet" in html)
    # Found by driving the page: a refused pick left PICKED holding the
    # rejected date while the dropdown showed AUTO, so START sent a date the
    # page no longer displayed.
    check("a refused pick snaps the page back",
          "if (r && r.ok === false)" in html and "sel.value = r.date || 'auto'" in html)
    check("  and it says why", "r.error || 'That date was not accepted.'" in html)
    check("the server's selection wins over the page's own",
          "if (STATE.sheet_date) PICKED = STATE.sheet_date;" in html
          and "else PICKED = '';" in html)

    print("\n-- a stray-spaced filename must OPEN, not say 'share it again' --")
    # Live: 'DAC OFFLINE OCTOBER 12  2026' (TWO spaces before the year). The
    # picker listed 2026-10-12 (the date regex is \s+-tolerant) but
    # open_dated_sheet() compared titles with == and told the operator to share
    # a file that WAS already shared.
    import gspread

    class _Resp:
        def __init__(self, files):
            self._files = files

        def json(self):
            return {"files": self._files}

    class _FakeSheet:
        def __init__(self, title, sid=None):
            self.title = title
            self.id = sid

    class _FakeGC:
        def __init__(self, files):
            self._files = files
            self.opened = []

        def request(self, method, url, params=None):
            return _Resp(self._files)

        def open_by_key(self, key):
            for f in self._files:
                if f.get("id") == key:
                    self.opened.append(key)
                    return _FakeSheet(f.get("name"), key)
            raise KeyError(key)

        def open(self, title):
            raise gspread.SpreadsheetNotFound(title)

    gc = _FakeGC([{"id": "ID-OCT12", "name": "DAC OFFLINE OCTOBER 12  2026",
                   "createdTime": "2026-09-08T13:53:00Z",
                   "modifiedTime": "2026-10-12T09:00:00Z"}])
    sh = pending_tos.open_dated_sheet(gc, "", PREFIX, "2026-10-12")
    check("a double-spaced title opens by its DATE",
          sh is not None and sh.title == "DAC OFFLINE OCTOBER 12  2026",
          getattr(sh, "title", None))
    check("  and it opened the file by id", gc.opened == ["ID-OCT12"],
          str(gc.opened))

    # Two files for the same date: the most-recently-edited is the live one.
    gc2 = _FakeGC([
        {"id": "OLD", "name": "DAC OFFLINE OCTOBER 12 2026",
         "createdTime": "2026-09-08T13:00:00Z",
         "modifiedTime": "2026-10-11T08:00:00Z"},
        {"id": "LIVE", "name": "DAC OFFLINE OCTOBER 12  2026",
         "createdTime": "2026-09-08T13:53:00Z",
         "modifiedTime": "2026-10-12T09:00:00Z"},
    ])
    pending_tos.open_dated_sheet(gc2, "", PREFIX, "2026-10-12")
    check("the newest edit for the date is the one opened",
          gc2.opened == ["LIVE"], str(gc2.opened))

    # A date genuinely absent from Drive must still REFUSE (never another day).
    gc3 = _FakeGC([{"id": "X", "name": "DAC OFFLINE OCTOBER 11 2026",
                    "createdTime": "a", "modifiedTime": "b"}])
    try:
        pending_tos.open_dated_sheet(gc3, "", PREFIX, "2026-10-12")
        check("a date not on Drive still raises", False, "it opened something")
    except FileNotFoundError:
        check("a date not on Drive still raises", True)

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
