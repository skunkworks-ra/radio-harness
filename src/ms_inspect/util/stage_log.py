"""
stage_log.py — the workdir stage log, written by generated scripts and read by
ms_workflow_status.

A reduction's state cannot be inferred from the filesystem: every writing tool
takes its caltable path as a caller-chosen argument, so no fixed set of names
can be searched for. The log replaces inference with a record. Each generated
script inserts one row per product it writes, AFTER CASA returns, so a row
exists only if that step actually completed. Rows are never deleted: a retry
adds a row, and a stage that must be redone is marked ``superseded_by``
rather than removed.

Storage is the ``stage_log`` table in ``<workdir>/analyst.db`` (stdlib
sqlite3, WAL). One insert is one transaction, so a job killed mid-write
leaves either the whole row or none of it. ``schema_version``/``analyst_rev``
are folded into this same table rather than kept as a second file.

Placed in ms_inspect because it is the package ms_modify and ms_create both
already import from; ms_inspect never imports either of them. The snippet is
embedded verbatim into generated scripts, so it must stay dependency-free and
self-contained — same contract as pathguard.SAFE_RM_TABLE_SNIPPET.

Two limits, both deliberate:

- The check is existence only. A caltable directory appears the moment CASA
  starts writing it, so this does not prove the solve produced solutions. Row
  counts were considered and deferred until an empty-caltable failure is
  actually observed.
- A script killed outright (SIGKILL, an OOM, the -6 abort seen when the disk
  filled) writes no row at all. The log explains a failure; it does not
  detect every one. The driver's recorded exit code remains the outer truth.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

#: The per-workdir database. Shared by the stage log and the reduction log.
ANALYST_DB_NAME = "analyst.db"

#: Created by the writer snippet on first use. ``product_exists`` because
#: ``exists`` is an SQL keyword; the reader maps it back. ``schema_version``/
#: ``analyst_rev`` are nullable: a row written before either column existed
#: has neither — see schema_version_of()'s docstring.
STAGE_LOG_DDL = (
    "CREATE TABLE IF NOT EXISTS stage_log ("
    "id INTEGER PRIMARY KEY, "
    "stage TEXT NOT NULL, "
    "product TEXT NOT NULL, "
    "at TEXT NOT NULL, "
    "product_exists INTEGER NOT NULL, "
    "measurement TEXT, "
    "error TEXT, "
    "superseded_by TEXT, "
    "schema_version INTEGER, "
    "analyst_rev TEXT)"
)

#: Embedded verbatim in generated scripts. Call once after each product is
#: written. Opens, inserts one row, commits and closes — never holds a
#: connection, because an uncommitted write is lost when a job dies mid-stage,
#: which is precisely the case the row has to explain.
RECORD_STAGE_SNIPPET = (
    '''\
def _record_stage(workdir, stage, product, measurement=None):
    """Insert one row into analyst.db stage_log. Raise if the product is missing.

    Raising is the point: a multi-step script must not run its next step
    against a product the previous step failed to write.

    ``measurement`` carries what the stage actually changed, for the tools that
    modify an MS in place rather than writing a new table. For those the
    existence check is vacuous — the MS was there before the tool ran — so the
    measurement is the only real content of the row. Those scripts raise on
    their own after recording, because what counts as failure is the
    measurement, not the path.
    """
    import json
    import os
    import sqlite3
    from datetime import datetime, timezone

    exists = os.path.exists(product)
    con = sqlite3.connect(os.path.join(workdir, "analyst.db"), timeout=30, isolation_level=None)
    try:
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # another writer holds the lock while switching; the mode is persistent
        # IMMEDIATE takes the write lock up front, so a busy database waits
        # out the timeout instead of failing on a read-to-write upgrade.
        con.execute("BEGIN IMMEDIATE")
        con.execute(@DDL@)
        con.execute(
            "INSERT INTO stage_log"
            " (stage, product, at, product_exists, measurement, error, schema_version, analyst_rev)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage,
                product,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                1 if exists else 0,
                None if measurement is None else json.dumps(measurement),
                None if exists else "product not found after the step that writes it",
                1,
                os.environ.get("ANALYST_REV", "unknown"),
            ),
        )
        con.execute("COMMIT")
    finally:
        con.close()
    if not exists:
        raise RuntimeError(
            f"{stage}: expected product {product!r} does not exist; stopping "
            "rather than continuing with a missing input."
        )
'''
).replace("@DDL@", repr(STAGE_LOG_DDL))


#: Pasted into the scripts that must measure what they changed. Kept here
#: rather than copied into each generator, so the three callers cannot drift.
TABLE_PROBE_SNIPPET = '''\
def _table_colnames(path):
    """Column names of a CASA table; empty list if it cannot be opened."""
    from casatools import table as _table

    tb = _table()
    try:
        tb.open(path, nomodify=True)
        return list(tb.colnames())
    except Exception:
        return []
    finally:
        try:
            tb.close()
        except Exception:
            pass


def _table_rows(path):
    """Row count of a CASA table; 0 if it cannot be opened."""
    from casatools import table as _table

    tb = _table()
    try:
        tb.open(path, nomodify=True)
        return int(tb.nrows())
    except Exception:
        return 0
    finally:
        try:
            tb.close()
        except Exception:
            pass
'''


def _connect(workdir: str | Path) -> sqlite3.Connection | None:
    """Connection to an existing analyst.db, or None if there is none.

    Never creates the file: a reader must not make a workdir look used.
    """
    path = Path(workdir) / ANALYST_DB_NAME
    if not path.is_file():
        return None
    return sqlite3.connect(path, timeout=30)


def read_stage_log(workdir: str | Path) -> list[dict]:
    """Return the log's rows as dicts, oldest first. No database is an empty list.

    Keys: ``id``, ``stage``, ``product``, ``at``, ``exists``; ``measurement``,
    ``error``, ``superseded_by``, ``schema_version`` and ``analyst_rev`` only
    when set. Absent, not null: a null measurement would read as "measured,
    and it was nothing".

    A database that exists but cannot be read raises. Reading it as empty
    would report every stage as not run, which is a silent failure.
    """
    con = _connect(workdir)
    if con is None:
        return []
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_log'"
        ).fetchone()
        if not has_table:
            return []
        rows = con.execute(
            "SELECT id, stage, product, at, product_exists, measurement, error,"
            " superseded_by, schema_version, analyst_rev"
            " FROM stage_log ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    entries: list[dict] = []
    for (
        id_,
        stage,
        product,
        at,
        exists,
        measurement,
        error,
        superseded_by,
        schema_version,
        analyst_rev,
    ) in rows:
        entry: dict = {
            "id": id_,
            "stage": stage,
            "product": product,
            "at": at,
            "exists": bool(exists),
        }
        if measurement is not None:
            entry["measurement"] = json.loads(measurement)
        if error is not None:
            entry["error"] = error
        if superseded_by is not None:
            entry["superseded_by"] = superseded_by
        if schema_version is not None:
            entry["schema_version"] = schema_version
        if analyst_rev is not None:
            entry["analyst_rev"] = analyst_rev
        entries.append(entry)
    return entries


def _live(entry: dict) -> bool:
    return entry.get("exists") is True and not entry.get("superseded_by")


def completed_stages(entries: list[dict]) -> set[str]:
    """Stages with at least one live product recorded present.

    A stage that appears only with ``exists: false`` did not complete, and must
    not count: that row is the record of its failure. A superseded row does not
    count either: the stage has been marked for redoing.
    """
    return {str(e.get("stage")) for e in entries if _live(e) and e.get("stage") is not None}


def schema_version_of(entry: dict) -> int:
    """The envelope version one row was written with.

    A row written before schema_version existed carries no such key at all —
    that is read as version 0, not a parse failure, so a caller can refuse
    cleanly on a version it does not recognise instead of guessing at fields
    it was never taught about.
    """
    return int(entry.get("schema_version", 0))


def products_for(entries: list[dict], stage: str) -> list[str]:
    """Live products recorded present for one stage, oldest first, de-duplicated.

    A retry adds a row rather than overwriting, so the same product can appear
    more than once; the caller wants the set of paths, not the attempt count.
    """
    seen: list[str] = []
    for e in entries:
        if e.get("stage") == stage and _live(e):
            product = e.get("product")
            if isinstance(product, str) and product not in seen:
                seen.append(product)
    return seen


def supersede_stages(workdir: str | Path, stages: list[str], by: str) -> int:
    """Mark every live row of ``stages`` as superseded. Returns rows marked.

    For redoing a stage and everything downstream of it: the rows stay, so the
    history of what ran is kept, but ``completed_stages`` stops counting them.
    ``by`` says why, e.g. "rerun of initial_bandpass". Which stages are
    downstream is the caller's decision; this module holds no stage order.
    """
    if not stages:
        return 0
    if not by:
        raise ValueError("supersede_stages needs a non-empty reason in `by`.")
    con = _connect(workdir)
    if con is None:
        return 0
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in stages)
        cur = con.execute(
            f"UPDATE stage_log SET superseded_by = ? "  # noqa: S608 - placeholders only
            f"WHERE superseded_by IS NULL AND stage IN ({placeholders})",
            (by, *stages),
        )
        con.execute("COMMIT")
        return cur.rowcount
    finally:
        con.close()


# The in-process path (execute=True) bypasses the generated script entirely, so
# it needs the same write or a tool run that way records nothing. Rather than
# keep a second implementation that can drift from the snippet, execute the
# snippet and take the function it defines: one definition, two callers.
_snippet_ns: dict = {}
exec(RECORD_STAGE_SNIPPET, _snippet_ns)  # noqa: S102 - our own source, defined above

#: ``record_stage(workdir, stage, product)`` — identical to what the generated
#: scripts call, because it IS what they call.
record_stage = _snippet_ns["_record_stage"]
