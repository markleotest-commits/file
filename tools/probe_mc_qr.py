"""Inspect the live MC tab's OID+TO QR generator state (READ-ONLY).

Answers: which MC pages are open, does the CURRENT one have the OID+TO
generator (openQRGen_OidTo / #qrInput / setQRMode), what mode is active,
and what is in the textarea right now (did a previous paste land?).

    py tools\\probe_mc_qr.py

Deliberately NO tabs.begin_session() (that hung before). Direct CDP attach.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CDP = "http://127.0.0.1:9222"

JS = r"""() => {
    const out = {
        url: location.href.slice(0, 120),
        hasOpenQRGenOidTo: typeof openQRGen_OidTo === 'function',
        hasOpenQRGen: typeof openQRGen === 'function',
        hasQRRenderNow: typeof qrRenderNow === 'function',
        hasSetQRMode: typeof setQRMode === 'function',
        hasQrParseInput: typeof qrParseInput === 'function',
        hasQrInput: !!document.getElementById('qrInput'),
        hasQrResults: !!document.getElementById('qrResults'),
        qrInputLen: (document.getElementById('qrInput')?.value || '').length,
        qrInputSnippet: (document.getElementById('qrInput')?.value || '')
            .split('\n').slice(0, 5).map(s => s.slice(0, 60)),
        mode: typeof _qrMode !== 'undefined' ? _qrMode : '(no _qrMode)',
        modeManual: typeof _qrModeManual !== 'undefined' ? _qrModeManual : null,
        modalTitle: document.getElementById('qrModalTitle')?.textContent || null,
        modalDisplay: document.getElementById('qrModal')?.style.display || null,
        qrCards: document.querySelectorAll('#qrResults .qr-card').length,
        dockHeads: document.querySelectorAll('#qrResults .qr-dock-head').length,
        summary: (document.getElementById('qrSummary')?.textContent || '')
            .trim().slice(0, 120),
        badLines: (document.getElementById('qrBadLines')?.textContent || '')
            .trim().slice(0, 80),
    };
    return out;
}"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        mc_pages = []
        for q in ctx.pages:
            url = (q.url or "").lower()
            if "mc_" in url or "mcv" in url or "mischeck" in url:
                mc_pages.append(q)
        print(f"Chrome has {len(ctx.pages)} open tab(s); MC-like tabs: {len(mc_pages)}")
        if not mc_pages:
            print("No MC tab found — page_mc would be None and the paste would be")
            print("skipped with 'No MC page attached — paste this manually'.")
            return 1
        for i, q in enumerate(mc_pages):
            print(f"\n== MC tab #{i + 1}: {q.url[:120]} ==")
            try:
                s = q.evaluate(JS)
                for k, v in s.items():
                    txt = str(v)
                    txt = txt.encode("ascii", "backslashreplace").decode("ascii")
                    print(f"  {k:16s} {txt}")
            except Exception as e:
                print("  evaluate failed:", str(e).splitlines()[0][:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())