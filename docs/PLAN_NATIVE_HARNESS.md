# PLAN — native harness for `analyst_driver`

Status: agreed with the user 2026-09-16, not started. Written for a fresh
session with none of the conversation that produced it: every fact needed is
below or re-derivable with the commands given. Read
`DESIGN_NATIVE_HARNESS.md` (same directory) first — it holds the principles
P1–P12 this plan refers to. Do not relitigate §9 (decisions already made).

## 0. Ground truth

- Repo: `/Users/ssekhar/src/skunkworks-ra/radio-harness`, branch
  `native-harness`, cut from `main` at `e7a3aff`.
- Env: `pixi run <cmd>` from the repo root. Baseline: `pixi run pytest
  tests/unit -q` → 1123 passed; `pixi run ruff check src tests` clean.
- Python ≥3.12, `mcp` 1.26.0, `pydantic` 2.12, `httpx` 0.28 installed.
  `anthropic` (latest 1.6.0) and `openai` (latest 3.14.1) are **not**
  installed yet.
- Code to be replaced: `src/analyst_driver/backends.py` (`ClaudeBackend`,
  `OpencodeBackend`, `CodexBackend`, `DEFAULT_DISALLOWED_TOOLS`), `hooks/`,
  `hooks.json`, `src/ms_inspect/util/sense_log.py`.
- Code that must not change: `src/analyst_driver/loop.py`, `db.py`,
  `executors.py`, `owner.py`; `src/ms_inspect/`, `src/ms_modify/`,
  `src/ms_create/` (except deleting `sense_log.py` in stage 5).
- Skills: `.claude/skills/stage-orchestration/` (SKILL.md, 01-macro-stages.md,
  14-belief-state.md) and `.claude/skills/radio-interferometry-driver/`
  (SKILL.md + 01…13 files). Total ~46k tokens; the two SKILL.md bodies
  ~1.8k.
- FastMCP registries, importable in-process without a server:
  `from ms_inspect.server import mcp`, `from ms_modify.server import mcp`,
  `from ms_create.server import mcp`. Verified 2026-09-16:
  `await mcp.list_tools()` → `list[mcp.types.Tool]` with `.name`,
  `.description`, `.inputSchema`, `.annotations.readOnlyHint`;
  `await mcp.call_tool(name, {"params": {...}})` → tuple
  `(list[ContentBlock], dict)`; the text payload is
  `"".join(b.text for b in blocks if b.type == "text")`. Errors from the
  tool layer arrive as a JSON envelope `{"status":"error",...}` in that
  text, not as exceptions.
- Every schema wraps its fields under a top-level `params` object with a
  `$ref` into `$defs` (e.g. `{"params": {"$ref": "#/$defs/MSPathInput"}}`).
- Tool classes (from `readOnlyHint` and the presence of an `execute` field):
  - *read-only*: all 34 `ms-inspect` tools except `ms_supersede_stage`;
    `ms_sdm_summary`.
  - *bookkeeping writes* (no `execute`): `ms_supersede_stage`,
    `ms_reduction_log`.
  - *script-producing* (have `execute` and `workdir`): all 16 `ms-modify`
    tools and `ms_import_asdm`. Re-derive with the snippet in §2.
- Job logs live in `<run_root>/<run_key>/jobs/NNNN/job.log` (`db._run_dir`),
  **not** in the work directory. The brief passes these paths as `logs=`.
