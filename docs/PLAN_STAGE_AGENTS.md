# Stage agents: one small agent per calibration stage, under an orchestrator

Status: draft, 2026-09-24. Discussed, nothing decided, nothing built.
Builds on `docs/PLAN_NATIVE_HARNESS.md` (the driver as built) and
`PLAN_STRUCTURED_STATE.md` (harness-measured state, rerun). Input for the
stage cards comes from the capture tool (`src/analyst_driver/capture.py`,
`b76b93a`).

## 1. The idea

Today one turn is one fresh model call that advances one stage and ends with
`submit_decision` (`PLAN.md` "The turn"). The model in that turn sees
everything: 79 tools and a 27,444-character system prompt in the smoke
capture (`/var/mnt/fast/harness_capture/smoke_report.md`), plus both skill
bodies.

Split it in two layers:

| Layer | Knows | Decides |
|---|---|---|
| Orchestrator | whole-dataset state in SQLite, the stage graph, the goal | which stage runs next, reruns, going back a stage, fan-out |
| Stage agent | one stage card, one slice of state | the parameters of that one stage (solint, refant, combine, ...) |

A stage agent is what a person does when they sit down to "do the
bandpass": they read one section of the cookbook, look at a few numbers, and
make one call.

## 2. The stage card

One file per stage. It is data, not code, so a new stage is a new card.

| Field | Content | Example: bandpass |
|---|---|---|
| `skill` | the skill sections this stage needs, by file and heading | `07-calibration-execution.md` "Step 3 — Bandpass calibration (B)", "solint guidance", "Caltable solution flagging" |
| `tools` | the tools the agent may see | `ms_bandpass`, `ms_calsol_stats`, `ms_calsol_stats_detail`, `ms_refant`, `ms_gaincal_snr_predict` |
| `inputs` | facts the harness pulls from SQLite before the call | refant ranking, bandpass calibrator field, prior caltables (G0, K) and their stats, flag fraction per antenna |
| `output` | a stage-specific `submit_decision` schema | `caltable`, `solint`, `combine`, `refant`, `minsnr`, `gaintable`, `cited`, `notes` |
| `posterior` | measurement tools the harness runs after the job, results into SQLite | `ms_calsol_stats` on the new table |

The `output` fields for bandpass follow `BandpassInput` in
`src/ms_modify/server.py` (around line 1214). The card does not copy the
schema; the harness reads it from the tool.

## 3. What this resolves

`PLAN.md` dropped the stage-to-tool table (the "leash") because recovery
sometimes means going back a stage, and a table built from a linear playbook
would refuse that. With stage agents, going back is the orchestrator
launching an earlier stage's card. Restricting tools inside one card is then
safe.

The tools-measure contract holds. Tools measure. The stage agent proposes.
Anything that gates (proceed, rerun, go back) lives in the orchestrator,
which is the skill layer.

## 4. Scale

- Model calls are cheap and fast. CASA jobs are the bottleneck.
- Parallelism comes from two places: across datasets (`run --all` already),
  and independent branches inside one dataset (per-SpW solves, per-field
  imaging, polcal beside imaging after applycal). So the ladder becomes a
  graph of stages.
- Hard constraint: CASA table locks. One writer per MS at a time. Parallel
  branches either work on split MSs or write caltables only.
- Rerun with downstream invalidation (planned) becomes "invalidate this
  node's descendants".

## 5. The hypothesis: who orchestrates

Open question, raised 2026-09-24, and the owner has no answer yet. So test
it.

Should the orchestrator be a model from the start, or deterministic code
(`ms_workflow_status` plus the loop), so that only the stage agents vary?

The choice decides what an experiment measures. Two factors, two levels
each:

| | Stage agent: frontier | Stage agent: small |
|---|---|---|
| **Orchestrator: code** | A. reference run | B. small models per stage, nothing else varies |
| **Orchestrator: model** | C. orchestration alone, with good workers | D. the full system |

- B against A: how well small models do one stage, with the order of
  stages fixed.
- C against A: what a model orchestrator adds or breaks (reruns, going
  back, recovery), with reliable workers.
- D: whether the two errors compound or one layer covers for the other.

Stated as hypotheses, to be measured, not assumed:

- H1. With narrow cards, small models reach frontier quality on at least
  some stages (per-stage scorecards, B against A).
- H2. A deterministic orchestrator is enough for a standard dataset (3C391),
  and a model orchestrator only pays off on recovery (C against A on a
  dataset with a known problem).
- H3. A narrow card beats the full context for a small model on the same
  stage (B against today's single-turn harness, same model, same frozen
  state).

H3 is the cheapest and should go first.

## 6. Measurement

- Frozen-state sampling (`harness.md` §7 step 4): rebuild the state before a
  stage, run one card N times, grade offline against the skill's checks.
- Grade with posterior measurements already in the tools (`ms_calsol_stats`
  flagged fraction and SNR, `ms_image_stats` dynamic range), stored with
  their inputs. The grade is computed offline, never fed back into the run.
- Record per card: model, rounds, rejections, tokens, the decision, the
  posterior numbers.

## 7. Order of work (estimates, not measured)

1. Read the overnight 3C391 Sonnet capture. Tag each request by stage (the
   capture schema has no stage field yet). About half a day.
2. From that, write two cards by hand: gaincal and bandpass. About a day.
3. Frozen-state sampling for those two stages. About a day (shared with
   `harness.md` §7 step 4).
4. H3: card against full context, 20 samples each, 2 to 3 Jetstream models.
5. Arm B for the two stages. Then decide whether to build the graph and a
   model orchestrator (arms C and D).

## 8. Open

- Where cards live: `skills/stage-cards/` beside the skills, or in the
  harness package.
- Whether a card's `skill` field cites headings (fragile if a heading is
  renamed) or line ranges (fragile on any edit). A test that every cited
  heading exists would catch the first.
- How the orchestrator model is given state: the same SQLite snapshots, or a
  summary of stage outcomes only.
- What "frontier" means for arm A and C: Sonnet, Opus, or both.
