
"""Wait for a real file, not a clock. Used from STEP 6 onward."""

from __future__ import annotations

import os
import time
from pathlib import Path

INCOMPLETE = {".crdownload", ".tmp", ".download", ".part", ".opdownload"}


SKIP_SUFFIX = INCOMPLETE | {".zip"}

# Real FMS downloads look like this. We strongly prefer them over anything else.
PREFER_PREFIXES = (
    "export_reception_order_",
    "export_reception_",
    "fms_reception_",
)

from lib import cli
info = cli.info
ok = cli.ok
warn = cli.warn
error = cli.error


def _looks_stale_artifact(p: Path) -> bool:
    """Path-based safety net only.

    NOTE: FMS downloads often arrive as a bare GUID with NO extension (CSV
    bytes) — that is the REAL download and must be accepted. Old stale files
    are excluded by the before-snapshot and min_mtime checks instead, NOT by
    their GUID-looking name. So never reject by filename here.
    """
    if "playwright-artifacts" in str(p).lower():
        return True
    return False


def _score_candidate(p: Path) -> int:
    """Higher = better. Real FMS names outrank anything else."""
    name = p.name.lower()
    if name.startswith(PREFER_PREFIXES):
        return 3
    if ".csv" in name or ".txt" in name or "reception" in name or "export" in name:
        return 2
    return 1


def download_watch_folders(root: Path) -> list[Path]:
    """Do not watch %TEMP% / playwright-artifacts (stale GUID downloads)."""
    return [
        Path.home() / "Downloads",
        root / "downloads",
        root / "chrome-debug-profile" / "Downloads",
        root / "chrome-debug-profile" / "Default" / "Downloads",
    ]


def classify_bytes(data: bytes) -> tuple[str, str]:
    """Return (extension_with_dot, human_type)."""
    import io
    import zipfile

    head = data[:16]
    low = data[:400].lower()
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            if any(n.startswith("xl/") for n in names):
                return ".xlsx", "Excel (.xlsx)"
        except Exception:
            pass
        return ".zip", "ZIP (not FMS)"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return ".xls", "Excel 97 (.xls)"
    if low.lstrip().startswith(b"<") or b"<html" in low:
        return ".html", "HTML (not a spreadsheet)"
    try:
        sample = data[:4000].decode("utf-8", errors="strict")
        if "," in sample or "\t" in sample:
            return ".csv", "CSV text"
    except UnicodeDecodeError:
        pass
    return ".bin", "unknown"


def copy_as_spreadsheet(raw: Path, dest_dir: Path, stamp: str) -> Path:
    dest_dir.mkdir(exist_ok=True)
    data = raw.read_bytes()
    ext, kind = classify_bytes(data)
    info(f"Source: {raw} ({raw.stat().st_size} bytes)")
    info(f"Type: {kind}")
    out = dest_dir / f"FMS_Reception_{stamp}{ext}"
    out.write_bytes(data)
    user = Path.home() / "Downloads" / out.name
    if user.resolve() != out.resolve():
        user.write_bytes(data)
    ok("Saved", out)
    info("WPS file:", user)
    return out


def snapshot_names(folders: list[str | Path]) -> dict[Path, set[str]]:
    snap: dict[Path, set[str]] = {}
    for raw in folders:
        folder = Path(raw)
        try:
            folder = folder.resolve()
        except OSError:
            pass
        snap[folder] = (
            {p.name for p in folder.iterdir() if p.is_file()} if folder.is_dir() else set()
        )
    return snap


def _new_candidates(
    folders: list[str | Path],
    before: dict[Path, set[str]],
    min_mtime: float | None,
) -> list[Path]:
    """Files that are new since `before`, finished, non-artifact, >= min_mtime."""
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in folders:
        folder = Path(raw)
        try:
            folder = folder.resolve()
        except OSError:
            pass
        if not folder.is_dir():
            continue
        old = before.get(folder, set())
        for p in folder.iterdir():
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            key = p.resolve()
            if key in seen:
                continue
            seen.add(key)
            if p.name in old:
                continue
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            if "sheet-bot" in p.name.lower():
                continue
            if _looks_stale_artifact(p):
                info("Ignoring stale artifact:", p.name)
                continue
            if min_mtime is not None:
                try:
                    if p.stat().st_mtime < min_mtime:
                        continue
                except OSError:
                    continue
            out.append(p)
    return out


def wait_for_new_file(
    folders: list[str | Path],
    before: dict[Path, set[str]],
    timeout: float = 180,
    stable_checks: int = 4,
    interval: float = 0.4,
    min_mtime: float | None = None,
) -> Path:
    """Wait for a new, finished, non-artifact file >= `min_mtime` seconds old.

    Prefers real FMS names (export_reception_order_* / FMS_Reception_*).
    Skips Playwright GUID leftovers and anything modified before the click.
    """
    t0 = time.time()
    last_size = -1
    stable = 0
    current: Path | None = None
    chosen: Path | None = None

    while time.time() - t0 < timeout:
        cands = _new_candidates(folders, before, min_mtime)
        if not cands:
            chosen = None
            last_size = -1
            stable = 0
            time.sleep(interval)
            continue

        # Prefer the best-named file; tie-break by newest mtime.
        chosen = max(cands, key=lambda p: (_score_candidate(p), p.stat().st_mtime))
        try:
            size = chosen.stat().st_size
        except OSError:
            size = -1

        if size > 0 and chosen == current and size == last_size:
            stable += 1
            if stable >= stable_checks:
                ok("New file stable:", chosen)
                return chosen
        else:
            current = chosen
            last_size = size
            stable = 0
        time.sleep(interval)

    raise TimeoutError(f"No new finished file in {folders} within {timeout}s")


def wait_until_file_ready(
    folder: str | Path,
    suffix: str = ".xlsx",
    timeout: float = 180,
    stable_checks: int = 4,
    interval: float = 0.4,
) -> Path:
    """
    Return the newest matching file once:
    - it exists
    - it is not a browser temp name
    - its size stayed the same for stable_checks * interval seconds
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {folder}")

    t0 = time.time()
    last_size = -1
    stable = 0

    while time.time() - t0 < timeout:
        candidates = [
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == suffix.lower()
        ]
        temps = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in INCOMPLETE]

        if temps or not candidates:
            last_size = -1
            stable = 0
            time.sleep(interval)
            continue

        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        size = newest.stat().st_size
        if size == last_size and size > 0:
            stable += 1
            if stable >= stable_checks:
                return newest
        else:
            stable = 0
            last_size = size
        time.sleep(interval)

    raise TimeoutError(
        f"No finished {suffix} file in {folder} within {timeout}s"
    )