- Anthropic facts used below come from the API docs current at 2026-09-16
  (via the `claude-api` skill): default model `claude-opus-5`; thinking is on
  by default (omit the `thinking` param); no `temperature`; assistant prefill
  rejected; `strict: true` on a tool definition validates `input`;
  `cache_control: {"type": "ephemeral"}` (optional `"ttl": "1h"`); usage
  fields `input_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, `output_tokens`; stop reasons `tool_use`,
  `end_turn`, `max_tokens`, `refusal`; `max_tokens` ~16000 for non-streaming.
  All `tool_result` blocks for one assistant message go back in **one** user
  message.

## 1. Deliverables

| # | Path | New/changed |
|---|---|---|
| 1 | `src/analyst_driver/providers.py` | new |
| 2 | `src/analyst_driver/tools.py` | new |
| 3 | `src/analyst_driver/skills.py` | new |
| 4 | `src/analyst_driver/agent.py` | new |
| 5 | `src/analyst_driver/backends.py` | `ApiBackend` added; CLI backends removed in stage 5 |
| 6 | `src/analyst_driver/cli.py` | `[backend] kind="api"` wiring; `DEFAULT_CONFIG` |
| 7 | `src/analyst_driver/loop.py` | brief template steps 4–6 wording only (stage 3) |
| 8 | `skills/` (repo root) | the two skill directories moved here from `.claude/skills/` |
| 9 | `tests/unit/test_driver_providers.py`, `test_driver_tools.py`, `test_driver_skills.py`, `test_driver_agent.py` | new |
| 10 | `pyproject.toml`, `pixi.toml` | add `anthropic>=1.6`, `openai>=3.14` |
| 11 | `docs/`, `README.md`, `CLAUDE.md` | stage 5 |

## 2. Stage 0 — measure before building

Purpose: pin the context numbers in DESIGN §5 with a real tokenizer and keep
the script for later regressions. Output: `docs/context_budget.md` (numbers
+ command) and `tests/unit/test_driver_tools.py::test_schema_size_ceiling`.

```python
# pixi run python - <<'EOF'
import asyncio, json
from ms_inspect.server import mcp as a
from ms_modify.server import mcp as b
from ms_create.server import mcp as c
async def main():
    for nm, m in (("inspect", a), ("modify", b), ("create", c)):
        ts = await m.list_tools()
        n = sum(len(json.dumps({"name": t.name, "description": t.description,
                                "input_schema": t.inputSchema}, sort_keys=True)) for t in ts)
        print(nm, len(ts), "tools", n, "chars")
        for t in ts:
            s = json.dumps(t.inputSchema)
            print("  ", t.name, "execute" if '"execute"' in s else "-",
                  "ro" if t.annotations and t.annotations.readOnlyHint else "RW")
asyncio.run(main())
# EOF
```

For Anthropic, count exactly with `client.messages.count_tokens(model=...,
system=..., tools=..., messages=[...])` once the registry exists (stage 2).
Ceiling test: assert total schema chars < 120 000 (~30k tokens) so a future
tool cannot silently double the prefix.

## 3. Stage 1 — `providers.py`

### 3.1 Internal model (dataclasses, no SDK types leak upward)

```python
@dataclass(frozen=True)
class ToolSpec:            # produced by tools.py, consumed by providers
    name: str
    description: str
    input_schema: dict     # JSON Schema, refs inlined, keys sorted
    strict: bool = False

@dataclass
class ToolCall:            # model → harness
    id: str
    name: str
    args: dict | None      # None when the arguments did not parse
    raw_args: str | None   # the unparsed string, for the journal

@dataclass
class ToolResult:          # harness → model
    call_id: str
    text: str
    is_error: bool = False

@dataclass
class Usage:
    input_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output_tokens: int | None = None

@dataclass
class Reply:
    text: str              # concatenated text blocks ("" if none)
    tool_calls: list[ToolCall]
    stop: Literal["tool_use", "end_turn", "max_tokens", "refusal", "other"]
    usage: Usage
    model: str | None
    raw: Any               # provider-native assistant message; replayed verbatim
```

### 3.2 `Provider` protocol

```python
class Provider(Protocol):
    kind: str
    model: str
    def new_history(self, brief: str) -> list[Any]: ...
    def append_reply(self, history: list[Any], reply: Reply) -> None: ...
    def append_tool_results(self, history: list[Any], results: list[ToolResult]) -> None: ...
    def complete(self, system: str, tools: list[ToolSpec], history: list[Any]) -> Reply: ...
