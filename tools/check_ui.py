"""Offline check of the Validate UI: server routes + page wiring. No FMS, no writes."""

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from lib.dc_run import all_dcs, tabs_for_dc
from lib.panel_server import PHASE_LABELS, PanelServer, PanelState

pstate = PanelState()
pstate.sheet = "DAC OFFLINE OCTOBER 1 2026"
for dc in all_dcs(config):
    pstate.dcs.append({
        "name": dc,
        "tabs": tabs_for_dc(config, dc),
        "status": "idle",
        "phases": {p: "idle" for p, _ in PHASE_LABELS},
        "result": None,
    })

# Bind an OS-assigned port, NEVER the default 8787.
#
# BUGS.md #10 said "use a free port". That is not enough on Windows: the socket
# layer sets SO_REUSEADDR, which lets a second process bind a port that is
# already LISTENing. So constructing PanelServer(pstate) while the user's
# validate_ui.py was running would *succeed* on 8787, and requests were served
# by whichever process won -- half the checks silently ran against the stale
# live server (old /api/state shape, old /api/run payload) and reported nonsense.
# port=0 is the only reliable way to get a port nobody else can be on.
server = PanelServer(pstate, port=0)
url = server.start()
print(f"check server on {server.url}  (port {server.port})")
if server.port == 8787:
    print("  FAIL  landed on the default port - another server may answer instead")
    sys.exit(1)


def get(path):
    with urllib.request.urlopen(server.url.rstrip("/") + path) as r:
        return r.status, r.read(), dict(r.headers)


def post(path, payload):
    req = urllib.request.Request(
        server.url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read() or b"{}")


def post_err(path, payload):
    """Same, but 4xx is the expected result. Returns (code, body)."""
    try:
        return post(path, payload)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def get_err(path):
    try:
        return get(path)
    except urllib.error.HTTPError as e:
        return e.code, b"", {}


fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


print("\n-- page --")
code, body, hdrs = get("/")
check("GET / returns 200", code == 200)
html = body.decode("utf-8")
check("serves the real file", len(html) > 2000, f"{len(html)} bytes")
check("has START button text", "START VALIDATION FOR" in html)
check("CORS header for file://", hdrs.get("Access-Control-Allow-Origin") == "*")
check("references every phase", all(p in html for p, _ in PHASE_LABELS),
      ", ".join(p for p, _ in PHASE_LABELS if p not in html) or "all present")

print("\n-- state --")
code, body, _ = get("/api/state")
st = json.loads(body)
check("GET /api/state 200", code == 200)
check("lists both DCs", [d["name"] for d in st["dcs"]] == all_dcs(config),
      str([d["name"] for d in st["dcs"]]))
check("each DC has its dock tabs", all(d["tabs"] for d in st["dcs"]))
check("phase order exposed", st["phase_order"] == [p for p, _ in PHASE_LABELS])

print("\n-- queue --")
code, resp = post("/api/run", {"dc": "DAVAO DC"})
check("POST /api/run -> 202", code == 202, str(resp))
jobs = server.drain()
check("job queued for the main thread", len(jobs) == 1 and jobs[0]["dc"] == "DAVAO DC", str(jobs))
check("a plain run is not a re-validate", jobs[0].get("revalidate") is False)
check("drain is empty after", server.drain() == [])

print("\n-- re-validate --")
code, resp = post("/api/run", {"dc": "DAVAO DC", "revalidate": True})
check("POST /api/run revalidate -> 202", code == 202, str(resp))
jobs = server.drain()
check("the flag reaches the job",
      len(jobs) == 1 and jobs[0].get("revalidate") is True, str(jobs))

