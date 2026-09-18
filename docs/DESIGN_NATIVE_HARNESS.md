# DESIGN — native harness for `analyst_driver`

Status: design agreed with the user 2026-09-16. Companion: `PLAN_NATIVE_HARNESS.md` (same
directory) holds the implementation detail. Read this file first.

## 1. What this is, in one paragraph

`radio-harness` drives a CASA reduction as a sequence of turns. Each turn a
model reads the state of the data, calls measurement tools, and names one
script for the loop to run. Today the *inner* agentic loop — reading skills,
calling MCP tools, producing the decision — is delegated to a third-party
coding agent (`claude -p`, `opencode run`, `codex exec`) as a subprocess, and
the harness patches that agent's behaviour from outside with hooks and tool
bans. This design replaces the subprocess with an inner loop the harness owns,
which talks to the model providers' HTTP APIs directly (Anthropic Messages
API; any OpenAI-compatible Chat Completions endpoint — TACC, llama.cpp,
OpenAI). Everything above the backend boundary — the outer loop, the journal,
the executors, the DB — is unchanged.

## 2. The boundary: what stays, what goes

Verified against `radio-harness` `main` at `e7a3aff` (1123 unit tests pass).

| Layer | File | Fate |
|---|---|---|
| Outer loop: sense → brief → decide → dispatch → record | `analyst_driver/loop.py` | **unchanged** |
| Journal / SQLite / belief state | `analyst_driver/db.py` | unchanged |
| Executors: local, SLURM, HTCondor | `analyst_driver/executors.py` | unchanged |
| CLI, `config.toml` | `analyst_driver/cli.py` | `[backend] kind = "api"` added; old kinds removed at the end |
| Backend contract `Backend.run(prompt, workdir, *, ms_path) -> BackendResult` | `analyst_driver/backends.py` | **kept as the seam**; new `ApiBackend` implements it |
| `ClaudeBackend` | `backends.py` | **kept** (decision reversed 2026-09-17, see §6): the subscription path. Serves the harness registry to `claude -p` over MCP (`tools_server.py`); same skills, tools, policy and decision as `ApiBackend` |
| `OpencodeBackend`, `CodexBackend` | `backends.py` | removed (last stage) |
| Tool ban (`DEFAULT_DISALLOWED_TOOLS`, `tools_ban_violated`, `banned_tools_offered`) | `backends.py` | kept for `ClaudeBackend` only |
| Hooks (`hooks/sense.py`, `hooks/gate.py`, `hooks.json`, `sense_log`) | `hooks/`, `ms_inspect/util/sense_log.py` | removed — the loop stands where the hook fired |
| Claude Code plugin packaging (`.claude/`, `plugin.json`, `.mcp.json`, `bin/install-local.sh`) | repo root | removed from the driver path; see §7 |
| MCP servers `ms-inspect`, `ms-modify`, `ms-create` | `src/ms_*/server.py` | **unchanged**; still usable interactively from Claude Code. The harness imports their registries in-process |
| Skills | `.claude/skills/{stage-orchestration,radio-interferometry-driver}/` | content unchanged; moved to a path the harness owns (`skills/`) so the `.claude/` directory can go |

The single most important property: `BackendResult` and the `decision` dict
keep their shape, so `loop.py`, `db.py` and their 184 tests do not change.

## 3. Architecture

```
loop.py  ── brief ──►  ApiBackend.run()                          (backends.py)
                          │
                          ▼
                       Agent.run_turn()                          (agent.py)
                          │  system = skills.system_prompt()     (skills.py)
                          │  tools  = registry.specs()           (tools.py)
                          │
                          │  round:  provider.complete(system, tools, history)
                          │            │                          (providers.py)
                          │            ▼  Reply{blocks, stop, usage, raw}
                          │          for each ToolCall in reply:
                          │              policy.check(call)  ──► reject → error result
                          │              registry.dispatch(call) ──► FastMCP.call_tool (in-process)
                          │          history += reply.raw + tool results
                          │  until: submit_decision called | end_turn | max_rounds
                          ▼
                       BackendResult{text, transcript, tool_calls, usage, model, error}
```

Five modules, all inside `src/analyst_driver/`:

1. **`providers.py`** — wire format only. `Provider.complete(system, tools,
   history) -> Reply`. `AnthropicProvider`, `OpenAICompatProvider`,
   `FakeProvider` (scripted replies, for tests). A provider knows nothing
   about CASA, skills, or policy.
2. **`tools.py`** — the registry. Loads `ToolSpec`s from the three FastMCP
   objects in-process (`await mcp.list_tools()` for schemas,
   `await mcp.call_tool()` to run — both verified to work outside a request
   context). Adds two harness-owned tools, `read_file` and `submit_decision`.
   Holds the policy checks.
