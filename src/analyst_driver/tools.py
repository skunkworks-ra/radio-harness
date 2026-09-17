"""Tool registry, policy, and the two harness-owned tools.

The registry is the one source of truth for what the model can call. The
CASA tools come from the three FastMCP servers imported in-process:
``list_tools()`` gives the schemas, ``call_tool()`` runs them. Nothing here
re-declares a CASA tool. The harness adds exactly two of its own:

- ``read_file`` — skill sub-files, the work directory, job logs. Read-only,
  root-confined, size-capped.
- ``submit_decision`` — the turn's decision as a schema-checked tool call.
  It is recorded, not executed; the agent stops after the round that
  called it.

Policy is enforced here, at dispatch, before anything runs. The model gets
an error result naming the rule; the rejection is counted on the turn.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from analyst_driver.providers import ToolCall, ToolResult, ToolSpec

ToolClass = Literal["read_only", "bookkeeping", "script", "harness"]

#: Characters of a tool result returned to the model. The full text still
#: goes to the journal.
RESULT_TEXT_CAP = 60_000
#: Bytes of a file read_file will open.
READ_FILE_CAP = 200_000

READ_FILE_NAME = "read_file"
SUBMIT_DECISION_NAME = "submit_decision"

SUBMIT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "Path to the generated script. Omit only with done=true.",
        },
        "tool": {"type": "string", "description": "The script-producing tool you called."},
        "stage": {"type": "string", "description": "The stage this advances."},
        "done": {
            "type": "boolean",
            "description": "True when the reduction is finished and no stage remains.",
        },
        "cited": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {},
                    "source": {"type": "string"},
                },
                "required": ["name", "value", "source"],
                "additionalProperties": False,
            },
        },
        "outputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["caltable", "image", "plot", "ms", "other"],
                    },
                },
                "required": ["path", "kind"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "One sentence."},
        "belief_state": {"type": "string"},
    },
    "required": ["notes"],
    "additionalProperties": False,
}

READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "A skill file name (e.g. '07-calibration-execution.md'), a path relative "
                "to the work directory, or an absolute path under an allowed root."
            ),
        },
        "start_line": {"type": "integer", "description": "1-based, inclusive."},
        "end_line": {"type": "integer", "description": "1-based, inclusive."},
    },
    "required": ["path"],
    "additionalProperties": False,
}


@dataclass
class TurnState:
    """What one turn has done so far. Written by dispatch, read by the agent."""

    workdir: Path
    script_calls: int = 0
    last_script_tool: str | None = None
    decision: dict[str, Any] | None = None
    decision_source: Literal["tool", "text"] | None = None
    rounds: int = 0
    #: ``{"tool": name, "result": full_text}`` — the shape loop.py harvests.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)
    files_read: list[str] = field(default_factory=list)
    truncated_results: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace every local ``$ref`` with its definition and drop ``$defs``.

    Some OpenAI-compatible servers reject ``$ref``. The FastMCP schemas are
    not recursive, so a plain substitution terminates.
    """
    defs = schema.get("$defs") or {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref[len("#/$defs/") :])
                if target is None:
                    raise ValueError(f"unresolvable $ref {ref!r}")
                merged = {**walk(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(schema))


def _run(coro):
    return asyncio.run(coro)


def _result_text(result: Any) -> str:
    """Text of a FastMCP call_tool result: ``(blocks, structured)`` or a list."""
    blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, dict):
        return json.dumps(blocks, sort_keys=True)
    parts = []
    for b in blocks or []:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
    return "".join(parts)


@dataclass(frozen=True)
class _Entry:
    spec: ToolSpec
    cls: ToolClass
    server: Any  # FastMCP, or None for harness tools


