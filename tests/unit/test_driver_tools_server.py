"""
Unit tests for analyst_driver/tools_server.py — the registry over MCP.

Two levels: the request handlers in-process over a fake registry (policy and
state-file behaviour), and one real stdio round trip through ``main()`` with
the real registry — the path ``claude -p`` takes, so the transport, the env
contract and the tool inventory are exercised, not assumed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent, Tool

from analyst_driver import tools_server as ts
from analyst_driver.tools import READ_FILE_NAME, SUBMIT_DECISION_NAME, ToolRegistry, TurnState

REPO = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO / "skills"


class FakeServer:
    """One script tool, one read-only tool; records the calls it receives."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        script = {
            "type": "object",
            "properties": {
                "params": {
                    "type": "object",
                    "properties": {
                        "ms_path": {"type": "string"},
                        "workdir": {"type": "string"},
                        "execute": {"type": "boolean", "default": False},
                    },
                }
            },
        }
        ro = {"type": "object", "properties": {"ms_path": {"type": "string"}}}
        return [
            Tool(name="ms_fake_script", description="writes a script", inputSchema=script),
            Tool(
                name="ms_fake_probe",
                description="measures",
                inputSchema=ro,
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        payload = {"status": "ok", "tool": name, "args": arguments}
        return ([TextContent(type="text", text=json.dumps(payload))], payload)


@pytest.fixture
def skill_tree(tmp_path: Path) -> Path:
    d = tmp_path / "skills" / "alpha-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: a\n---\n# alpha\n")
    (d / "01-detail.md").write_text("one\ntwo\n")
    return tmp_path / "skills"


@pytest.fixture
def served(skill_tree: Path, tmp_path: Path):
    fake = FakeServer()
    workdir = tmp_path / "work"
    workdir.mkdir()
    reg = ToolRegistry([fake], skill_root=skill_tree, read_roots=[])
    turn = TurnState(workdir=workdir)
    state_path = tmp_path / "turn_state.json"
    server = ts.build_server(reg, turn, state_path)
    return server, fake, turn, state_path, workdir


def _list(server) -> list[types.Tool]:
    handler = server.request_handlers[types.ListToolsRequest]
    res = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return res.root.tools


def _call(server, name: str, args: dict) -> types.CallToolResult:
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)
    )
    return asyncio.run(handler(req)).root


# ------------------------------------------------------------ in-process


def test_every_registry_tool_is_served_under_its_own_name(served):
    server, _, _, _, _ = served
    names = {t.name for t in _list(server)}
    assert names == {"ms_fake_script", "ms_fake_probe", READ_FILE_NAME, SUBMIT_DECISION_NAME}


def test_a_call_goes_through_dispatch_and_lands_in_the_state_file(served):
    server, fake, turn, state_path, workdir = served
    res = _call(server, "ms_fake_probe", {"ms_path": "/x.ms"})
    assert not res.isError
    assert fake.calls == [("ms_fake_probe", {"ms_path": "/x.ms"})]
    state = json.loads(state_path.read_text())
    assert state["tool_calls"][0]["tool"] == "ms_fake_probe"
    assert state["workdir"] == str(workdir)
    assert state["decision"] is None


def test_policy_is_the_registrys_not_the_servers(served):
    """R1: a script tool without workdir is rejected before it runs, and the
    rejection is counted on the turn — the same rule ApiBackend enforces."""
    server, fake, turn, state_path, _ = served
    res = _call(server, "ms_fake_script", {"params": {"ms_path": "/x.ms"}})
    assert res.isError
    assert "workdir" in res.content[0].text
    assert fake.calls == []
    assert json.loads(state_path.read_text())["rejections"] == {"R1": 1}


def test_submit_decision_is_recorded_not_executed(served):
    server, fake, turn, state_path, _ = served
    decision = {"stage": "flag", "script": "s.py", "tool": "ms_fake_script", "notes": "n"}
    res = _call(server, SUBMIT_DECISION_NAME, decision)
    assert not res.isError
    assert fake.calls == []
    state = json.loads(state_path.read_text())
    assert state["decision"] == decision
    assert state["decision_source"] == "tool"


def test_the_state_file_is_rewritten_after_every_call(served):
    server, _, _, state_path, _ = served
    _call(server, "ms_fake_probe", {"ms_path": "/a.ms"})
    first = json.loads(state_path.read_text())
    _call(server, "ms_fake_probe", {"ms_path": "/b.ms"})
    second = json.loads(state_path.read_text())
    assert len(first["tool_calls"]) == 1
    assert len(second["tool_calls"]) == 2
    assert second["rounds"] == 2


def test_turn_state_dict_is_json_serialisable(tmp_path: Path):
    turn = TurnState(workdir=tmp_path)
    turn.rejections["R2"] += 1
    d = ts.turn_state_dict(turn)
    json.dumps(d)
    assert d["rejections"] == {"R2": 1}
    assert d["workdir"] == str(tmp_path)


# ------------------------------------------------------------ env contract


def test_from_env_requires_workdir_and_state(tmp_path: Path):
    with pytest.raises(RuntimeError, match=ts.WORKDIR_ENV):
        ts.from_env({ts.STATE_ENV: str(tmp_path / "s.json")})
    with pytest.raises(RuntimeError, match=ts.STATE_ENV):
        ts.from_env({ts.WORKDIR_ENV: str(tmp_path)})


def test_from_env_reads_roots_and_builds_the_real_registry(tmp_path: Path):
    env = {
        ts.WORKDIR_ENV: str(tmp_path),
        ts.STATE_ENV: str(tmp_path / "s.json"),
        ts.SKILL_ROOT_ENV: str(SKILL_ROOT),
        ts.READ_ROOTS_ENV: os.pathsep.join([str(tmp_path / "r1"), str(tmp_path / "r2")]),
    }
    reg, turn, state_path = ts.from_env(env)
    assert turn.workdir == tmp_path
    assert state_path == tmp_path / "s.json"
    assert reg.read_roots == [tmp_path / "r1", tmp_path / "r2"]
    assert SUBMIT_DECISION_NAME in reg.names()


# ------------------------------------------------------------ stdio round trip


def test_stdio_round_trip_through_main(tmp_path: Path):
    """The path claude -p takes: a subprocess started from the MCP config's
    command + env, tools listed and called over stdio, state file on disk.
    read_file is called rather than a CASA tool, so no MS is needed."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_path = tmp_path / "turn_state.json"
    env = {
        **os.environ,
        ts.WORKDIR_ENV: str(workdir),
        ts.STATE_ENV: str(state_path),
        ts.SKILL_ROOT_ENV: str(SKILL_ROOT),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "analyst_driver.tools_server"], env=env
    )

    async def go():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            res = await session.call_tool(READ_FILE_NAME, {"path": "SKILL.md"})
            return {t.name for t in listed.tools}, res

    names, res = asyncio.run(go())
    assert {READ_FILE_NAME, SUBMIT_DECISION_NAME, "ms_workflow_status"} <= names
    assert not res.isError
    assert "SKILL.md" in res.content[0].text
    state = json.loads(state_path.read_text())
    assert state["tool_calls"][0]["tool"] == READ_FILE_NAME
    assert len(state["files_read"]) == 1