```

The history is a list of provider-native messages the agent treats as
opaque (DESIGN risk 2). The agent keeps its own normalized transcript
separately.

### 3.3 Mapping tables

**`AnthropicProvider`** (`anthropic.Anthropic(api_key=os.environ[api_key_env], base_url=base_url or None)`):

| concept | wire |
|---|---|
| system | `system=[{"type":"text","text":system,"cache_control":{"type":"ephemeral", **({"ttl":"1h"} if cache_ttl=="1h" else {})}}]` |
| tools | `tools=[{"name","description","input_schema", **({"strict": True} if spec.strict else {})}]`, sorted by name |
| first user msg | `{"role":"user","content":[{"type":"text","text":brief}]}` |
| reply → history | append `{"role":"assistant","content": response.content}` **unchanged** (thinking blocks included) |
| tool calls in reply | every block with `type=="tool_use"` → `ToolCall(id=block.id, name=block.name, args=block.input)` |
| tool results | one `{"role":"user","content":[{"type":"tool_result","tool_use_id":r.call_id,"content":r.text,"is_error":r.is_error} ...]}` |
| stop | `stop_reason` mapped 1:1; `refusal` → `Reply.stop="refusal"` and `stop_details` text into `Reply.text` |
| usage | `usage.input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens` |
| params | `model`, `max_tokens=16000`; no `thinking`, no `temperature` |

**`OpenAICompatProvider`** (`openai.OpenAI(api_key=os.environ[api_key_env], base_url=base_url)`; `chat.completions.create`):

| concept | wire |
|---|---|
| system | `{"role":"system","content":system}` as `messages[0]` (part of the history list so the prefix is stable) |
| tools | `[{"type":"function","function":{"name","description","parameters":input_schema, **({"strict":True} if spec.strict else {})}}]` |
| first user msg | `{"role":"user","content":brief}` |
| reply → history | append `choice.message.model_dump(exclude_none=True)` (keeps `tool_calls`; also keeps `reasoning_content` if the server returns it) |
| tool calls | `choice.message.tool_calls[*]` → `ToolCall(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))`; on `JSONDecodeError` → `args=None, raw_args=<string>` |
| tool results | one `{"role":"tool","tool_call_id":r.call_id,"content":r.text}` **per result**, in call order (this API has no `is_error`; prefix the text with `ERROR: ` when `is_error`) |
| stop | `finish_reason`: `tool_calls`→`tool_use`, `stop`→`end_turn`, `length`→`max_tokens`, `content_filter`→`refusal`, else `other` |
| usage | `usage.prompt_tokens`, `usage.prompt_tokens_details.cached_tokens` (may be absent → None), `completion_tokens`; `cache_write` always None |
| params | `model`, `max_tokens` (config, default 16000), `temperature` (config, default 0.2 as in the arxiv-fetcher reference client) |
| `content is None` with `finish_reason=="length"` | `Reply.text=""`, `stop="max_tokens"` — reasoning models can spend the whole budget before emitting content |

**`FakeProvider(replies: list[Reply])`** — pops one `Reply` per `complete`
call; records every `(system, tools, history)` triple it was given; raises
if it runs out. `append_*` append plain dicts. This is the only provider
the unit tests of stages 2–3 use.

### 3.4 Config (`[backend]` in `config.toml`)

```toml
[backend]
kind = "api"
provider = "anthropic"          # anthropic | openai
model = "claude-opus-5"
api_key_env = "ANTHROPIC_API_KEY"
# base_url = "https://ai.tejas.tacc.utexas.edu/v1"   # openai-compatible endpoints
# cache_ttl = "5m"                                    # anthropic only: 5m | 1h
# max_rounds = 30
# strict_tools = true                                 # anthropic default true, openai default false
# max_tokens = 16000
# temperature = 0.2                                   # openai only
```

`api_key_env` names the variable; the key is never in the file
(arxiv-fetcher pattern). A missing variable is a `RuntimeError` at backend
construction, not at the first turn.

### 3.5 Tests (`test_driver_providers.py`)

No network. Construct each real provider with a stub client object and
assert the exact request kwargs: cache marker present and last in `system`;
tools sorted; tool results in one user message (Anthropic) / one `tool`
message each (OpenAI); assistant reply appended verbatim; every stop-reason
mapping; usage mapping incl. absent `cached_tokens`; malformed arguments →
`args=None`; `content=None`+`length`. Missing `api_key_env` raises.

## 4. Stage 2 — `tools.py`

### 4.1 Registry

```python
class ToolRegistry:
    def __init__(self, servers: list[FastMCP], extra: list[HarnessTool]): ...
    @classmethod
    def default(cls, *, skill_root: Path, read_roots: list[Path]) -> "ToolRegistry"
    def specs(self) -> list[ToolSpec]              # sorted by name, refs inlined
    def dispatch(self, call: ToolCall, turn: TurnState) -> ToolResult
    def classify(self, name) -> Literal["read_only", "bookkeeping", "script"]
