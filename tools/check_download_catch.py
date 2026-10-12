"""Download-catch helpers must fast-fail dead clicks (bug 44), no live FMS needed.

  * a Download click that opens a blank tab and starts nothing must return
    in ~12 s (not 60 s), with the stray tab closed;
  * a growing .crdownload must count as "started" (slow-but-live downloads
    still get the full wait);
  * only BLANK tabs opened after the click are closed — real pages are kept.

    py tools\\check_download_catch.py
"""

import sys
import tempfile
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


def main() -> int:
    from playwright.sync_api import sync_playwright

    from fms_batch_export import (
        _catch_download,
        _close_stray_blank_tabs,
        _download_inflight,
    )
    from lib.wait_file import snapshot_names

    print("\n-- in-flight temp detection (no browser) --")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        check("no temp files -> not in flight",
              _download_inflight([d], time.time() - 60) is False)
        old = d / "old.crdownload"
        old.write_bytes(b"x")
        import os

        ancient = time.time() - 3600
        os.utime(old, (ancient, ancient))
        check("stale temp -> not in flight",
              _download_inflight([d], time.time() - 60) is False)
        live = d / "live.crdownload"
        live.write_bytes(b"y" * 100)
        check("fresh temp -> in flight",
              _download_inflight([d], time.time() - 60) is True)

    print("\n-- blank-tab closing (headless browser) --")
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto("data:text/html,<h1>real page</h1>")
        known = list(pg.context.pages)
        pg.evaluate("() => window.open('about:blank', '_blank')")
        pg.wait_for_timeout(400)
        pages = list(pg.context.pages)
        stray = next(q for q in pages if q not in known)
        check("stray blank tab exists", stray.url in ("", "about:blank"),
              f"url={stray.url!r}")
        _close_stray_blank_tabs(pg, known)
        check("stray blank tab closed", stray.is_closed())
        check("real page kept", not pg.is_closed())
        check("known list grew by exactly the stray",
              len(known) == 2, f"len={len(known)}")
        b.close()

    print("\n-- dead-click fast-fail timing --")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto("data:text/html,<h1>fake FMS</h1>")
            before = snapshot_names([d])
            t0 = time.time()
            got, alive = _catch_download(
                pg, [], [d], before, time.time(), d,
                timeout_s=12, known_tabs=list(pg.context.pages))
            took = time.time() - t0
            check("dead click returns nothing", got is None)
            check("dead click reports no life", alive is False)
            check("fast-fail took ~12 s, not 60 s", 10 <= took <= 20,
                  f"took={took:.1f}s")
            b.close()

    print("\n-- empty save_as falls back to the on-disk GUID (bug 47) --")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto("data:text/html,<h1>fake FMS</h1>")
            before = snapshot_names([d])
            click_mtime = time.time() - 1   # window opened a moment ago
            # The real export lands on disk as a bare GUID (CDP allowAndName).
            guid = d / "283960a9-8877-42ba-b886-bd4d1cc9df1e"
            guid.write_bytes(b"Order ID,SLS Tracking Number\n"
                             b"PH260047717830G,SPXPH1\n" * 20)

            class FakeDownload:
                suggested_filename = "export_reception_order_x.csv"

                def save_as(self, path):
                    Path(path).write_bytes(b"")  # the 0-byte artifact

            got, alive = _catch_download(
                pg, [FakeDownload()], [d], before, click_mtime, d,
                timeout_s=10, known_tabs=list(pg.context.pages))
            check("empty save_as rejected, on-disk GUID used",
                  got is not None and got.name == guid.name, f"got={got}")
            check("empty artifact removed",
                  not (d / "export_reception_order_x.csv").exists())
            b.close()

    print("\n-- a new 0-byte GUID file counts as life (bug 47) --")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto("data:text/html,<h1>fake FMS</h1>")
            before = snapshot_names([d])
            click_mtime = time.time() - 1
            # CDP "allowAndName" writes the GUID directly; a slow download is
            # 0 bytes with no .crdownload suffix. That must still read as life.
            (d / "8f7791d8-0e57-489f-96ac-a05d78e5fcfe").write_bytes(b"")
            got, alive = _catch_download(
                pg, [], [d], before, click_mtime, d,
                timeout_s=3, known_tabs=list(pg.context.pages))
            check("new 0-byte GUID reported as life",
                  got is None and alive is True, f"got={got} alive={alive}")
            b.close()

    print("\n-- a slow 0-byte GUID that later completes is NOT abandoned (bug 52) --")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto("data:text/html,<h1>fake FMS</h1>")
            before = snapshot_names([d])
            click_mtime = time.time() - 1
            # allowAndName drops a 0-byte GUID first; the bytes arrive later.
            # The "wait for it to finish" phase must keep watching it instead
            # of returning on first sight and declaring "never finished".
            guid = d / "0e57aaaa-1111-2222-3333-444455556666"
            guid.write_bytes(b"")

            def complete():
                guid.write_bytes(b"Order ID,SLS Tracking Number\n"
                                 b"PH260047717830G,SPXPH1\n" * 20)

            threading.Timer(1.5, complete).start()
            t0 = time.time()
            got, alive = _catch_download(
                pg, [], [d], before, click_mtime, d,
                timeout_s=10, known_tabs=list(pg.context.pages))
            took = time.time() - t0
            check("slow GUID is waited out and returned",
                  got is not None and got.name == guid.name, f"got={got}")
            check("did not return early on the 0-byte placeholder",
                  took >= 1.0, f"took={took:.1f}s")
            b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())