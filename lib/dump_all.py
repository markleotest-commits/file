
"""Write copied MC rows into today's DUMP ALL tab (A=TO, B=Status, C=Remarks).

Never overwrite a cell that already has a value. Only fill blanks / new rows.
New rows are placed TOP-TO-BOTTOM: blank A:C rows inside the table are filled
first (from the top), and only when no blank row is left do we continue right
after the last row that has data in A:C.  append_rows() is NOT used — it jumps
to the very bottom of the sheet's used range (stray formulas/old rows down
there made pastes land at the bottom, at risk of being clobbered).
"""

from __future__ import annotations


from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def _cell(row: list, idx: int) -> str:
    if idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def write_rows(ws, rows: list[tuple[str, str, str]]) -> tuple[int, int, int]:
    """Fill blank B/C only. Append TOs that are not in A. Returns (filled, appended, skipped)."""
    if not rows:
        info("Nothing to write to DUMP ALL.")
        return 0, 0, 0

    grid = ws.get_values("A:C")
    norm = [(_cell(r, 0), _cell(r, 1), _cell(r, 2)) for r in grid]
    info(f"DUMP ALL row 1 (expect TO / Status / Remarks): {norm[0] if norm else '(empty tab)'}")
    index: dict[str, int] = {}
    has_b: dict[str, bool] = {}
    has_c: dict[str, bool] = {}
    last_data = 0
    for i, (a, b, c) in enumerate(norm):
        if a or b or c:
            last_data = i + 1
        if a and a not in index:
            index[a] = i + 1
            has_b[a] = bool(b)
            has_c[a] = bool(c)

    # Blank A:C rows (top to bottom, header row 1 protected) = fill slots.
    slots = [i + 1 for i, (a, b, c) in enumerate(norm)
             if not (a or b or c) and i >= 1]
    slot_i = 0
    cursor = last_data + 1  # used only when no blank slot is left

    def take_row() -> int:
        nonlocal slot_i, cursor
        while slot_i < len(slots):
            r = slots[slot_i]
            slot_i += 1
            return r
        r = cursor
        cursor += 1
        return r

    updates: list[dict] = []
    skipped = 0
    filled = 0
    placed: list[int] = []

    for to, status, remarks in rows:
        to = (to or "").strip()
        if not to:
            continue
        if to in index:
            r = index[to]
            b_empty = not has_b[to]
            c_empty = not has_c[to]
            if not b_empty and not c_empty:
                skipped += 1
                continue
            if b_empty and c_empty:
                updates.append({"range": f"B{r}:C{r}", "values": [[status, remarks]]})
                filled += 1
            elif b_empty:
                updates.append({"range": f"B{r}", "values": [[status]]})
                filled += 1
            else:
                updates.append({"range": f"C{r}", "values": [[remarks]]})
                filled += 1
            has_b[to] = True
            has_c[to] = True
        else:
            r = take_row()
            updates.append({"range": f"A{r}:C{r}",
                            "values": [[to, status, remarks]]})
            index[to] = r
            has_b[to] = True
            has_c[to] = True
            placed.append(r)

    if updates:
        if placed:
            max_row = max(placed)
            if max_row > ws.row_count:
                need = max_row - ws.row_count + 100
                try:
                    ws.add_rows(need)
                    info(f"DUMP ALL grew by {need} row(s) to fit new T.O.s.")
                except Exception as e:
                    raise RuntimeError(
                        "DUMP ALL is at Google Sheets' row limit and cannot "
                        f"grow ({str(e).splitlines()[0]}). Archive old rows "
                        "(move them to another tab) and re-run — nothing was "
                        "written this pass."
                    )
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    where = ""
    if placed:
        where = f" | new rows at A{min(placed)}:C{max(placed)}"

    ok(
        f"DUMP ALL: filled {filled} blank row(s), "
        f"added {len(placed)} new (top-to-bottom){where}, "
        f"skipped {skipped} (already written — not overwritten)"
    )
    return filled, len(placed), skipped


