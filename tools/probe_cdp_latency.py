"""How long does attach_over_cdp actually take right now?

tools/check_sessions.py started failing on
    BrowserType.connect_over_cdp: Timeout 180000ms exceeded
Its own assertions all passed first, and it imports only the CDP constant and
pick_page from fms_batch_export -- neither of which the scroll work touched.

This measures the bare minimum: launch Playwright, attach, count tabs, detach.
No fms_batch_export code, no tabs.py, nothing from the project.
Run with:  py tools\\probe_cdp_latency.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
ROUNDS = 3


def main() -> int:
    times = []
    for i in range(1, ROUNDS + 1):
        t0 = time.time()
        try:
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(CDP, timeout=180_000)
                dt = time.time() - t0
                tabs = sum(len(c.pages) for c in b.contexts)
                b.close()
            times.append(dt)
            print(f"  attempt {i}: attached in {dt:6.1f}s  ({tabs} tab(s))")
        except Exception as e:
            dt = time.time() - t0
            print(f"  attempt {i}: FAILED after {dt:6.1f}s  "
                  f"{str(e).splitlines()[0][:70]}")
    if times:
        print(f"\n  fastest {min(times):.1f}s, slowest {max(times):.1f}s")
        print("  a bare attach has no project code in it, so this is the browser "
              "and the machine, not the pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())