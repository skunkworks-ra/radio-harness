# 01 — Macro stages: seam checks and retry judgment

One row per macro-stage. "Sub-steps" are `ms_workflow_status`'s own
per-tool-call stage names (`stages_completed`, `products_recorded`) — several
map to one macro-stage. "Retry-worthy" is judgment (a): a measured outcome
bad enough that the whole macro-stage, not just one sub-step, needs redoing.
"Seam check" is judgment (b): what to verify before trusting
`next_recommended_step`'s move to the *next* row.

| Macro-stage | Sub-steps (`ms_workflow_status` names) | Retry-worthy signal | Seam check for the next stage |
|---|---|---|---|
| Import | `import_asdm` | MS reports invalid after import (probe fails, not just "not yet imported") | MS opens (`ms_observation_info` succeeds) |
| Intents | `set_intents` | `intents_populated` still false after the stage is logged done | `ms_field_list`/`ms_scan_intent_summary` show non-empty intents, not just `intents_populated=true` |
| Precal | `preflag`, `priorcals`, `initial_bandpass`, `initial_rflag`, `applycal` (on `calibrators.ms`) | `ms_flag_summary`/`ms_antenna_flag_fraction` on `calibrators.ms` shows catastrophic flagging after `initial_rflag`+`applycal` — a known real failure mode is an all-field residual rflag driving flagged fraction from single digits to ~90%. Any jump of that order after this stage is retry-worthy, not tunable within the stage. | `corrected_populated_calibrators=true` **and** `ms_antenna_flag_fraction` on `calibrators.ms` is not catastrophic (rough bar: worse than ~2x the pre-rflag fraction is suspect, not just "some flagging happened") |
| Calibration | `gaincal`, `bandpass`, `fluxscale` (`ms_workflow_status`'s own `_FINAL_SOLVES` grouping), then `applycal` (target) | Either: (i) `ms_calsol_stats`/`ms_calsol_stats_detail` on a final-solve caltable shows near-total flagged solutions, or (ii) an antenna-survival check across the stacked cal tables (`ms_antenna_list` for array total N, `ms_calsol_stats` per table for antennas lost, intersect) drops below roughly half the array — applying calibration under that condition collapses the usable antenna set regardless of `applymode`. | `corrected_populated_target=true` — the target MS's own `CORRECTED_DATA`, not the calibrators' |
| Polcal (if in scope) | polcal-specific solves (Kcross/D-terms/Xf) | D-term or cross-hand solve quality poor per `ms_calsol_stats_detail` | Polcal caltables present and applied before an IQUV image is attempted |
| Imaging | `tclean` (first pass) | `ms_image_stats` shows a pathological peak/DR **for the source class** — a resolved or genuinely low-surface-brightness target legitimately has low peak/DR; do not treat that alone as retry-worthy without checking whether the target is expected to be extended/resolved first | Image exists with `ms_image_stats` reporting real (non-`UNAVAILABLE`) numbers |
| Selfcal | one-pass phase selfcal | Dynamic range did not improve vs. the pre-selfcal image (before/after comparison, not just "selfcal ran") | Selfcal solutions applied, `CORRECTED_DATA` reflects the update |
| Postcal flagging | SpW severity triage on target/phase cal | N/A — terminal judgment is drop-vs-salvage per SpW, not a whole-stage redo | None — `next_recommended_step` reports `selfcal_or_done`; only you can say which, per the turn brief's own note that the tool cannot tell the two apart |

## Notes

- The "retry-worthy" thresholds above are the ones already validated by real
  incidents in this project — they are starting points, not tuned
  statistical cutoffs. Where a row says "roughly" or gives a ratio rather
  than an exact number, use judgment against the specific data, and say so in
  your turn's notes rather than picking a number silently.
- A macro-stage with multiple sub-steps (Precal, Calibration) is exactly
  where judgment (a) matters: `ms_workflow_status`'s `stages_completed` can
  show every sub-step present while the *measured* outcome is still bad
  (e.g. every final solve logged, but one caltable is 95% flagged). Do not
  treat "all sub-steps logged" as equivalent to "stage passed."
