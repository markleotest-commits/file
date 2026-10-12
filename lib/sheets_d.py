"""Sheets side of the dispatch bot — batch read blanks, batch write qtys,
read-back verify. Never touches a cell that is not blank in the target column."""

from __future__ import annotations

from lib import cli

# defaults (0-indexed) — overridable via config.py
LT_COL = 2      # C = LH Trip
HUB_COL = 8     # I = HUB
TARGET_COL = 17  # R = Total Order Outbounded (blank = needs qty)


def _col_letter(idx0: int) -> str:
    n, s = idx0 + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def connect(cfg):
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(
        str(cfg.SERVICE_ACCOUNT_JSON), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(cfg.SHEET_ID)
    ws = sh.worksheet(cfg.WORKSHEET_NAME)
    return ws


def find_blank_rows(ws, lt_col: int = LT_COL, hub_col: int = HUB_COL,
                    target_col: int = TARGET_COL) -> tuple[list[dict], dict[str, list[dict]]]:
    """Rows with LT in C + hub in I + blank target. Returns (rows, grouped_by_lt)."""
    last_col = max(lt_col, hub_col, target_col)
    vals = ws.get_values(range_spec := f"A:{_col_letter(last_col)}")
    cli.info(f"Sheet read: {len(vals)} row(s) (A:{_col_letter(last_col)}).")
    rows: list[dict] = []
    for i, row in enumerate(vals[1:], start=2):
        lt = (row[lt_col] or "").strip() if len(row) > lt_col else ""
        hub = (row[hub_col] or "").strip() if len(row) > hub_col else ""
        tgt = (row[target_col] or "").strip() if len(row) > target_col else ""
        if lt.startswith("LT") and hub and not tgt:
            rows.append({"row": i, "lt": lt, "hub": hub})
    grouped: dict[str, list[dict]] = {}
    for e in rows:
        grouped.setdefault(e["lt"], []).append(e)
    cli.ok(f"Blank qty rows: {len(rows)} across {len(grouped)} unique LT(s).")
    return rows, grouped


def write_qtys(ws, sheet_name: str, writes: list[tuple[int, int]],
               target_col: int = TARGET_COL) -> int:
    """writes = [(row_num, qty)]; one batch_update; then read-back verify."""
    if not writes:
        cli.warn("Nothing to write.")
        return 0
    letter = _col_letter(target_col)
    updates = [{"range": f"{sheet_name}!{letter}{r}", "values": [[q]]}
               for r, q in writes]
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    # read-back proof
    bad = 0
    for r, q in writes:
        got = ws.get_values(f"{letter}{r}:{letter}{r}")
        val = (got[0][0] if got and got[0] else "").strip()
        if str(q) not in str(val):
            bad += 1
            cli.warn(f"Row {r}: wrote {q}, sheet shows '{val}'.")
    if bad:
        cli.warn(f"{bad}/{len(writes)} cell(s) failed read-back verification.")
    else:
        cli.ok(f"All {len(writes)} cell(s) verified in column {letter}.")
    return len(writes) - bad
