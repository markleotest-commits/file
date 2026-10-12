"""The export must open IN FRONT of the terminal, not behind it.

Symptom this guards (bug 32 + 37): after the FMS export was downloaded and
converted, WPS did start — but its window came up behind the command
terminal and stayed there. The operator had to click the terminal for the
sheet to appear. Two stacked causes:

  * Windows only lets a window take the foreground when the calling process
    owns the input queue or is exempt from the foreground lock. A console
    app running the pipeline is neither, so its activation request was
    refused and the window stayed behind.
  * The bug-32 pre-grant `AllowSetForegroundWindow(ASFW_ANY)` is a no-op
    from a background process: that API only works when the CALLER owns the
    foreground, and during the export flow the foreground is Chrome. The fix
    for bug 37 zeroes `ForegroundLockTimeout` (SPI_SETFOREGROUNDLOCKTIMEOUT,
    in-session, restored afterwards) so a background process can legally
    front the window, then retries the AttachThreadInput + ALT unlock.

This tool asserts the post-condition end to end on a real WPS window: it
opens a throwaway .xlsx through `to_xlsx.open_in_wps` and fails unless that
window is genuinely foreground when the call returns. Run it a few times —
the lock refusal is timing-dependent ("OFTEN", not "always").

    py tools\\check_wps_open.py            # open -> assert foreground -> close
    py tools\\check_wps_open.py --naive    # os.startfile only, for diagnosis
    py tools\\check_wps_open.py --keep     # leave the window open

WPS will steal focus for a second or two. That is the point of the test.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import wps_copy  # noqa: E402

WM_CLOSE = 0x0010


def make_test_xlsx() -> Path:
    """A real .xlsx so WPS opens it exactly as it opens the export."""
    from openpyxl import Workbook

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(tempfile.gettempdir()) / f"WPS_OPEN_TEST_{stamp}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Reception"
    ws.append(["Order ID", "SLS Tracking Number", "Current TO Number"])
    ws.append(["PH000000000", "SLS000000000", "TO2026TEST"])
    wb.save(path)
    return path


def close_window(hwnd: int) -> None:
    try:
        import ctypes

        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
    except Exception as e:
        print(f"  (could not close the test window: {e})")
    # WPS answers WM_CLOSE with a "Save the Document?" modal. If we leave it
    # up, the NEXT test launch queues behind it and fails with "No WPS
    # window" (seen live). ESC dismisses it like a "Don't Save".
    time.sleep(0.8)
    try:
        front = wps_copy.front_window_title()
        if "Save the Document" in front or "保存" in front:
            import ctypes

            u = ctypes.windll.user32
            u.keybd_event(0x1B, 0, 0, 0)  # VK_ESCAPE down
            u.keybd_event(0x1B, 0, 2, 0)  # VK_ESCAPE up
            time.sleep(0.3)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive", action="store_true",
                    help="only os.startfile(), the pre-fix behaviour")
    ap.add_argument("--keep", action="store_true",
                    help="leave the test window open")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    if sys.platform != "win32":
        print("Windows only.")
        return 1

    path = make_test_xlsx()
    print(f"Test file: {path}")
    print(f"Foreground before: {wps_copy.front_window_title() or '(none)'}")

    t0 = time.time()
    hwnd = None
    try:
        if args.naive:
            # What the bot used to do. Reported, not asserted: on some
            # desktops the plain launch does happen to win the foreground.
            os.startfile(path)
            try:
                hwnd, title = wps_copy.wait_for_export_window(path, args.timeout)
            except TimeoutError as e:
                print(f"FAIL: {e}")
                return 1
        else:
            from lib.to_xlsx import open_in_wps

            if not open_in_wps(path):
                print("FAIL: open_in_wps() did not get the window in front.")
                return 1
            hwnd, title = wps_copy.wait_for_export_window(path, 5)

        fg = wps_copy.front_window_title()
        took = time.time() - t0
        print(f"Window appeared in {took:.1f}s: {title}")
        print(f"Foreground now  : {fg or '(none)'}")

        front_ok = path.stem.lower() in fg.lower()
        if front_ok:
            print("PASS: the export is open and in front.")
        else:
            print("FAIL: WPS opened but something else is in front "
                  "(this is the click-the-terminal bug).")
        return 0 if front_ok else 1
    finally:
        if hwnd and not args.keep:
            time.sleep(0.5)
            close_window(hwnd)
        if not args.keep:
            # WPS keeps the file locked for a moment after the window closes,
            # so one unlink is not enough. Leftovers are harmless (temp dir),
            # but say so rather than pretending it went away.
            for _ in range(10):
                try:
                    path.unlink()
                    print(f"Cleaned up {path.name}")
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                print(f"Left behind {path} (WPS still had it locked).")


if __name__ == "__main__":
    raise SystemExit(main())
