"""Loopback launcher: a SEPARATE page that starts the WHOLE pipeline for one DC.

The page is mc/validate_ui.html. It never drives Misroute Checker directly -- it
queues a job here, and this process runs every phase for the chosen DC:

    sheets read -> FMS Batch Search -> Export -> Download -> WPS copy ->
    MC validate (that DC only) -> DUMP ALL -> packed scrape

Why a queue: Playwright's sync API is not thread-safe, so the HTTP thread may
only enqueue. The main thread drains the queue and does the real work, one DC
at a time (FMS and WPS cannot be driven concurrently).

Endpoints
    GET  /                the page
    GET  /api/state       JSON snapshot
    POST /api/run         {"dc": "DAVAO DC", "date": "2026-10-01",
                           "revalidate": false}
                           start that DC's full pipeline on that date
    POST /api/date        {"date": "2026-10-01"}   pick the day ("auto" = today).
                           Applied synchronously so the answer is in the
                           response; 400 if that date is not on Drive.
    POST /api/dates                            re-list the dated sheets on Drive
    POST /api/stop                            ask the current run to stop
    POST /api/trace       {"oid": "PH...", "days": 30}
                            sweep today -> past N days of DAC OFFLINE sheets
                            for that T.O./OID (lib/trace_oid.py). Queued for
                            the main thread -- it is all Google calls. 400
                            without an oid, 409 while a run or another trace
                            is in progress. Progress lands in state.trace.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from lib import cli

ROOT = Path(__file__).resolve().parent.parent
PANEL_HTML = ROOT / "mc" / "validate_ui.html"
DEFAULT_PORT = 8787

# The pipeline phases, in order, for display only.
PHASE_LABELS = [
    ("sheets_read", "Read pending T.O.s"),
    ("fms_search", "FMS Batch Search"),
    ("fms_export", "Export results"),
    ("download_verify", "Download + WPS"),
    ("wps_copy", "Copy export"),
    ("mc_validate", "Validate in MC"),
    ("dump_all_write", "Write DUMP ALL"),
    ("packed_scrape", "Packed scrape"),
]


class PanelState:
    """Mutable snapshot the page renders. Written by the main thread only."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sheet = ""
        # Which day's 'DAC OFFLINE …' file to work on. "" means AUTO = today,
        # exactly what the pipeline did before there was a picker.
        self.sheet_date: str = ""
        # [{date, title, modified}] newest first, filled from Drive at startup
        # and on demand by the page's REFRESH button.
        self.dates: list[dict] = []
        self.dcs: list[dict] = []          # [{name, tabs, status, phases, result}]
        self.running: str | None = None
        self.stop_requested = False
        # TRACE sweep for the page: who was asked for, over how many days,
        # where the main thread has got to, and the final result.
        # status: idle -> queued -> running -> done | failed.
        self.trace: dict = {
            "oid": "", "days": 0, "status": "idle",
            "progress": "", "result": None, "error": "",
        }
        self.message = "Pick a date and a DC to start."
        self.log: list[str] = []

    def set_trace(self, **fields) -> None:
        """Merge into the trace block under the lock.

        The HTTP thread writes 'queued' (so the page sees the request at
        once), the main thread drives 'running'/'done'/'failed' and the
        per-sheet progress line.
        """
        with self.lock:
            self.trace.update(fields)

    def say(self, msg: str) -> None:
        with self.lock:
            self.message = str(msg)
            self.log.append(str(msg))
            del self.log[:-40]

    def dc(self, name: str) -> dict | None:
        return next((d for d in self.dcs if d["name"] == name), None)

    def set_dates(self, dates: list[dict]) -> None:
        """Replace the date list, dropping a selection that no longer exists.

        A selected date can vanish between refreshes (Drive file deleted or
        renamed). Keeping it would let a run start against a date with no file,
        so it is reset to AUTO instead -- the safe default.
        """
        with self.lock:
            self.dates = list(dates)
            if self.sheet_date and self.sheet_date not in {d["date"] for d in dates}:
                self.sheet_date = ""

    def try_set_date(self, want: str) -> tuple[bool, str]:
        """Apply a date pick NOW and say whether it was accepted.

        Deliberately synchronous. The pick is an in-memory change under a lock,
        and the list of dates on Drive is already in memory too, so there is
        nothing here that needs the main thread.

        Doing it in the queue instead left a window where the page displayed one
        date and sent another: the page set its own ``PICKED`` optimistically,
        the server refused the date, and because a refusal changes no state the
        page never re-rendered -- so START would go out carrying a date the
        dropdown no longer showed. Resolving it here means the response already
        knows the answer, and the page can snap back at once.

        An empty list means Drive could not be listed; in that case the pick is
        accepted and left for the main thread to re-check, because refusing
        everything would make the picker useless during a Drive outage.
        """
        want = (want or "").strip()
        if want.lower() in ("auto", "today"):
            want = ""          # AUTO means today's sheet, not a date named "auto"
        with self.lock:
            if want:
                known = {d["date"] for d in self.dates}
                if known and want not in known:
                    have = ", ".join(sorted(known, reverse=True)[:8])
                    return False, (
                        f"{want} is not among the dated sheets on Drive. "
                        f"Available: {have}"
                    )
            self.sheet_date = want
            return True, (f"Date set to {want}." if want else "Date set to AUTO (today).")

    def date_label(self) -> str:
        with self.lock:
            if not self.sheet_date:
                return "AUTO (today)"
            return self.sheet_date

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "sheet": self.sheet,
                "sheet_date": self.sheet_date,
                "dates": [dict(d) for d in self.dates],
                "dcs": [dict(d) for d in self.dcs],
                "running": self.running,
                "message": self.message,
                "phase_order": [p for p, _ in PHASE_LABELS],
                "trace": dict(self.trace),
                "log": list(self.log[-14:]),
            }


