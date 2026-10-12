
"""Start a second Chrome the bot can control. Your normal Chrome can stay open."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PORT = 9222

# Health-probe budget. A healthy debug Chrome answers CDP in ~50 ms; a wedged one
# (port open, CDP silent — bug 31) is written off in this many seconds.
PROBE_CAP_S = 2.0

URL = "https://spx.shopee.ph/#/orderTracking"
ROOT = Path(__file__).resolve().parent
# This file ships in two places (the project root and lib/) and the two copies
# must stay byte-identical. Resolve the PROJECT root by where config.py lives, so
# the profile and downloads land in the same place no matter which copy runs.
if not (ROOT / "config.py").is_file() and (ROOT.parent / "config.py").is_file():
    ROOT = ROOT.parent
PROFILE = ROOT / "chrome-debug-profile"

CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def find_chrome() -> Path:
    for p in CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("Chrome not found.")


def port_open() -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _probe() -> dict:
    """The real CDP health check, from the one implementation (lib.cdp)."""
    from lib import cdp
    return cdp.probe(cap=PROBE_CAP_S)


def debug_ok() -> bool:
    """True only when the debug Chrome actually ANSWERS CDP, not merely HTTP.

    `/json/version` returns 200 even when Chrome is wedged (bug 31): the port is
    open and the browser never answers a single command. The old port-only check
    therefore called a stuck browser healthy, so re-running this script was a
    no-op and the operator had no way to get a usable browser back (bug 53). The
    probe takes ~50 ms when healthy and tells "no port" from "wedged".
    """
    try:
        return bool(_probe().get("ok"))
    except Exception:
        return False


def main() -> None:
    chrome = find_chrome()
    PROFILE.mkdir(exist_ok=True)
    info("Chrome exe:", chrome)
    info("Bot profile:", PROFILE)
    info("(Your other Chrome windows can stay open.)")

    pre = _probe()
    if pre.get("ok"):
        ok("Debug port already works. Next:  py fms_batch_export.py")
        return

    if port_open():
        if pre.get("why") == "wedged":
            # A wedge (bug 31) is not a foreign process and not fixable here:
            # launching Chrome with the same profile only talks to the wedged
            # process. The operator must close THAT window first.
            warn("The debug Chrome on port 9222 is WEDGED (the port answers, "
                 "but CDP never replies).")
            info("It cannot recover in place, and re-launching would just talk to "
                 "the same wedged process.")
            info("Close THAT Chrome window, then run:  py start_chrome.py")
            info("(Task Manager -> end the chrome.exe whose command line contains")
            info(" 'chrome-debug-profile'. Leave your normal Chrome running.)")
            sys.exit(1)
        info("Port 9222 is busy but not Chrome debug. Close leftover chrome.exe in Task Manager.")
        sys.exit(1)

    dest = ROOT / "downloads"
    dest.mkdir(exist_ok=True)
    cmd = [
        str(chrome),
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--safebrowsing-disable-download-protection",
        # Cap renderer processes so Chrome stops pre-spawning a spare renderer
        # per tab. On this 7.69 GB box the bot Chrome (FMS + MC + panel) was
        # ballooning to ~9 renderer processes (~0.5 GB resident); a limit of 4
        # keeps those tabs working while freeing ~0.3-0.5 GB (bug 52).
        "--renderer-process-limit=4",
        # ...but the limit above is defeated by extensions, which each get their
        # own renderer: the debug Chrome showed 7 renderers for only 3 tabs
        # (3 pages + top-chrome-webui + ~3 extensions). The bot drives FMS itself
        # and needs no extension, so turn them off, and likewise component
        # extensions that run background pages (bug 53).
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        URL,
    ]
    info("Starting:", cmd)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(30):
        time.sleep(0.3)
        if debug_ok():
            info("Chrome debug is ON. Log in to FMS in THAT new window (first time only).")
            info("Leave it open. Next:  py fms_batch_export.py")
            return

    warn("FAILED: Chrome opened but debug port did not start.")
    info("Task Manager → end ALL Google Chrome → run py start_chrome.py again.")


if __name__ == "__main__":
    cli.entry("start_chrome", "Start the FMS debug Chrome")(main)()


