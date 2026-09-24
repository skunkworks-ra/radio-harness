# What the model is given: Claude Code versus the harness

Written 2026-09-23, from the 3C391 Jetstream runs (`/var/mnt/fast/harness_jetstream`)
and the code at `776b337` plus the uncommitted B2 change.

Question: how do we present the harness model the same information a person
gives Claude Code when they point it at a Measurement Set and say "go"?

## 1. What Claude Code gets

Listed from what was in context in the session that wrote this file.

| # | Layer | Content | When |
|---|---|---|---|
| 1 | Harness instructions | tool use, safety rules, environment (working directory, git status, date) | always |
| 2 | Global `CLAUDE.md` | how the owner works: evidence before claims, style, commit rules | always, full text |
| 3 | Project `CLAUDE.md` | the contract ("tools measure, the skill reasons"), repo layout, tool inventory, error codes, conventions | always, full text |
| 4 | Memory | `MEMORY.md` index plus recalled notes (where the runs live, design decisions) | always, plus recall |
| 5 | Skill index | each skill's name and one-line description only | always |
| 6 | Skill bodies | full text, when the `Skill` tool is called | on demand |
| 7 | Tools | Read, Bash, Grep across the machine; the MCP tools; subagents | always |
| 8 | The request | the goal in the owner's words, with constraints ("read the code first") | each turn |
| 9 | The conversation | everything so far, persistent, compacted when long | always |
| 10 | The owner | corrections, approvals, answers | any time |

## 2. What the harness model gets per turn

| # | Present? | Where |
|---|---|---|
| 1 | a 7-line preamble | `skills.py:20-28` |
| 2 | no | |
| 3 | no; fragments survive inside the two `SKILL.md` bodies | |
| 4 | no; belief state is the per-run analogue and is off | `PLAN_BELIEF_STATE.md` |
| 5 | no; frontmatter `description` is stripped | `skills.py:31-39` |
| 6 | both `SKILL.md` bodies always pasted in; 18 sub-files (3,950 lines at `592ebd8`) through `read_file` | `skills.py:55-63` |
| 7 | every tool the three MCP servers list (53 in r3) plus `read_file` and `submit_decision`; `read_file` confined to the skill root, workdir and run root; no code or docs | `tools.py:210-236`, `tools.py:404-405`, `cli.py:129` |
| 8 | a per-turn brief: input path, MS, workdir, telescope, free space, a one-line scope, optional belief state, the full `ms_workflow_status` JSON, the previous turn, ordered instructions. No overall goal beyond the scope line | `loop.py:222-240` |
| 9 | none; history starts fresh every turn | `agent.py:44` |
| 10 | none; `blocked` ends the run | `loop.py:540` |

What that looked like in practice (muse-glimmer, calibration run, 14 turns):

- `01-macro-stages.md` read 15 times, `14-belief-state.md` 14 times (belief
  state was off; the skill says read it only when the brief carries it),
  `07-calibration-execution.md` 5, `10-precal-workflow.md` 3.
- Turn 6, the rflag turn, never opened `10-precal-workflow.md`. The correct
  recovery came from the rflag tool's refusal message, not from the skill.
- The key fact for that turn (`setjy` recorded `model_data: false`) was
  already in `analyst.db` before the turn started. The brief shows stage
  names only, not those recorded values.

## 2b. Measured: what Claude Code got in capture r3 (2026-09-24)

Section 1 was listed from a development session, not a reduction. This is
the recording of a reduction: Sonnet, full 3C391 prompt, captured by
`analyst_driver.capture` at `f9c7368`.

- Run: `/var/mnt/fast/harness_capture/3c391_sonnet_r3` (`capture.db`, capture 1).
- Flags: `--plugin-dir <repo> --setting-sources project --add-dir <repo>/skills <run>/ms
  --allowedTools Skill mcp__ms-*__*`, cwd `<run>/work`, `ANALYST_MS_PATH` set, hooks on.
