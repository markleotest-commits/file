"""Run state management — idempotency keys, resume, audit trail."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lib import cli

STATE_FILE = Path(__file__).resolve().parent.parent / "pending_state.json"

PHASES = [
    "sheets_read",
    "chrome_start",
    "fms_search",
    "fms_export",
    "download_verify",
    "wps_copy",
    "mc_validate",
    "dump_all_write",
    "packed_scrape",
]

PHASE_ORDER = {p: i for i, p in enumerate(PHASES)}


@dataclass
class PendingTO:
    to: str
    row: int
    col: str
    tab: str


@dataclass
class RunState:
    run_id: str
    sheet_title: str
    source_tabs: list[str]
    pending: list[PendingTO]
    excluded_validated: list[str]
    created_at: str
    dc: str | None = None
    phases_done: list[str] = field(default_factory=list)
    current_phase: str | None = None
    fms_export_clicked_at: str | None = None
    download_path: str | None = None
    wps_copy_verified: bool = False
    wps_copied_text: str | None = None
    mc_stats: dict[str, Any] = field(default_factory=dict)
    dump_all_result: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "sheet_title": self.sheet_title,
            "source_tabs": self.source_tabs,
            "pending": [asdict(p) for p in self.pending],
            "excluded_validated": self.excluded_validated,
            "created_at": self.created_at,
            "dc": self.dc,
            "phases_done": self.phases_done,
            "current_phase": self.current_phase,
            "fms_export_clicked_at": self.fms_export_clicked_at,
            "download_path": self.download_path,
            "wps_copy_verified": self.wps_copy_verified,
            "wps_copied_text": self.wps_copied_text,
            "mc_stats": self.mc_stats,
            "dump_all_result": self.dump_all_result,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RunState":
        pending = [PendingTO(**p) for p in data.get("pending", [])]
        return cls(
            run_id=data["run_id"],
            sheet_title=data["sheet_title"],
            source_tabs=data["source_tabs"],
            pending=pending,
            excluded_validated=data.get("excluded_validated", []),
            created_at=data["created_at"],
            dc=data.get("dc"),
            phases_done=data.get("phases_done", []),
            current_phase=data.get("current_phase"),
            fms_export_clicked_at=data.get("fms_export_clicked_at"),
            download_path=data.get("download_path"),
            wps_copy_verified=data.get("wps_copy_verified", False),
            wps_copied_text=data.get("wps_copied_text"),
            mc_stats=data.get("mc_stats", {}),
            dump_all_result=data.get("dump_all_result", {}),
        )

    def phase_completed(self, phase: str) -> bool:
        return phase in self.phases_done

    def can_resume_from(self, phase: str) -> bool:
        """True if all prior phases are done."""
        if phase not in PHASE_ORDER:
            return False
        target_idx = PHASE_ORDER[phase]
        return all(PHASE_ORDER.get(p, -1) < target_idx for p in self.phases_done)

    def mark_phase_done(self, phase: str) -> None:
        if phase not in self.phases_done:
            self.phases_done.append(phase)
        self.current_phase = None
        self.save()

    def mark_phase_started(self, phase: str) -> None:
        self.current_phase = phase
        self.save()

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        cli.muted(f"State saved: {STATE_FILE.name} (phase: {self.current_phase or 'idle'})")


def generate_run_id(pending_tos: list[PendingTO]) -> str:
    """Deterministic ID from timestamp + hash of pending T.O.s."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    to_hash = hashlib.md5("".join(sorted(p.to for p in pending_tos)).encode()).hexdigest()[:6]
    return f"{ts}_{to_hash}"


def load_state() -> RunState | None:
    if not STATE_FILE.is_file():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return RunState.from_json(data)
    except Exception as e:
        cli.warn(f"Could not load state file: {e}")
        return None


def save_state(state: RunState) -> None:
    state.save()


def clear_state() -> None:
    if STATE_FILE.is_file():
        STATE_FILE.unlink()
        cli.info("Cleared previous run state.")


def get_last_completed_phase(state: RunState) -> str | None:
    if not state.phases_done:
        return None
    return max(state.phases_done, key=lambda p: PHASE_ORDER.get(p, -1))