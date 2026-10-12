"""Tab tracking: event-driven discovery, role-keyed handles, self-tagging.

Three upgrades over the old "scan context.pages by URL" approach. Every one
degrades to the legacy behavior (or a clean None) on error, so nothing that
works today can break:

1. EVENT TRACKING — context.on("page") logs every new tab the moment it opens
   (including tabs the SITE opens, e.g. View Detail / download windows) and
   keeps it as a lookup candidate. Stray tabs are ONLY logged, never closed.
2. ROLE HANDLES — {"fms": page, "mc": page}; before each use the handle is
   health-checked (not closed, marker or URL still matches) and re-attached
   automatically when a human closed the tab between steps.
3. SELF-TAGGING — our tabs carry window.name = "sheetbot-fms" / "sheetbot-mc".
   window.name survives same-tab navigations, so the FMS tab is still found
   while parked on the login page (URL hints alone would miss it and the old
   code opened a second tab in that case).
"""

from __future__ import annotations

from lib import cli

info = cli.info

MARKERS = {"fms": "sheetbot-fms", "mc": "sheetbot-mc"}
URL_HINTS = {"fms": ("orderTracking", "spx.shopee.ph")}

_registry: dict[str, object] = {}
_wired: set[int] = set()
_new_pages: list = []
_session: object = None


def begin_session() -> None:
    """Call at the start of every Playwright session.

    Page/Context objects are bound to the sync_playwright() session that made
    them. Once that session stops the objects are dead but still LOOK alive:
    is_closed() returns False and .url still returns the old value, so
    healthy() waves them through and the first real call dies with
    "Event loop is closed! Is Playwright already stopped!"

    Running a second DC in the same process means a second session, so forget
    everything held over from the last one.
    """
    global _session
    _registry.clear()
    _wired.clear()
    _new_pages.clear()
    _session = object()


# ------------------------------------------------------------ 1) events

def wire(context) -> None:
    """Attach the new-tab listener once per context."""
    if _session is None:
        begin_session()
    if id(context) in _wired:
        return
    try:
        context.on("page", _on_new_page)
        _wired.add(id(context))
        info("Tab tracking ON - new tabs are logged and classified.")
    except Exception:
        pass


def _on_new_page(page) -> None:
    try:
        url = (page.url or "")[:90]
    except Exception:
        url = "?"
    info(f"Tab tracking: new tab detected: {url or '(blank)'}")
    _new_pages.append(page)
    del _new_pages[:-12]  # keep only the last few candidates


# ------------------------------------------------------------ 3) tagging

def tag(page, role: str) -> None:
    """Stamp window.name so discovery never depends on URL shapes."""
    try:
        page.evaluate("(n) => { window.name = n; }", MARKERS[role])
    except Exception:
        pass


def register(page, role: str) -> None:
    _registry[role] = page
    tag(page, role)


def _marker_of(page) -> str:
    try:
        return str(page.evaluate("() => window.name || ''") or "")
    except Exception:
        return ""


# ------------------------------------------------------------ 2) handles

def healthy(page, role: str) -> bool:
    """Alive + (marker match, or URL-hint match for untagged legacy tabs)."""
    if page is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    if _marker_of(page) == MARKERS[role]:
        return True
    url = page.url or ""
    return any(h in url for h in URL_HINTS.get(role, ()))


def get(context, role: str, extra_hints: tuple[str, ...] = ()):
    """Return a healthy handle for `role`, re-attaching when needed.

    Lookup order: registry -> marker scan -> URL scan -> recent event pages.
    Returns None when nothing matches (caller creates a tab or falls back).
    """
    wire(context)
    p = _registry.get(role)
    if healthy(p, role):
        return p
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    found = None
    for cand in pages:  # markers first (survive login redirects etc.)
        if _marker_of(cand) == MARKERS[role]:
            found = cand
            break
    if found is None:  # legacy URL hints (untagged tabs from older versions)
        hints = URL_HINTS.get(role, ()) + tuple(extra_hints)
        for cand in pages:
            url = cand.url or ""
            if any(h in url for h in hints):
                found = cand
                break
    if found is None:  # tabs the site opened that we classified later
        for cand in reversed(_new_pages):
            if _marker_of(cand) == MARKERS[role]:
                found = cand
                break
    if found is not None:
        register(found, role)  # tag it so the next lookup is marker-based
    return found