# run_one_dc must accept and forward the flag, or the page's button is a no-op.
rdsrc = Path(__file__).resolve().parent.parent.joinpath("run_dc.py").read_text(encoding="utf-8")
check("run_one_dc takes a revalidate argument", "revalidate: bool = False" in rdsrc)
check("run_one_dc forwards it to build_state", "revalidate=revalidate" in rdsrc)
check("build_state skips the DUMP ALL filter when set",
      "if revalidate:" in Path(__file__).resolve().parent.parent
      .joinpath("lib", "dc_run.py").read_text(encoding="utf-8"))
check("the page has a RE-VALIDATE button", "RE-VALIDATE" in html)
check("the page sends the flag", "revalidate: true" in html)
check("the CLI has --revalidate", '"--revalidate"' in rdsrc)

code, resp = post("/api/stop", {})
check("POST /api/stop -> 202", code == 202)
server.drain()

code, resp = post_err("/api/run", {})
check("POST /api/run without dc -> 400", code == 400, str(resp))

print("\n-- 404s --")
code = get_err("/api/nope")[0]
check("unknown route 404s", code == 404, f"got {code}")

print("\n-- the date picker --")
code, body, _ = get("/api/state")
st = json.loads(body)
check("state carries a dates list", isinstance(st.get("dates"), list))
check("state carries the current sheet_date", st.get("sheet_date") == "",
      f"{st.get('sheet_date')!r} (AUTO)")

pstate.set_dates([
    {"date": "2026-10-02", "title": "DAC OFFLINE OCTOBER 2 2026",
     "modified": "2026-10-02 09:00"},
    {"date": "2026-10-01", "title": "DAC OFFLINE OCTOBER 1 2026",
     "modified": "2026-10-01 09:00"},
])
_, body, _ = get("/api/state")
st = json.loads(body)
check("the page is handed the dates newest first",
      [d["date"] for d in st["dates"]] == ["2026-10-02", "2026-10-01"],
      str([d["date"] for d in st["dates"]]))
check("each date carries its title",
      all(d["title"].startswith("DAC OFFLINE") for d in st["dates"]))

code, resp = post("/api/date", {"date": "2026-10-01"})
check("POST /api/date -> 200", code == 200, str(resp))
check("  it is applied in the RESPONSE, not queued",
      resp.get("ok") is True and resp.get("date") == "2026-10-01", str(resp))
check("  the state changed before the response was sent",
      pstate.to_dict()["sheet_date"] == "2026-10-01",
      pstate.to_dict()["sheet_date"])
jobs = server.drain()
check("only the Google title lookup is queued",
      len(jobs) == 1 and jobs[0]["kind"] == "sheet", str(jobs))

code, resp = post_err("/api/date", {"date": "1999-01-01"})
check("a date not on Drive -> 400", code == 400, str(resp))
check("  and the selection is left alone",
      pstate.to_dict()["sheet_date"] == "2026-10-01", pstate.to_dict()["sheet_date"])
check("  and nothing was queued", server.drain() == [])
check("  and the reason names what IS available",
      "Available:" in (resp.get("error") or ""), str(resp.get("error"))[:70])

code, resp = post("/api/date", {"date": "auto"})
check("'auto' is accepted", code == 200 and resp.get("date") == "", str(resp))
check("  and means AUTO", pstate.to_dict()["sheet_date"] == "")
server.drain()
pstate.sheet_date = "2026-10-01"
code, resp = post("/api/run", {"dc": "DAVAO DC", "date": "2026-10-01"})
check("POST /api/run carries the date -> 202", code == 202, str(resp))
check("  and the response echoes it", resp.get("date") == "2026-10-01", str(resp))
jobs = server.drain()
check("  and the job has it", len(jobs) == 1
      and jobs[0]["date"] == "2026-10-01", str(jobs))

code, resp = post("/api/run", {"dc": "DAVAO DC", "date": "auto"})
jobs = server.drain()
check("'auto' on a run means today's sheet", code == 202
      and jobs[0]["date"] == "", str(jobs))

code, resp = post("/api/run", {"dc": "DAVAO DC"})
jobs = server.drain()
check("a run with no date at all is AUTO, not undefined",
      code == 202 and jobs[0]["date"] == "", str(jobs))