3. **`skills.py`** — reads the two `SKILL.md` bodies (frontmatter stripped)
   into one byte-stable system prompt; resolves sub-file names for
   `read_file`.
4. **`agent.py`** — the inner loop and its stop conditions; builds the
   normalized transcript; maps to `BackendResult`.
5. **`backends.py`** — `ApiBackend(kind="api")`, constructed from
   `[backend]` in `config.toml`, wraps 1–4.

## 4. Principles

**P1 — The outer loop's contract is fixed.** `Backend.run` in, `BackendResult`
out, `decision` dict unchanged. The new code is an implementation of the
existing seam, not a new seam.

**P2 — A provider adapter translates wire format and nothing else.** No
prompt text, no retry policy beyond the SDK's own, no tool logic. If two
providers behave differently the difference is expressed in the mapping
table (`PLAN.md` §3), not in `agent.py`.

**P3 — One source of truth for tools.** The FastMCP registries define name,
description, schema and implementation. The harness never re-declares a
CASA tool. `read_file` and `submit_decision` are the only tools the harness
owns, and they exist because the API offers nothing by itself: function
calling is "we send schemas, the model returns name + arguments, we run it".

**P4 — The model gets only what we declare.** No shell, no file write, no
web, no sub-agents. The old ban list and its verification machinery become
dead code, not configuration.

**P5 — Policy is code.** Three rules were previously prompt text plus Claude
Code hooks: a script-producing tool is called with `execute=False`; at most
one script-producing tool per turn; a decision names a script that exists.
They are now argument checks at dispatch time in `tools.py`. A violating
call is not executed; the model receives an error tool result naming the
rule, and the rejection is counted in the transcript. The brief still states
the rules for the model's benefit, but the guarantee is the check.

**P6 — Context is explicit and deterministic.** The two `SKILL.md` bodies
are always in the system prompt. Sub-files (`01-macro-stages.md`,
`07-calibration-execution.md`, …) are read on demand through `read_file`,
restricted to the skill root, the work directory and the run directory (job
logs). This reproduces Claude Code's progressive disclosure without relying
on its skill discovery. The `allowed-tools` frontmatter is advisory and
unused.

**P7 — The decision is a tool call.** `submit_decision` carries the schema
the brief used to describe in prose (`script`, `tool`, `stage`, `cited`,
`outputs`, `notes`, `done`, `belief_state`). Calling it ends the turn. The
API validates the arguments (Anthropic `strict: true`; OpenAI `strict`
function schemas where the server supports them). `parse_decision` remains
as the fallback for a model that answers in text.

**P8 — Every turn starts fresh; the journal is the memory.** Same as today
with `claude -p`: no context carries across outer turns except through the
brief (status, digest, belief state). Within a turn the inner history grows
by rounds. The normalized transcript (JSONL of system, user, assistant,
tool-call, tool-result, rejection, usage records) is stored in
`BackendResult.transcript` exactly where the `stream-json` dump went.

**P9 — A check that cannot fail is not evidence.** The turn record reports
how much work it did: rounds, tool calls made, calls rejected, files read,
tokens in/out/cache-read/cache-write, and whether the decision came from
`submit_decision` or the text fallback. A turn with zero tool calls and a
decision is legal but visible.

**P10 — Stop conditions, not budgets.** The outer loop stops at `max_turns`
(exists). The inner loop stops on `submit_decision`, `end_turn` with no
decision, or `max_rounds` (new, one integer, default 30). These are two
different levels and are not merged. There is no token budget.

**P11 — Cache-friendly prefix.** Providers render `tools → system →
messages` and cache by byte prefix. Tools are sorted by name and serialized
deterministically; the system prompt contains no timestamp or path that
varies per run; the brief — which varies — is the first user message.
On Anthropic one `cache_control` breakpoint closes the system prompt.
`usage.cache_read_input_tokens` is recorded per round and must be non-zero
from round two; zero means a silent invalidator and is reported, not
ignored.

**P12 — Minimum viable first.** v1 has: no streaming, no thinking
configuration beyond the model default, no compaction or context editing,
no parallel tool execution (calls in one reply run sequentially — CASA
table access on one MS is not thread-safe, and the registry already
serializes per path), no provider fallbacks, no automatic retries beyond the
SDK defaults. Each is a later addition behind the same `Provider` interface
if measurement shows a need.

## 5. Measured context and cost facts (2026-09-16, chars/4)

| piece | tokens |
|---|---|
| 53 tool schemas (34 `ms-inspect`, 16 `ms-modify`, 3 `ms-create`) | ~20k (`ms-modify` alone ~10k) |
| two `SKILL.md` bodies | ~1.8k |
| brief (template + `ms_workflow_status` JSON) | ~2–4k |
| **fixed prefix per turn** | **~25k** |
| all 20 skill files (upper bound if every one is read) | ~46k |

