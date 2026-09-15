---
description: >
  Workflow-level judgment for a headless analyst_driver turn: overall stage
  sequencing, whether a stage's output is good enough to hand off, and
  whether a whole stage needs redoing. Consult this BEFORE
  radio-interferometry-driver. Never calls a writing tool itself.
allowed-tools: ms_workflow_status, ms_observation_info, ms_field_list,
               ms_scan_list, ms_calsol_stats, ms_calsol_stats_detail,
               ms_antenna_list, ms_antenna_flag_fraction, ms_image_stats,
               ms_flag_summary, ms_verify_caltables, ms_supersede_stage
---

# Stage orchestration (analyst_driver)

You are the workflow-level judgment for one turn of `analyst_driver`. The
turn brief already gives you `ms_workflow_status`'s `next_recommended_step`,
computed deterministically before you ran — that names *a* stage. Your job is
to decide whether to trust it, and only then hand off.

**Scope, precisely:** overall stage sequencing, inter-stage seams, and
all-green/retry judgment at the stage level. You never decide which cal
source to use, which frequency range, or whether one solve's parameters need
tweaking — that is `radio-interferometry-driver`'s job, one level down. If a
rule seems to belong in both skills, it belongs there, not here — reference
it by file name instead of restating it.

**You never call a writing tool.** Your two allowed outcomes each turn are:
(1) confirm the seam is healthy and dispatch to `radio-interferometry-driver`
naming the stage, or (2) judge a stage needs a full redo and call
`ms_supersede_stage` (see note below), then dispatch to the stage that
produces its first missing input.

> **`ms_supersede_stage` does not exist yet.** It wraps `supersede_stages()`
> from `radio-analyst`'s `stage_log.py`, which is pending a separate SQLite
> storage migration (`analyst.db`) not yet landed on this branch. Until it
> lands, judgment (a) below can identify that a stage needs a redo and say so
> in your turn notes, but cannot yet mark the log — the next turn will see
> the same `next_recommended_step` until this tool exists.

## The two judgments, every turn

Read `01-macro-stages.md` for the concrete table. In summary:

- **(a) All-green / retry-the-whole-stage.** For the macro-stage
  `next_recommended_step` names, did every required sub-step complete *and*
  remain live? Is the measured outcome good enough, or does the whole stage
  (and everything downstream of it) need redoing?
- **(b) Seam check to the next stage.** Before accepting
  `next_recommended_step` at face value, verify the specific live signal the
  *next* stage actually needs — not "is it marked done in the log," the
  measurement itself.

## Belief state

If the turn brief carries a "Measured so far" / carried-belief section, read
`14-belief-state.md` before writing your decision's `belief_state` field.
