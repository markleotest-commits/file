
"""cli — small professional console toolkit for sheet-bot scripts.

Colors the cmd output, shows a live spinner / progress line, timestamps a
plain-text transcript, and turns any crash into an easy-to-share report file
under ``logs/`` so you can paste one path to report an error.

Usage (any script):

    from lib import cli

    cli.info("Connecting...")
    cli.ok("Spreadsheet opened")
    cli.warn("No DUMP ALL tab yet")
    cli.error("Bad credentials")

    with cli.activity("Waiting for Succeed") as act:
        while running:
            act.update("still running")
            time.sleep(1)

    if __name__ == "__main__":
        cli.entry("run_fms", "Full FMS → WPS → Misroute Checker flow")(main)

Notes
-----
- Colour is enabled only when stdout is a real terminal and NO_COLOR is not set.
  When output is redirected (e.g. ``> log.txt``) everything stays plain.
- On Windows the console VT-mode is switched on automatically; if that fails we
  silently fall back to plain text so scripts never crash over colour.
- Every ``info/ok/warn/error/...`` call is also written to an in-memory
  transcript that ``entry()`` saves to ``logs/<name>_<timestamp>.log`` on exit,
  so the error report always contains the full run history.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
#  ANSI helpers
# ─────────────────────────────────────────────────────────────────────
_RESET = "\x1b[0m"
_CODES = {
    "bold": "1", "dim": "2", "italic": "3", "under": "4",
    "black": "30", "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    "bred": "91", "bgreen": "92", "byellow": "93", "bblue": "94",
    "bmagenta": "95", "bcyan": "96", "bwhite": "97",
    "onred": "41", "ongreen": "42", "onyellow": "43",
}

_color_ok = None  # resolved lazily


def _init_windows_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def color_enabled() -> bool:
    global _color_ok
    if _color_ok is None:
        if os.environ.get("NO_COLOR"):
            _color_ok = False
        else:
            try:
                _color_ok = bool(sys.stdout.isatty())
            except Exception:
                _color_ok = False
            if _color_ok and os.name == "nt":
                _init_windows_vt()
    return _color_ok


def paint(text: object, *styles: str) -> str:
    text = str(text)
    if not color_enabled() or not styles:
        return text
    seq = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\x1b[{seq}m{text}{_RESET}" if seq else text


class _C:
    """Chainable colour helpers, e.g. c.red('x'), c.bold(c.green('y'))."""

    def _g(self, s, code): return paint(s, code)

    def black(self, s): return self._g(s, "black")
    def red(self, s): return self._g(s, "red")
    def green(self, s): return self._g(s, "green")
    def yellow(self, s): return self._g(s, "yellow")
    def blue(self, s): return self._g(s, "blue")
    def magenta(self, s): return self._g(s, "magenta")
    def cyan(self, s): return self._g(s, "cyan")
    def white(self, s): return self._g(s, "white")
    def bwhite(self, s): return self._g(s, "bwhite")
    def bold(self, s): return self._g(s, "bold")
    def dim(self, s): return self._g(s, "dim")
    def bgreen(self, s): return self._g(s, "bgreen")
    def bred(self, s): return self._g(s, "bred")
    def byellow(self, s): return self._g(s, "byellow")
    def bcyan(self, s): return self._g(s, "bcyan")
    def bmagenta(self, s): return self._g(s, "bmagenta")
    def bblue(self, s): return self._g(s, "bblue")
    def under(self, s): return self._g(s, "under")


c = _C()

# ─────────────────────────────────────────────────────────────────────
#  Transcript (plain text) so the report file has the whole run history
# ─────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_ts = lambda: datetime.now().strftime("%H:%M:%S")
_transcript: list[str] = []
_show_time = bool(os.environ.get("SHEETBOT_TIME"))


def _write_transcript(level: str, text: str) -> None:
    _transcript.append(f"[{_ts()}] {level:<5} {text}")
    if len(_transcript) > 3000:  # bound memory
        _transcript.pop(0)


def _safe_print(text: str = "", **kw) -> None:
    """print() that cannot die on a character the console cannot encode.

    Bug 30. The Windows console here is cp1252, which has no `→` (U+2192), no
    `✓` (U+2713), no `⚠` (U+26A0) and none of the box-drawing characters. Any
    message containing one raised UnicodeEncodeError out of `print()`.

    That is not cosmetic. Measured: `lib/to_xlsx.py` prints
    `Wrote 999 rows × 57 cols → <path>` immediately AFTER a successful FMS
    download, so every run died there — 45s of good work thrown away at the
    last step, with the real error buried under a codec complaint. Three runs
    in a row failed at exactly that spot and nobody could see why, because the
    pipeline "worked" right up until it tried to say so.

    It is also self-defeating in the worst way: `_print_crash` reports an
    exception by printing its message, so any exception whose text contains an
    unencodable character could not be reported either. The error reporter was
    crashing on the errors it existed to report.

    `lib/cli.py` already knew about this hazard — the spinner frames and banner
    are ASCII on purpose, with comments saying so — but it was patched one call
    site at a time. There are ~50 sites. Fixing it in the funnel means the next
    arrow anyone writes is safe by construction.

    Degradation is deliberate and in this order:
      1. print normally (keeps UTF-8 terminals and redirected UTF-8 files exact);
      2. re-encode to the console's own codec with 'replace', so `→` becomes
         '?' and the line still lands, rather than vanishing;
      3. strip to ASCII as a last resort.
    The transcript always keeps the true text, so the crash report file has the
    real characters even when the terminal could not show them.
    """
    try:
        print(text, **kw)
        return
    except UnicodeEncodeError:
        pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace") + kw_end(kw))
        sys.stdout.flush()
        return
    except Exception:
        pass
    sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + kw_end(kw))
    sys.stdout.flush()


def kw_end(kw: dict) -> str:
    """The literal text `print(text, **kw)` would have appended after text."""
    if kw.get("end", "\n") == "":
        return ""
    return kw.get("end", "\n") if kw.get("end") is not None else "\n"


def _emit(text: str, level: str = "INFO") -> None:
    _write_transcript(level, re.sub(r"\x1b\[[0-9;]*m", "", text))
    prefix = f"{_ts()}  " if _show_time else ""
    _safe_print(prefix + text)


# ─────────────────────────────────────────────────────────────────────
#  Semantic log helpers
# ─────────────────────────────────────────────────────────────────────
def _join(parts) -> str:
    return " ".join(str(p) for p in parts)


def info(*parts: object) -> None:
    _emit(c.bcyan(_join(parts)), "INFO")


def muted(*parts: object) -> None:
    _emit(c.dim(_join(parts)), "INFO")


def ok(*parts: object) -> None:
    _emit(c.green(_join(parts)), "OK")


def good(*parts: object) -> None:
    _emit(c.bgreen(_join(parts)), "OK")


def warn(*parts: object) -> None:
    _emit(c.byellow(_join(parts)), "WARN")


def error(*parts: object) -> None:
    _emit(c.bred(_join(parts)), "ERROR")


def step(step_no: int | None, total: int | None, text: object) -> None:
    if step_no is None:
        label = "STEP"
    elif total:
        label = f"STEP {step_no}/{total}"
    else:
        label = f"STEP {step_no}"
    _emit(c.bold(c.white(label)) + "  " + text, "STEP")


def section(title: object, char: str = "=") -> None:
    """Draw a coloured section rule line, e.g. section('Spreadsheet')."""
    line = str(title)
    width = max(8, 72 - len(line))
    bar = char * width
    _emit("", "---")
    _emit(c.bold(c.bblue("|")) + " " + c.bold(c.white(line)) + " " + c.dim(bar))
    _emit("", "---")


def hr(char: str = "-", color: str = "dim", width: int = 72) -> None:
    _emit(c.dim(char * width) if color == "dim" else paint(char * width, color), "---")


def kv(label: object, value: object, value_color: str = "white") -> None:
    _emit(c.dim(str(label) + ": ") + paint(value, value_color))


# ─────────────────────────────────────────────────────────────────────
#  Banner
# ─────────────────────────────────────────────────────────────────────
def banner(title: str, subtitle: str = "") -> None:
    line = "=" * 60
    _emit("")
    _emit(c.bold(c.bblue(line)))
    _emit(c.bold(c.bwhite(title.center(60))))
    if subtitle:
        _emit(c.dim(subtitle.center(60)))
    _emit(c.bold(c.bblue(line)))
    _emit("")


# ─────────────────────────────────────────────────────────────────────
#  Activity spinner / progress line (thread based)
# ─────────────────────────────────────────────────────────────────────
# ASCII-safe spinner frames for Windows cp1252 compatibility
_FRAMES = ["|", "/", "-", "\\"]
_BUSY = {"on": False}


class activity:
    """Animated progress line that returns early from the terminal on stop.

        with cli.activity("Waiting for Latest Task") as act:
            while running:
                act.update("still running")
                time.sleep(0.5)
    """

    def __init__(self, message: str = ""):
        self._message = message
        self._idx = 0
        self._stop = threading.Event()
        self._thread = None
        self._show = color_enabled()
        self._done = False

    def __enter__(self) -> "activity":
        _BUSY["on"] = True
        if self._show:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        else:
            _safe_print(c.dim(self._message), end="", flush=True)
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = _FRAMES[self._idx % len(_FRAMES)]
            _safe_print("\r" + c.bcyan(frame) + " " + self._message, end="", flush=True)
            self._idx += 1
            self._stop.wait(0.1)

    def update(self, message: str = None) -> None:
        if message is not None:
            self._message = str(message)

    def stop(self, done_text: str | None = None, ok: bool = True) -> None:
        if self._done:
            return
        self._done = True
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=0.5)
        _BUSY["on"] = False
        if self._show:
            sys.stdout.write("\x1b[2K\r")
            sys.stdout.flush()
        else:
            _safe_print()  # finish the plain-text inline line
        end = done_text or self._message
        mark = c.bgreen("[OK]") if ok else c.bred("[FAIL]")
        _emit(mark + " " + end, "OK" if ok else "INFO")

    def fail(self, done_text: str | None = None) -> None:
        self.stop(done_text, ok=False)

    def __exit__(self, *exc) -> None:
        if not exc[0]:
            self.stop()
        else:
            self.fail(str(exc[1]) or type(exc[0]).__name__)


def spinner(message: str = "") -> activity:
    """Simple alias: ``with cli.spinner('working'):``, no live updates."""
    return activity(message)


# ─────────────────────────────────────────────────────────────────────
#  Crash reporter + entry wrapper
# ─────────────────────────────────────────────────────────────────────
def diagnostics() -> str:
    lines = [
        f"timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"platform  : {sys.platform} ({os.name})",
    ]
    try:
        lines.append(f"python    : {sys.version.split()[0]} ({sys.executable})")
    except Exception:
        pass
    try:
        import config  # type: ignore
        lines.append(f"prefix    : {getattr(config, 'SHEET_TITLE_PREFIX', '')}")
        lines.append(f"MC_DC     : {getattr(config, 'MC_DC', '')}")
        lines.append(f"source    : {getattr(config, 'SOURCE_TAB_PREFIX', getattr(config, 'SOURCE_TAB', ''))}")
    except Exception:
        lines.append("config    : <not loaded>")
    lines.append(f"cwd       : {os.getcwd()}")
    return "\n".join(lines)


def write_report(name: str, exc: BaseException | None) -> Path:
    """Write a timestamped report under logs/ and return its path."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{name}_{stamp}.log"
    body = [
        "=" * 70,
        f" SHEET-BOT ERROR REPORT  ·  {name}",
        "=" * 70,
        diagnostics(),
        "-" * 70,
        " WHAT HAPPENED",
        "-" * 70,
        str(exc) if exc else "finished with errors",
        "",
        "-" * 70,
        " FULL TRACEBACK",
        "-" * 70,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if exc and getattr(exc, "__traceback__", None)
        else "(none)",
        "",
        "-" * 70,
        " RUN TRANSCRIPT",
        "-" * 70,
        *(_transcript or ["(nothing logged)"]),
        "",
        "=" * 70,
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _banner_for(name: str, blurb: str) -> None:
    try:
        stamp = datetime.now().strftime("%b %d, %Y  %H:%M:%S")
    except Exception:
        stamp = ""
    # Use ASCII-safe characters for Windows cp1252 compatibility
    subtitle = (name + (" - " + blurb if blurb else "")).strip()
    line = "=" * 64
    _emit("")
    _emit(c.bold(c.cyan(line)))
    _emit(c.bold(c.white(" SHEET-BOT")))
    _emit(c.bold(c.bcyan(subtitle)))
    _emit(c.dim(" started " + stamp))
    _emit(c.bold(c.cyan(line)))
    _emit("")


def entry(name: str, blurb: str = ""):
    """Decorator that runs a ``main()`` under the crash reporter.

        @cli.entry("run_fms", "Full FMS flow")
        def main() -> None:
            ...
    """

    def deco(fn):
        def wrapper(*a, **k):
            _banner_for(name, blurb)
            try:
                rc = fn(*a, **k)
                # Only claim success when main() actually succeeded. main()
                # reports its own problems with cli.error() and returns 1, so
                # printing "DONE - finished successfully" unconditionally told
                # the user a failed run had worked.
                if not rc:
                    cli_ok(name)
                elif isinstance(rc, int):
                    _emit(c.byellow(f"[--] {name} stopped with exit code {rc}."))
                    _safe_print()
                return rc
            except KeyboardInterrupt:
                _safe_print()
                _emit(c.byellow("Interrupted by you (Ctrl+C). Nothing was finalised."))
                return 130
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                _safe_print()
                _emit(c.bred("Stopped: ") + str(e.code or ""))
                return code
            except Exception as e:
                path = write_report(name, e)
                _print_crash(e, path)
                return 1

        return wrapper

    return deco


def cli_ok(name: str) -> None:
    hr()
    _emit(c.bold(c.bgreen("[OK] DONE - ") + name + " finished successfully."))
    _safe_print()


def _print_crash(exc: BaseException, path: Path) -> None:
    # This function is the last line of defence. It runs after something has
    # already gone wrong, and `str(exc)` is arbitrary text we do not control —
    # including text from FMS, from a spreadsheet cell, or from a filename.
    # Before bug 30 was fixed it used bare print(), so an exception mentioning
    # an unencodable character took the error reporter down with it and the user
    # got a traceback with no explanation at all.
    _safe_print()
    hr(color="red")
    _emit(c.bold(c.bred("  [FAIL] SCRIPT FAILED")))
    _emit("")
    _emit(c.bold(c.white("  What happened: ")) + c.red(str(exc)))
    _emit("")
    _emit(c.yellow("  To report this error:"))
    _emit(c.yellow("   1) copy this file path:"))
    _emit("      " + c.bwhite(str(path)))
    _emit(c.yellow("   2) paste it into the report (or attach the file)."))
    _emit(c.yellow("      It already contains your config snapshot + full traceback."))
    _emit("")
    _emit(c.dim("  Tip: fix the first error above, then simply re-run the same command."))
    hr(color="red")
    _safe_print()


# Keep a tiny public reference so the import line reads naturally.
start = activity


