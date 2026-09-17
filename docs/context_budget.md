# Context budget — native harness prefix

Measured 2026-09-16 on branch `native-harness` (`b232481`). Token figures are
chars/4; the exact Anthropic count (`client.messages.count_tokens`) is still
to be recorded in stage 4 when a key is available. Regression guard:
`tests/unit/test_driver_tools.py::test_schema_size_ceiling`.

Command (from the repo root):

```
pixi run python - <<'EOF'
import asyncio, json
from ms_inspect.server import mcp as a
from ms_modify.server import mcp as b
from ms_create.server import mcp as c
async def main():
    for nm, m in (("ms-inspect", a), ("ms-modify", b), ("ms-create", c)):
        ts = await m.list_tools()
        n = sum(len(json.dumps({"name": t.name, "description": t.description,
                                "input_schema": t.inputSchema}, sort_keys=True)) for t in ts)
        print(nm, len(ts), "tools", n, "chars")
asyncio.run(main())
EOF
```

## Tool schemas (name + description + raw inputSchema, sorted keys)

| server | tools | chars | ~tokens |
|---|---|---|---|
| ms-inspect | 34 | 34,873 | ~8,718 |
| ms-modify | 16 | 41,829 | ~10,457 |
| ms-create | 3 | 4,342 | ~1,085 |
| **all** | 53 | 81,044 | ~20,261 |

Largest ten:

| tool | server | chars | ~tokens |
|---|---|---|---|
| ms_tclean | ms-modify | 5,636 | ~1,409 |
| ms_gaincal | ms-modify | 3,571 | ~892 |
| ms_postcal_flag | ms-modify | 3,104 | ~776 |
| ms_initial_bandpass | ms-modify | 2,937 | ~734 |
| ms_applycal | ms-modify | 2,936 | ~734 |
| ms_bandpass | ms-modify | 2,935 | ~733 |
| ms_setjy_polcal | ms-modify | 2,837 | ~709 |
| ms_setjy | ms-modify | 2,726 | ~681 |
| ms_polcal | ms-modify | 2,706 | ~676 |
| ms_verify_model | ms-inspect | 2,505 | ~626 |

Ref inlining (PLAN §4.1) will grow these slightly (each `$ref` becomes its
definition; every schema has exactly one, so the growth is the `$defs`
header overhead only, not duplication).

## Skills

| piece | chars | ~tokens |
|---|---|---|
| `stage-orchestration/SKILL.md` | 2,854 | ~713 |
| `radio-interferometry-driver/SKILL.md` | 4,250 | ~1,062 |
| all 20 skill files | 182,563 | ~45,640 |
| largest sub-file, `07-calibration-execution.md` | 32,833 | ~8,200 |
| next: `10-precal-workflow.md`, `09-polcal-execution.md`, `11-imaging.md` | 19.4k / 18.8k / 16.3k | ~4.8k / ~4.7k / ~4.1k |

## Per-turn picture

| | ~tokens |
|---|---|
| fixed prefix: tools + two SKILL.md + preamble | ~22–23k |
| brief (template + `ms_workflow_status` JSON) | ~2–4k |
| typical turn: 2–4 sub-files read + 3–10 tool results | +15–45k |
| **typical turn total** | **~40–70k** |
| worst plausible turn (every sub-file read) | ~110k |

Fits: Anthropic 1M; TACC pool 128k–256k; local Gemma E4B 128k. The worst
plausible turn is inside 128k but leaves little room for large tool
results on a 128k model — the `RESULT_TEXT_CAP` (PLAN §4.1) exists for that
case. Context does not accumulate across outer turns (DESIGN P8).
