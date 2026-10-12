"""MC tab lookup must work for the Vercel site, not just the local file.

With config.MC_URL set, _find_mc_page() matches by site host (the local
filename never appears in a https:// URL); empty MC_URL keeps the legacy
filename match. Credentials must never reach the logs.

    py tools\\check_mc_url.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def fake_ctx(*urls):
    return SimpleNamespace(pages=[SimpleNamespace(url=u) for u in urls])


def main() -> int:
    import config
    from lib import mc_drive
    from lib.mc_drive import MC_HTML, _find_mc_page, _mc_desc, _mc_host

    old = getattr(config, "MC_URL", "")
    try:
        print("\n-- MC_URL set (Vercel site) --")
        config.MC_URL = "https://anyuser:s3cret@sheet-mc.vercel.app"
        check("host parsed", _mc_host(config.MC_URL) == "sheet-mc.vercel.app",
              _mc_host(config.MC_URL))
        desc = _mc_desc(config.MC_URL)
        check("logs never carry the password",
              "s3cret" not in desc and "anyuser" not in desc, desc)
        ctx = fake_ctx(
            "https://spx.shopee.ph/#/orderTracking",
            "https://sheet-mc.vercel.app/",
            "file:///C:/Users/phlspxuser/sheet-bot/mc/MC_V2.html",
        )
        hit = _find_mc_page(ctx, MC_HTML)
        check("finds the Vercel tab by host",
              hit is not None and "vercel.app" in hit.url, repr(hit))
        ctx2 = fake_ctx(
            "https://spx.shopee.ph/#/orderTracking",
            "file:///C:/Users/phlspxuser/sheet-bot/mc/MC_V2.html",
        )
        check("ignores the local-file tab when MC_URL is set",
              _find_mc_page(ctx2, MC_HTML) is None)
        check("None when no MC tab is open",
              _find_mc_page(fake_ctx("https://example.com/"), MC_HTML) is None)

        print("\n-- MC_URL empty (legacy local file) --")
        config.MC_URL = ""
        check("host empty", _mc_host("") == "")
        hit = _find_mc_page(ctx2, MC_HTML)
        check("finds the local-file tab by filename",
              hit is not None and hit.url.endswith("MC_V2.html"), repr(hit))
        check("None when no MC tab is open",
              _find_mc_page(fake_ctx("https://example.com/"), MC_HTML) is None)
    finally:
        config.MC_URL = old

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())