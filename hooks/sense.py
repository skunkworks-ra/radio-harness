#!/usr/bin/env python3
"""
sense.py — Mechanism A's PostToolUse hook body.

Runs the instant `radio-interferometry-driver` or `stage-orchestration` is
read (see hooks.json). Calls `ms_workflow_status` directly — the same call
`analyst_driver.loop.Loop.sense()` makes — and injects its result as
`additionalContext`, so the model has current, real state the moment the
skill loads instead of depending on remembering to call the tool itself.
Also records the sense in `sense_log` (via `ms_inspect.util.sense_log`) so
the PreToolUse write-gate hook (`gate.py`) can check it happened.

A failed or skipped sense must never break the turn: on any problem this
prints nothing and exits 0, which Claude Code reads as "no hook output,
proceed normally" — the model falls back to calling the tool itself, exactly
today's un-enforced behavior, not a hard failure.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def _discover_ms_path(workdir: str) -> str | None:
    """Env var if the driver set one; otherwise the one non-calibrators MS.

    `ClaudeBackend` sets `ANALYST_MS_PATH` once a run has an MS (backends.py).
    The glob fallback remains for callers that don't set it (an older backend,
    or before import, when there is no MS yet) and degrades to None rather
    than guessing when a workdir holds more than one candidate.
    """
    env_path = os.environ.get("ANALYST_MS_PATH")
    if env_path:
        return env_path
    candidates = [
        p
        for p in Path(workdir).glob("*.ms")
        if p.name != "calibrators.ms" and (p / "table.info").exists()
    ]
    return str(candidates[0]) if len(candidates) == 1 else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    workdir = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id")
    if not session_id:
        return 0

    ms_path = _discover_ms_path(workdir)
    if not ms_path:
        return 0  # nothing to sense yet (e.g. before import) — allow silently

    try:
        from ms_inspect.tools import workflow_status
        from ms_inspect.util.sense_log import record_sense

        result = workflow_status.run(ms_path, workdir)
    except Exception:
        return 0  # a failed sense must not break the turn — see module docstring

    data = result.get("data", result) if isinstance(result, dict) else {}
    next_step = data.get("next_recommended_step", "unknown")

    # The injection below is the important half; a logging failure isn't fatal.
    with contextlib.suppress(Exception):
        record_sense(workdir, session_id, next_step)

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": json.dumps(result),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
