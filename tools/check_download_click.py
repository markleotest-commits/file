"""The Download click must hit the BUTTON, not the row container.

Role queries match only real controls (button, then link), with the legacy
text match as fallback. A container-click theory was tested and DISPROVEN:
get_by_text("Downlo") matches exactly the BUTTON, so this guard asserts the
true behavior — old and new paths both land on the button — plus the link
and legacy fallbacks.

    py tools\\check_download_click.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  .taskrow { width: 600px; padding: 8px; border: 1px solid #999; }
  .taskrow button { float: right; }
  .taskrow a.dl { float: right; }
</style></head><body>
  <div class="taskrow" id="row-btn">
    <span>Reception Order</span> <span>2026-10-07 05:04:55</span>
    <span>Succeed</span> <button id="dl-btn">Download</button>
  </div>
  <div class="taskrow" id="row-link">
    <span>Reception Order</span> <span>2026-10-07 05:04:55</span>
    <span>Succeed</span> <a class="dl" id="dl-a" href="/file">Download</a>
  </div>
  <div class="taskrow" id="row-span">
    <span>Reception Order</span> <span>2026-10-07 05:04:55</span>
    <span>Succeed</span> <span class="dl" id="dl-span">Download</span>
  </div>
<script>
  window.__clicked = [];
  document.querySelectorAll('.taskrow').forEach(r => {
    r.addEventListener('click', () => window.__clicked.push('row:' + r.id));
  });
  document.querySelectorAll('#dl-btn,#dl-a,#dl-span').forEach(el => {
    el.addEventListener('click', e => {
      window.__clicked.push('control:' + el.id);
      e.stopPropagation();
      e.preventDefault();
    });
  });
</script></body></html>"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    from fms_batch_export import _click_row_download

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context().new_page()
        pg.set_content(HTML)
        pg.wait_for_timeout(300)

        print("\n-- text match resolves to the button, not the container --")
        pg.evaluate("() => { window.__clicked = []; }")
        row = pg.locator("#row-btn")
        n = row.get_by_text(re.compile(r"Downlo")).count()
        tag = row.get_by_text(re.compile(r"Downlo")).first.evaluate(
            "e => e.tagName + '#' + e.id")
        check("get_by_text matches exactly one element", n == 1, f"got {n}")
        check("... and it is the button", tag == "BUTTON#dl-btn", f"got {tag}")
        row.get_by_text(re.compile(r"Downlo")).first.click()
        clicked = pg.evaluate("() => window.__clicked")
        check("old path clicks the button", clicked == ["control:dl-btn"],
              f"got {clicked}")

        print("\n-- the fix clicks the real control --")
        pg.evaluate("() => { window.__clicked = []; }")
        how = _click_row_download(pg.locator("#row-btn"))
        clicked = pg.evaluate("() => window.__clicked")
        check("button row -> clicks the button", how == "button"
              and clicked == ["control:dl-btn"], f"{how} {clicked}")

        pg.evaluate("() => { window.__clicked = []; }")
        how = _click_row_download(pg.locator("#row-link"))
        clicked = pg.evaluate("() => window.__clicked")
        check("link row -> clicks the link", how == "link"
              and clicked == ["control:dl-a"], f"{how} {clicked}")

        pg.evaluate("() => { window.__clicked = []; }")
        how = _click_row_download(pg.locator("#row-span"))
        clicked = pg.evaluate("() => window.__clicked")
        check("span row -> legacy text fallback still clicks something",
              how == "text" and clicked == ["control:dl-span"],
              f"{how} {clicked}")

        b.close()

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())