"""
Unit tests for analyst_driver/backends.py — the tool ban in particular.

The fixture in tests/unit/fixtures/ is a REAL captured system/init event, not
a hand-written one. That matters here more than usual: a synthetic fixture
would be written to match what we believed the harness does, which is exactly
how a tool ban can end up documented as working while it silently is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst_driver.backends import ClaudeBackend

FIXTURES = Path(__file__).parent / "fixtures"

#: A realistic --allowedTools list, matching the captured fixture's turn.
G55_ALLOWED = [
    "mcp__ms-inspect",
    "mcp__ms-modify",
    "mcp__ms-create",
    "Read",
    "Glob",
    "Grep",
    "Skill",
]

BANNED = ["Bash", "Write", "Edit", "NotebookEdit", "Task", "WebFetch", "WebSearch"]

MCP_CONFIG = Path("/tmp/turn/mcp.json")
SYSTEM_PROMPT_FILE = Path("/tmp/turn/system_prompt.md")


def _backend(**kw) -> ClaudeBackend:
    # The system prompt is read from the repo's skills; the flag tests do not
    # depend on its content.
    return ClaudeBackend(_system_prompt="", **kw)


def _args(**kw) -> list[str]:
    return _backend(**kw)._args(MCP_CONFIG, SYSTEM_PROMPT_FILE)


@pytest.fixture
def g55_init_event() -> dict:
    return json.loads((FIXTURES / "g55_turn1_init_event.json").read_text())


# ---------------------------------------------------------------- the evidence


def test_the_g55_run_was_offered_every_tool_it_thought_it_had_banned(g55_init_event):
    """The measurement the whole change rests on.

    If this ever fails because the harness stopped offering these, the ban is
    being enforced somewhere else and --disallowedTools may be redundant. Until
    then it is the proof that an allow list does not remove anything.
    """
    offered = set(g55_init_event["tools"])
    assert {"Bash", "Write", "Edit", "Task", "WebFetch", "WebSearch", "NotebookEdit"} <= offered
    # ... and none of them were on the allow list.
    assert not (offered & set(BANNED)) & set(G55_ALLOWED)


# ---------------------------------------------------------------- the flags


def test_allowed_and_disallowed_are_separate_flags():
    args = _args(allowed_tools=["Read"], disallowed_tools=["Bash"])
    assert "--allowedTools" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__analyst__*,Read"
    assert "--disallowedTools" in args
    assert args[args.index("--disallowedTools") + 1] == "Bash"


def test_disallowed_tools_are_comma_joined_not_variadic():
    """--allowedTools is variadic: a space-separated list eats whatever follows.
    The same trap applies here, and the prompt goes on stdin for the same
    reason."""
    args = _args(disallowed_tools=BANNED)
    assert args[args.index("--disallowedTools") + 1] == ",".join(BANNED)


def test_no_flag_when_the_ban_is_explicitly_empty():
    assert "--disallowedTools" not in _args(disallowed_tools=[])


def test_the_prompt_is_not_on_the_command_line():
    """Regression: a prompt appended as a positional is eaten by --allowedTools."""
    args = _args(allowed_tools=["Read"], disallowed_tools=["Bash"])
    assert not any("You are one decision point" in a for a in args)


# ------------------------------------------------- the ban is verified, not assumed


def test_a_ban_that_did_not_take_is_detected(g55_init_event):
    """A flag that is silently ignored looks exactly like a flag that works.

    This is the assertion that would have caught the original defect on turn 1
    rather than after 16 turns and a post-mortem.
    """
    backend = _backend(allowed_tools=G55_ALLOWED, disallowed_tools=BANNED)
    leaked = backend.banned_tools_offered(g55_init_event["tools"])
    assert "Bash" in leaked
    assert leaked == {"Bash", "Write", "Edit", "NotebookEdit", "Task", "WebFetch", "WebSearch"}


def test_a_ban_that_took_reports_nothing(g55_init_event):
    offered = [t for t in g55_init_event["tools"] if t not in BANNED]
    backend = _backend(allowed_tools=G55_ALLOWED, disallowed_tools=BANNED)
    assert backend.banned_tools_offered(offered) == set()


def test_an_absent_init_event_is_not_reported_as_a_violation():
    """No evidence is not evidence of a leak. Reporting one here would make
    every backend that emits no init event look like a safety failure."""
    backend = _backend(disallowed_tools=BANNED)
    assert backend.banned_tools_offered(None) == set()


def test_nothing_is_checked_when_the_ban_is_explicitly_empty(g55_init_event):
    assert _backend(disallowed_tools=[]).banned_tools_offered(g55_init_event["tools"]) == set()


# ---------------------------------------------------------------- parse


def test_parse_extracts_the_offered_tool_list(g55_init_event):
    raw = json.dumps(g55_init_event) + "\n"
    assert "Bash" in ClaudeBackend.parse(raw).tool_names_offered


def test_parse_leaves_the_offered_list_none_without_an_init_event():
    raw = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    assert ClaudeBackend.parse(raw).tool_names_offered is None


def test_tools_offered_is_not_the_same_as_tools_used(g55_init_event):
    """A turn that used no Bash still had Bash available. The ban is about what
    the harness loaded, not about what the model happened to call."""
    raw = json.dumps(g55_init_event) + "\n"
    res = ClaudeBackend.parse(raw)
    assert res.tool_names_offered
    assert res.tool_calls == []


# ---------------------------------------------------- the ban defaults to ON


def test_the_ban_applies_without_any_configuration():
    """A config.toml written before the ban existed has no disallowed_tools
    key. Taking the ban from config alone would leave every such run
    unprotected while the file still claimed Bash was absent — the same failure
    shape the ban exists to fix."""
    args = _args()
    assert "--disallowedTools" in args
    assert "Bash" in args[args.index("--disallowedTools") + 1].split(",")


def test_an_explicit_empty_list_turns_the_ban_off(g55_init_event):
    """ "Not specified" and "no ban" are different. Silently upgrading the
    second to the first would make the flag impossible to switch off."""
    backend = _backend(disallowed_tools=[])
    assert "--disallowedTools" not in backend._args(MCP_CONFIG, SYSTEM_PROMPT_FILE)
    assert backend.banned_tools_offered(g55_init_event["tools"]) == set()


def test_the_default_ban_catches_the_g55_leak(g55_init_event):
    """End to end on the real event, with no arguments at all."""
    assert "Bash" in _backend().banned_tools_offered(g55_init_event["tools"])


# ---------------------------------------------------------------- token usage
#
# The fixture is a real captured result event. Its usage block is why a
# hand-written one would not have caught the bug this guards against:
# input_tokens is small while cache_read_input_tokens carries almost
# everything real, and a cost figure taken from input_tokens alone looks
# perfectly plausible while being off by orders of magnitude.


@pytest.fixture
def g55_result_event() -> dict:
    return json.loads((FIXTURES / "g55_turn2_result_event.json").read_text())


def test_all_three_input_counts_are_parsed(g55_result_event):
    res = ClaudeBackend.parse(json.dumps(g55_result_event) + "\n")
    assert res.tokens_in == 26
    assert res.tokens_cache_read == 417313
    assert res.tokens_cache_creation == 31666
    assert res.tokens_out == 4138


def test_the_uncached_count_alone_is_not_the_input_total(g55_result_event):
    """The defect in one assertion: tokens_in is a rounding error on this turn."""
    res = ClaudeBackend.parse(json.dumps(g55_result_event) + "\n")
    assert res.total_tokens_in == 26 + 417313 + 31666
    assert res.total_tokens_in > 10_000 * res.tokens_in


def test_total_is_none_when_no_input_counts_are_reported():
    """None is 'the backend told us nothing', not 'the turn used no input'.
    A 0 here would be a measurement, and this is the absence of one."""
    raw = json.dumps({"type": "result", "result": "x", "usage": {"output_tokens": 5}}) + "\n"
    assert ClaudeBackend.parse(raw).total_tokens_in is None


def test_total_counts_a_reported_zero(g55_result_event):
    """A backend that genuinely reports zero uncached input must not be
    confused with one that reports nothing."""
    raw = (
        json.dumps(
            {
                "type": "result",
                "result": "x",
                "usage": {"input_tokens": 0, "cache_read_input_tokens": 100},
            }
        )
        + "\n"
    )
    res = ClaudeBackend.parse(raw)
    assert res.total_tokens_in == 100


def test_the_components_are_kept_separately_not_only_summed(g55_result_event):
    """They bill at different rates, so a single total cannot support a cost
    figure. Both the parts and the sum have to survive."""
    res = ClaudeBackend.parse(json.dumps(g55_result_event) + "\n")
    assert (res.tokens_in, res.tokens_cache_read, res.tokens_cache_creation) == (
        26,
        417313,
        31666,
    )


# ------------------------------------------------- run: the state file is the turn

FAKE_CLAUDE = '''\
#!/usr/bin/env python3
"""Stands in for claude -p: reads the prompt on stdin, finds the harness MCP
config on argv, and behaves as the real thing would after the tool server
ran — the state file appears at the path the config named. Emits a
stream-json init event and a result event."""
import json, sys
from pathlib import Path

prompt = sys.stdin.read()
argv = sys.argv[1:]
cfg = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
server = cfg["mcpServers"]["analyst"]
env = server["env"]
sp = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
Path(env["ANALYST_TURN_WORKDIR"], "seen.json").write_text(json.dumps({
    "prompt": prompt, "argv": argv, "server": server, "system_prompt": sp,
}))
if "WRITE_STATE" in sp:
    Path(env["ANALYST_TURN_STATE"]).write_text(json.dumps({
        "decision": {"stage": "flag", "script": "s.py", "tool": "ms_x", "notes": "n"},
        "decision_source": "tool",
        "tool_calls": [{"tool": "ms_x", "result": "x" * 100000}],
        "rejections": {},
    }))
print(json.dumps({"type": "system", "subtype": "init", "model": "claude-fake",
                  "tools": ["mcp__analyst__ms_x", "Read"]}))
print(json.dumps({"type": "result", "result": "I called ms_x.",
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 30, "output_tokens": 5}}))
'''


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    cmd = tmp_path / "claude"
    cmd.write_text(FAKE_CLAUDE)
    cmd.chmod(0o755)
    return cmd


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_run_hands_claude_the_registry_server_and_the_skills(fake_claude, workdir, tmp_path):
    runroot = tmp_path / "runs"
    backend = ClaudeBackend(
        cmd=str(fake_claude),
        _system_prompt="WRITE_STATE skills here",
        skill_root=tmp_path / "skills",
        read_roots=[runroot],
    )
    backend.run("the brief", workdir)
    seen = json.loads((workdir / "seen.json").read_text())
    assert seen["prompt"] == "the brief"
    assert seen["system_prompt"] == "WRITE_STATE skills here"
    server = seen["server"]
    assert server["args"] == ["-m", "analyst_driver.tools_server"]
    assert server["env"]["ANALYST_TURN_WORKDIR"] == str(workdir)
    assert server["env"]["ANALYST_SKILL_ROOT"] == str(tmp_path / "skills")
    assert server["env"]["ANALYST_READ_ROOTS"] == str(runroot)
    assert "--strict-mcp-config" in seen["argv"]
    assert "--bare" not in seen["argv"]


def test_run_maps_the_state_file_like_api_backend_does(fake_claude, workdir):
    backend = ClaudeBackend(cmd=str(fake_claude), _system_prompt="WRITE_STATE")
    res = backend.run("brief", workdir)
    assert res.error is None
    # decision: trailing JSON object, where loop.parse_decision looks
    assert res.text.startswith("I called ms_x.\n")
    assert json.loads(res.text.splitlines()[-1])["script"] == "s.py"
    # tool calls: the full result from the state file, not the event stream
    assert res.tool_calls == [{"tool": "ms_x", "result": "x" * 100000}]
    # usage and model: from the event stream
    assert res.model == "claude-fake"
    assert (res.tokens_in, res.tokens_cache_read, res.tokens_cache_creation, res.tokens_out) == (
        10,
        20,
        30,
        5,
    )


def test_run_reports_a_tool_server_that_never_started(fake_claude, workdir):
    """No state file means claude ran without any harness tool. That is a
    backend failure, not a model answer, and must not be retried as one."""
    backend = ClaudeBackend(cmd=str(fake_claude), _system_prompt="no state")
    res = backend.run("brief", workdir)
    assert res.error is not None
    assert "tool server never started" in res.error
    assert res.tool_calls == []
    assert res.text == "I called ms_x."