- Valid through event 626 (precal, calibration, post-cal flagging, imaging setup).
  Imaging broke on Claude Code's 1800 s MCP idle cutoff
  (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`); no restored image.
- 88 requests (79 to the model, 9 `count_tokens`), 103 tool calls, 87 of them MCP.
- Every input, per stage, as text: `<run>/r3_inputs_by_stage.txt`, written by `<run>/dump_stages.py`.

The harness column is from section 2, re-verified against code at `592ebd8`
(line refs and rows 7 and 8 corrected there).

| # | Layer | Claude Code (r3, measured) | Harness per turn (section 2) | Gap |
|---|---|---|---|---|
| 1 | Instructions | 27,453-char coding-agent system prompt, environment block (cwd, added dirs, model, date), subagent list | 7-line preamble | CC's is mostly generic coding text |
| 2 | Global `CLAUDE.md` | absent | absent | none |
| 3 | Project `CLAUDE.md` | absent (cwd outside repo: the plugin-user view) | fragments in the `SKILL.md` bodies | about even |
| 4 | Memory | auto-memory instructions only, memory dir empty | none | negligible |
| 5 | Skill index | plugin skills listed by name only, no description | description stripped | none |
| 6 | Skill bodies | model pulls them: 2 `Skill` calls, 6 sub-files read once each (01-workflow, 01-macro-stages, 07, 10, 11, 13), kept in context | both `SKILL.md` bodies pasted every turn; sub-files via `read_file`, re-read every turn | once and kept vs. every turn |
| 7 | Tools | 79 schemas: 26 builtin (82,961 chars) + 53 MCP (78,889 chars) | about 50 CASA tools + `read_file` | CC has Bash and Read anywhere; 16 non-MCP calls in r3 |
| 8 | Request | one prompt for the whole reduction, "make the choices yourself" | per-turn brief with measured state, previous turn, and a one-line scope | harness gives more state per turn, less goal |
| 9 | Conversation | persistent across 79 model requests (plus 9 `count_tokens` calls); three compactions, at requests 25, 59 and 77 (at 165,900, 165,013 and 128,868 cached tokens): each a summary of about 13k tokens, then a restart at 73.6k cached tokens with the summary and the recently read skill files re-attached | fresh every turn | largest gap |
| 10 | Owner | none (prompt said unavailable) | none; `blocked` ends the run | none |
| 11 | Hooks | `sense.sh` injects `ms_workflow_status` (1,611 chars) at skill load; `gate.sh` checks every write | the brief carries the full `ms_workflow_status` JSON every turn (`loop.py:233`) | none on content; CC gets it once per skill load, the harness every turn |
| 12 | Tool errors | pydantic validation text returned verbatim; Sonnet retried (event 398, `params` sent as a string) | not recorded | unknown |

Gaps that matter, in order:

1. Within-run memory (9). CC keeps everything and summarizes when full; the
   harness starts from zero each turn. Consistent with muse re-reading
   `01-macro-stages.md` 15 times.
2. Skill delivery (6). CC's model chooses what to read and keeps it; the
   harness pastes the bodies but leaves sub-files to be re-fetched.
3. Builtin tools (7). Sonnet used Bash to inspect files and processes; the
   harness has no equivalent. Small in r3 (16 calls).

Model behavior worth keeping from r3: caught the `usescratch=False` setjy
default through `ms_residual_stats` before rflag refused (run 1 without
skills hit the refusal); picked tclean `threshold` 0.02 mJy against a run-1
RMS of 0.636 mJy, `niter` 50000.

## 3. Presenting it the way a person presents it to Claude Code

1. **Project context file** in the system prompt: the contract, error codes
   and conventions from the project `CLAUDE.md`. Static, so it caches.
2. **Skill index**: each skill's name and description (from the frontmatter
   the harness strips today), bodies loaded on demand. On top of that,
   stage-to-file routing: the harness names the relevant skill section for
   the status step (turn 6 would have been handed `10-precal-workflow.md`
   Step 8).
3. **Memory**, two parts. Per run: what the harness already recorded
   (stage measurements, flag fractions, which setjy mode was used). Across
   runs: a lessons file, e.g. "initial rflag needs a physical MODEL_DATA
   column; set `usescratch=True`".
4. **The goal in the owner's words** instead of a scope keyword.
   "Calibrate 3C391 and make a first-pass mosaic image" says more than
   "calibration + imaging".
5. **`ask`** instead of `blocked`, answered by the owner or a scripted
   operator.
6. **Continuity across turns** through 3 and 4, not by carrying the whole
   conversation. Fresh history per turn stays.

## 4. Comparison with the existing plans

Plans read: `docs/DESIGN_NATIVE_HARNESS.md` and `docs/PLAN_NATIVE_HARNESS.md`
(2026-09-16, amended 09-22), `PLAN_STRUCTURED_STATE.md` (2026-09-13),
`PLAN_BELIEF_STATE.md` (2026-09-08), `REFACTOR_PLAN.md` (2026-09-13).

| Proposal (section 3) | Status in the plans | Where |
|---|---|---|
| 1. Project context file | not in any plan. P4 and P6 limit the model to the two `SKILL.md` bodies plus declared tools | `DESIGN_NATIVE_HARNESS.md` §4 |
| 2a. Skill index, bodies on demand | conflicts with P6 ("the two `SKILL.md` bodies are always in the system prompt"). Needs no new tool: `read_file` already resolves `SKILL.md` by bare name, so it fits "two harness tools only" (`PLAN_NATIVE_HARNESS.md` §9) | `DESIGN_NATIVE_HARNESS.md` P6 |
| 2b. Stage-to-file routing | planned as harness skills injected by trigger, with each injection's sha256 recorded. Not built | `PLAN_STRUCTURED_STATE.md` §2, order item 2 |
| 3a. Per-run memory | belief state built, off by default. The stronger version, five harness-measured state snapshots (antenna, caltable, RFI/SpW, PA, calibrator), is planned and not built. The snapshots make state independent of what the model chose to measure, which this proposal does not | `PLAN_BELIEF_STATE.md`; `PLAN_STRUCTURED_STATE.md` §1, order item 4 |
| 3b. Cross-run lessons | explicitly deferred: "needs a corpus that does not yet exist" | `PLAN_BELIEF_STATE.md`, Context |
| 4. Goal in own words | scope string exists (`config.toml`, `--scope`). The planned `scripted` operator's per-run answer file carries goal and risk tolerance | `PLAN_STRUCTURED_STATE.md` §3 |
| 5. `ask` | planned with `null`, `scripted`, `human` operators. Not built | `PLAN_STRUCTURED_STATE.md` §3, order item 5 |
| 6. Fresh history per turn | matches P8 | `DESIGN_NATIVE_HARNESS.md` P8 |

In the plans and not in this proposal: rerun with downstream invalidation
(`ms_supersede_stage` now covers part of it), the injection record,
decision sampling at a frozen state (§5), and the `events` table that makes
transcripts queryable (§6).

## 5. Correction: the `claude` backend is not Claude Code as a person uses it

`ClaudeBackend` (`backends.py:298-321`) runs `claude -p` with
`--setting-sources ""`, `--strict-mcp-config`, the harness skills appended as
system prompt, and Bash, Write, Edit, NotebookEdit, Task, WebFetch and
WebSearch disallowed. So it gets layer 1 and its own Read, Glob and Grep, but
no host settings, no host MCP servers, no hooks. Whether `CLAUDE.md` files
and auto-memory still load under an empty `--setting-sources` is not
verified. It is not a like-for-like stand-in for sections 1 and 2 until that
is checked.

## 6. Open

- Whether P6 (bodies always in context) should be reopened for 2a.
- Where a cross-run lessons file would live, and who writes it.
- What `claude -p --setting-sources ""` actually loads (layers 2 to 4).

## 7. Direction agreed 2026-09-23: intro plus an enforced action-to-skill map

No project `CLAUDE.md` for the model. Instead, a general data reduction intro
in the system prompt that carries an action-to-skill map, and a harness rule
that makes the model read the mapped skill before it acts. This is the plan's
"harness skills injected by trigger" (`PLAN_STRUCTURED_STATE.md` §2), made
stricter.

### What exists to build from

- The map existed as `00-playbook.md` ("Stage -> next action") in the old
  `radio-interferometry` skill. Dropped in `deffeaa` with the driver-only
  fork. Its states were written in plain language, not in the labels
  `ms_workflow_status` emits; `PLAN.md` already records that mismatch.
- Enforcement is nearly free. The turn already records files read
  (`turn.files_read`, `tools.py:431`) and policy is already checked at
  dispatch (`_policy`, `tools.py:340`). New rule: a script-producing call is
  rejected unless this turn has read the skill file mapped to the current
  status step. Counted like R1 to R5; the model reads and calls again.
- Evidence it is needed: muse turn 6 (rflag on a virtual model) never opened
  `10-precal-workflow.md`; the imaging run never checked skill 11 Step 6 and
  defaulted the gridder to `standard` for a 7-pointing VLA mosaic.

### Needs owner approval

1. The new rule gates the model's process, not a science result. It is
   harness policy (P5), not a tool, so the tools' no-gates contract does not
   apply, but it is a new kind of rejection.
2. It reopens design decision P6. The intro plus map replaces the preamble;
   `SKILL.md` bodies are read on demand.

### Map, first draft (keyed on `ms_workflow_status` labels)

| Status step | Must read |
|---|---|
| `import_asdm` | `10-precal-workflow.md` Step 0 |
| `set_intents` | `02-orientation.md` (intents) |
| `apply_preflag` | 10, Steps 1 and 2 |
| `generate_priorcals` | 10, Step 3 |
| `setjy` | 10, Step 4 |
| `initial_bandpass` | 10, Steps 5 and 6 |
| `initial_rflag` | 10, Steps 7 and 8 |
| `delay_bandpass_gain` | `07-calibration-execution.md` Steps 1 to 6 |
| `applycal_target` | 07, Step 7 |
| `first_image` | `11-imaging.md` |
| `selfcal_or_done` | `12-selfcal.md`, `13-postcal-rfi-flagging.md` |

### Changes to the existing plan's order

- Drop item 1 (separation into a submodule): replaced by `c0e7f4a`
  ("go standalone").
- Defer section 6 (SQLite as truth, `events` table): the turn journals were
  enough to read everything needed on 2026-09-23.
- Move sampling up: without it there is no telling whether the map helped.

### Next plan of action (estimates, not measured)

1. Commit B2. **Done: `8d076a4`**, 1225 unit tests pass.
2. Write the intro and the map as a harness skill fragment, replacing the
   preamble. About 2 hours, then owner review.
3. Enforce it: policy rule in `tools.py`, map lookup keyed on the brief's
   status step, unit tests. About half a day.
4. Frozen state and sampling: rebuild a state by replaying scripts 1 to k-1
   on a fresh extract; `sample` command with read-only results cached per
   state and `analyst.db` restored per sample. About 1 day.
5. Measure the map: sample muse turn 6 (rflag on a virtual model) and
   imaging turn 3 (`first_image`) 20 times each, with and without the map,
   graded offline against the skill's checks.
6. Then the plan's items 4 and 5: harness-measured state snapshots, and `ask`.

Also: update `PLAN_STRUCTURED_STATE.md`'s order of work and mark P6 reopened
in `docs/DESIGN_NATIVE_HARNESS.md` once approved.
