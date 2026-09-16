"""
reduction_log.py — ms_reduction_log

A place to shuttle KNOWN-GOOD calls as a reduction proceeds. After a tool call
succeeds, append it here with the exact parameters that worked, the salient
output, and why it was done. The accumulated log is the replayable "working
path" through the data — the canonical recipe for this dataset, and the
artifact a cheaper model (or a future run) can replay step by step.

Only validated calls should be shuttled in: failed attempts and dead ends stay
out, so the ledger is the clean path, not the search for it.

Storage is the `reduction_log` table in `<workdir>/analyst.db` (stdlib
sqlite3), the same database as the stage log. Rows are only ever inserted.

Actions:
  append  — record one working call (tool, params, outputs, rationale, rule)
  render  — emit the ordered recipe and a replay Python script
  list    — compact summary (step, tool, rationale) of the recorded path
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ms_inspect.util.formatting import field as fmt_field
from ms_inspect.util.formatting import response_envelope
from ms_inspect.util.stage_log import ANALYST_DB_NAME

TOOL_NAME = "ms_reduction_log"

_DDL = (
    "CREATE TABLE IF NOT EXISTS reduction_log ("
    "step INTEGER PRIMARY KEY, "
    "ts TEXT NOT NULL, "
    "tool TEXT NOT NULL, "
    "params TEXT NOT NULL, "
    "outputs TEXT NOT NULL, "
    "rationale TEXT NOT NULL, "
    "skill_rule TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "supersedes TEXT)"
)


def _log_path(workdir: str) -> Path:
    return Path(workdir) / ANALYST_DB_NAME


def _read_records(path: Path) -> list[dict]:
    """Rows as dicts, in step order. ``supersedes`` only when set."""
    if not path.is_file():
        return []
    con = sqlite3.connect(path, timeout=30)
    try:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reduction_log'"
        ).fetchone():
            return []
        rows = con.execute(
            "SELECT step, ts, tool, params, outputs, rationale, skill_rule, status, supersedes"
            " FROM reduction_log ORDER BY step"
        ).fetchall()
    finally:
        con.close()
    records: list[dict] = []
    for step, ts, tool, params, outputs, rationale, skill_rule, status, supersedes in rows:
        record = {
            "step": step,
            "ts": ts,
            "tool": tool,
            "params": json.loads(params),
            "outputs": json.loads(outputs),
            "rationale": rationale,
            "skill_rule": skill_rule,
            "status": status,
        }
        if supersedes is not None:
            record["supersedes"] = supersedes
        records.append(record)
    return records


def _append_record(path: Path, record: dict) -> int:
    """Insert one row; return its step number."""
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        # Another writer may hold the lock while switching; the mode is persistent.
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN IMMEDIATE")
        con.execute(_DDL)
        cur = con.execute(
            "INSERT INTO reduction_log"
            " (ts, tool, params, outputs, rationale, skill_rule, status, supersedes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["ts"],
                record["tool"],
                json.dumps(record["params"], default=str),
                json.dumps(record["outputs"], default=str),
                record["rationale"],
                record["skill_rule"],
                record["status"],
                record.get("supersedes"),
            ),
        )
        con.execute("COMMIT")
        return int(cur.lastrowid)
    finally:
        con.close()


# Maps the recorded tool name to the package module whose run() implements it.
# Steps whose tool name is not in this registry are emitted as MANUAL markers
# (e.g. ad-hoc casatasks bypasses), so the script is honest about what it can
# and cannot replay automatically.
_RUN_REGISTRY: dict[str, str] = {
    "ms_sdm_summary": "ms_create.sdm_summary",
    "ms_import_asdm": "ms_create.import_asdm",
    # NOTE: ms_set_intents is intentionally NOT registered — its entrypoint is
    # set_intents(), not run(), so it cannot be replayed via the run() convention
    # and is emitted as a MANUAL step. It is also a rare one-off (only for MSs
    # that lack scan intents).
    "ms_apply_preflag": "ms_modify.preflag",
    "ms_generate_priorcals": "ms_modify.priorcals",
    "ms_initial_bandpass": "ms_modify.initial_bandpass",
    "ms_apply_initial_rflag": "ms_modify.initial_rflag",
    "ms_setjy": "ms_modify.setjy",
    "ms_setjy_polcal": "ms_modify.setjy_polcal",
    "ms_gaincal": "ms_modify.gaincal",
    "ms_bandpass": "ms_modify.bandpass",
    "ms_fluxscale": "ms_modify.fluxscale",
    "ms_polcal": "ms_modify.polcal",
    "ms_applycal": "ms_modify.applycal",
    "ms_apply_rflag": "ms_modify.rflag",
    "ms_flag_caltable": "ms_modify.flag_caltable",
    "ms_tclean": "ms_modify.tclean",
}


def _replay_script(records: list[dict]) -> str:
    """
    Render an EXECUTABLE replay of the recorded working calls.

    Each recognised step becomes ``importlib.import_module(mod).run(**params)``.
    Faithful replay requires the appended params to be the literal working
    kwargs (absolute paths, full gaintable lists) — abbreviated/placeholder
    params will not run as-is.
    """
    lines = [
        "#!/usr/bin/env python",
        '"""',
        "Auto-generated by ms_reduction_log render — executable replay of the",
        "working path. Run inside the project environment (pixi run python ...).",
        "Review before running: paths and selections are dataset-specific.",
        '"""',
        "import importlib",
        "",
    ]
    for r in records:
        tool = r.get("tool", "?")
        params = r.get("params", {})
        rationale = r.get("rationale")
        if rationale:
            lines.append(f"# step {r.get('step')}: {rationale}")
        mod = _RUN_REGISTRY.get(tool)
        if mod is None:
            lines.append(f"# MANUAL STEP — no run() mapping for {tool!r}:")
            lines.append(f"#   params = {params!r}")
        else:
            lines.append(f"importlib.import_module({mod!r}).run(")
            for k, v in params.items():
                lines.append(f"    {k}={v!r},")
            lines.append(")")
        lines.append("")
    return "\n".join(lines)