```

- Load once per process; `asyncio.run` around `list_tools()` at
  construction and around `call_tool()` at dispatch (the agent is
  synchronous; the outer loop is synchronous).
- **Ref inlining**: replace every `{"$ref": "#/$defs/X"}` with the
  definition, drop `$defs`. Do it once at load; keep the original for the
  journal. Reason: some OpenAI-compatible servers reject `$ref`.
- Keep the `params` wrapper — it is what the tool functions expect and what
  the schemas describe. Do not flatten.
- **Result text**: join text blocks; if longer than
  `RESULT_TEXT_CAP = 60_000` chars, return the first cap with a trailing
  line `[truncated: N more chars; full payload in the journal]`. The full
  text goes to the transcript. Applied count is reported (P9).
- A tool that raises (not an error envelope) → `ToolResult(is_error=True,
  text=f"{type(e).__name__}: {e}")`; never propagate into the loop.

### 4.2 Policy (P5), enforced in `dispatch` before any execution

| rule | check | on violation |
|---|---|---|
| R1 script tools write scripts only | `classify(name)=="script"` and `args["params"].get("execute") is not False` | error result: `"<name> must be called with execute=false; the loop executes scripts"` |
| R2 one script tool per turn | `turn.script_calls >= 1` and `classify(name)=="script"` | error result: `"a script-producing tool was already called this turn (<prev>); call submit_decision"` |
| R3 unknown tool | name not in registry | error result |
| R4 unparseable args | `call.args is None` | error result quoting the first 200 chars of `raw_args` |
| R5 `read_file` path | resolved path not under an allowed root, or not a regular file, or > `READ_FILE_CAP = 200_000` bytes | error result naming the roots |

Every rejection increments `turn.rejections[rule]` and is written to the
transcript as `{"type":"rejected","rule":...,"tool":...}`.

### 4.3 Harness-owned tools

**`read_file`** — `{"path": str, "start_line": int?, "end_line": int?}`.
Resolution order for a relative path: each skill directory
(`skill_root/<skill>/<path>`), then the work directory. Absolute paths must
fall under one of `read_roots` (skill root, workdir, run directory).
Returns numbered lines. Description text tells the model the skill root and
that sub-files named in `SKILL.md` are read with this tool.

**`submit_decision`** — schema (`additionalProperties: false`):

```json
{"type":"object","properties":{
  "script":{"type":"string","description":"path to the generated script; omit only with done=true"},
  "tool":{"type":"string"},
  "stage":{"type":"string"},
  "done":{"type":"boolean"},
  "cited":{"type":"array","items":{"type":"object","properties":{
      "name":{"type":"string"},"value":{},"source":{"type":"string"}},
      "required":["name","value","source"],"additionalProperties":false}},
  "outputs":{"type":"array","items":{"type":"object","properties":{
      "path":{"type":"string"},"kind":{"type":"string","enum":["caltable","image","plot","ms","other"]}},
      "required":["path","kind"],"additionalProperties":false}},
  "notes":{"type":"string"},
  "belief_state":{"type":"string"}},
 "required":["notes"]}
```

`dispatch` does not "run" it: it records `turn.decision = call.args`,
returns `ToolResult(text="decision recorded")`, and the agent stops after
this round. The dict shape equals what `parse_decision` returns today, so
`loop.py` reads it unchanged (`script`, `done`, `stage`, `cited`,
`outputs`, `belief_state`).

### 4.4 Tests (`test_driver_tools.py`)

Registry against the real FastMCP objects (they import without CASA data):
53 + 2 specs, sorted, no `$ref` left, `params` wrapper present; classify
matches §0 lists exactly; each policy rule with a `FakeProvider`-shaped
`ToolCall`; `read_file` traversal (`../`), symlink outside roots, size cap,
relative resolution into the skill dir; `submit_decision` records and
returns; result cap applied and reported; schema-size ceiling (stage 0).
Dispatch of a real read-only tool on a nonexistent MS returns the
`MS_NOT_FOUND` envelope text, `is_error=False` (an envelope is data).

## 5. Stage 3 — `skills.py`, `agent.py`, `ApiBackend`

### 5.1 `skills.py`

- `git mv .claude/skills skills` (both directories, content untouched).
  Update `tests/unit/test_skill_telescope_refs.py` paths and any other test
  that globs `.claude/skills`.
- `system_prompt(skill_root) -> str`: fixed preamble (below) + for each of
  `stage-orchestration`, `radio-interferometry-driver` in that order: a
  heading, the `SKILL.md` body with YAML frontmatter removed, and the list
  of sibling file names. Deterministic bytes: sorted file lists, no
  timestamps, `skill_root` rendered as given (it is constant per install;
  it is the one path in the prefix and is acceptable).
- Preamble (verbatim, keep short):

  > You are one decision point in a CASA reduction driven by an external
  > loop. Tools measure; you reason. Skill sub-files are read with
  > `read_file`. A script-producing tool must be called with
  > `execute=false`; the loop executes the script. Finish every turn by
  > calling `submit_decision`.

### 5.2 `agent.py`

```python
@dataclass
class TurnState:
    script_calls: int = 0; last_script_tool: str | None = None
    decision: dict | None = None; decision_source: Literal["tool","text",None] = None
    rounds: int = 0; tool_calls: list[dict] = ...; rejections: Counter = ...
    files_read: list[str] = ...; truncated_results: int = 0
    transcript: list[dict] = ...; usage: list[Usage] = ...

