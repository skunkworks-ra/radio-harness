"""
Unit tests for analyst_driver/tools.py — registry, policy, read_file,
submit_decision.

Two registries are used: the real one over the three FastMCP servers (for
inventory, classification and schema shape — CASA is not touched), and a
tiny fake server (for dispatch paths that would otherwise need an MS).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp.types import TextContent, Tool, ToolAnnotations

from analyst_driver.providers import ToolCall
from analyst_driver.tools import (
    READ_FILE_CAP,
    READ_FILE_NAME,
    RESULT_TEXT_CAP,
    SUBMIT_DECISION_NAME,
    ToolRegistry,
    TurnState,
    inline_refs,
)

REPO = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO / "skills" if (REPO / "skills").is_dir() else REPO / ".claude" / "skills"

SCRIPT_TOOLS = {
    "ms_set_intents",
    "ms_initial_bandpass",
    "ms_apply_rflag",
    "ms_apply_preflag",
    "ms_generate_priorcals",
    "ms_setjy",
    "ms_setjy_polcal",
    "ms_apply_initial_rflag",
    "ms_postcal_flag",
    "ms_flag_caltable",
    "ms_gaincal",
    "ms_polcal",
    "ms_bandpass",
    "ms_fluxscale",
    "ms_applycal",
    "ms_tclean",
    "ms_import_asdm",
}
BOOKKEEPING_TOOLS = {"ms_supersede_stage", "ms_reduction_log"}


# ------------------------------------------------------------ fake server


def _schema(execute: bool) -> dict:
    props = {"ms_path": {"type": "string", "minLength": 1}, "workdir": {"type": "string"}}
    if execute:
        props["execute"] = {"type": "boolean", "default": False}
    return {
        "$defs": {"In": {"type": "object", "properties": props, "additionalProperties": False}},
        "properties": {"params": {"$ref": "#/$defs/In"}},
        "required": ["params"],
        "type": "object",
    }


class FakeServer:
    """Quacks like FastMCP for list_tools / call_tool."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.big = False
        self.raise_on: set[str] = set()

    async def list_tools(self):
        ro = ToolAnnotations(readOnlyHint=True)
        rw = ToolAnnotations(readOnlyHint=False)
        return [
            Tool(
                name="fake_inspect", description="reads", inputSchema=_schema(False), annotations=ro
            ),
            Tool(
                name="fake_script", description="writes", inputSchema=_schema(True), annotations=rw
            ),
            Tool(
                name="fake_script2", description="writes", inputSchema=_schema(True), annotations=rw
            ),
            Tool(
                name="fake_log", description="records", inputSchema=_schema(False), annotations=rw
            ),
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.raise_on:
            raise RuntimeError("boom")
        payload = {"status": "ok", "tool": name, "args": arguments}
        if self.big:
            payload["blob"] = "x" * (RESULT_TEXT_CAP + 100)
        return ([TextContent(type="text", text=json.dumps(payload))], payload)


@pytest.fixture
def skill_tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name in ("alpha-skill", "beta-skill"):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\ndescription: {name}\n---\n# {name}\nbody\n")
        (d / "01-detail.md").write_text("line one\nline two\nline three\n")
    (root / "not-a-skill").mkdir()
    return root


@pytest.fixture
def fake(skill_tree: Path, tmp_path: Path):
    server = FakeServer()
    workdir = tmp_path / "work"
    workdir.mkdir()
    runroot = tmp_path / "runs"
    runroot.mkdir()
    reg = ToolRegistry([server], skill_root=skill_tree, read_roots=[runroot])
    return reg, server, TurnState(workdir=workdir), runroot


def _call(name, args, id_="c1"):
    return ToolCall(id=id_, name=name, args=args)


# -------------------------------------------------------------- inline_refs


def test_inline_refs_replaces_and_drops_defs():
    out = inline_refs(_schema(True))
    assert "$defs" not in out
    assert "$ref" not in json.dumps(out)
    assert out["properties"]["params"]["properties"]["execute"]["default"] is False
    assert out["properties"]["params"]["additionalProperties"] is False


def test_inline_refs_unresolvable_raises():
    with pytest.raises(ValueError):
        inline_refs({"properties": {"p": {"$ref": "#/$defs/Missing"}}})


# ---------------------------------------------------- real registry inventory


@pytest.fixture(scope="module")
def real():
    return ToolRegistry.default(skill_root=SKILL_ROOT, read_roots=[])


def test_real_registry_inventory(real: ToolRegistry):
    names = real.names()
    assert len(names) == 55  # 34 + 16 + 3 CASA tools + 2 harness tools
    assert names == sorted(names)
    assert READ_FILE_NAME in names and SUBMIT_DECISION_NAME in names
    by_class = {}
    for n in names:
        by_class.setdefault(real.classify(n), set()).add(n)
    assert by_class["script"] == SCRIPT_TOOLS
    assert by_class["bookkeeping"] == BOOKKEEPING_TOOLS
    assert by_class["harness"] == {READ_FILE_NAME, SUBMIT_DECISION_NAME}
    assert len(by_class["read_only"]) == 34


def test_real_registry_schemas_are_flat_and_wrapped(real: ToolRegistry):
    for spec in real.specs():
        s = json.dumps(spec.input_schema)
        assert "$ref" not in s and "$defs" not in s, spec.name
        if real.classify(spec.name) != "harness":
            assert "params" in spec.input_schema["properties"], spec.name
    strict = [s.name for s in real.specs() if s.strict]
    assert strict == [SUBMIT_DECISION_NAME]


def test_schema_size_ceiling(real: ToolRegistry):
    total = sum(
        len(json.dumps({"n": s.name, "d": s.description, "s": s.input_schema}, sort_keys=True))
        for s in real.specs()
    )
    assert total < 120_000, f"tool schemas are {total} chars (~{total // 4} tokens)"


def test_real_dispatch_returns_envelope_not_exception(real: ToolRegistry, tmp_path: Path):
    turn = TurnState(workdir=tmp_path)
    res = real.dispatch(
        _call("ms_observation_info", {"params": {"ms_path": str(tmp_path / "nope.ms")}}), turn
    )
    assert res.is_error is False  # an error envelope is data, not a failure
    assert '"MS_NOT_FOUND"' in res.text
    assert turn.tool_calls == [{"tool": "ms_observation_info", "result": res.text}]


# ------------------------------------------------------------------ policy


def test_r1_execute_true_rejected(fake):
    reg, server, turn, _ = fake
    res = reg.dispatch(
        _call("fake_script", {"params": {"ms_path": "/a", "workdir": "/w", "execute": True}}), turn
    )
    assert res.is_error and "execute=false" in res.text
    assert turn.rejections["R1"] == 1
    assert server.calls == []
    assert turn.transcript[-1]["type"] == "rejected" and turn.transcript[-1]["rule"] == "R1"


def test_r1_missing_workdir_rejected(fake):
    reg, server, turn, _ = fake
    res = reg.dispatch(_call("fake_script", {"params": {"ms_path": "/a"}}), turn)
    assert res.is_error and "workdir=" in res.text and str(turn.workdir) in res.text
    assert server.calls == []


def test_script_tool_runs_with_execute_pinned_false(fake):
    reg, server, turn, _ = fake
    res = reg.dispatch(_call("fake_script", {"params": {"ms_path": "/a", "workdir": "/w"}}), turn)
    assert not res.is_error
    assert server.calls == [
        ("fake_script", {"params": {"ms_path": "/a", "workdir": "/w", "execute": False}})
    ]
    assert turn.script_calls == 1 and turn.last_script_tool == "fake_script"


def test_r2_second_script_tool_rejected(fake):
    reg, server, turn, _ = fake
    reg.dispatch(_call("fake_script", {"params": {"ms_path": "/a", "workdir": "/w"}}), turn)
    res = reg.dispatch(
        _call("fake_script2", {"params": {"ms_path": "/a", "workdir": "/w"}}, "c2"), turn
    )
    assert res.is_error and "already called" in res.text and "fake_script" in res.text
    assert turn.rejections["R2"] == 1
    assert [c[0] for c in server.calls] == ["fake_script"]


def test_read_only_and_bookkeeping_are_not_capped(fake):
    reg, server, turn, _ = fake
    for name in ("fake_inspect", "fake_log", "fake_inspect"):
        res = reg.dispatch(_call(name, {"params": {"ms_path": "/a"}}), turn)
        assert not res.is_error
    assert turn.script_calls == 0 and len(server.calls) == 3


def test_r3_unknown_tool(fake):
    reg, server, turn, _ = fake
    res = reg.dispatch(_call("nope", {}), turn)
    assert res.is_error and "unknown tool" in res.text and turn.rejections["R3"] == 1


def test_r4_unparsed_args(fake):
    reg, server, turn, _ = fake
    res = reg.dispatch(ToolCall(id="x", name="fake_inspect", args=None, raw_args="{oops"), turn)
    assert res.is_error and "{oops" in res.text and turn.rejections["R4"] == 1
    assert server.calls == []


def test_tool_exception_becomes_error_result(fake):
    reg, server, turn, _ = fake
    server.raise_on.add("fake_inspect")
    res = reg.dispatch(_call("fake_inspect", {"params": {"ms_path": "/a"}}), turn)
    assert res.is_error and res.text == "RuntimeError: boom"


def test_result_cap_applied_and_reported(fake):
    reg, server, turn, _ = fake
    server.big = True
    res = reg.dispatch(_call("fake_inspect", {"params": {"ms_path": "/a"}}), turn)
    assert len(res.text) < RESULT_TEXT_CAP + 200
    assert "[truncated:" in res.text
    assert turn.truncated_results == 1
    assert len(turn.tool_calls[0]["result"]) > RESULT_TEXT_CAP  # journal keeps it all
    assert turn.transcript[-1]["truncated"] is True


# --------------------------------------------------------- submit_decision


def test_submit_decision_records_and_does_not_run(fake):
    reg, server, turn, _ = fake
    dec = {"script": "/w/s.py", "stage": "setjy", "notes": "ok", "cited": []}
    res = reg.dispatch(_call(SUBMIT_DECISION_NAME, dec), turn)
    assert not res.is_error and res.text == "decision recorded"
    assert turn.decision == dec and turn.decision_source == "tool"
    assert server.calls == []


# --------------------------------------------------------------- read_file


def test_read_file_bare_name_resolves_into_skill_dir(fake, skill_tree):
    reg, _, turn, _ = fake
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": "01-detail.md"}), turn)
    assert not res.is_error
    assert res.text.startswith(str(skill_tree / "alpha-skill" / "01-detail.md"))
    assert "     2\tline two" in res.text
    assert turn.files_read == [str(skill_tree / "alpha-skill" / "01-detail.md")]


def test_read_file_line_range(fake):
    reg, _, turn, _ = fake
    res = reg.dispatch(
        _call(READ_FILE_NAME, {"path": "01-detail.md", "start_line": 2, "end_line": 2}), turn
    )
    assert "(lines 2-2 of 3)" in res.text
    assert "line one" not in res.text and "line two" in res.text


def test_read_file_relative_to_workdir(fake):
    reg, _, turn, _ = fake
    (turn.workdir / "casa.log").write_text("hello\n")
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": "casa.log"}), turn)
    assert not res.is_error and "hello" in res.text


def test_read_file_absolute_under_run_root(fake):
    reg, _, turn, runroot = fake
    log = runroot / "r1" / "jobs" / "0001" / "job.log"
    log.parent.mkdir(parents=True)
    log.write_text("job output\n")
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": str(log)}), turn)
    assert not res.is_error and "job output" in res.text


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "does-not-exist.md"])
def test_read_file_outside_roots_rejected(fake, bad):
    reg, _, turn, _ = fake
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": bad}), turn)
    assert res.is_error and "allowed root" in res.text
    assert turn.rejections["R5"] == 1 and turn.files_read == []


def test_read_file_symlink_escape_rejected(fake, tmp_path):
    reg, _, turn, _ = fake
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, turn.workdir / "link.txt")
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": "link.txt"}), turn)
    assert res.is_error and turn.rejections["R5"] == 1


def test_read_file_size_cap(fake):
    reg, _, turn, _ = fake
    big = turn.workdir / "big.txt"
    big.write_bytes(b"x" * (READ_FILE_CAP + 1))
    res = reg.dispatch(_call(READ_FILE_NAME, {"path": "big.txt"}), turn)
    assert res.is_error and "limit" in res.text and turn.rejections["R5"] == 1


def test_read_file_description_names_skills(fake, skill_tree):
    reg, _, _, _ = fake
    spec = next(s for s in reg.specs() if s.name == READ_FILE_NAME)
    assert "alpha-skill, beta-skill" in spec.description
    assert "not-a-skill" not in spec.description
    assert str(skill_tree) in spec.description
