#!/usr/bin/env python3
"""
gate.py — Mechanism B's PreToolUse hook body.

Runs before any `ms-modify`/`ms-create` writing tool call (see hooks.json's
`mcp__ms-modify__.*`/`mcp__ms-create__.*` matchers). Denies the call unless
`sense.py` has already recorded a sense for this exact session — real
enforcement, not a skill-file suggestion: a denied call does not execute.

On any problem reading the request, this allows rather than denies (prints
nothing, exits 0) — a broken gate must fail open, not block every write tool
forever.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    workdir = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id")
    if not session_id:
        return 0

    try:
        from ms_inspect.util.sense_log import has_sensed

        sensed = has_sensed(workdir, session_id)
    except Exception:
        return 0  # a broken gate fails open — see module docstring

    if sensed:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "No ms_workflow_status sensing recorded for this turn. "
                        "Read the stage-orchestration skill first — it senses "
                        "automatically when loaded — before calling a writing tool."
                    ),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