class Agent:
    def __init__(self, provider, registry, system_prompt, *, max_rounds=30): ...
    def run_turn(self, brief: str) -> TurnState
```

`run_turn`:

1. `history = provider.new_history(brief)`; transcript ← system, brief.
2. Loop while `rounds < max_rounds`:
   a. `reply = provider.complete(system_prompt, specs, history)`; record
      usage, text, calls; `provider.append_reply(history, reply)`.
   b. If `reply.stop == "refusal"`: stop; `error = reply.text`.
   c. If no tool calls: stop (`end_turn` or `max_tokens`).
   d. For each call in order: `registry.dispatch(call, turn)`; collect
      results; **sequential** (P12).
   e. `provider.append_tool_results(history, results)`.
   f. If `turn.decision` is set: stop.
3. If `turn.decision is None`: `parse_decision(last_text)`; if found,
   `decision_source="text"`.
4. On any SDK exception (`anthropic.APIError`, `openai.APIError`,
   `httpx.HTTPError`): stop with `error = f"{type(e).__name__}: {e}"[:2000]`.
   No retry beyond the SDK default (2).
5. Cache check (P11): if provider is Anthropic and `rounds >= 2` and every
   `usage.cache_read` is 0 → transcript record
   `{"type":"warning","what":"no cache reads across rounds"}`.

Transcript record types: `system`, `user`, `assistant` (text, stop, model),
`tool_call` (id, name, args), `tool_result` (id, text_full, is_error,
truncated), `rejected`, `usage`, `warning`, `summary` (rounds, n_calls,
n_rejected, files_read, decision_source). Serialized as JSONL into
`BackendResult.transcript`.

### 5.3 `ApiBackend` (`backends.py`)

```python
class ApiBackend:
    kind = "api"
    def __init__(self, *, provider: str, model: str, api_key_env: str, base_url=None,
                 cache_ttl="5m", max_rounds=30, strict_tools=None, max_tokens=16000,
                 temperature=0.2, skill_root: Path, read_roots: list[Path]): ...
    def run(self, prompt, workdir, *, ms_path=None) -> BackendResult