def run(
    action: str,
    workdir: str,
    tool: str = "",
    params: dict | None = None,
    outputs: dict | None = None,
    rationale: str = "",
    skill_rule: str = "",
    status: str = "ok",
) -> dict:
    """
    Append to / render / list the reduction working-calls ledger.

    Args:
        action:     'append', 'render', or 'list'.
        workdir:    Directory holding (or to hold) analyst.db.
        tool:       (append) name of the tool/call that worked, e.g. 'ms_gaincal'.
        params:     (append) exact parameters that worked.
        outputs:    (append) salient outputs worth recording (paths, key numbers).
        rationale:  (append) why this step was done.
        skill_rule: (append) skill file / threshold cited, e.g. '07 Step 3'.
        status:     (append) outcome tag; default 'ok'. Only shuttle working calls.

    Returns:
        Standard envelope. append → n_records; render → recipe + replay_script;
        list → compact step/tool/rationale summary.
    """
    wd = Path(workdir)
    if not wd.is_dir():
        from ms_inspect.exceptions import ComputationError

        raise ComputationError(f"workdir does not exist: {workdir}", ms_path=workdir)

    path = _log_path(workdir)

    if action == "append":
        record = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool,
            "params": params or {},
            "outputs": outputs or {},
            "rationale": rationale,
            "skill_rule": skill_rule,
            "status": status,
        }
        step = _append_record(path, record)
        return response_envelope(
            tool_name=TOOL_NAME,
            ms_path=workdir,
            data={
                "action": "append",
                "log_path": fmt_field(str(path)),
                "step_recorded": fmt_field(step),
                "n_records": fmt_field(len(_read_records(path))),
            },
            casa_calls=[f"append → {path}"],
        )

    records = _read_records(path)

    if action == "list":
        summary = [
            {
                "step": r.get("step"),
                "tool": r.get("tool"),
                "rationale": r.get("rationale"),
                "skill_rule": r.get("skill_rule"),
            }
            for r in records
        ]
        return response_envelope(
            tool_name=TOOL_NAME,
            ms_path=workdir,
            data={"action": "list", "n_records": fmt_field(len(records)), "steps": summary},
            casa_calls=[f"read → {path}"],
        )

    if action == "render":
        script = _replay_script(records)
        script_path = wd / "reduction_replay.py"
        script_path.write_text(script)
        return response_envelope(
            tool_name=TOOL_NAME,
            ms_path=workdir,
            data={
                "action": "render",
                "n_records": fmt_field(len(records)),
                "recipe": fmt_field(records),
                "replay_script": fmt_field(str(script_path)),
            },
            casa_calls=[f"read → {path}", f"write → {script_path}"],
        )

    from ms_inspect.exceptions import ComputationError

    raise ComputationError(
        f"Unknown action '{action}'; use 'append', 'render', or 'list'.",
        ms_path=workdir,
    )
