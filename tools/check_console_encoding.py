"""Can the bot print a character the Windows console cannot encode?

    py tools\\check_console_encoding.py

Bug 30. This machine's console is cp1252. `lib/to_xlsx.py` printed
`Wrote 999 rows x 57 cols -> <path>` with a real U+2192 RIGHTWARDS ARROW right
after a successful FMS download, so every run died there: ~45s of correct work
thrown away at the final step, and a codec complaint instead of the result.

The guard deliberately asserts that the OLD behaviour was wrong — that a plain
`print()` of `→` really does raise on this console. Without that assertion the
test would still pass if someone fixed it by making the arrow ASCII everywhere,
which is not the fix: there are ~50 call sites, and the next one written would
reintroduce the crash. What is being protected is the funnel, not the strings.

Also covers the worst version of the bug: `_print_crash` reports a failure by
printing `str(exc)`, so before the fix an exception mentioning an unencodable
character destroyed its own error report.

Runs in a subprocess with stdout forced to cp1252, so it tests the real
condition on any machine rather than whatever the caller's terminal happens
to be. Touches nothing: no browser, no network, no files but this script.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Characters that appear in real messages in this repo and that cp1252 cannot
# encode. Every one of these has crashed a run or nearly has.
HOSTILE = [
    "\u2192",   # → rightwards arrow   (to_xlsx.py "Wrote N rows ... → path")
    "\u2713",   # ✓ check mark
    "\u26a0",   # ⚠ warning sign       (ldcmd.py)
    "\u2500",   # ─ box drawing
    "\u2551",   # ║ double vertical
    "\u2022",   # • bullet (cp1252 has this, kept as a control)
    "\u2014",   # — em dash           (cp1252 has this, kept as a control)
    "\ufffd",   # replacement char
    "\U0001F504",  # 🔄 arrows emoji
    "\u4e2d",   # 中 CJK
]


def child() -> int:
    """Runs with stdout forced to cp1252. Returns 0 if every path survives."""
    import sys as _s

    _REAL = _s.stdout
    _REAL_WRITE = _REAL.write

    class Cp1252Stdout:
        """A stdout that hard-fails on unencodable text, like the real console.

        Hand-rolled rather than a TextIOWrapper over sys.stdout.buffer: two
        wrappers over one buffer lose each other's output at interpreter
        shutdown, which is a confusing way to discover your guard is not
        guarding anything.
        """

        encoding = "cp1252"

        def write(self, s):
            s.encode("cp1252")          # raises UnicodeEncodeError, as Windows does
            _REAL_WRITE(s.encode("cp1252", "replace").decode("cp1252"))

        def flush(self):
            try:
                _REAL.flush()
            except Exception:
                pass

        def isatty(self):
            return False

        def __getattr__(self, k):
            return getattr(_REAL, k)

    _s.stdout = Cp1252Stdout()

    fails = []

    def check(label, fn):
        try:
            fn()
            print(f"  PASS  {label}")
        except Exception as e:
            print(f"  FAIL  {label}\n          {type(e).__name__}: {str(e)[:90]}")
            fails.append(label)

    # ── 1. the old behaviour really was broken ────────────────────────────
    # NOTE: this guard runs against a stdout that HARD-FAILS on unencodable
    # text, so the guard's own output must be pure ASCII. Printing a literal
    # arrow here kills the test that exists to prove arrows are dangerous.
    print("-- 1. prove the bug was real, not imagined --")
    for ch in HOSTILE:
        try:
            ch.encode("cp1252")
            encodable = True
        except UnicodeEncodeError:
            encodable = False
        label = f"U+{ord(ch):04X}"
        if encodable:
            print(f"  {label}  cp1252 can encode it      (control, kept in the set)")
            continue
        # This MUST raise. If it does not, the harness is not actually
        # cp1252-restricted and every other assertion here is meaningless.
        try:
            print(ch, end="")
            raised = False
        except UnicodeEncodeError:
            raised = True
        finally:
            try:
                print("")
            except Exception:
                pass
        if raised:
            print(f"  {label}  bare print() RAISES       "
                  f"<- this is what killed three runs in a row")
        else:
            print(f"  {label}  bare print() did not raise  "
                  f"<- GUARD BROKEN: stdout is not cp1252-restricted")
            fails.append(f"stdout not cp1252-restricted ({label})")

    # ── 2. every cli output path survives hostile text ────────────────────
    print("\n-- 2. every cli helper must survive it --")
    sys.path.insert(0, str(ROOT))
    from lib import cli

    nasty = " ".join(HOSTILE) + " plain"

    check("cli.info", lambda: cli.info(nasty))
    check("cli.ok", lambda: cli.ok(nasty))
    check("cli.warn", lambda: cli.warn(nasty))
    check("cli.error", lambda: cli.error(nasty))
    check("cli.muted", lambda: cli.muted(nasty))
    check("cli.good", lambda: cli.good(nasty))
    check("cli.kv", lambda: cli.kv("label " + nasty, "value " + nasty))
    check("cli.step", lambda: cli.step(1, 9, "text " + nasty))
    check("cli.section", lambda: cli.section("title " + nasty))
    check("cli.hr", lambda: cli.hr(char=nasty))
    check("cli.banner", lambda: cli.banner(nasty, nasty))
    # cli_ok takes the script name; pass a hostile one and ensure it survives.
    check("cli.cli_ok", lambda: cli.cli_ok(nasty))

    def with_activity():
        with cli.activity("waiting " + nasty) as act:
            act.update("still " + nasty)
            act.stop("done " + nasty)

    check("cli.activity (message + update + stop)", with_activity)

    # The exact shape that killed three runs in a row.
    check("the real crashing line (to_xlsx.py:89)",
          lambda: cli.ok(f"Wrote 999 rows \u00d7 57 cols \u2192 "
                         f"C:\\Users\\phlspxuser\\sheet-bot\\downloads\\x.xlsx"))

    # ── 3. the crash reporter must survive a hostile exception message ────
    print("\n-- 3. the error reporter must not die on the error --")
    exc = ValueError("FMS said: order \u2192 status \u2713 failed \u4e2d")

    def report_it():
        p = cli.write_report("check_console_encoding", exc)
        cli._print_crash(exc, p)
        body = p.read_text(encoding="utf-8")
        assert "\u2192" in body, "crash report lost the arrow -- transcript must keep true text"

    check("_print_crash + write_report keep the true text", report_it)

    def entry_crash():
        @cli.entry("check_console_encoding", "guard")
        def boom():
            raise RuntimeError("upstream said \u2192 nope")

        assert boom() == 1, "entry() must report failure as exit code 1"

    check("cli.entry wrapper survives a hostile exception", entry_crash)

    # ── 4. and the transcript is what people actually read ─────────────────
    print("\n-- 4. transcript fidelity --")
    cli.info("probe " + nasty)
    tail = cli._transcript[-1] if cli._transcript else ""
    check("transcript keeps the real characters",
          lambda: None if "\u2192" in tail else (_ for _ in ()).throw(
              AssertionError(f"transcript lost them: {tail!r}")))

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED:"))
    for f in fails:
        print(f"  - {f}")
    return 0 if not fails else 1


def main() -> int:
    # Child inherits the parent's imports; run it isolated so the forced cp1252
    # stdout cannot leak into anything else in this process.
    code = child.__code__
    r = subprocess.run([sys.executable, __file__, "--child"],
                       cwd=str(ROOT), env=_env())
    return r.returncode


def _env():
    import os
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = ""       # ignore, we force it in the wrapper
    e.pop("PYTHONIOENCODING", None)
    return e


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(child())
    raise SystemExit(main())