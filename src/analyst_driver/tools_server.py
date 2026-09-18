"""The harness tool registry as a stdio MCP server, for the ``claude -p`` backend.

``ApiBackend`` calls ``ToolRegistry.dispatch`` in-process. ``ClaudeBackend``
cannot: the model runs inside a ``claude`` subprocess and reaches tools only
over MCP. This module is that bridge and nothing more. Every tool the
registry offers — the CASA tools, ``read_file``, ``submit_decision`` — is
served under its registry name, and every call goes through the same
``dispatch`` with the same policy. Nothing here re-declares a tool or a rule.

One server process serves one turn: ``claude -p`` starts it and ends it. The
turn's ``TurnState`` is written to ``ANALYST_TURN_STATE`` after every call,
so the backend can read the decision and the tool calls once ``claude``
exits, whether or not the server shut down cleanly.

Configuration is by environment, set by ``ClaudeBackend`` in the MCP config
it hands to ``claude``:

- ``ANALYST_TURN_WORKDIR`` — the turn's work directory (required)
- ``ANALYST_TURN_STATE``   — path the ``TurnState`` JSON is written to (required)
- ``ANALYST_SKILL_ROOT``   — skill root; default ``skills.default_skill_root()``
- ``ANALYST_READ_ROOTS``   — extra ``read_file`` roots, ``os.pathsep``-separated
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from analyst_driver.providers import ToolCall
from analyst_driver.tools import ToolRegistry, TurnState

SERVER_NAME = "analyst"

STATE_ENV = "ANALYST_TURN_STATE"
WORKDIR_ENV = "ANALYST_TURN_WORKDIR"
SKILL_ROOT_ENV = "ANALYST_SKILL_ROOT"
READ_ROOTS_ENV = "ANALYST_READ_ROOTS"


def turn_state_dict(turn: TurnState) -> dict[str, Any]:
    """``TurnState`` as JSON-serialisable data. ``ClaudeBackend`` reads it back."""
    d = asdict(turn)
    d["workdir"] = str(turn.workdir)
    d["rejections"] = dict(turn.rejections)
    return d


def write_turn_state(turn: TurnState, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(turn_state_dict(turn), default=str, sort_keys=True))
    os.replace(tmp, path)


def build_server(registry: ToolRegistry, turn: TurnState, state_path: Path) -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in registry.specs()
        ]

    # validate_input=False: the registry's policy is the one gate, as in
    # ApiBackend, and reports a violation as a tool error the model can act on.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        call = ToolCall(id=f"{name}:{turn.rounds}", name=name, args=arguments)
        turn.rounds += 1
        # dispatch runs the CASA tools with asyncio.run, which cannot nest
        # inside this server's loop; a worker thread gives it a loop of its own.
        result = await asyncio.to_thread(registry.dispatch, call, turn)
        write_turn_state(turn, state_path)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.text)],
            isError=result.is_error,
        )

    return server


def from_env(environ: dict[str, str] | None = None) -> tuple[ToolRegistry, TurnState, Path]:
    env = os.environ if environ is None else environ
    try:
        workdir = Path(env[WORKDIR_ENV])
        state_path = Path(env[STATE_ENV])
    except KeyError as e:
        raise RuntimeError(f"environment variable {e.args[0]} is not set") from None
    skill_root = env.get(SKILL_ROOT_ENV)
    if skill_root:
        root = Path(skill_root)
    else:
        from analyst_driver.skills import default_skill_root

        root = default_skill_root()
    read_roots = [Path(p) for p in env.get(READ_ROOTS_ENV, "").split(os.pathsep) if p]
    registry = ToolRegistry.default(skill_root=root, read_roots=read_roots)
    return registry, TurnState(workdir=workdir), state_path


async def serve(registry: ToolRegistry, turn: TurnState, state_path: Path) -> None:
    server = build_server(registry, turn, state_path)
    # An empty state file up front: a turn in which the model never called a
    # tool is then distinguishable from one in which the server never started.
    write_turn_state(turn, state_path)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    registry, turn, state_path = from_env()
    asyncio.run(serve(registry, turn, state_path))


if __name__ == "__main__":
    main()
