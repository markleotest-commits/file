"""The Batch Search wait must prove THIS search rendered (bug 49), no live FMS.

FMS keeps the previous search's rows AND its "All N orders have been found."
banner on screen until the new search renders. The old wait was a bare
`get_by_text("orders have been found").wait_for(...)`, which the leftover banner
satisfied INSTANTLY — so the packed walk copied the previous (reception)
search's 1006-row table while the footer flipped to the packed total, the paste
carried 96 rows / 46 strangers, and it reported 13 of 63 packed OIDs missing
even though FMS itself said all 63 were found.

    py tools\\check_search_freshness.py
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


class FakePage:
    """Scripted FMS page: token + loading answers, consumed then repeated."""

    def __init__(self, tokens, loading):
        self._tokens = list(tokens)
        self._loading = list(loading)
        self.token_calls = 0
        self.loading_calls = 0

    @staticmethod
    def _next(q):
        if len(q) > 1:
            return q.pop(0)
        return q[0] if q else None

    def evaluate(self, js, *a):
        if ".ssc-loading" in js:
            self.loading_calls += 1
            return bool(self._next(self._loading))
        self.token_calls += 1
        return self._next(self._tokens)

    def wait_for_timeout(self, ms):
        time.sleep(min(ms, 250) / 1000.0)


def main() -> int:
    from fms_batch_export import (
        _results_token,
        _wait_for_new_results,
        _RESULTS_TOKEN_JS,
    )

    src = (ROOT / "fms_batch_export.py").read_text(encoding="utf-8")

    print("\n-- the wait must require a CHANGE, not a pre-existing banner --")
    check("the old bare banner wait is gone",
          'get_by_text(re.compile(r"orders have been found"' not in src)
    check("Submit snapshots the page BEFORE clicking",
          "before = _results_token(page)" in src
          and src.index("before = _results_token(page)")
          < src.index('get_by_role("button", name="Submit").click'))
    check("and waits for that snapshot to change",
          "_wait_for_new_results(page, before, timeout)" in src)
    check("the token reads banner + total + rows",
          "orders have been found" in _RESULTS_TOKEN_JS
          and "Total" in _RESULTS_TOKEN_JS
          and "SPX Tracking Number" in _RESULTS_TOKEN_JS)

    print("\n-- scenario A: stale reception banner, then the packed results --")
    # Reception search left "1006 rows / 50 shown / banner 250". Submit must not
    # accept that; only the changed token (banner 63, 63 rows) is fresh.
    stale = "250|1006|50|PH2600000000001~x|PH2600000000050~y"
    fresh = "63|63|50|PH2605640590300~a|PH2605640590349~b"
    pg = FakePage([stale, stale, stale, fresh, fresh], [False])
    t0 = time.time()
    got = _wait_for_new_results(pg, stale, 5)
    check("returns True only after the change", got is True)
    check("it did NOT accept the stale token first", pg.token_calls >= 4,
          f"token_calls={pg.token_calls}")
    check("fast (returned as soon as the change appeared)", time.time() - t0 < 4)

    print("\n-- scenario B: identical repeat search, proven by a spinner --")
    # A re-run of the SAME batch yields an identical token. A loading spinner
    # appearing AFTER Submit is proof a new search ran, so it may proceed.
    same = "63|63|50|PH2605640590300~a|PH2605640590349~b"
    pg2 = FakePage([same, same, same], [True, False, False, False, False])
    got2 = _wait_for_new_results(pg2, same, 5)
    check("an unchanged result is accepted once a spinner ran", got2 is True,
          f"got={got2}")

    print("\n-- scenario C: nothing changed and no spinner -> not ready --")
    # This is the guard that keeps the stale-table bug from silently returning:
    # with no change and no spinner, the invalid state must be reported, not
    # treated as success (submit_and_wait_results raises and retries).
    pg3 = FakePage([stale], [False])
    got3 = _wait_for_new_results(pg3, stale, 0)
    check("returns False when nothing proves a new render", got3 is False,
          f"got={got3}")

    print("\n-- _results_token never throws on a weird page --")
    class Boom:
        def evaluate(self, js, *a):
            raise RuntimeError("detached")

    check("token falls back to ''", _results_token(Boom()) == "")

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
