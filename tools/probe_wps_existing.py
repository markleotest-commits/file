"""Reproduce open_in_wps when WPS is ALREADY running (the real export flow).

State A: WPS has an older workbook open, Chrome/terminal foreground ->
         open a NEW timestamped xlsx (like FMS_Reception_<stamp>.xlsx).
State B: same, but with a "Save the Document?" modal pending from a
         hand-closed workbook -> what does the launch do, and what is
         the foreground HWND/title while it is stuck?

    py tools\\probe_wps_existing.py      # state A
    py tools\\probe_wps_existing.py --modal   # state B (leaves a modal up;
                                              # run state A afterwards to see
                                              # the queued-launch behavior)

READ-ONLY w.r.t. your work: only throwaway temp files are opened.
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WM_CLOSE = 0x0010


def make_xlsx(tag: str) -> Path:
    from openpyxl import Workbook

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(tempfile.gettempdir()) / f"WPS_EXISTING_{tag}_{stamp}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Reception"
    ws.append(["Order ID", "SLS Tracking Number", "Current TO Number"])
    ws.append(["PH000000000", "SLS000000000", "TO2026TEST"])
    wb.save(path)
    return path


def main() -> int:
    from lib import wps_copy
    from lib.to_xlsx import open_in_wps

    modal = "--modal" in sys.argv

    print(f"Foreground at start: {wps_copy.front_window_title() or '(none)'}")

    if modal:
        # Put up the save modal the way a hand-closed workbook does, then
        # report what the foreground looks like (separate HWND or in-window?).
        first = make_xlsx("FIRST")
        os_start(first)
        try:
            hwnd, title = wps_copy.wait_for_export_window(first, 30)
        except TimeoutError as e:
            print(f"setup open failed: {e}")
            return 1
        print(f"First workbook open: {title}")
        import ctypes

        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
        time.sleep(1.5)
        print(f"Foreground with modal up: "
              f"{wps_copy.front_window_title() or '(none)'}")
        print("Modal is up. Now run WITHOUT --modal to see what a new "
              "launch does behind it.")
        return 0

    second = make_xlsx("SECOND")
    t0 = time.time()
    ok = open_in_wps(second)
    print(f"open_in_wps -> {ok} in {time.time() - t0:.1f}s")
    print(f"Foreground now: {wps_copy.front_window_title() or '(none)'}")
    try:
        hwnd, title = wps_copy.wait_for_export_window(second, 5)
        print(f"Window present: {title}")
    except TimeoutError as e:
        print(f"Window NOT found: {e}")
    return 0 if ok else 1


def os_start(path: Path) -> None:
    import os

    os.startfile(path)  # noqa: PTH Missing


if __name__ == "__main__":
    raise SystemExit(main())