"""Attach to the debug Chrome — fast, checked, and honest about failure.

Why this module exists
----------------------
Every entry point did `p.chromium.connect_over_cdp(CDP)` bare, which inherits
Playwright's **180 second** default timeout. When Chrome is unhealthy that is
three minutes of a completely silent, frozen run, per attempt — and then a
message that says nothing actionable:

    BrowserType.connect_over_cdp: Timeout 180000ms exceeded.

Measured repeatedly on this machine: a run would finish its sheets read in ~10 s
and then sit for 180 s and die. That is the single largest source of "this is
too slow to automate". A pipeline that cannot be watched unattended is not
automatable, whatever its steady-state speed.

What actually goes wrong
------------------------
Not "slow to connect". The debug port is healthy the whole time — `/json/version`
answers in 2–170 ms — and the websocket **upgrade completes in ~10 ms with HTTP
101**. Chrome then accepts the CDP socket and never replies to the first command
(`Target.getTargets`). Playwright's error is accurate but uninformative, and its
180 s budget is the worst possible choice: waiting longer has never once helped,
because Chrome is not busy, it is wedged.

The fix is three things, all cheap:

1. **Pre-flight** — before handing over to Playwright, open the websocket by
   hand and send one trivial CDP command. A healthy Chrome answers in ~50 ms.
   This distinguishes "Chrome is fine" from "Chrome is wedged" in about a tenth
   of a second, and tells the two apart from "the port is closed".
2. **A sane timeout** — 25 s, not 180 s. Measured healthy attaches take 0.6–1.4 s
   on this box, so 25 s is roughly twenty times the observed worst case.
3. **Free RAM first** — this box is 7.69 GB, and the debug Chrome accumulates
   the operator's personal tabs (Discord, video, manga) until free memory is
   down to ~0.4 GB and Chrome wedges. Before probing, `attach` closes every tab
   in the BOT'S Chrome that is not FMS, the Misroute Checker or the control
   panel (see `prune_extra_tabs`). The operator's normal Chrome is never touched.
4. **Recover, then an actionable error** — after freeing tabs a wedge is given a
   bounded chance to clear (`WEDGE_RECOVER_S`), except when free RAM is below
   `WEDGE_MIN_FREE_GB`, where waiting cannot help so it fails immediately with
   the reason. That RAM-starved path also probes with a short cap
   (`LOW_RAM_PROBE_CAP_S`), so an attempt that provably cannot succeed costs
   ~1.5 s instead of ~6 s, and the raised message states the RAM fact on its
   **first** line (the control panel only shows that line plus a short reason).
   A healthy-but-failed attach is retried once. When it still fails, the error
   names the cause and the fix instead of restating a timeout.

Nothing here launches or kills Chrome. Attaching is the only job; deciding to
restart a user's browser is theirs to make.

Not purely read-only: it closes the bot Chrome's non-essential tabs over the
plain HTTP endpoint (`/json/list`, `/json/close/<id>`), which keeps answering
even while the CDP websocket is wedged. It opens no tab and changes no other
state.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request

CDP = "http://127.0.0.1:9222"
CDP_PORT = 9222

# 25 s. Healthy attaches on this machine measured 0.6-1.4 s (tools/probe_cdp_latency.py),
# so this is ~20x the observed worst case while still being 7x faster than the
# default that made the pipeline look broken.
ATTACH_TIMEOUT_S = 25

# Cheap pre-flight budget. A healthy Chrome replies to Target.getTargets in
# ~50 ms; this only has to be long enough to be conclusive on a slow machine.
PREFLIGHT_CAP_S = 6.0

# Pre-flight budget when the machine is ALREADY out of RAM. In that state the
# pre-flight is guaranteed to time out (the wedge cannot clear), so the long cap
# only makes a RAM-starved run *feel* slower: every failing attempt paid the full
# 6 s before the RAM verdict was even reached (bug 53). A healthy Chrome still
# answers in ~50 ms, so this stays conclusive and the failed attempt drops to
# ~1.5 s.
LOW_RAM_PROBE_CAP_S = 1.5

# A wedge is almost always RAM pressure, and freeing the operator's extra tabs
# can take a few seconds to show up. This is the total time `attach` waits for a
# wedged Chrome to come back before writing the run off -- bounded, so a browser
# that is wedged for good still fails in under a minute (not Playwright's 180 s).
#
# The loop is deliberately a SHORT, fast-cycling check: it catches a Chrome that
# clears in a few seconds and prunes its tabs between probes, then hands the tail
# to the real connect (which returns the instant Chrome answers). It used to run
# a full 30 s of 6 s probes and only THEN connect, paying twice for one recovery
# and detecting the clear only every ~12 s (bug 53).
WEDGE_RECOVER_S = 20.0
WEDGE_POLL_S = 2.0

# A recovery probe only has to outlast a healthy reply (~50 ms); 3 s is enough to
# be conclusive and keeps the cost of each wedge probe small, so the loop cycles
# every ~2-3 s instead of every ~12 s.
WEDGE_PROBE_CAP_S = 3.0

# Below this much free physical RAM a wedge is plain exhaustion: no amount of
# waiting frees the machine (the operator must close their normal Chrome / heavy
# apps), so `attach` fails immediately with the reason instead of burning the
# recovery budget and feeling slower. Measured on this box: ~0.7 GB free was
# enough for a wedge to recover on its own; the repeated failures lived at
# 0.4-0.5 GB free.
WEDGE_MIN_FREE_GB = 0.6

# URL substrings for the tabs the bot's Chrome must keep; anything else in it is
# the operator's personal browsing and is closed to free RAM. "mc_v2"/"mc_v1"/
# "/mc/" cover the local MC_V2.html; a configured MC_URL host is added at run
# time. "127.0.0.1:"/"localhost:" keep the control panel no matter which port it
# picked. Case-insensitive.
BOT_TAB_HINTS = ("shopee.ph", "127.0.0.1:", "localhost:",
                 "mc_v2", "mc_v1", "/mc/")


# ── machine state, for the error message ────────────────────────────────────
def _memory_phys() -> tuple[float | None, float | None]:
    """(free GB, total GB) via GlobalMemoryStatusEx — sub-millisecond, no PS."""
    free = total = None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        free, total = st.ullAvailPhys / 1024 ** 3, st.ullTotalPhys / 1024 ** 3
    except Exception:
        pass
    return free, total


def memory_state() -> tuple[float | None, float | None, int | None]:
    """(free GB, total GB, chrome process count). Any element may be None."""
    free, total = _memory_phys()
    procs = None
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count"],
            capture_output=True, text=True, timeout=20)
        procs = int((out.stdout or "").strip() or 0)
    except Exception:
        pass
    return free, total, procs


def top_memory(limit: int = 6) -> list[tuple[str, int, float, float]]:
    """The processes actually eating memory, as (name, pid, private GB, resident GB).

    Added after this advice turned out to be wrong. The original message said
    "close Chrome tabs, or restart Chrome", and that was tested: Chrome was fully
    killed, relaunched with the same profile, and it wedged again within seconds.
    Restarting Chrome was never going to work.

    The real measurement on this box:

        physical total      7.69 GB
        physical free       0.58 GB
        commit used        22.49 GB of a 26.02 GB limit
        all processes resident  2.93 GB      <-- tiny
        biggest single private: `ollama app`  3.89 GB

    Chrome held 2.48 GB across 39 processes and was the obvious suspect, but the
    committed-vs-resident gap is the actual story: this machine is thrashing,
    and the largest single commitment belonged to Ollama, not to Chrome. A tool
    that guesses "it's Chrome" makes the user close the wrong thing and conclude
    the diagnosis is nonsense. So the message names the real hogs.

    Returns [] if the counter is unavailable (non-Windows, or no permission) --
    callers must treat this as "no data", never as "nothing is using memory".
    """
    try:
        import subprocess
        ps = (
            "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | "
            "Where-Object { $_.Name -ne '_Total' -and $_.IDProcess -gt 0 } | "
            "Sort-Object PrivateBytes -Descending | "
            f"Select-Object -First {int(limit)} "
            "Name, IDProcess, PrivateBytes, WorkingSet | "
            "ConvertTo-Json -Compress"
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30)
        raw = (out.stdout or "").strip()
        if not raw:
            return []
        import json as _j
        data = _j.loads(raw)
        if isinstance(data, dict):
            data = [data]
        rows = []
        for d in data:
            rows.append((str(d.get("Name", "?")), int(d.get("IDProcess", 0)),
                         float(d.get("PrivateBytes", 0)) / 1024 ** 3,
                         float(d.get("WorkingSet", 0)) / 1024 ** 3))
        return rows
    except Exception:
        return []


# ── the pre-flight probe ────────────────────────────────────────────────────
def probe(cap: float = PREFLIGHT_CAP_S) -> dict:
    """Ask Chrome one trivial CDP question over a hand-rolled websocket.

    Returns a dict with at least ``ok`` (bool) and ``why`` (str). Distinguishes
    the three states that look identical from Playwright's timeout:

        port closed          -> why="no port"
        upgrade refused      -> why="upgrade refused"
        connected, no reply  -> why="wedged"      <-- the one that burns 180 s

    ``ok`` True means Chrome is answering CDP commands right now.
    """
    result = {"ok": False, "why": "unknown", "handshake_s": None,
              "reply_s": None, "browser": None}

    # 1. is anything listening?
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=min(cap, 5.0)) as r:
            info = json.loads(r.read())
        result["browser"] = info.get("Browser")
    except Exception as e:
        result["why"] = "no port"
        result["detail"] = str(e)[:70]
        return result

    ws_url = info.get("webSocketDebuggerUrl", "")
    path = "/devtools/browser/" + ws_url.rsplit("/", 1)[-1] if ws_url else ""
    if not path:
        result["why"] = "no ws url"
        return result

    # 2. can we complete the HTTP upgrade?
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=min(cap, 5.0))
    except Exception as e:
        result["why"] = "no port"
        result["detail"] = str(e)[:70]
        return result

    key = base64.b64encode(os.urandom(16)).decode()
    try:
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{CDP_PORT}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                   ).encode())
        s.settimeout(cap)
        resp = s.recv(4096)
    except Exception as e:
        result["why"] = "upgrade failed"
        result["detail"] = f"{type(e).__name__}"
        return result

    result["handshake_s"] = round(time.monotonic() - t0, 3)
    if b"101" not in resp.splitlines()[0]:
        result["why"] = "upgrade refused"
        result["detail"] = resp.splitlines()[0][:60].decode("latin-1", "replace")
        return result

    # 3. THE decisive question: does it answer a command? This is the step that
    #    hangs on a wedged Chrome, and the reason for the whole module.
    payload = json.dumps({"id": 1, "method": "Target.getTargets"}).encode()
    frame = bytearray([0x81])
    n = len(payload)
    if n < 126:
        frame += bytes([0x80 | n])
    else:
        frame += bytes([0x80 | 126]) + struct.pack(">H", n)
    try:
        s.sendall(bytes(frame) + payload)
        s.settimeout(cap)
        buf = s.recv(65536)
        result["reply_s"] = round(time.monotonic() - t0 - result["handshake_s"], 3)
        result["ok"] = True
        result["why"] = "healthy"
        result["targets"] = len(buf)
    except socket.timeout:
        result["why"] = "wedged"
        result["detail"] = (f"websocket upgrade returned 101 in "
                            f"{result['handshake_s']:.3f}s, then Chrome never "
                            f"replied to Target.getTargets")
    except Exception as e:
        result["why"] = "closed after upgrade"
        result["detail"] = f"{type(e).__name__}: {str(e)[:50]}"
    finally:
        try:
            s.close()
        except Exception:
            pass
    return result


def describe(p: dict | None = None, chrome_procs: int | None = None) -> str:
    """A short, specific explanation with the next action spelled out."""
    p = p if p is not None else probe()
    free = total = procs = None

    lines = []
    if p.get("why") == "no port":
        lines.append("Nothing is listening on 127.0.0.1:9222, so the debug "
                     "Chrome is not running.")
        lines.append("  Fix:  py start_chrome.py     (keep that window open)")
    elif p.get("why") in ("upgrade refused", "upgrade failed", "no ws url"):
        lines.append(f"The debug port answered but the CDP websocket was not "
                     f"usable ({p.get('why')}: {p.get('detail')}).")
        lines.append("  Fix: close every Chrome window, then  py start_chrome.py")
    elif p.get("why") == "wedged":
        lines.append("Chrome's debug port is open and completes the websocket "
                     "handshake in "
                     f"{p.get('handshake_s')}s, but it never answers a single "
                     "CDP command.")
        lines.append("This is a wedged browser, not a slow one - waiting longer "
                     "on its own does not help; freeing RAM does. The bot closes "
                     "its Chrome's extra tabs before every attach (bug 51).")
        free, total, procs = memory_state()
        if free is not None and total is not None:
            lines.append(f"  Memory: {free:.2f} GB free of {total:.2f} GB"
                         + (f", {procs} Chrome process(es)"
                            if procs is not None else ""))
        # The wedge was measured to survive a full Chrome restart, so telling
        # the user to restart Chrome sends them in circles. Name the hogs.
        hogs = [h for h in top_memory(6)
                if h[2] >= 0.25 and not h[0].lower().startswith("chrome")]
        if free is not None and free < 1.5:
            lines.append("  This machine is out of memory. Restarting Chrome does NOT "
                         "fix it - that was measured: a fresh Chrome wedged again "
                         "in seconds.")
            if hogs:
                lines.append("  Biggest non-Chrome users right now (private GB):")
                for name, pid, priv, ws in hogs:
                    lines.append(f"      {priv:5.2f} GB  {name} (pid {pid})")
                lines.append("  Free one of those, then re-run. Check again with:")
            else:
                lines.append("  Free some memory, then re-run. Check again with:")
            lines.append("      py tools\\probe_cdp_stall.py --ram")
        else:
            lines.append("  Fix: close Chrome tabs you are not using, then re-run.")
            lines.append("  Check first:  py tools\\probe_cdp_stall.py --ram")
    elif p.get("why") == "closed after upgrade":
        lines.append(f"Chrome accepted the CDP connection and then dropped it "
                     f"({p.get('detail')}).")
        lines.append("  Fix: restart the debug Chrome:  py start_chrome.py")
    else:
        lines.append(f"CDP probe said '{p.get('why')}'.")
    return "\n".join(lines)


# ── free RAM: close the bot Chrome's non-bot tabs ───────────────────────────
def _bot_page_patterns() -> tuple[str, ...]:
    """URL substrings that mark a tab as one the bot needs to keep open."""
    pats = list(BOT_TAB_HINTS)
    try:
        from lib.mc_drive import _mc_host

        import config

        host = _mc_host(getattr(config, "MC_URL", "") or "")
        if host:
            pats.append(host)
    except Exception:
        pass
    return tuple(pats)


def prune_extra_tabs(cdp: str = CDP, log=None) -> list[str]:
    """Close the bot Chrome's non-essential tabs and return the URLs closed.

    The debug Chrome is meant to hold three tabs: FMS, the Misroute Checker and
    the control panel. In practice it also collects the operator's personal
    browsing, and on a 7.69 GB box that is what tips Chrome into the wedged
    state where it stops answering CDP. Closing those tabs is the one lever the
    bot has over the RAM pressure, and it goes over Chrome's plain HTTP endpoint
    because that keeps answering even while the CDP websocket is wedged.

    Only "page" targets are considered; workers, extensions and browser UI are
    left alone, and the operator's normal Chrome (a different profile on a
    different port) is never touched. Best-effort: errors are logged and
    ignored, so a failure here can never break an otherwise healthy attach.
    """
    say = log or (lambda *a, **k: None)
    closed: list[str] = []
    try:
        with urllib.request.urlopen(f"{cdp}/json/list", timeout=5) as r:
            targets = json.loads(r.read())
    except Exception as e:
        say(f"Could not list Chrome tabs ({type(e).__name__})")
        return closed

    keep = _bot_page_patterns()
    for t in targets or []:
        if not isinstance(t, dict) or t.get("type") != "page":
            continue
        url = str(t.get("url") or "")
        if any(p in url.lower() for p in keep):
            continue
        tid = t.get("id")
        if not tid:
            continue
        try:
            with urllib.request.urlopen(f"{cdp}/json/close/{tid}", timeout=5) as r:
                r.read()
            closed.append(url)
        except Exception as e:
            say(f"Could not close a non-bot tab ({type(e).__name__})")

    if closed:
        shown = ", ".join(u[:48] for u in closed[:3])
        more = f" (+{len(closed) - 3} more)" if len(closed) > 3 else ""
        say(f"Closed {len(closed)} non-bot tab(s) to free RAM: {shown}{more}")
    return closed


def _ram_exhausted() -> bool:
    """True when the machine has no spare RAM for a wedge to recover into."""
    free, total = _memory_phys()
    return free is not None and total is not None and free < WEDGE_MIN_FREE_GB


def _try_recover_wedge(pre: dict, cdp: str = CDP, log=None) -> dict:
    """Give a wedged Chrome a bounded chance to clear *when it can help*.

    A wedge is almost always RAM pressure. The bot's lever is closing the bot
    Chrome's extra tabs; the operator's is closing their normal Chrome / heavy
    apps. When free RAM is already below ``WEDGE_MIN_FREE_GB`` the wedge is
    plain exhaustion -- no amount of waiting frees the machine, so report the
    verdict fast instead of burning the recovery budget and making the failure
    feel slower (bug 51 follow-up). Otherwise prune and re-probe for up to
    ``WEDGE_RECOVER_S``; a wedge was observed to clear that way.

    ``pre`` is the probe that found the wedge -- reused, not re-run. Returns the
    final probe dict.
    """
    say = log or (lambda *a, **k: None)
    free, total = _memory_phys()
    if free is not None and total is not None and free < WEDGE_MIN_FREE_GB:
        say(f"Chrome is wedged and only {free:.2f} GB of {total:.2f} GB is free "
            "- that is RAM exhaustion, so the wait is skipped. Close the normal "
            "Chrome / heavy apps, then re-run.")
        return pre
    say("Chrome is wedged - freeing tabs and waiting for it to clear...")
    t0 = time.monotonic()
    p = pre
    while not p["ok"] and time.monotonic() - t0 < WEDGE_RECOVER_S:
        prune_extra_tabs(cdp, log=say)
        # Probe FIRST (a healthy Chrome answers in ~50 ms) so the moment the
        # wedge clears is caught immediately instead of after a 6 s sleep.
        p = probe(cap=WEDGE_PROBE_CAP_S)
        if p["ok"]:
            break
        free, total = _memory_phys()
        mem = (f"{free:.2f}/{total:.2f} GB free"
               if free is not None and total is not None else "memory ?")
        say(f"Chrome still wedged after {time.monotonic() - t0:.0f}s ({mem})")
        time.sleep(WEDGE_POLL_S)
    return p


# ── the attach every entry point should use ─────────────────────────────────
def attach(playwright, cdp: str = CDP, timeout: int = ATTACH_TIMEOUT_S,
           retries: int = 1, log=None):
    """``playwright.chromium.connect_over_cdp(cdp)`` that fails usefully.

    ``playwright`` is a live ``sync_playwright()`` context.

    Sequence per attempt: close the bot Chrome's non-bot tabs to free RAM, then
    the pre-flight probe (cheap, names the failure), then the real attach with a
    bounded timeout. A wedge -- the failure this module exists for -- is RAM
    pressure: it is given a bounded chance to clear (``WEDGE_RECOVER_S``), except
    when free RAM is already below ``WEDGE_MIN_FREE_GB``, where waiting cannot
    help so the run fails immediately with the reason. When it still fails, the
    raised ``RuntimeError`` names the cause and the fix, not "timeout".
    """
    say = log or (lambda *a, **k: None)
    last_exc = None

    # When the machine is already out of RAM a wedged probe is guaranteed to
    # time out, so the long pre-flight only makes a RAM-starved run *feel*
    # slower (bug 53). Use a short cap in that state: a healthy Chrome still
    # answers in ~50 ms, but a wedge is written off in ~1.5 s, not ~6 s.
    cap = LOW_RAM_PROBE_CAP_S if _ram_exhausted() else PREFLIGHT_CAP_S

    for attempt in range(1, retries + 2):
        # Free RAM before asking Chrome for anything. This is what turned the
        # "6 runs in 10" attach failures back into runs: the bot Chrome was
        # carrying the operator's personal tabs on a 7.69 GB box (bug 51).
        prune_extra_tabs(cdp, log=say)

        pre = probe(cap=cap)
        if pre["ok"]:
            say(f"CDP pre-flight ok (handshake {pre['handshake_s']}s, "
                f"reply {pre['reply_s']}s)")
        elif pre["why"] == "wedged":
            pre = _try_recover_wedge(pre, cdp, say)
            if pre["ok"]:
                say("Chrome recovered — attaching now.")
        else:
            say(f"Chrome is not answering CDP ({pre['why']})")

        # RAM exhaustion does not need a 25 s connect to prove: with under
        # WEDGE_MIN_FREE_GB free a connect cannot recover and can only hang.
        # Fail now, fast, with the reason (bug 51 follow-up: the bounded wait
        # made every RAM-starved failure feel slower with no wins).
        if (not pre["ok"]) and _ram_exhausted():
            raise RuntimeError(_fail_text(pre))

        try:
            b = playwright.chromium.connect_over_cdp(cdp, timeout=timeout * 1000)
            say(f"Attached to Chrome in under {timeout}s")
            return b
        except Exception as e:
            msg = str(e).splitlines()[0][:110]
            say(f"Attach attempt {attempt}/{retries + 1} failed: {msg}")

            # Still unhealthy even with RAM to spare: the recovery wait and this
            # connect both had their chance, so stop rather than loop.
            if not pre["ok"]:
                last_exc = RuntimeError(
                    f"{_fail_text(pre)}\n  Playwright said: {msg}")
                break

            # The probe was healthy, so this is contention or a mid-flight
            # navigation -- that genuinely succeeds on a second try.
            last_exc = RuntimeError(
                f"{_fail_text(probe())}\n  Playwright said: {msg}")
            if attempt <= retries:
                wait = 5 * attempt
                say(f"Chrome answered the pre-flight, so this may be a race — "
                    f"waiting {wait}s and retrying once...")
                time.sleep(wait)

    raise last_exc


def _fail_text(p: dict) -> str:
    """The raised message: the actionable cause first, then the full detail.

    The control panel shows only the FIRST line plus a short "reason", so the
    RAM fact -- the most common real cause and the one the operator can act on
    immediately -- is stated up front instead of being buried under the generic
    "wedged browser" wording (bug 53).
    """
    head = "Could not attach to the debug Chrome."
    free, total = _memory_phys()
    if free is not None and total is not None and free < WEDGE_MIN_FREE_GB:
        head = (f"Could not attach to the debug Chrome: the machine is out of "
                f"RAM ({free:.2f} GB free of {total:.2f} GB), so the wedge cannot "
                "clear.")
    return head + "\n" + describe(p)


def health() -> dict:
    """probe() + memory, for `py tools/probe_cdp_stall.py` and diagnostics."""
    p = probe()
    free, total, procs = memory_state()
    p["free_gb"], p["total_gb"], p["chrome_procs"] = free, total, procs
    return p


if __name__ == "__main__":  # pragma: no cover
    r = health()
    print(f"ok={r['ok']}  why={r['why']}  browser={r.get('browser')}")
    if r.get("free_gb") is not None:
        print(f"free {r['free_gb']:.2f} GB of {r['total_gb']:.2f} GB, "
              f"{r.get('chrome_procs')} chrome process(es)")
    hogs = top_memory(8)
    if hogs:
        print("\nbiggest private-memory users:")
        for name, pid, priv, ws in hogs:
            print(f"  {priv:6.2f} GB private  {ws:6.2f} GB resident  {name} ({pid})")
    print("\n" + describe(r))