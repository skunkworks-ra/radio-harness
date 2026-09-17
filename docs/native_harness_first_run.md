# Native harness — first real run (2026-09-17)

Status: partial. TACC/Qwen3 leg run to completion (model-declared, not
verified-complete). Anthropic comparison leg not yet run.

Dataset: `3c391_ctm_mosaic_10s_spw0.ms` — fresh download from
`casa.nrao.edu/Data/EVLA/3C391/`, never reduced before this run, extracted to
`/var/mnt/fast/datasets/3c391_fresh/`. Run root:
`/var/mnt/fast/datasets/3c391_run/`.

Backend: `provider=openai`, `base_url=https://ai.tejas.tacc.utexas.edu/v1`,
`model=Qwen3-235B-A22B-Instruct-2507`. Chosen after probing all 10 models on
the endpoint against the real 55-tool-schema payload
(`probe_tacc_models.py`, kept in the run dir) — `Qwen3-32B` and
`Mistral-Large-3-675B-Instruct-2512` do not fit (32,768-token ceiling on this
deployment; payload needs ~37–40k). `E5-Mistral-7B-Instruct` is an embedding
model, no tool-calling support at all.

## Three real driver bugs found and fixed during this run

1. **Missing workdir was a live failure mode.** `--workdir` was never
   `mkdir`'d before the first `analyst-driver run`, and nothing in the CLI
   created it. Every `ms_modify` tool call failed with a raw
   `FileNotFoundError` trying to write its script, for 37 consecutive turns
   (`set_intents`, ordinals 1–37), burning up to 104,522 input tokens per
   turn on the last several attempts before being caught. Fixed:
   `cli.py::_resolve_run` now creates the workdir on `run` registration.
   Test: `test_run_creates_a_missing_workdir`.

2. **`run_all` hot-looped on a permanent backend error.** A
   `ContextWindowExceededError` (or any `result.error`) was classified the
   same as ordinary progress, so the outer loop retried with zero backoff —
   observed as a 400 every ~1 second. Fixed: `turn_failed` now carries
   `backend_error: bool`; `run_all` treats a backend-error failure like
   "waiting" and sleeps `poll_interval` between attempts. Tests:
   `test_step_marks_a_backend_error_turn_failure_distinctly`,
   `test_run_all_backs_off_a_repeated_backend_error_instead_of_hot_looping`.

3. **R2 (one script-tool call per turn) counted failed attempts.** A script
   tool that returned its own error envelope (e.g. `ms_apply_initial_rflag`
   refusing for missing `MODEL_DATA`) still consumed the turn's one-script
   budget, so the model's follow-up fix call (`ms_setjy`) was R2-rejected in
   the same turn — even though nothing had been written to disk. The model
   was then forced to name a script that never existed, for 4 turns running
   (43, 45, 46, 47) before self-recovering across turn boundaries. Fixed:
   `tools.py::_call_mcp` only counts the call against the budget when the
   tool's own envelope does not report `status: "error"`. Test:
   `test_a_failed_script_call_does_not_consume_the_turns_script_budget`.

Separately, not a driver bug: `ms_initial_bandpass`'s SpW-coverage guardrail
silently swallowed an unresolved `target_fields` token (the model correctly
named the science-target mosaic fields, but the check ran against the
calibrator-only MS split, which never had them) and returned no warning at
all — a real violation of this repo's "silence is never used to indicate
failure" contract. Fixed in `spw_coverage.py`; unresolved tokens now produce
an explicit warning naming the field and the reason. Test:
`test_unresolved_target_field_warns_instead_of_silently_skipping`.

## Model-behavior finding — not a driver bug

Turns 52–54 (macro-stage `delay_bandpass_gain`) repeated the identical
initial-phase-only `ms_gaincal` call (`calmode="p"`, output
`initial_phase.G0`) three times running, never proceeding to `ms_bandpass`
or the final gain/fluxscale/applycal sequence described in
`07-calibration-execution.md`. Turn 54 then called `submit_decision(done=true,
notes="all calibration steps are complete")`.

Verified against ground truth (`ms_workflow_status`, not the model's claim):
false. `final_solves_completed: ["gaincal"]` only — `bandpass` and
`fluxscale` never ran; `next_recommended_step` was still
`"delay_bandpass_gain"`. The model had this exact information in its own
brief (the status JSON) and still declared done.

This is not a code defect in the sense the three bugs above are — the
information needed to know the reduction was incomplete was present and
correct. It is a real, recorded instance of premature/incorrect
self-reported completion by `Qwen3-235B-A22B-Instruct-2507`, exactly the
class of failure this harness exists to measure. Logged here rather than
patched, per the driver's own design principle: the driver measures and
records, it does not gate or second-guess a `done` declaration
(`DESIGN_NATIVE_HARNESS.md` P5/P9; `PLAN.md` "What the driver checks").

Run status: `stopped` (model-declared, not goal-complete). Not resumed as-is
— a fresh run was started instead (see below) now that the three bugs above
are fixed, to get a clean comparison sample.

## Open, for the decision-sampling plan (separate, approved, not yet built)

This run is a natural first candidate for a frozen-turn replay once the
`sample_sets`/`samples` schema exists: turn 53 or 54's exact stored brief,
replayed k times against Qwen3-235B, would directly measure whether the
premature-`done` call above is a one-off sample or a systematic tendency of
this model at this stage.
