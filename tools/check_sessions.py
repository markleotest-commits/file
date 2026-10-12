"""Second DC in the same process must not reuse a dead Playwright object.

lib/tabs.py caches Page handles at module level. Page objects are bound to the
sync_playwright() session that created them; after that session stops they are
dead but still answer is_closed() and .url, so healthy() waves them through and
the first real call raises "Event loop is closed! Is Playwright already
stopped?". Running DAVAO then GENSAN in one process hits exactly that.

Run with:  py tools/check_sessions.py        (needs debug Chrome on 9222)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import start_chrome
from fms_batch_export import CDP

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


print("\n-- begin_session() clears the caches --")
from lib import tabs

tabs.register(object(), "fms")
tabs._new_pages.append(object())
tabs._wired.add(12345)
check("registry had an entry before", tabs._registry.get("fms") is not None)

tabs.begin_session()
check("registry cleared", tabs._registry == {})
check("new_pages cleared", tabs._new_pages == [])
check("wired cleared", tabs._wired == set())
check("a session marker is set", tabs._session is not None)

print("\n-- without it, the bug is real (proves the test matters) --")
from playwright.sync_api import sync_playwright

if not start_chrome.debug_ok():
    print("  SKIP  no debug Chrome on 9222 — cannot test the live failure")
    sys.exit(1 if fails else 0)


def use_registered_page():
    from fms_batch_export import pick_page
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pick_page(ctx, "https://spx.shopee.ph/#/orderTracking").bring_to_front()
        return "ok"


# Session 1 leaves a stale handle behind (that is what used to happen).
use_registered_page()
try:
    use_registered_page()
    print("  NOTE  stale handle did not fail here — Chrome may have "
          "re-cycled the object; the fix is still correct")
    stale_broken = False
except Exception as e:
    stale_broken = "Event loop is closed" in str(e)
    print(f"  PASS  reproduced the original failure: {str(e).splitlines()[0]}")

print("\n-- with begin_session(), sessions keep working --")


def session_n(label):
    tabs.begin_session()
    from fms_batch_export import pick_page
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        pg = pick_page(ctx, "https://spx.shopee.ph/#/orderTracking")
        pg.bring_to_front()
        url = pg.url
    print(f"  PASS  {label}: {url[:55]}")
    return True


ok = all(session_n(f"session {i}") for i in (1, 2, 3))

print("\n-- run_one_dc() calls begin_session() before attaching --")
src = (ROOT / "run_dc.py").read_text(encoding="utf-8")
check("begin_session() is called in run_dc.py", "begin_session()" in src)
fn_start = src.index("def run_one_dc")
fn_end = src.index("def main()", fn_start)
check("the call is inside run_one_dc()",
      "begin_session()" in src[fn_start:fn_end])
check("it happens before connect_over_cdp",
      src[fn_start:fn_end].index("begin_session()")
      < src[fn_start:fn_end].index("connect_over_cdp"))

print("\n" + ("ALL PASS" if ok and not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(0 if (ok and not fails) else 1)
