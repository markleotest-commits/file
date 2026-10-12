"""Is `connect_over_cdp` slow, or is Chrome wedged, or is RAM the cause?

    py tools\\probe_cdp_stall.py
    py tools\\probe_cdp_stall.py --ram
    py tools\\probe_cdp_stall.py --watch 20

Bug 23 recorded `connect_over_cdp: Timeout 180000ms exceeded` and blamed the
machine. That was a guess, and it cost a lot: the 180s timeout sat in the middle
of every run, so "slow" and "broken" were indistinguishable in practice.

The discriminating measurement, now settled:

  * `/json/version` answers in 2-170 ms   -> the port and the HTTP server are fine
  * the websocket upgrade returns 101 in ~10-200 ms -> Chrome accepts the socket
  * `Target.getTargets` NEVER replies      -> Chrome is wedged, not slow

Those three together mean Chrome took a connection and stopped servicing it.
Waiting longer has never once fixed it; it has been observed to clear on its own
within a minute, which is almost always memory pressure (measured here at
0.2-0.7 GB free of 7.69 GB with ~40 Chrome processes).

lib/cdp.py is now the canonical probe -- this tool only reports. It delegates so
there is one implementation to keep correct; an earlier version of this file had
its own copy and its two accept loops raced, which made a guard pass for the
wrong reason.

Read-only: opens a websocket and closes it immediately. Opens no tabs, changes
no state, writes nothing.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import cdp  # noqa: E402


def show_ram() -> None:
    free, total, procs = cdp.memory_state()
    if free is None:
        print("  (could not read memory state)")
        return
    pct = (1 - free / total) * 100 if total else 0
    print(f"  free RAM   {free:.2f} GB of {total:.2f} GB  ({pct:.0f}% in use)")
    print(f"  chrome     {procs} process(es)")
    if free < 1.0:
        hogs = [h for h in cdp.top_memory(6)
                if h[2] >= 0.25 and not h[0].lower().startswith("chrome")]
        print("  ^ low. Biggest non-Chrome users (private GB):")
        if hogs:
            for name, pid, priv, _ws in hogs:
                print(f"       {priv:5.2f}  {name} (pid {pid})")
        else:
            print("       (none over 0.25 GB)")
        # Corrected after measuring it: restarting Chrome does NOT fix this.
        # A fresh Chrome, clean profile, no extensions and no tabs wedged again
        # in seconds. See BUGS.md 31a.
        print("     Restarting Chrome does NOT fix this - free memory on the machine,")
        print("     then re-check with:  py lib\\cdp.py")


def main() -> int:
    args = sys.argv[1:]
    if "--ram" in args:
        show_ram()
        return 0

    watch = 20.0
    if "--watch" in args:
        try:
            watch = float(args[args.index("--watch") + 1])
        except Exception:
            pass

    show_ram()
    print(f"  probing   {watch:.0f}s\n")
    print("  t(s)   verdict    handshake  reply")
    rows = []
    end = time.monotonic() + watch
    t_start = end - watch
    while time.monotonic() < end:
        t0 = time.monotonic()
        p = cdp.probe()
        rows.append(p)
        hs = f"{p['handshake_s']:.3f}s" if p.get("handshake_s") is not None else "-"
        rp = f"{p['reply_s']:.3f}s" if p.get("reply_s") is not None else "-"
        print(f"  {t0 - t_start:5.0f}   {p['why']:<9}  {hs:>8}  {rp:>6}")
        if p["ok"]:
            break
        if time.monotonic() < end:
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    print()
    if not rows:
        print("  no probe completed")
        return 1

    healthy = [r for r in rows if r["ok"]]
    if healthy:
        r = healthy[-1]
        print(f"  CDP is healthy. handshake {r['handshake_s']}s, "
              f"reply {r['reply_s']}s.")
        print("  A healthy attach means slowness is NOT in connecting -- it is in")
        print("  the pipeline phases. Read what the last run spent its time on:")
        print("      py tools\\show_timings.py")
        print("      py tools\\show_timings.py --compare")
        return 0

    print("  CDP never became healthy. Why each attempt failed:")
    for r in rows:
        print(f"    {r['why']:<18} {r.get('detail') or ''}")

    why = rows[-1]["why"]
    print()
    if why == "wedged":
        print("  Chrome completed the websocket handshake but never answered a CDP")
        print("  command. It is wedged, not busy. Waiting longer does not help.")
        print()
        print("  What to do — free memory on the machine. NOT restart Chrome:")
        print("    Restarting was tried and does not work. A fresh Chrome with a clean")
        print("    profile, extensions disabled and every tab closed wedged again in")
        print("    seconds. See BUGS.md 31a for what was ruled out.")
        print()
        print("    1. Check the memory picture:  py tools\\probe_cdp_stall.py --ram")
        print("    2. Close something big (the list above names the hogs).")
        print("    3. Confirm Chrome is healthy:  py lib\\cdp.py   -> want ok=True")
        print("    4. Re-run. A healthy Chrome attaches in about a second.")
        print()
        print("  If page endpoints answer but the browser endpoint does not, only the")
        print("  browser's session manager is stuck -- check with:")
        print("      py tools\\probe_cdp_scope.py")
        print("  Note: the bot itself now fails in ~33s with this same diagnosis,")
        print("  instead of freezing silently for 180s. See lib/cdp.py (bug 31).")
    else:
        print("  " + cdp.describe(rows[-1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())