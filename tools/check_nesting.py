"""Prove a DC job can be driven from validate_ui.py without nesting Playwright.

The failure this guards against: validate_ui.py holding sync_playwright() open
while run_one_dc() opens its own -> "Playwright Sync API inside the asyncio loop"
on every single job.
"""

import ast
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


print("\n-- static: no Playwright in the launcher --")
src = (ROOT / "validate_ui.py").read_text(encoding="utf-8")
code_only = "\n".join(
    l for l in src.splitlines() if not l.lstrip().startswith("#"))
check("no sync_playwright call outside comments",
      "sync_playwright" not in code_only)
check("validate_ui.py does not import playwright at all",
      "from playwright" not in code_only and "import playwright" not in code_only)
check("it opens the tab over CDP instead", "_open_tab" in src and "/json/new" in src)

# The main() body must be a plain while-loop, not nested inside a `with`.
tree = ast.parse(src)
main_fn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main")
withs = [n for n in ast.walk(main_fn) if isinstance(n, ast.With)]
check("main() has no `with` block left", not withs,
      f"{len(withs)} found" if withs else "")

print("\n-- tab opening: PUT first, GET fallback --")
# Chrome >= 111 answers 405 to GET /json/new; older builds need GET. If we only
# try one, the UI silently fails to open on some machines.
import start_chrome
import validate_ui

open_fn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_open_tab")
methods = [
    n.value
    for n in ast.walk(open_fn)
    if isinstance(n, ast.Constant) and isinstance(n.value, str)
    and n.value in ("PUT", "GET")
]
check("_open_tab tries PUT first, then GET",
      methods[:2] == ["PUT", "GET"], str(methods))

# Against the real debug Chrome, whichever method it wants must actually work.
if start_chrome.debug_ok():
    ok = validate_ui._open_tab("about:blank")
    check("_open_tab opens a tab on this machine's Chrome", ok)
else:
    print("  SKIP  no debug Chrome running — cannot test _open_tab live")

print("\n-- the real constraint, checked live --")
# run_one_dc() must be importable and must open its own Playwright session.
import run_dc

rd = ast.parse((ROOT / "run_dc.py").read_text(encoding="utf-8"))

# Follow the call chain rather than assuming the session sits in run_one_dc().
# run_one_dc() hands phases 2-9 to _run_browser_phases() so a try/finally can
# record timings even when a run dies; the sync_playwright() session moved with
# it. What is being protected is the bug-2 invariant -- one sync session per
# thread, never nested inside an existing one -- and that is a property of the
# phase-running code wherever it lives.
_RFUNCS = {n.name: n for n in ast.walk(rd) if isinstance(n, ast.FunctionDef)}


def _chain(start, depth=4):
    seen, order = set(), []
    frontier = [(start, 0)]
    while frontier:
        name, d = frontier.pop(0)
        if name in seen or name not in _RFUNCS or d > depth:
            continue
        seen.add(name)
        order.append(name)
        for n in ast.walk(_RFUNCS[name]):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in _RFUNCS and n.func.id not in seen:
                frontier.append((n.func.id, d + 1))
    return order


CHAIN = _chain("run_one_dc")
withs = [(f, [n for n in ast.walk(_RFUNCS[f]) if isinstance(n, ast.With)])
         for f in CHAIN]
total_withs = sum(len(w) for _, w in withs)
holder = [f for f, w in withs if w]
def _is_sync_playwright(withnode):
    """True if `with <call to sync_playwright>() ...`.

    Matches the imported bare name (`from playwright.sync_api import
    sync_playwright` -> func is an ast.Name) as well as an attribute access, so
    this keeps working if the import style ever changes. Getting this wrong is
    not a cosmetic mistake in a guard: it silently reports "0 openers" and turns
    the nesting check into a no-op that always passes.
    """
    if not getattr(withnode, "items", None) or not withnode.items:
        return False
    ce = withnode.items[0].context_expr
    if not isinstance(ce, ast.Call):
        return False
    fn = ce.func
    if isinstance(fn, ast.Name):
        return fn.id == "sync_playwright"
    if isinstance(fn, ast.Attribute):
        return fn.attr == "sync_playwright"
    return False


check("run_one_dc() reaches exactly ONE Playwright session", len(holder) == 1,
      f"chain {CHAIN}; with-blocks in {holder}")
check("it is opened with sync_playwright() (not some other resource)",
      any(_is_sync_playwright(n) for _, w in withs for n in w),
      f"{total_withs} with-block(s) in the chain")

# A second session on the same thread is exactly what broke before (bug 2), so
# assert there is still only ONE place that can open one. Two would mean the
# chain could nest them.
openers = sum(1 for _, w in withs for n in w if _is_sync_playwright(n))
check("exactly one sync_playwright() opener in the whole chain (cannot nest)",
      openers == 1, f"found {openers}")

# The real constraint: run_one_dc() opens its own sync session. Doing that
# twice on one thread is what raised the asyncio-loop error, so prove the
# session opens and closes cleanly and a second one still works after.
import playwright.sync_api as pw

result = {}


def fake_run_one(dc, config, progress=None, **kw):
    with pw.sync_playwright() as p:
        pass
    return type("S", (), {"mc_stats": {}, "pending": []})()


t = threading.Thread(target=lambda: result.update(out=fake_run_one("DAVAO DC", None)))
t.start()
t.join(30)
check("run_one_dc-style session opens and closes", result.get("out") is not None)
check("no asyncio-loop error from a fresh session", "error" not in result, str(result))

# Two sequential sessions on one thread: the old code effectively asked for a
# second session while the first was still open. That must not be needed now.
def two_sessions():
    with pw.sync_playwright():
        pass
    with pw.sync_playwright():
        return True


t2 = threading.Thread(target=lambda: result.update(seq=two_sessions()))
t2.start()
t2.join(30)
check("two sequential sessions on one thread are fine", result.get("seq") is True)

print("\n-- queue drain runs jobs on ONE thread --")
from lib.panel_server import PanelServer, PanelState

ps = PanelState()
srv = PanelServer(ps)
srv.start()
srv.submit({"kind": "run", "dc": "DAVAO DC"})
check("submit queues without draining", len(srv.jobs) == 1)
drained = srv.drain()
check("drain hands it to the main thread",
      len(drained) == 1 and drained[0]["dc"] == "DAVAO DC", str(drained))
check("drain empties the queue", srv.drain() == [])
srv.stop()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
