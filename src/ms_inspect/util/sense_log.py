"""
sense_log.py — records that ms_workflow_status was actually called for a
given analyst_driver turn, so a PreToolUse hook on the writing tools can
refuse a call that was never preceded by a fresh sense.

"Sensing" is analyst_driver's own term (`Loop.sense()`): calling
`ms_workflow_status` to get the deterministic, tool-measured truth about
reduction state, as opposed to the model reasoning from memory or a stale
belief. The failure this closes: the model reads a skill's instruction to
sense first and sometimes doesn't, then calls a writing tool against
outdated understanding. A `PostToolUse` hook on skill-read calls
`record_sense` right after it senses; a `PreToolUse` hook on the writing
tools calls `has_sensed` and denies the call if sensing never happened this
session.

Storage is its own `<workdir>/analyst.db` — independent of this repo's
`stage_log.py`, which still uses `stage_log.jsonl`. `radio-analyst` has a
separate, unlanded migration of `stage_log`/`reduction_log` onto a
SQLite `analyst.db`; if this repo's vendored tool-layer copy is ever
replaced by a pinned dependency on that work, sense_log's table can move into
the same database and `ANALYST_DB_NAME` can be imported instead of
duplicated. Until then, two storage files in one workdir is a real but minor
inconsistency, not a functional problem — the two logs record unrelated
facts and neither reads the other.

Keyed by the driver's own `session_id`, one fresh session per turn (each
turn is a new `claude -p` process, never `--resume`/`--continue`), so no
expiry logic is needed — a session_id is never reused.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

#: Duplicated from the pending stage_log.py SQLite migration — see docstring.
ANALYST_DB_NAME = "analyst.db"

_DDL = (
    "CREATE TABLE IF NOT EXISTS sense_log ("
    "id INTEGER PRIMARY KEY, "
    "session_id TEXT NOT NULL, "
    "at TEXT NOT NULL, "
    "next_recommended_step TEXT NOT NULL)"
)


def _db_path(workdir: str | Path) -> Path:
    return Path(workdir) / ANALYST_DB_NAME


def record_sense(workdir: str | Path, session_id: str, next_recommended_step: str) -> None:
    """Insert one row recording that sensing happened this session.

    One insert, one transaction — a hook killed mid-write leaves either the
    whole row or none of it, same contract as stage_log's own writer.
    """
    con = sqlite3.connect(_db_path(workdir), timeout=30, isolation_level=None)
    try:
        # Another writer may hold the lock while switching; the mode is persistent.
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN IMMEDIATE")
        con.execute(_DDL)
        con.execute(
            "INSERT INTO sense_log (session_id, at, next_recommended_step) VALUES (?, ?, ?)",
            (session_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), next_recommended_step),
        )
        con.execute("COMMIT")
    finally:
        con.close()


def has_sensed(workdir: str | Path, session_id: str) -> bool:
    """True if `record_sense` has been called for this session in this workdir.

    No database, or no table yet, both read as "not sensed" rather than
    raising — a workdir with nothing written to it yet is a legitimate state
    (e.g. before the first turn), not a corruption.
    """
    path = _db_path(workdir)
    if not path.is_file():
        return False
    con = sqlite3.connect(path, timeout=30)
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sense_log'"
        ).fetchone()
        if not has_table:
            return False
        row = con.execute(
            "SELECT 1 FROM sense_log WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        return row is not None
    finally:
        con.close()
