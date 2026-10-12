"""Narrow down a wedged Chrome: which CDP endpoints still answer?

    py tools\\probe_cdp_scope.py

`lib/cdp.py` probes the BROWSER-level websocket (`/devtools/browser/<id>`),
which is what `connect_over_cdp` uses. When that goes mute, the natural next
question is whether the wedge is total or specific -- in particular whether
individual PAGE targets still answer. That distinction decides whether a
workaround exists at all:

  * browser endpoint mute, page endpoints alive -> the browser's session
    manager is stuck. A per-page attach (CDP over a page's own websocket) might
    still work, which would be a usable fallback.
  * everything mute -> the renderer/network service is wedged too, and only a
    browser restart can help.

Also reports how many targets exist, because a plausible cause is accumulated
zombie sessions: every crashed or timed-out `connect_over_cdp` leaves a websocket
half-open on the browser, and a browser that has accumulated dozens of unserviced
CDP sessions is a plausible cause of a browser-level endpoint going quiet.

Read-only. Opens websockets and closes them; changes no state.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CDP = "http://127.0.0.1:9222"


def ws_roundtrip(path, method, params=None, cap=8.0, msg_id=1):
    """Open a websocket, send one CDP command, return (secs, text|None)."""
    t0 = time.monotonic()
    try:
        s = socket.create_connection(("127.0.0.1", 9222), timeout=5)
    except Exception as e:
        return None, f"tcp: {type(e).__name__}"
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                   ).encode())
        s.settimeout(cap)
        resp = s.recv(4096)
        if b"101" not in resp.splitlines()[0]:
            return time.monotonic() - t0, f"upgrade: {resp.splitlines()[0][:40]!r}"
        hs = time.monotonic() - t0

        payload = json.dumps({"id": msg_id, "method": method,
                              "params": params or {}}).encode()
        frame = bytearray([0x81])
        n = len(payload)
        if n < 126:
            frame += bytes([0x80 | n])
        elif n < 65536:
            frame += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            frame += bytes([0x80 | 127]) + struct.pack(">Q", n)
        s.sendall(bytes(frame) + payload)
        s.settimeout(cap)
        buf = s.recv(262144)
        return time.monotonic() - t0, f"replied {len(buf)}B"
    except socket.timeout:
        return time.monotonic() - t0, "TIMEOUT (never replied)"
    except Exception as e:
        return time.monotonic() - t0, f"{type(e).__name__}"
    finally:
        try:
            s.close()
        except Exception:
            pass


def main() -> int:
    print("-- HTTP endpoints (cheap, no websocket) --")
    for ep in ("/json/version", "/json/list"):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(f"{CDP}{ep}", timeout=6) as r:
                body = r.read()
            print(f"  {ep:16} {time.monotonic() - t0:6.3f}s  {r.status}  {len(body)} bytes")
        except Exception as e:
            print(f"  {ep:16} FAILED  {type(e).__name__}")
            return 1

    ver = json.loads(urllib.request.urlopen(f"{CDP}/json/version", timeout=6).read())
    bpath = "/devtools/browser/" + ver["webSocketDebuggerUrl"].rsplit("/", 1)[-1]
    targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=6).read())

    print(f"\n-- targets: {len(targets)} total --")
    pages = [t for t in targets if t.get("type") == "page"]
    for t in targets:
        print(f"  {t.get('type','?'):10} {(t.get('title') or '')[:44]}")
    if len(targets) > 25:
        print(f"  ^ {len(targets)} targets is a lot. Every crashed or timed-out")
        print("    connect_over_cdp leaves a websocket half-open on the browser.")
        print("    That is a plausible cause of a browser-level endpoint going mute.")

    print("\n-- browser-level websocket (what connect_over_cdp uses) --")
    dt, txt = ws_roundtrip(bpath, "Target.getTargets")
    print(f"  Target.getTargets   {dt if dt is None else round(dt, 3)}s   {txt}")

    print("\n-- page-level websockets (a possible fallback) --")
    alive = 0
    for i, t in enumerate(pages[:4]):
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        path = "/" + ws.split("/", 3)[3]
        dt, txt = ws_roundtrip(path, "Runtime.evaluate",
                              {"expression": "1+1", "returnByValue": True}, msg_id=i + 10)
        ok = "TIMEOUT" not in (txt or "")
        alive += 1 if ok else 0
        print(f"  {(t.get('title') or '?')[:34]:36} {dt if dt is None else round(dt,3)}s  {txt}")

    print("\n-- verdict --")
    browser_ok = txt is not None and "TIMEOUT" not in txt
    if browser_ok:
        print("  The browser endpoint answers. Nothing is wedged -- run the bot.")
        return 0
    if alive:
        print("  Browser endpoint is mute, but PAGE endpoints still answer.")
        print("  So the browser's session manager is stuck, not the renderers.")
        print("  This is recoverable without a restart in principle (attach per page),")
        print("  but Playwright's connect_over_cdp needs the browser endpoint.")
        print("  Practical fix now: restart the debug Chrome.")
        print("  Restart, then re-run this tool -- if page endpoints are healthy on a")
        print("  fresh Chrome, the accumulation theory is the likely one.")
        return 1
    print("  EVERYTHING is mute: browser endpoint and all page endpoints.")
    print("  The wedge is total. Only restarting the debug Chrome will clear it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())