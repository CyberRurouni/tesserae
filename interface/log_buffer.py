"""
log_buffer.py — the recent log lines, for display in the browser.

The file on disk is the durable record. This is the live view: a run can be
watched while it happens, and stopped on evidence rather than on suspicion.

Kept in a bounded deque rather than tailed from disk so that a request is O(1)
in the number of lines ever written. That matters because the alternative —
reading the tail of a file a crawl is appending to — can block behind a
partial line and show the viewer a truncated entry.

Per-process, like the run state it sits beside. A restart clears it, which is
honest: those lines belonged to a process that is gone. The file is what
survives a restart.
"""

import logging
import threading
from collections import deque

MAX_LINES = 400

_lock = threading.Lock()
_lines: deque = deque(maxlen=MAX_LINES)


class TailHandler(logging.Handler):
    """Copies every record into the in-memory tail, formatted like the file."""

    def __init__(self, formatter: logging.Formatter):
        super().__init__()
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a formatting failure must not recurse
            return
        with _lock:
            _lines.append((record.created, line, record.levelno, record.name))


_handler: TailHandler | None = None


def install(formatter: logging.Formatter) -> TailHandler:
    """Attach the tail to the root logger, once."""
    global _handler
    with _lock:
        if _handler is None:
            _handler = TailHandler(formatter)
            logging.getLogger().addHandler(_handler)
    return _handler


def snapshot(limit: int = 200, name_filter: str | None = None) -> list[dict]:
    """The most recent lines, oldest first. Never raises."""
    with _lock:
        rows = list(_lines)
    if name_filter:
        needle = name_filter.lower()
        rows = [r for r in rows if needle in r[3].lower()]
    rows = rows[-max(1, min(limit, MAX_LINES)):]
    return [{"at": ts, "line": line, "level": lvl, "logger": name}
            for ts, line, lvl, name in rows]
