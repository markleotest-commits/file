"""Does the bot fail FAST and HONESTLY when Chrome is unhealthy?

    py tools\\check_cdp_attach.py

Bug 31. `connect_over_cdp` inherited Playwright's 180-second default in five
pipeline entry points. Measured: a run finished its sheets read in ~10s and then
sat silently for 180s and died with "Timeout 180000ms exceeded", which names no
cause and suggests no action. That is the single worst thing about this pipeline
for unattended use -- a three-minute freeze at 95% memory looks identical to a
hang.

lib/cdp.py replaces the bare call. This guard checks the three properties that
make it trustworthy:

  1. DETECTION IS FAST. A wedge must be identified in seconds, not minutes.
     Asserted against a fake CDP port that accepts the socket and never replies,
     which reproduces the real wedge exactly without needing a broken Chrome.
  2. THE MESSAGE IS ACTIONABLE. It must name the cause and the next command.
     A guard that only counts milliseconds would happily pass on a message that
     says "timeout".
  3. NO CALL SITE WAS MISSED. A bare `connect_over_cdp(` in a pipeline script
     reintroduces the 180s freeze the moment that file runs. This asserts the
     AST of every entry point, so a future edit that adds one back fails here.

Also asserts the timeout is bounded and sane against what a healthy attach
actually costs (0.6-1.4s measured), so the guard cannot be defeated by setting
ATTACH_TIMEOUT_S back to 180000.

Touches a socket on an unused port and reads source files. No browser, no FMS,
no sheet, no writes.
"""

import ast
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import cdp  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


# ─────────────────────────────────────────────────────────────────────────────
# A fake CDP port that behaves exactly like a wedged Chrome: it completes the
# websocket upgrade, then never answers a command. That is the real failure mode
# (measured against the live browser in tools/probe_cdp_stall.py), and it is the
# only one the old 180s timeout ever saw.
# ─────────────────────────────────────────────────────────────────────────────
class WedgedChrome:
    """A CDP endpoint that is healthy right up until it has to answer.

    Exactly the shape of the real failure, measured live: `/json/version`
    responds promptly, the websocket upgrade returns a proper 101, and then the
    browser never replies to a single CDP command. One accept loop only -- an
    earlier version of this guard ran two and they raced for connections, so the
    probe got "no port" and the test passed for the wrong reason.

    Set ``mute_after`` to None to make it a perfectly healthy CDP endpoint that
    answers Target.getTargets, which is how the "don't break the good path"
    assertion below is able to run against a known-good server.
    """

    def __init__(self, mute_after: bool = True):
        self.mute_after = mute_after
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.upgrades = 0
        self.commands_answered = 0
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _read_headers(conn):
        data = b""
        conn.settimeout(5)
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        return data

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            req = self._read_headers(conn)
            if req is None:
                return
            line0 = req.splitlines()[0].decode("latin-1")

            if line0.startswith("GET /json/version"):
                import json as _j
                body = _j.dumps({
                    "Browser": "FakeChrome/1.0",
                    "webSocketDebuggerUrl":
                        f"ws://127.0.0.1:{self.port}/devtools/browser/FAKE",
                }).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(body)).encode() +
                             b"\r\n\r\n" + body)
                return

            # A websocket upgrade: answer it properly, then go mute (or not).
            import base64
            import hashlib
            key = ""
            for h in req.decode("latin-1").splitlines():
                if h.lower().startswith("sec-websocket-key:"):
                    key = h.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            self.upgrades += 1

            if self.mute_after:
                # The whole point: connected, upgraded, and permanently silent.
                while not self._stop:
                    if not conn.recv(4096):
                        return
                return

            # Healthy path: answer one CDP command.
            conn.settimeout(10)
            frame = conn.recv(65536)
            if not frame:
                return
            import json as _j
            import struct
            ln = frame[1] & 0x7F
            off = 2
            if ln == 126:
                ln = struct.unpack(">H", frame[2:4])[0]
                off = 4
            req_id = _j.loads(frame[off:off + ln].decode()).get("id", 1)
            reply = _j.dumps({"id": req_id, "result": {"targetInfos": []}}).encode()
            hdr = bytearray([0x81])
            n = len(reply)
            hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
            conn.sendall(bytes(hdr) + reply)
            self.commands_answered += 1
        except Exception:
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