class PanelServer:
    """Loopback HTTP + job queue, drained by the main thread."""

    def __init__(self, state: PanelState, port: int = DEFAULT_PORT):
        self.state = state
        self.jobs: list[dict] = []
        self._jlock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self.port = port
        self.url = ""

    def start(self) -> str:
        owner = self

        class Handler(_Handler):
            panel = owner

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        cli.ok(f"Validate UI: {self.url}")
        return self.url

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def submit(self, job: dict) -> None:
        with self._jlock:
            self.jobs.append(job)

    def drain(self) -> list[dict]:
        with self._jlock:
            out, self.jobs = self.jobs, []
        return out


class _Handler(BaseHTTPRequestHandler):
    panel: PanelServer

    def log_message(self, *a):  # keep the console clean
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is often opened straight off disk (file:// -> Origin: null).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode()) if n > 0 else {}
        except Exception:
            return {}

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self):  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            try:
                self._send(200, PANEL_HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"validate_ui.html missing", "text/plain")
            return
        if route == "/api/state":
            self._json(200, self.panel.state.to_dict())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path
        body = self._body()
        if route == "/api/run":
            dc = (body.get("dc") or "").strip()
            if not dc:
                self._json(400, {"error": "dc required"})
                return
            revalidate = bool(body.get("revalidate"))
            # "" / null / "auto" all mean AUTO = today, so the page can send an
            # empty string without the server having to special-case it.
            sheet_date = (body.get("date") or "").strip()
            if sheet_date.lower() in ("auto", "today"):
                sheet_date = ""
            self.panel.submit({"kind": "run", "dc": dc, "revalidate": revalidate,
                               "date": sheet_date})
            self._json(202, {"queued": dc, "revalidate": revalidate,
                             "date": sheet_date or "auto"})
            return
        if route == "/api/date":
            # try_set_date() normalises "auto"/"today" and validates.
            ok, msg = self.panel.state.try_set_date(body.get("date") or "")
            if ok:
                self.panel.state.say(msg)
                applied = self.panel.state.sheet_date
                # Only the title lookup needs Google, so only that is queued.
                self.panel.submit({"kind": "sheet", "date": applied or None})
                self._json(200, {"ok": True, "date": applied,
                                 "label": self.panel.state.date_label(),
                                 "message": msg})
            else:
                self.panel.state.say(msg)
                self._json(400, {"ok": False, "error": msg,
                                 "date": self.panel.state.sheet_date,
                                 "label": self.panel.state.date_label()})
            return
        if route == "/api/dates":
            # Queued, not done here: this hits Google's Drive API, and every
            # Google call belongs on the main thread (see the queue rationale
            # at the top of this file).
            self.panel.submit({"kind": "dates"})
            self._json(202, {"queued": "dates"})
            return
        if route == "/api/trace":
            # Queued, not done here: every Google call belongs on the main
            # thread (the queue rationale at the top of this file).
            oid = (body.get("oid") or "").strip()
            if not oid:
                self._json(400, {"error": "oid required"})
                return
            if len(oid) > 64:
                self._json(400, {"error": "that is not a T.O. / OID"})
                return
            try:
                days = int(body.get("days") or 30)
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(days, 365))
            if self.panel.state.running:
                msg = "A validation run is in progress — trace after it finishes."
                self._json(409, {"error": msg})
                return
            if self.panel.state.trace.get("status") in ("queued", "running"):
                msg = "A trace is already running — wait for it to finish."
                self._json(409, {"error": msg})
                return
            self.panel.state.set_trace(
                oid=oid, days=days, status="queued",
                progress="", result=None, error="",
            )
            self.panel.submit({"kind": "trace", "oid": oid, "days": days})
            self._json(202, {"queued": oid, "days": days})
            return
        if route == "/api/stop":
            self.panel.submit({"kind": "stop"})
            self._json(202, {"queued": "stop"})
            return
        self._json(404, {"error": "not found"})
