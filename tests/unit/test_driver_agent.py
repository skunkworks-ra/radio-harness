"""
Unit tests for analyst_driver/agent.py and ApiBackend — the inner loop over
FakeProvider and a fake FastMCP server, and its mapping into BackendResult
as the outer loop consumes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_driver_tools import FakeServer

from analyst_driver.agent import Agent, sum_usage, transcript_jsonl
from analyst_driver.backends import ApiBackend
from analyst_driver.db import DriverDB
from analyst_driver.executors import LocalExecutor
from analyst_driver.loop import Loop
from analyst_driver.providers import FakeProvider, ToolCall, Usage, reply
from analyst_driver.tools import SUBMIT_DECISION_NAME, ToolRegistry

SYSTEM = "SYSTEM PROMPT"


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path / "skills"
    d = root / "only-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# only\n")
    (d / "01-detail.md").write_text("detail text\n")
    workdir = tmp_path / "work"
    workdir.mkdir()
    server = FakeServer()
    reg = ToolRegistry([server], skill_root=root, read_roots=[])
    return reg, server, workdir


def _script_call(id_="s1", **extra):
    return ToolCall(id_, "fake_script", {"params": {"ms_path": "/a", "workdir": "/w", **extra}})


def _decision_call(id_="d1", **fields):
    return ToolCall(id_, SUBMIT_DECISION_NAME, {"notes": "ok", **fields})


def _agent(env, replies, **kw):
    reg, _, _ = env
    fp = FakeProvider(replies)
    return Agent(fp, reg, SYSTEM, **kw), fp


# ------------------------------------------------------------------ happy path


def test_happy_path_three_rounds(env):
    reg, server, workdir = env
    agent, fp = _agent(
        env,
        [
            reply("look", tool_calls=[ToolCall("r1", "read_file", {"path": "01-detail.md"})]),
            reply("write", tool_calls=[_script_call()]),
            reply(tool_calls=[_decision_call(script="/w/s.py", stage="x")]),
        ],
    )
    turn, error = agent.run_turn("BRIEF", workdir)
    assert error is None
    assert turn.rounds == 3
    assert turn.decision == {"notes": "ok", "script": "/w/s.py", "stage": "x"}
    assert turn.decision_source == "tool"
    assert [c["tool"] for c in turn.tool_calls] == [
        "read_file",
        "fake_script",
        SUBMIT_DECISION_NAME,
    ]
    assert turn.files_read and turn.files_read[0].endswith("01-detail.md")
    assert server.calls[0][1]["params"]["execute"] is False
    # every request carried the same system prompt and tool list
    assert {r.system for r in fp.requests} == {SYSTEM}
    assert all([t.name for t in r.tools] == reg.names() for r in fp.requests)
    # history grew: user, assistant, tool results, assistant, tool results, ...
    assert [m["role"] for m in fp.requests[-1].history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    kinds = [r["type"] for r in turn.transcript]
    assert kinds[:2] == ["system", "user"] and kinds[-1] == "summary"
    assert turn.transcript[-1]["decision_source"] == "tool"
    assert turn.transcript[-1]["n_rejected"] == 0


def test_text_fallback_decision(env):
    _, _, workdir = env
    agent, _ = _agent(env, [reply('done here {"done": true, "notes": "n"}')])
    turn, error = agent.run_turn("B", workdir)
    assert error is None
    assert turn.decision == {"done": True, "notes": "n"}
    assert turn.decision_source == "text"
    assert turn.rounds == 1


# ---------------------------------------------------------------- policy paths


def test_execute_true_rejected_then_corrected(env):
    _, server, workdir = env
    agent, fp = _agent(
        env,
        [
            reply(tool_calls=[_script_call("s1", execute=True)]),
            reply(tool_calls=[_script_call("s2")]),
            reply(tool_calls=[_decision_call(script="/w/s.py")]),
        ],
    )
    turn, error = agent.run_turn("B", workdir)
    assert error is None
    assert turn.rejections == {"R1": 1}
    assert [c[0] for c in server.calls] == ["fake_script"]
    # the model saw the rejection as an error result
    err_msg = fp.requests[1].history[-1]
    assert err_msg["content"][0]["is_error"] is True
    assert "execute=false" in err_msg["content"][0]["content"]


def test_second_script_tool_rejected(env):
    _, server, workdir = env
    agent, _ = _agent(
        env,
        [
            reply(
                tool_calls=[
                    _script_call("s1"),
                    ToolCall("s2", "fake_script2", {"params": {"ms_path": "/a", "workdir": "/w"}}),
                ]
            ),
            reply(tool_calls=[_decision_call(script="/w/s.py")]),
        ],
    )
    turn, error = agent.run_turn("B", workdir)
    assert error is None
    assert turn.rejections == {"R2": 1}
    assert [c[0] for c in server.calls] == ["fake_script"]


def test_parallel_calls_run_in_order_and_return_together(env):
    _, server, workdir = env
    agent, fp = _agent(
        env,
        [
            reply(
                tool_calls=[
                    ToolCall("a", "fake_inspect", {"params": {"ms_path": "/1"}}),
                    ToolCall("b", "fake_log", {"params": {"ms_path": "/2"}}),
                    ToolCall("c", "fake_inspect", {"params": {"ms_path": "/3"}}),
                ]
            ),
            reply(tool_calls=[_decision_call(done=True)]),
        ],
    )
    turn, error = agent.run_turn("B", workdir)
    assert error is None
    assert [c[1]["params"]["ms_path"] for c in server.calls] == ["/1", "/2", "/3"]
    batch = fp.requests[1].history[-1]["content"]
    assert [r["tool_use_id"] for r in batch] == ["a", "b", "c"]


def test_malformed_args_error_result_and_loop_continues(env):
    _, _, workdir = env
    agent, fp = _agent(
        env,
        [
            reply(tool_calls=[ToolCall("x", "fake_inspect", None, raw_args="{bad")]),
            reply(tool_calls=[_decision_call(done=True)]),
        ],
    )
    turn, error = agent.run_turn("B", workdir)
    assert error is None and turn.rejections == {"R4": 1}
    assert fp.requests[1].history[-1]["content"][0]["is_error"] is True


# -------------------------------------------------------------- stop conditions


def test_max_rounds_exhaustion(env):
    _, _, workdir = env
    calls = [
        reply(tool_calls=[ToolCall(f"i{n}", "fake_inspect", {"params": {"ms_path": "/a"}})])
        for n in range(5)
    ]
    agent, fp = _agent(env, calls, max_rounds=3)
    turn, error = agent.run_turn("B", workdir)
    assert turn.rounds == 3 and len(fp.requests) == 3
    assert turn.decision is None
    assert error == "no decision after max_rounds=3"


def test_refusal_stops_the_turn(env):
    _, server, workdir = env
    agent, _ = _agent(env, [reply("refusal: cyber", stop="refusal", tool_calls=[_script_call()])])
    turn, error = agent.run_turn("B", workdir)
    assert error.startswith("model refused")
    assert server.calls == []  # a refused turn's tool calls never run
    assert turn.decision is None


def test_end_turn_without_decision(env):
    _, _, workdir = env
    agent, _ = _agent(env, [reply("I am not sure what to do.")])
    turn, error = agent.run_turn("B", workdir)
    assert error == "model ended the turn without a decision"


def test_max_tokens_without_decision(env):
    _, _, workdir = env
    agent, _ = _agent(env, [reply("partial…", stop="max_tokens")])
    _, error = agent.run_turn("B", workdir)
    assert error == "model hit max_tokens before deciding"


def test_provider_exception_becomes_error(env):
    _, _, workdir = env

    class Boom(FakeProvider):
        def complete(self, *a, **k):
            raise ConnectionError("dns")

    reg, _, _ = env
    agent = Agent(Boom([]), reg, SYSTEM)
    turn, error = agent.run_turn("B", workdir)
    assert error == "ConnectionError: dns"
    assert turn.transcript[-2]["type"] == "error"


# -------------------------------------------------------------------- cache


def test_cache_warning_only_for_anthropic_with_zero_reads(env):
    reg, _, workdir = env

    class Anth(FakeProvider):
        kind = "anthropic"

    def turn_with(provider_cls, reads):
        replies = [
            reply(
                tool_calls=[ToolCall("i", "fake_inspect", {"params": {"ms_path": "/a"}})],
                usage=Usage(cache_read=reads[0]),
            ),
            reply(tool_calls=[_decision_call(done=True)], usage=Usage(cache_read=reads[1])),
        ]
        agent = Agent(provider_cls(replies), reg, SYSTEM)
        t, _ = agent.run_turn("B", workdir)
        return [r for r in t.transcript if r["type"] == "warning"]

    assert turn_with(Anth, [0, 0]) == [
        {"type": "warning", "what": "no prompt-cache reads across rounds 2+"}
    ]
    assert turn_with(Anth, [0, 20000]) == []
    assert turn_with(FakeProvider, [0, 0]) == []


def test_sum_usage_and_transcript_jsonl(env):
    _, _, workdir = env
    agent, _ = _agent(
        env,
        [
            reply(
                tool_calls=[ToolCall("i", "fake_inspect", {"params": {"ms_path": "/a"}})],
                usage=Usage(input_tokens=100, cache_read=None, output_tokens=5),
            ),
            reply(
                tool_calls=[_decision_call(done=True)],
                usage=Usage(input_tokens=50, cache_read=90, output_tokens=7),
            ),
        ],
    )
    turn, _ = agent.run_turn("B", workdir)
    u = sum_usage(turn)
    assert (u.input_tokens, u.cache_read, u.cache_write, u.output_tokens) == (150, 90, None, 12)
    lines = transcript_jsonl(turn).splitlines()
    assert all(json.loads(line)["type"] for line in lines)
    assert json.loads(lines[-1])["type"] == "summary"


# ------------------------------------------------------------- ApiBackend


def _backend(env, replies, **kw):
    reg, _, _ = env
    return ApiBackend(
        provider="fake",
        model="fake-model",
        api_key_env="UNUSED",
        skill_root=reg.skill_root,
        _provider=FakeProvider(replies),
        _registry=reg,
        **kw,
    )


def test_api_backend_maps_to_backend_result(env):
    _, _, workdir = env
    be = _backend(
        env,
        [
            reply("thinking aloud", tool_calls=[_script_call()]),
            reply(tool_calls=[_decision_call(script="/w/s.py", stage="setjy")]),
        ],
    )
    res = be.run("BRIEF", workdir, ms_path="/a")
    assert res.error is None and res.exit_code is None
    assert res.model == "fake"
    # the decision is the last JSON object in text, where loop.parse_decision looks
    from analyst_driver.loop import parse_decision

    assert parse_decision(res.text) == {"notes": "ok", "script": "/w/s.py", "stage": "setjy"}
    assert res.text.startswith("thinking aloud\n")
    assert [c["tool"] for c in res.tool_calls] == ["fake_script", SUBMIT_DECISION_NAME]
    assert json.loads(res.transcript.splitlines()[-1])["type"] == "summary"


def test_api_backend_error_and_usage(env):
    _, _, workdir = env
    be = _backend(env, [reply("nothing", usage=Usage(input_tokens=10, output_tokens=1))])
    res = be.run("BRIEF", workdir)
    assert res.error == "model ended the turn without a decision"
    assert res.tokens_in == 10 and res.tokens_out == 1 and res.tokens_cache_read is None
    assert res.total_tokens_in == 10


def test_api_backend_through_the_outer_loop(env, tmp_path):
    """The outer loop accepts ApiBackend exactly as it accepts StubBackend."""
    _, _, workdir = env
    script = workdir / "s.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    be = _backend(
        env,
        [
            reply(tool_calls=[_script_call()]),
            reply(
                tool_calls=[
                    _decision_call(
                        script=str(script),
                        stage="setjy",
                        cited=[{"name": "tool", "value": "fake_script", "source": "fake_script"}],
                    )
                ]
            ),
        ],
    )
    db = DriverDB(tmp_path / "runs")
    db.create_run("k1", ms_path=str(tmp_path / "a.ms"), workdir=str(workdir), executor="local")
    loop = Loop(db, be, LocalExecutor(runner="/bin/sh"), poll_interval=0.01)
    loop.sense = lambda run: {"data": {"next_recommended_step": "setjy"}}
    result = loop.step("k1")
    assert result["action"] == "completed" and result["outcome"] == "accepted"
    turn = db._read_json(db._turn_json("k1", 1))
    assert turn["stage"] == "setjy"
    assert turn["decision"]["script"] == str(script)
    assert turn["citations"][0]["found_value"] == "fake_script"
    assert turn["backend_error"] is None
    first = json.loads(turn["transcript"].splitlines()[0])
    assert first["type"] == "system" and "## Skill: only-skill" in first["text"]
    db.close()


def test_api_backend_default_registry_and_skills(tmp_path):
    """Without injected pieces, the backend builds the real registry and the
    real system prompt — the wiring cli.py uses."""
    be = ApiBackend(
        provider="fake",
        model="m",
        api_key_env="UNUSED",
        read_roots=[tmp_path],
        _provider=FakeProvider([]),
    )
    assert len(be.registry.names()) == 55
    assert "## Skill: stage-orchestration" in be.agent.system_prompt
    assert be.read_roots == [tmp_path]
