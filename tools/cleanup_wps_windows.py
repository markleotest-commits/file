"""Close WPS windows left behind by check_wps_open.py runs (test + start-up).

Only touches windows that are clearly from the automated test: titles
containing "WPS_OPEN_TEST", plus bare WPS start windows ("WPS Sheets",
"WPS Office" with no workbook name — they hold no user data). Real workbook
windows (e.g. "FMS_Reception_... - WPS Office") are left alone.

    py tools\\cleanup_wps_windows.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WM_CLOSE = 0x0010


def main() -> int:
    from lib import wps_copy

    hits = []
    for hwnd, title in wps_copy._enum_titles():
        t = title.lower()
        if "wps_open_test" in t or t in ("wps sheets", "wps office"):
            hits.append((hwnd, title))
    if not hits:
        print("No test/start-up WPS windows to close.")
        return 0
    print(f"Closing {len(hits)} WPS window(s):")
    for hwnd, title in hits:
        print(f"  {title}")
    import ctypes

    u = ctypes.windll.user32
    for hwnd, title in hits:
        try:
            u.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
        except Exception as e:
            print(f"  (close failed for {title}: {e})")
    time.sleep(1.5)
    # Dismiss any "Save the Document?" modal WPS answered with.
    try:
        front = wps_copy.front_window_title()
        if "Save the Document" in front or "保存" in front:
            wps_copy.dismiss_dialog()
            time.sleep(0.8)
    except Exception:
        pass
    remaining = [t for _, t in wps_copy._enum_titles()
                 if "wps_open_test" in t.lower()]
    if remaining:
        print(f"Still open (WPS keeps them): {remaining}")
    else:
        print("All test windows closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())