class ToolRegistry:
    def __init__(
        self,
        servers: list[Any],
        *,
        skill_root: Path,
        read_roots: list[Path],
    ):
        self.skill_root = Path(skill_root)
        self.skill_dirs = sorted(p for p in self.skill_root.iterdir() if (p / "SKILL.md").is_file())
        self.read_roots = [Path(p) for p in read_roots]
        self._entries: dict[str, _Entry] = {}
        for server in servers:
            for t in _run(server.list_tools()):
                schema = inline_refs(t.inputSchema)
                spec = ToolSpec(name=t.name, description=t.description or "", input_schema=schema)
                self._entries[t.name] = _Entry(
                    spec=spec, cls=self._classify_mcp(t, schema), server=server
                )
        self._entries[READ_FILE_NAME] = _Entry(
            spec=ToolSpec(READ_FILE_NAME, self._read_file_description(), READ_FILE_SCHEMA),
            cls="harness",
            server=None,
        )
        self._entries[SUBMIT_DECISION_NAME] = _Entry(
            spec=ToolSpec(
                SUBMIT_DECISION_NAME,
                "Record this turn's decision and end the turn. Call it exactly once, last. "
                "A turn that advances a stage names the script a script-producing tool wrote; "
                "a finished reduction sets done=true and names no script.",
                SUBMIT_DECISION_SCHEMA,
                strict=True,
            ),
            cls="harness",
            server=None,
        )

    @classmethod
    def default(cls, *, skill_root: Path, read_roots: list[Path]) -> ToolRegistry:
        from ms_create.server import mcp as create
        from ms_inspect.server import mcp as inspect
        from ms_modify.server import mcp as modify

        return cls([inspect, modify, create], skill_root=skill_root, read_roots=read_roots)

    @staticmethod
    def _classify_mcp(tool: Any, schema: dict[str, Any]) -> ToolClass:
        ann = getattr(tool, "annotations", None)
        if ann is not None and getattr(ann, "readOnlyHint", False):
            return "read_only"
        # A tool that takes execute writes a script when execute is false;
        # the loop runs the script. A write tool without execute only keeps
        # records (stage log, reduction log).
        params = (schema.get("properties") or {}).get("params") or {}
        return "script" if "execute" in (params.get("properties") or {}) else "bookkeeping"

    def _read_file_description(self) -> str:
        names = ", ".join(p.name for p in self.skill_dirs)
        return (
            "Read a text file, returned with line numbers. Skill sub-files named in the "
            f"system prompt live under {self.skill_root} (skills: {names}) and are read by "
            "bare file name. Relative paths also resolve against the work directory. "
            "Absolute paths must lie under the skill root, the work directory, or the run "
            "directory (job logs). Read-only."
        )

    # ------------------------------------------------------------ queries

    def specs(self) -> list[ToolSpec]:
        return [self._entries[n].spec for n in sorted(self._entries)]

    def names(self) -> list[str]:
        return sorted(self._entries)

    def classify(self, name: str) -> ToolClass:
        return self._entries[name].cls

    # ----------------------------------------------------------- dispatch

    def dispatch(self, call: ToolCall, turn: TurnState) -> ToolResult:
        rejected = self._policy(call, turn)
        if rejected is not None:
            rule, why = rejected
            turn.rejections[rule] += 1
            turn.transcript.append(
                {"type": "rejected", "rule": rule, "tool": call.name, "id": call.id, "why": why}
            )
            return ToolResult(call.id, why, is_error=True)

        entry = self._entries[call.name]
        args = dict(call.args or {})
        if call.name == SUBMIT_DECISION_NAME:
            turn.decision = args
            turn.decision_source = "tool"
            text, is_error = "decision recorded", False
        elif call.name == READ_FILE_NAME:
            text, is_error = self._read_file(args, turn)
        else:
            text, is_error = self._call_mcp(entry, args, turn)

        turn.tool_calls.append({"tool": call.name, "result": text})
        shown = text
        truncated = False
        if len(shown) > RESULT_TEXT_CAP:
            truncated = True
            turn.truncated_results += 1
            shown = (
                shown[:RESULT_TEXT_CAP]
                + f"\n[truncated: {len(text) - RESULT_TEXT_CAP} more chars; full payload in the journal]"
            )
        turn.transcript.append(
            {
                "type": "tool_result",
                "id": call.id,
                "tool": call.name,
                "text": text,
                "is_error": is_error,
                "truncated": truncated,
            }
        )
        return ToolResult(call.id, shown, is_error=is_error)

    def _policy(self, call: ToolCall, turn: TurnState) -> tuple[str, str] | None:
        if call.name not in self._entries:
            return "R3", f"unknown tool {call.name!r}"
        if call.args is None:
            raw = (call.raw_args or "")[:200]
            return "R4", f"arguments for {call.name} did not parse as a JSON object: {raw!r}"
        if self._entries[call.name].cls == "script":
            params = call.args.get("params")
            if not isinstance(params, dict):
                return "R4", f"{call.name} takes its arguments under a 'params' object"
            if params.get("execute") not in (False, None):
                return "R1", (
                    f"{call.name} must be called with execute=false; "
                    "the loop executes the script it writes"
                )
            if not params.get("workdir"):
                return "R1", (
                    f"{call.name} needs workdir={str(turn.workdir)!r} so the script "
                    "is written where the loop can run it"
                )
            if turn.script_calls >= 1:
                return "R2", (
                    f"a script-producing tool was already called this turn "
                    f"({turn.last_script_tool}); call {SUBMIT_DECISION_NAME}"
                )
        return None

    def _call_mcp(self, entry: _Entry, args: dict, turn: TurnState) -> tuple[str, bool]:
        if entry.cls == "script":
            # execute defaults to false in every schema; pin it so the default
            # cannot drift underneath the policy.
            args = {**args, "params": {**args["params"], "execute": False}}
            turn.script_calls += 1
            turn.last_script_tool = entry.spec.name
        try:
            result = _run(entry.server.call_tool(entry.spec.name, args))
        except Exception as e:  # noqa: BLE001 — anything from the tool layer is data here
            return f"{type(e).__name__}: {e}", True
        return _result_text(result), False

    # ---------------------------------------------------------- read_file

    def _allowed_roots(self, turn: TurnState) -> list[Path]:
        return [self.skill_root, Path(turn.workdir), *self.read_roots]

    def _resolve(self, raw: str, turn: TurnState) -> Path | None:
        p = Path(raw)
        candidates: list[Path]
        if p.is_absolute():
            candidates = [p]
        else:
            candidates = [d / p for d in self.skill_dirs] + [Path(turn.workdir) / p]
        roots = [r.resolve() for r in self._allowed_roots(turn)]
        for c in candidates:
            if not c.is_file():
                continue
            real = c.resolve()
            if any(real == r or real.is_relative_to(r) for r in roots):
                return real
        return None

    def _read_file(self, args: dict, turn: TurnState) -> tuple[str, bool]:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw:
            return "read_file needs a 'path' string", True
        real = self._resolve(raw, turn)
        if real is None:
            roots = ", ".join(str(r) for r in self._allowed_roots(turn))
            turn.rejections["R5"] += 1
            return f"{raw!r} is not a readable file under an allowed root ({roots})", True
        size = real.stat().st_size
        if size > READ_FILE_CAP:
            turn.rejections["R5"] += 1
            return f"{real} is {size} bytes; the limit is {READ_FILE_CAP}", True
        try:
            lines = real.read_text(errors="replace").splitlines()
        except OSError as e:
            return f"{type(e).__name__}: {e}", True
        start = args.get("start_line") or 1
        end = args.get("end_line") or len(lines)
        start = max(1, int(start))
        end = min(len(lines), int(end))
        turn.files_read.append(str(real))
        body = "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, end + 1))
        return f"{real} (lines {start}-{end} of {len(lines)})\n{body}", False