code, resp = post("/api/dates", {})
check("POST /api/dates -> 202", code == 202, str(resp))
jobs = server.drain()
check("  it queues a refresh for the main thread rather than calling Drive "
      "in the HTTP thread",
      len(jobs) == 1 and jobs[0]["kind"] == "dates", str(jobs))

html = Path(__file__).resolve().parent.parent.joinpath(
    "mc", "validate_ui.html").read_text(encoding="utf-8")
check("the page has a date dropdown", 'id="dates"' in html)
check("both run buttons send it",
      "revalidate: false, date: chosenDate()" in html
      and "revalidate: true, date: chosenDate()" in html)

print("\n-- the OID trace --")
code, resp = post_err("/api/trace", {})
check("POST /api/trace without oid -> 400", code == 400, str(resp))
check("  and nothing was queued", server.drain() == [])

code, resp = post("/api/trace", {"oid": "PH261001234567"})
check("POST /api/trace -> 202", code == 202, str(resp))
_, body, _ = get("/api/state")
st = json.loads(body)
check("  the page sees it queued at once",
      st["trace"]["status"] == "queued" and st["trace"]["oid"] == "PH261001234567",
      str(st.get("trace"))[:90])
jobs = server.drain()
check("  a trace job is queued for the main thread",
      len(jobs) == 1 and jobs[0]["kind"] == "trace"
      and jobs[0]["oid"] == "PH261001234567" and jobs[0]["days"] == 30,
      str(jobs))
# The main thread would clear 'queued' when it picks the job up.
pstate.set_trace(status="idle", oid="", days=0)

code, resp = post("/api/trace", {"oid": "PH1", "days": 9999})
jobs = server.drain()
check("days is clamped, not trusted",
      code == 202 and jobs[0]["days"] == 365, str(jobs))
server.drain()

# 409s: a second sweep must never overwrite the first one's progress.
pstate.set_trace(status="running", oid="PH1", days=30)
code, resp = post_err("/api/trace", {"oid": "PH2"})
check("a second trace while one runs -> 409", code == 409, str(resp))
check("  and it was not queued", server.drain() == [])
pstate.set_trace(status="idle", oid="", days=0)

pstate.running = "DAVAO DC"
code, resp = post_err("/api/trace", {"oid": "PH2"})
check("a trace during a DC run -> 409", code == 409, str(resp))
check("  and it was not queued", server.drain() == [])
pstate.running = None

pstate.set_trace(status="done", progress="", result={"checked": 30,
                                                     "dates_hit": 1})
_, body, _ = get("/api/state")
st = json.loads(body)
check("the sweep result reaches the page", st["trace"]["result"]["dates_hit"] == 1)
pstate.set_trace(status="idle", oid="", days=0, result=None, error="")

check("the page has the trace box", 'id="traceOid"' in html)
check("the page sends the sweep", "/api/trace" in html and "startTrace()" in html)
check("the page renders the result block", "renderTrace" in html)
check("trace state changes re-render the page", "s.trace" in html)

print("\n-- progress shapes --")
d = pstate.dc("GENSAN DC")
d["phases"]["sheets_read"] = "done"
d["status"] = "running"
pstate.running = "GENSAN DC"
pstate.say("GENSAN DC - sheets_read - 803 pending")
_, body, _ = get("/api/state")
st = json.loads(body)
check("phase status visible to the page",
      st["dcs"][1]["phases"]["sheets_read"] == "done")
check("running DC visible", st["running"] == "GENSAN DC")
check("log line visible", any("803 pending" in l for l in st["log"]))

d["result"] = {"pending": 803, "misroutes": 63, "soc_lh": 305}
d["status"] = "done"
pstate.running = None
_, body, _ = get("/api/state")
st = json.loads(body)
check("result block survives to the page", st["dcs"][1]["result"]["misroutes"] == 63)

server.stop()
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
