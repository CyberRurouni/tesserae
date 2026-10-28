"""
Live progress for an assistant-run scrape.

The assistant runs `interface.manual` in its own process, so it cannot update the
FastAPI process's in-memory run state. This module is the shared handoff: the CLI
writes one small JSON file per run, and the API reads it so the frontend can show
the scrape while it is happening.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from genesis.config import DATA_DIR

PROGRESS_DIR = DATA_DIR / "agent_audits"
STALE_RUN_SECONDS = 600
TERMINAL_STATES = {"completed", "failed", "cancelled", "abandoned", "partial"}


def _path(run_id: str) -> Path:
    return PROGRESS_DIR / f"progress_{run_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def write(run_id: str, payload: dict) -> None:
    """Publish the current state. Called by the CLI as the scrape moves."""
    state = {
        **payload,
        "id": run_id,
        "mode": "manual",
        "last_progress_at": _now(),
    }
    _write_payload(_path(run_id), state)


def _read_path(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None

    if data.get("status") not in TERMINAL_STATES:
        raw = data.get("last_progress_at") or data.get("started_at")
        if raw:
            try:
                beat = datetime.fromisoformat(raw)
            except ValueError:
                beat = None
            if beat is not None:
                if beat.tzinfo is None:
                    beat = beat.replace(tzinfo=timezone.utc)
                idle = (datetime.now(timezone.utc) - beat).total_seconds()
                if idle > STALE_RUN_SECONDS:
                    data = {
                        **data,
                        "status": "abandoned",
                        "error": (
                            f"No progress for {int(idle // 60)} minutes. The scrape "
                            "process is not believed to still be running."
                        ),
                        "completed_at": _now(),
                    }
    return data


def read(run_id: str) -> dict | None:
    path = _path(run_id)
    if not path.exists():
        return None
    return _read_path(path)


def list_active() -> list[dict]:
    if not PROGRESS_DIR.exists():
        return []
    states = []
    for path in sorted(PROGRESS_DIR.glob("progress_*.json")):
        state = _read_path(path)
        if state and state.get("status") not in TERMINAL_STATES:
            states.append(state)
    return states


def latest_active() -> dict | None:
    states = list_active()
    if not states:
        return None
    return max(states, key=lambda state: state.get("started_at") or "")


def clear(run_id: str) -> None:
    _path(run_id).unlink(missing_ok=True)
