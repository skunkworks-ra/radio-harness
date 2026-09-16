"""
tools/supersede_stage.py — ms_supersede_stage

Marks every live stage_log row for the named stages as superseded, so
ms_workflow_status stops counting them as done and next_recommended_step
re-offers the first stage that needs to run again.

The underlying supersede_stages() primitive lives in ms_inspect.util.stage_log,
alongside the log it mutates. This tool is a thin validating wrapper — the
row-marking logic and its tests live there.

Which stages are downstream of the one being redone is the caller's
judgment (the stage-orchestration skill), not this tool's — it holds no
notion of stage order.
"""

from __future__ import annotations

from pathlib import Path

from ms_inspect.util.formatting import field, response_envelope
from ms_inspect.util.stage_log import supersede_stages

TOOL_NAME = "ms_supersede_stage"


def run(workdir: str, stages: list[str], by: str) -> dict:
    wd = Path(workdir)
    if not wd.is_dir():
        from ms_inspect.exceptions import ComputationError

        raise ComputationError(f"workdir does not exist: {workdir}", ms_path=workdir)
    if not stages:
        from ms_inspect.exceptions import ComputationError

        raise ComputationError("supersede_stage needs at least one stage name.", ms_path=workdir)
    if not by:
        from ms_inspect.exceptions import ComputationError

        raise ComputationError("supersede_stage needs a non-empty reason in `by`.", ms_path=workdir)

    rows_marked = supersede_stages(wd, stages, by)

    return response_envelope(
        tool_name=TOOL_NAME,
        ms_path=workdir,
        data={
            "stages": field(stages),
            "by": field(by),
            "rows_marked": field(rows_marked),
        },
        casa_calls=[f"supersede_stages({stages!r}, by={by!r}) → {wd / 'analyst.db'}"],
    )