A turn reading 2–4 sub-files and making 3–10 tool calls runs ~40–70k tokens.
Fits every target: Anthropic (1M), TACC models (128k–256k), local Gemma
E4B via llama.cpp (128k). Per-turn reset (P8) means this does not grow with
the reduction.

Anthropic caching economics (current docs): minimum cacheable prefix 512
tokens on Opus 5; write 1.25× (5-min TTL) or 2× (1-h TTL); read 0.1×; a read
refreshes the timer. Inner rounds are seconds apart → 5-min TTL serves them.
Across outer turns the gap is the CASA job's wall time: <5 min warm, 5–60 min
the 1-h TTL pays, >1 h cold miss. OpenAI-compatible servers have no marker;
prefix reuse is server-side and automatic where enabled. Same stable prefix
is the only lever we hold; TACC's server configuration is unverified.

## 6. Alternatives considered

- **Anthropic SDK `tool_runner`** runs the loop for you with per-turn hooks.
  Provider-locked; the whole point is one loop across providers. Rejected.
- **Claude Agent SDK** is Claude Code as a library — the dependency being
  removed. Rejected.
- **MCP over stdio with the `mcp` client library** — spawn the three servers
  and talk JSON-RPC. Works, but the harness lives in the same pixi env as
  the tools; in-process import removes three processes, startup cost, and a
  transport, and gives the same schemas. Rejected for v1; the registry
  interface would allow it later.
- **Keep `claude -p` as one backend among several.** Rejected on 2026-09-16
  (would keep hooks, plugin packaging and the ban list alive for one path),
  **reversed on 2026-09-17**: the Messages API bills per token only and no
  SDK path reaches a claude.ai subscription; non-bare `claude -p` does. Kept
  in the narrowest form: `tools_server.py` serves the harness `ToolRegistry`
  over stdio MCP, `ClaudeBackend` hands `claude -p` that server
  (`--mcp-config` + `--strict-mcp-config`), the skills system prompt
  (`--append-system-prompt-file`) and no host settings
  (`--setting-sources ""`), and reads the turn's `TurnState` from the state
  file the server writes. Hooks and plugin packaging still go; the ban list
  stays. Residual differences: `claude` keeps its own Read/Glob/Grep, and
  there is no prompt-cache control or automated end-to-end test of the
  real `claude` binary.

## 7. Side-effects to expect

- `hooks/`, `hooks.json`, `.claude/settings.json`, `.claude/commands/`,
  `bin/install-local.sh`, `bin/uninstall-local.sh` lose their purpose for
  the driver. `bin/serve*.sh` and the MCP servers stay for interactive use.
- `ms_inspect/util/sense_log.py` and its `sense_log` table exist only for
  the hook pair; they go with the hooks (their tests too).
- The brief template's step 4 ("end your reply with one JSON object") is
  rewritten to "call `submit_decision`". Step 1 ("consult the
  stage-orchestration skill first") stays; the skill is already in context.
- `README.md` and `CLAUDE.md` in `radio-harness` describe plugin
  installation and `claude -p` flags; both need a rewrite (the `CLAUDE.md`
  was already stale before this work).
- A model that never calls tools (bad tool-call support) produces a turn
  with a text-fallback decision and zero tool calls. P9 makes this visible;
  nothing hides it.

## 8. Risks, ranked

1. **Provider tool-calling quality on non-Anthropic endpoints.** Local and
   some hosted open models emit malformed arguments or narrate a tool call
   as text. The loop returns a parse error as the tool result and continues,
   but a model that never recovers burns `max_rounds` per turn. Worse than
   the others because it can make a backend unusable rather than just slow.
   Mitigation: `FakeProvider` tests for every malformed shape; real-run
   stage before any removal.
2. **Preserved thinking and history replay on Anthropic.** Assistant
   content (including thinking blocks) must be appended back verbatim; a
   reconstructed message invalidates the cache and, on newer models, is
   rejected. Mitigation: providers own their native history; the agent
   never rebuilds an assistant message.
3. **Tool result size.** A tool returning a large JSON payload (per-antenna
   tables, plots as paths are fine) bloats the round. Mitigation: a single
   documented cap on the text returned to the model (full payload still in
   the journal), reported when applied.
4. **Schema fidelity across providers.** FastMCP schemas use `$defs`/`$ref`
   and a top-level `params` wrapper. Anthropic accepts JSON Schema directly;
   some OpenAI-compatible servers reject `$ref` or `strict`. Mitigation:
   inline refs once at registry load; `strict` is a per-provider option
   defaulting off for OpenAI-compatible.