def _prune_case():
    """Drive prune_extra_tabs against a fake /json endpoint; return closed ids."""
    import http.server
    import json as _j
    import threading

    hit = {"closed": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/json/list":
                self._send(200, _j.dumps([
                    {"id": "1", "type": "page",
                     "url": "https://spx.shopee.ph/#/orderTracking"},
                    {"id": "2", "type": "page",
                     "url": "file:///C:/x/mc/MC_V2.html"},
                    {"id": "3", "type": "page",
                     "url": "http://127.0.0.1:8787/"},
                    {"id": "4", "type": "page",
                     "url": "https://discord.com/channels/1/2"},
                    {"id": "5", "type": "page",
                     "url": "https://asurascans.com/comics/x"},
                    {"id": "6", "type": "worker", "url": "https://x/worker.js"},
                    {"id": "7", "type": "page", "url": "about:blank"},
                ]).encode())
                return
            if self.path.startswith("/json/close/"):
                hit["closed"].append(self.path.rsplit("/", 1)[-1])
                self._send(200, b"ok")
                return
            self._send(404)

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        closed = cdp.prune_extra_tabs(f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()
    return hit["closed"], closed


def main() -> int:
    print("-- 1. the timeout must be bounded and sane --")
    check("ATTACH_TIMEOUT_S is not Playwright's 180s default",
          cdp.ATTACH_TIMEOUT_S <= 40, f"= {cdp.ATTACH_TIMEOUT_S}s")
    check("ATTACH_TIMEOUT_S still allows a healthy attach (measured 0.6-1.4s)",
          cdp.ATTACH_TIMEOUT_S >= 5, f"= {cdp.ATTACH_TIMEOUT_S}s")
    check("the pre-flight probe is much cheaper than the attach",
          cdp.PREFLIGHT_CAP_S < cdp.ATTACH_TIMEOUT_S,
          f"{cdp.PREFLIGHT_CAP_S}s vs {cdp.ATTACH_TIMEOUT_S}s")

    print("\n-- 2. a wedge must be DETECTED, in seconds --")
    real_cdp, real_port = cdp.CDP, cdp.CDP_PORT
    fake = WedgedChrome(mute_after=True)
    try:
        cdp.CDP = f"http://127.0.0.1:{fake.port}"
        cdp.CDP_PORT = fake.port
        time.sleep(0.2)          # let the accept loop come up

        t0 = time.monotonic()
        r = cdp.probe(cap=3.0)
        dt = time.monotonic() - t0
        check("probe() returns without hanging", dt < 6.0, f"{dt:.2f}s")
        check("a connected-but-mute Chrome is called 'wedged'",
              r.get("why") == "wedged", f"why={r.get('why')}")
        check("the verdict arrives fast, not after minutes",
              dt < 6.0, f"{dt:.2f}s")
        check("it records that the handshake SUCCEEDED (the real distinguishing fact)",
              r.get("handshake_s") is not None, f"handshake={r.get('handshake_s')}s")
        check("the fake really was upgraded then ignored (test is not rigged)",
              fake.upgrades >= 1 and fake.commands_answered == 0,
              f"upgrades={fake.upgrades} answered={fake.commands_answered}")

        print("\n-- 3. the message must be actionable --")
        msg = cdp.describe(r)
        low = msg.lower()
        check("names the cause ('never answers' / 'wedged')",
              "never answers" in low or "wedge" in low)
        check("says waiting longer will not help",
              "waiting longer" in low or "does not help" in low)
        check("tells the user what to do (start_chrome / restart / close)",
              "start_chrome" in low or "restart" in low or "close" in low)
        check("points at a diagnostic command",
              "probe_cdp_stall" in low)
        check("does NOT merely restate the timeout",
              "180000" not in msg and "timeout 180" not in low)
        print("\n        message a wedged Chrome now produces:")
        for line in msg.splitlines():
            print("        " + line)

        print("\n-- 4. the healthy path must NOT be broken by any of this --")
        # A guard that only ever exercises the failure branch proves nothing
        # about the case that matters in production, so drive a known-good CDP
        # endpoint through the same probe and demand a clean "healthy".
        good = WedgedChrome(mute_after=False)
        try:
            cdp.CDP = f"http://127.0.0.1:{good.port}"
            cdp.CDP_PORT = good.port
            t0 = time.monotonic()
            rg = cdp.probe(cap=5.0)
            dtg = time.monotonic() - t0
            check("a responsive Chrome is reported 'healthy'",
                  rg.get("ok") is True and rg.get("why") == "healthy",
                  f"ok={rg.get('ok')} why={rg.get('why')}")
            check("and it is judged fast",
                  dtg < 5.0, f"{dtg:.2f}s")
            check("the healthy message does not send the user chasing a ghost",
                  "not running" not in cdp.describe(rg).lower())
        finally:
            good.close()
            cdp.CDP, cdp.CDP_PORT = real_cdp, real_port

        print("\n-- 5. no entry point may go back to a bare connect_over_cdp --")
        entry_points = ["run_dc.py", "run_fms.py", "run_mc.py",
                        "fms_batch_export.py", "lib/packed_scrape.py"]
        for rel in entry_points:
            f = ROOT / rel
            if not f.is_file():
                check(f"{rel} exists", False)
                continue
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            bare = []
            for node in ast.walk(tree):
                # AST, not text matching, so a comment explaining WHY we no
                # longer call it bare cannot trip this.
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "connect_over_cdp"):
                    bare.append(node.lineno)
            check(f"{rel} has no bare connect_over_cdp", not bare,
                  f"line(s) {bare}" if bare else "")

        print("\n-- 6. the entry points actually use the checked attach --")
        for rel in entry_points:
            src = (ROOT / rel).read_text(encoding="utf-8")
            check(f"{rel} goes through lib.cdp",
                  ("from lib.cdp import" in src) or ("lib.cdp" in src))

    finally:
        try:
            fake.close()
        except Exception:
            pass
        cdp.CDP, cdp.CDP_PORT = real_cdp, real_port

    print("\n-- 7. a wedge frees RAM, and waits ONLY while waiting can help --")
    # Asserted on the source because this is a decision, not an observable: it
    # costs a real Playwright attach to observe. Bug 51: the "6 runs in 10"
    # attach failures were RAM pressure, with the debug Chrome carrying the
    # operator's personal tabs. The fix closes those tabs and re-probes for a
    # bounded time -- but a follow-up showed the bounded wait made RAM-starved
    # failures feel slower for nothing, so below WEDGE_MIN_FREE_GB it fails
    # fast instead. Never Playwright's 180 s freeze, never an unbounded loop.
    src = (ROOT / "lib" / "cdp.py").read_text(encoding="utf-8")
    body = src.split("def attach(", 1)[1].split("\ndef ", 1)[0]
    check("attach() closes the bot Chrome's non-bot tabs to free RAM",
          "prune_extra_tabs(" in body)
    check("a wedge gets a recovery step, not just a bare report",
          "_try_recover_wedge(" in body)
    check("the recovery is bounded and sane",
          0 < cdp.WEDGE_RECOVER_S <= 40,
          f"WEDGE_RECOVER_S={cdp.WEDGE_RECOVER_S}s")
    check("a RAM-exhausted wedge FAILS FAST instead of waiting",
          "WEDGE_MIN_FREE_GB" in src and "_ram_exhausted(" in body,
          f"min_free={cdp.WEDGE_MIN_FREE_GB} GB")
    check("worst case (recover + one attach) stays far below 180 s",
          cdp.WEDGE_RECOVER_S + cdp.ATTACH_TIMEOUT_S < 180,
          f"{cdp.WEDGE_RECOVER_S + cdp.ATTACH_TIMEOUT_S}s")
    check("a recovery probe is cheaper than the full pre-flight",
          cdp.WEDGE_PROBE_CAP_S <= cdp.PREFLIGHT_CAP_S,
          f"{cdp.WEDGE_PROBE_CAP_S}s vs {cdp.PREFLIGHT_CAP_S}s")
    check("a RAM-starved run probes SHORT so it fails fast (bug 53)",
          cdp.LOW_RAM_PROBE_CAP_S < cdp.PREFLIGHT_CAP_S
          and "LOW_RAM_PROBE_CAP_S" in body,
          f"{cdp.LOW_RAM_PROBE_CAP_S}s vs {cdp.PREFLIGHT_CAP_S}s")
    check("the prune closes non-bot pages over Chrome's HTTP endpoint",
          "def prune_extra_tabs(" in src and "/json/close/" in src)
    wedge_block = body.split('if not pre["ok"]:', 1)[-1][:220]
    check("attach() still breaks (no unbounded loop) when it stays unhealthy",
          "break" in wedge_block)
    check("a healthy-but-failed attach is still retried once",
          "may be a race" in body)

    print("\n-- 8. the prune closes the operator's tabs and keeps the bot's --")
    ids, urls = _prune_case()
    check("closes the non-bot pages (Discord, manga, blank tab)",
          ids == ["4", "5", "7"], f"closed ids={ids}")
    check("never closes FMS, the Misroute Checker or the panel",
          not ({"1", "2", "3"} & set(ids)), f"closed ids={ids}")
    check("never closes a non-page target (worker/extension/browser UI)",
          "6" not in ids, f"closed ids={ids}")
    check("reports the URLs it closed",
          len(urls) == 3 and any("discord" in u for u in urls), f"{urls}")

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED:"))
    for f in fails:
        print(f"  - {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())