```

- `run` builds a per-call registry view with `read_roots + [workdir]`,
  runs `Agent.run_turn(prompt)`, and maps:
  `text` = last assistant text (or `json.dumps(decision)` when the decision
  came from the tool, so `parse_decision` in `loop.py` still finds it —
  **this is what keeps `loop.py` unchanged**); `transcript` = JSONL;
  `model`; `tokens_in` = sum of `input_tokens`; `tokens_cache_read`,
  `tokens_cache_creation`, `tokens_out` = sums; `tool_calls` =
  `[{"tool": name, "result": text_full}]` (the shape
  `harvest_from_tool_calls` and `check_citations` already read);
  `error` set when the agent stopped on refusal/exception or produced no
  decision after `max_rounds`; `exit_code=None`.
- `ms_path` is accepted and unused (the brief already carries it).
- `make_backend("api", **cfg)`; `cli.load_config` passes
  `skill_root=<repo>/skills` (resolved from `analyst_driver.__file__`) and
  `read_roots=[run_root]`.

### 5.4 Brief template (`loop.py`, wording only)

Replace step 4 with: "Call `submit_decision` with: script (path to the
generated script), tool, stage, cited [...], outputs [...], notes. Only
`notes` is required by the schema; a turn that advances a stage must name
`script`." Replace step 5's "reply instead with {...}" with "call
`submit_decision` with `done=true` and `notes`". Step 6 (belief state):
"include `belief_state` in the `submit_decision` call". Nothing else in
`loop.py` changes; `parse_decision` stays.

### 5.5 Tests

`test_driver_skills.py`: frontmatter stripped; both bodies present in
order; byte-identical across two calls; file lists sorted.
`test_driver_agent.py` with `FakeProvider` + real registry: happy path
(read tool → script tool → `submit_decision`, 3 rounds, decision_source
`tool`); text-only reply with trailing JSON → `text` fallback; `execute=true`
rejected then corrected next round; second script tool rejected;
`max_rounds` exhaustion → `error`; refusal → `error`; malformed args → error
result, loop continues; parallel calls in one reply run in order and return
in one batch; `ApiBackend.run` maps to `BackendResult` such that
`Loop._decide_and_dispatch` (existing tests' `StubBackend` pattern) accepts
it — add one `test_driver_loop.py` case using `ApiBackend` over
`FakeProvider`, not a new stub.

## 6. Stage 4 — real run before any removal

1. `export ANTHROPIC_API_KEY=…`; `analyst-driver init` in a scratch run
   root; set `[backend]` per §3.4; `[executor] kind="local"`.
2. `analyst-driver run --input <MS> --workdir <dir> --telescope VLA --scope
   "calibration only"` on the same MS used for the last `claude -p` run.
3. Compare per turn against that run's journal: stage sequence, tool call
   count, decision fields, wall time, `tokens_in`/`tokens_cache_read`.
   `tokens_cache_read` must be non-zero on rounds ≥2 of every turn.
4. Repeat with `provider="openai"`, `base_url` = TACC, one model from the
   arxiv-fetcher pool (`Qwen3-235B-A22B-Instruct-2507` first — largest
   context, instruct-tuned). Record: rounds/turn, rejections, whether the
   decision came from the tool or text.
5. Write both tables into `docs/native_harness_first_run.md`. If step 3
   fails, fix before stage 5; do not remove the old backend on a failing
   comparison.

## 7. Stage 5 — removal and doc sync

Delete: `ClaudeBackend`, `OpencodeBackend`, `CodexBackend`,
`DEFAULT_DISALLOWED_TOOLS`, `banned_tools_offered`, `tools_ban_violated`,
`tool_names_offered` (and their tests in `test_driver_backends.py`);
`hooks/`, `hooks.json`; `src/ms_inspect/util/sense_log.py` +
`tests/unit/test_sense_log.py`; `.claude/settings.json`,
`.claude/commands/`, `plugin.json`/`marketplace.json` if present;
`bin/install-local.sh`, `bin/uninstall-local.sh`. Keep `bin/serve*.sh`.
`DEFAULT_CONFIG`: `[backend]` becomes §3.4 verbatim; remove
`allowed_tools`/`disallowed_tools` prose. `README.md`: replace plugin
install + `claude -p` sections with the `[backend]` table and the
`ANTHROPIC_API_KEY`/`TACC_API_KEY` note. `CLAUDE.md`: rewrite the driver
section to DESIGN §3. `docs/handoff.md`/`session_context.md`: mark the hook
mechanism superseded with a pointer here. Suite must still pass minus the
deleted tests; report the count.

## 8. Done criteria

- `pixi run pytest tests/unit -q` passes; count reported; ruff clean.
- Stage 4 tables exist and show `tokens_cache_read > 0` on Anthropic and a
  completed calibration-only run on both providers.
- `grep -rn "claude -p\|opencode\|codex exec\|disallowedTools\|sense_log" src
  tests README.md CLAUDE.md` returns nothing.
- `loop.py` diff is confined to the brief template strings.

## 9. Decisions already made — do not reopen

- New code lives in `analyst_driver/` as four modules; no new package.
- In-process FastMCP registries, not MCP stdio.
- Two harness tools only: `read_file`, `submit_decision`.
- Sequential tool execution within a round.
- `parse_decision` stays as fallback.
- Skills move to `skills/` at the repo root; content untouched.
- Anthropic model default `claude-opus-5`; thinking left at model default.
- No streaming, no compaction, no fallbacks, no token budget in v1.
- OpenAI surface is Chat Completions only (not the Responses API).

## 10. Open questions (answer during stage 4, not before)

- `cache_ttl` default: 5m is correct within a turn; whether 1h pays across
  turns depends on typical CASA job wall time on the target machine.
- Whether `ms-modify` descriptions (~10k tokens) should be shortened for
  small-context models. Only if a real run on TACC/local shows pressure.
- Whether `strict_tools` can be on for TACC's server (vLLM accepts
  `strict` on recent versions; unverified).
