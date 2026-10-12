"""READ-ONLY: compare the live FMS batch-search results against the MC packed OIDs.

Answers the "if packed = how many pages" question with data:
  - total rows the pager reports, at what size/page
  - how many of the packed OIDs actually appear within the first N pages
  - the distinct OIDs that are NOT packed (extras) and where they came from
  - whether case/whitespace variants are inflating the distinct count

    py tools\\probe_fms_vs_packed.py [N]     # walk the first N pages (default 8)
    py tools\\probe_fms_vs_packed.py --all   # walk every page

No search is run, nothing is copied, no Export click. The pager is returned
to page 1 at the end (the packed step resets it anyway).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CDP = "http://127.0.0.1:9222"


def main() -> int:
    args = sys.argv[1:]
    walk_all = "--all" in args
    n_pages = 250 if walk_all else 8
    for a in args:
        if a.isdigit():
            n_pages = int(a)

    from playwright.sync_api import sync_playwright

    # reuse the tested walk helpers instead of guessing selectors again
    from lib.packed_scrape import (
        _active_page,
        _goto_page,
        _page_size,
        _read_table_stable,
        _table_total,
        WANTED,
        goto_first_page,
    )
    from lib.mc_drive import read_packed_oids

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        fms = None
        mc = None
        for q in ctx.pages:
            u = (q.url or "").lower()
            if "ordertracking" in u:
                fms = q
            if "mc_" in u or "mcv" in u or "mischeck" in u:
                mc = q
        if fms is None:
            print("No FMS tab open (orderTracking url missing).")
            return 1
        if mc is None:
            print("No MC tab open — cannot read the packed list.")
            return 2

        packed = [str(o).strip().upper() for o in read_packed_oids(mc)]
        packed_set = {o for o in packed if o}
        print(f"MC Packed tab: {len(packed_set)} OID(s)")

        total = _table_total(fms)
        size = _page_size(fms)
        start_page = _active_page(fms)
        print(f"FMS results: total={total} size={size}/page current_page={start_page}")

        # get back to page 1 first so the walk is deterministic
        if start_page is not None and start_page > 1:
            at = goto_first_page(fms)
            print(f"(returned pager to page {at})")

        mp = {}
        seen_raw: set[str] = set()
        found_packed: set[str] = set()
        per_page_new = []
        n = 1
        while n <= n_pages:
            landed, data = _goto_page(fms, n)
            if not landed or not data or not data.get("rows"):
                print(f"  page {n}: could not land — stopping walk here.")
                break
            if not mp:
                for i, h in enumerate(data["headers"]):
                    if h in WANTED and h not in mp:
                        mp[h] = i
            new = 0
            for cells in data["rows"]:
                if not cells or all(not c for c in cells):
                    continue
                idx = mp.get("SPX Tracking Number")
                oid = cells[idx] if idx is not None and idx < len(cells) else ""
                oid = (oid or "").strip()
                if not oid:
                    continue
                if oid not in seen_raw:
                    seen_raw.add(oid)
                    if oid.upper() in packed_set:
                        found_packed.add(oid.upper())
                    new += 1
            per_page_new.append((n, new))
            n += 1
            if walk_all and total and sum(x for _, x in per_page_new) >= total:
                break

        upper_set = {s.upper() for s in seen_raw}
        lower_set = {s.lower() for s in seen_raw}
        extras = sorted(upper_set - packed_set)
        still_missing = sorted(packed_set - upper_set)

        print()
        print(f"Pages walked: {len(per_page_new)}  "
              f"(new distinct per page: {[c for _, c in per_page_new]})")
        print(f"Distinct OIDs seen: {len(seen_raw)} "
              f"({len(upper_set)} unique ignoring case, "
              f"{len(lower_set)} unique lowercased)")
        print(f"Packed OIDs found: {len(found_packed)} / {len(packed_set)}")
        if still_missing:
            print(f"Packed OIDs still missing: {len(still_missing)}")
            for o in still_missing[:10]:
                print(f"    {o}")
        else:
            print("Packed OIDs still missing: 0")
        print(f"Non-packed (extra) distinct OIDs seen: {len(extras)}")
        for o in extras[:10]:
            print(f"    {o}")
        if len(extras) > 10:
            print(f"    ... and {len(extras) - 10} more")

        # tidy up: leave the pager on page 1 like copy_all_pages does
        try:
            at = goto_first_page(fms)
            print(f"(left the pager on page {at})")
        except Exception as e:
            print(f"(could not return pager to page 1: {str(e).splitlines()[0][:60]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())