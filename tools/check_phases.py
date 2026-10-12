"""Every phase in run_one_dc() must actually CALL its phase function.

A progress-marker patch once replaced the real calls
(phase_download_verify, phase_wps_copy, phase_mc_validate, phase_dump_all_write)
with the done() lines that report them. The pipeline then walked straight past
the browser work and died later on Path(None).

This walks the AST of run_one_dc and checks each phase name is both marked
started AND dispatched to the function that does the work.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


src = (ROOT / "run_dc.py").read_text(encoding="utf-8")
tree = ast.parse(src)

# The phase work no longer lives in one function.
#
# run_one_dc() used to contain every phase inline. It now hands phases 2-9 to
# _run_browser_phases() so that a plain try/finally could record timings even
# when a run dies (bug: a failed run is exactly the one whose timing is worth
# having). A guard that hardcoded "look inside run_one_dc" therefore failed on
# working code -- and worse, it had started checking a shape nobody needed.
#
# So the guard follows the call graph instead of a function name. That keeps the
# original protection (no phase may degrade into a bare progress marker) and
# makes it stronger: the whole chain is now inspected, so moving a phase into a
# helper cannot silently drop its work.
FUNCS = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def call_names_in(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.append((n.lineno, n.func.id))
    return out


def reachable(start="run_one_dc", depth=4):
    """Functions reachable from `start` through local calls, breadth first."""
    seen, order = set(), []
    frontier = [(start, 0)]
    while frontier:
        name, d = frontier.pop(0)
        if name in seen or name not in FUNCS or d > depth:
            continue
        seen.add(name)
        order.append(name)
        for _, callee in call_names_in(FUNCS[name]):
            if callee in FUNCS and callee not in seen:
                frontier.append((callee, d + 1))
    return order


CHAIN = reachable()
fn = FUNCS["run_one_dc"]
check_chain = ", ".join(CHAIN)

# Collect every call name in the whole chain, in source order.
calls = []
for fname in CHAIN:
    calls += call_names_in(FUNCS[fname])
calls.sort()
call_names = {c[1] for c in calls}

print(f"  (following the call chain: {check_chain})")

# The phase -> the function that actually does the work.
WORK = {
    "sheets_read": "build_state",
    "chrome_start": "phase_chrome_start",
    "fms_search": "submit_and_wait_results",
    "fms_export": "export_batch_results_with_retry",
    "download_verify": "phase_download_verify",
    "wps_copy": "phase_wps_copy",
    "mc_validate": "phase_mc_validate",
    "dump_all_write": "phase_dump_all_write",
}

print("\n-- every phase does its actual work --")
for phase, worker in WORK.items():
    check(f"{phase:16} calls {worker}", worker in call_names)

# A call is only real if the name is actually bound. Calling a phase function
# that was never imported fails at runtime with a NameError.
bound = set()
for n in tree.body:
    for node in ([n] if isinstance(n, (ast.Import, ast.ImportFrom))
                 else list(ast.walk(n)) if isinstance(n, ast.Try) else []):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
for n in ast.walk(fn):
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            bound.add(a.asname or a.name.split(".")[0])
for phase, worker in WORK.items():
    check(f"{phase:16} {worker} is imported", worker in bound)

print("\n-- no phase is only a progress marker --")
# done("<phase>", ...) without the worker call is the exact regression.
done_phases = set()
for fname in CHAIN:
    for n in ast.walk(FUNCS[fname]):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "done"):
            if n.args and isinstance(n.args[0], ast.Constant):
                done_phases.add(n.args[0].value)
for phase in sorted(done_phases):
    worker = WORK.get(phase)
    check(f"{phase:16} reports only after doing the work",
          worker is None or worker in call_names,
          "marker present but no real call" if worker and worker not in call_names else "")

print("\n-- the crash this caused is now guarded --")
body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
check("download_path is checked before Path()",
      "if not state.download_path" in body and "Path(state.download_path)" in body)
check("the downloaded file must exist",
      "if not xlsx.exists()" in body)
check("empty WPS text is refused",
      "if not state.wps_copied_text" in body)

print("\n-- progress reports real numbers, not placeholders --")
# The old code reported stats.get('mc_misroute') BEFORE mc_validate had run,
# so it always showed 0. Order matters: the call must precede the marker.
# Compared per-function, because ordering is only meaningful within the body
# that contains both -- a marker in run_one_dc and a worker in a helper are not
# comparable by line number, and pretending otherwise would be a false pass.
#
# Both `done` and `_done` count as markers: adding per-phase timing renamed them,
# and a check that quietly matched only the old name would have stopped testing
# anything while still printing "ALL PASS". Hence the explicit "examined N"
# assertion below -- a section that finds nothing must fail, not pass silently.
MARKERS = {"done", "_done"}
examined = []
for fname in CHAIN:
    body = FUNCS[fname]
    local_calls = call_names_in(body)
    for n in ast.walk(body):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in MARKERS and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value in ("mc_validate", "dump_all_write")):
            line = n.lineno
            worker = WORK[n.args[0].value]
            wline = min((l for l, nm in local_calls if nm == worker), default=10**9)
            examined.append((n.args[0].value, worker))
            check(f"done('{n.args[0].value}') comes after {worker}()  [{fname}]",
                  wline < line, f"worker@{wline} marker@{line}")

check("the ordering check actually examined both markers (guard is live)",
      {p for p, _ in examined} == {"mc_validate", "dump_all_write"},
      f"examined {[p for p, _ in examined]}")

print("\n-- every phase reports a marker after its work --")
# Same trap, same defence: a phase with no marker at all is invisible progress.
all_done = set()
for fname in CHAIN:
    for n in ast.walk(FUNCS[fname]):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in MARKERS and n.args
                and isinstance(n.args[0], ast.Constant)):
            all_done.add(n.args[0].value)
for phase in sorted(WORK):
    check(f"{phase:16} reports a result", phase in all_done)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
