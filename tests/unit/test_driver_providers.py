"""
Unit tests for analyst_driver/providers.py — the wire mapping, no network.

Each real provider is built with a stub client that records the kwargs it
was called with and returns a canned response object. The assertions are on
exact request shapes: that is the whole contract of a provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from analyst_driver.providers import (
    AnthropicProvider,
    FakeProvider,
    OpenAICompatProvider,
    ToolCall,
    ToolResult,
    ToolSpec,
    make_provider,
    reply,
)

SPECS = [
    ToolSpec("zeta", "last", {"type": "object", "properties": {}}),
    ToolSpec("alpha", "first", {"type": "object", "properties": {}}, strict=True),
]


# ---------------------------------------------------------------- anthropic


class _AnthropicStub:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _anthropic_response(content, stop_reason="end_turn", stop_details=None):
    return NS(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model="claude-opus-5",
        usage=NS(
            input_tokens=100,
            cache_read_input_tokens=20000,
            cache_creation_input_tokens=0,
            output_tokens=50,
        ),
    )


def test_anthropic_request_shape():
    stub = _AnthropicStub(_anthropic_response([NS(type="text", text="hi")]))
    p = AnthropicProvider(client=stub, model="claude-opus-5")
    history = p.new_history("the brief")
    p.complete("SYSTEM", SPECS, history)

    kw = stub.calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["max_tokens"] == 16000
    assert "thinking" not in kw and "temperature" not in kw
    # one system block, cache marker on it, default TTL has no ttl key
    assert kw["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
    # tools sorted by name; strict only where asked
    assert [t["name"] for t in kw["tools"]] == ["alpha", "zeta"]
    assert kw["tools"][0]["strict"] is True
    assert "strict" not in kw["tools"][1]
    assert kw["tools"][0]["input_schema"] == SPECS[1].input_schema
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "the brief"}]}]


def test_anthropic_1h_ttl():
    stub = _AnthropicStub(_anthropic_response([]))
    p = AnthropicProvider(client=stub, cache_ttl="1h")
    p.complete("S", [], p.new_history("b"))
    assert stub.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_anthropic_reply_parsing_and_verbatim_replay():
    content = [
        NS(type="thinking", thinking="…", signature="sig"),
        NS(type="text", text="calling"),
        NS(type="tool_use", id="tu_1", name="ms_field_list", input={"params": {"ms_path": "/a"}}),
        NS(type="tool_use", id="tu_2", name="read_file", input={"path": "x.md"}),
    ]
    stub = _AnthropicStub(_anthropic_response(content, stop_reason="tool_use"))
    p = AnthropicProvider(client=stub)
    history = p.new_history("b")
    r = p.complete("S", SPECS, history)

    assert r.stop == "tool_use"
    assert r.text == "calling"
    assert [(c.id, c.name, c.args) for c in r.tool_calls] == [
        ("tu_1", "ms_field_list", {"params": {"ms_path": "/a"}}),
        ("tu_2", "read_file", {"path": "x.md"}),
    ]
    assert r.usage.input_tokens == 100
    assert r.usage.cache_read == 20000
    assert r.usage.cache_write == 0
    assert r.usage.output_tokens == 50
    assert r.model == "claude-opus-5"

    # the assistant message is the response content object itself, not a rebuild
    p.append_reply(history, r)
    assert history[-1] == {"role": "assistant", "content": content}
    assert history[-1]["content"] is content

    # all tool results in ONE user message, is_error carried
    p.append_tool_results(
        history, [ToolResult("tu_1", "{}"), ToolResult("tu_2", "no such file", is_error=True)]
    )
    assert history[-1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "{}", "is_error": False},
            {
                "type": "tool_result",
                "tool_use_id": "tu_2",
                "content": "no such file",
                "is_error": True,
            },
        ],
    }


@pytest.mark.parametrize(
    "wire, expected",
    [
        ("tool_use", "tool_use"),
        ("end_turn", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("pause_turn", "other"),
        (None, "other"),
    ],
)
def test_anthropic_stop_mapping(wire, expected):
    stub = _AnthropicStub(_anthropic_response([], stop_reason=wire))
    r = AnthropicProvider(client=stub).complete("S", [], [{"role": "user", "content": "b"}])
    assert r.stop == expected


def test_anthropic_refusal_details_land_in_text():
    details = NS(type="refusal", category="cyber", explanation="no")
    stub = _AnthropicStub(_anthropic_response([], stop_reason="refusal", stop_details=details))
    r = AnthropicProvider(client=stub).complete("S", [], [{"role": "user", "content": "b"}])
    assert r.stop == "refusal"
    assert "refusal:" in r.text and "cyber" in r.text


def test_anthropic_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()


# ------------------------------------------------------------------- openai


class _OpenAIStub:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _Msg:
    """Stands in for ChatCompletionMessage: has model_dump like pydantic."""

    def __init__(self, content, tool_calls=None, **extra):
        self.content = content
        self.tool_calls = tool_calls
        self.extra = extra

    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content, **self.extra}
        if self.tool_calls is not None:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


def _tc(id_, name, arguments):
    return NS(id=id_, type="function", function=NS(name=name, arguments=arguments))


def _openai_response(msg, finish_reason="stop", usage=None):
    return NS(
        choices=[NS(message=msg, finish_reason=finish_reason)],
        model="Qwen3-235B",
        usage=usage,
    )


def test_openai_request_shape_and_system_in_history():
    stub = _OpenAIStub(_openai_response(_Msg("hello")))
    p = OpenAICompatProvider(client=stub, model="m")
    history = p.new_history("the brief")
    assert history[0] == {"role": "system", "content": ""}
    p.complete("SYSTEM", SPECS, history)

    kw = stub.calls[0]
    assert kw["model"] == "m"
    assert kw["max_tokens"] == 16000
    assert kw["temperature"] == 0.2
    assert "max_completion_tokens" not in kw
    assert kw["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert kw["messages"][1] == {"role": "user", "content": "the brief"}
    assert [t["function"]["name"] for t in kw["tools"]] == ["alpha", "zeta"]
    assert kw["tools"][0]["type"] == "function"
    assert kw["tools"][0]["function"]["strict"] is True
    assert "strict" not in kw["tools"][1]["function"]
    assert kw["tools"][1]["function"]["parameters"] == SPECS[0].input_schema
    # the same list object is what the server sees next time: prefix stable
    assert kw["messages"] is history


def test_openai_rejects_history_without_system():
    p = OpenAICompatProvider(client=_OpenAIStub(None), model="m")
    with pytest.raises(ValueError):
        p.complete("S", [], [{"role": "user", "content": "b"}])


def test_openai_tool_calls_parse_and_replay():
    msg = _Msg(
        None,
        tool_calls=[
            _tc("c1", "ms_field_list", json.dumps({"params": {"ms_path": "/a"}})),
            _tc("c2", "read_file", "{not json"),
            _tc("c3", "submit_decision", "[1,2]"),
        ],
    )
    usage = NS(
        prompt_tokens=300,
        completion_tokens=40,
        prompt_tokens_details=NS(cached_tokens=256, cache_write_tokens=None),
    )
    stub = _OpenAIStub(_openai_response(msg, finish_reason="tool_calls", usage=usage))
    p = OpenAICompatProvider(client=stub, model="m")
    history = p.new_history("b")
    r = p.complete("S", SPECS, history)

    assert r.stop == "tool_use"
    assert r.text == ""
    good, bad, notdict = r.tool_calls
    assert good.args == {"params": {"ms_path": "/a"}}
    assert bad.args is None and bad.raw_args == "{not json"
    assert notdict.args is None and notdict.raw_args == "[1,2]"
    assert r.usage.input_tokens == 300
    assert r.usage.cache_read == 256
    assert r.usage.cache_write is None
    assert r.usage.output_tokens == 40

    p.append_reply(history, r)
    assert history[-1]["role"] == "assistant"
    assert [tc["id"] for tc in history[-1]["tool_calls"]] == ["c1", "c2", "c3"]
    assert "content" not in history[-1]  # None dropped by exclude_none

    p.append_tool_results(history, [ToolResult("c1", "{}"), ToolResult("c2", "bad", True)])
    assert history[-2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "content": "ERROR: bad"},
    ]


def test_openai_reasoning_model_exhausted_budget():
    stub = _OpenAIStub(_openai_response(_Msg(None), finish_reason="length", usage=None))
    r = OpenAICompatProvider(client=stub, model="gpt-oss-120b").complete(
        "S", [], [{"role": "system", "content": ""}, {"role": "user", "content": "b"}]
    )
    assert r.text == "" and r.stop == "max_tokens" and r.tool_calls == []
    assert r.usage.input_tokens is None and r.usage.cache_read is None


def test_openai_usage_without_prompt_details():
    usage = NS(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None)
    stub = _OpenAIStub(_openai_response(_Msg("x"), usage=usage))
    r = OpenAICompatProvider(client=stub, model="m").complete(
        "S", [], [{"role": "system", "content": ""}, {"role": "user", "content": "b"}]
    )
    assert r.usage.input_tokens == 10 and r.usage.cache_read is None


@pytest.mark.parametrize(
    "wire, expected",
    [
        ("tool_calls", "tool_use"),
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
        ("weird", "other"),
        (None, "other"),
    ],
)
def test_openai_stop_mapping(wire, expected):
    stub = _OpenAIStub(_openai_response(_Msg("x"), finish_reason=wire))
    r = OpenAICompatProvider(client=stub, model="m").complete(
        "S", [], [{"role": "system", "content": ""}, {"role": "user", "content": "b"}]
    )
    assert r.stop == expected


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TACC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TACC_API_KEY"):
        OpenAICompatProvider(model="m", api_key_env="TACC_API_KEY")


# --------------------------------------------------------------------- fake


def test_fake_provider_records_and_exhausts():
    fp = FakeProvider([reply("one"), reply(tool_calls=[ToolCall("i", "t", {})])])
    h = fp.new_history("b")
    r1 = fp.complete("S", SPECS, h)
    assert r1.stop == "end_turn"
    fp.append_reply(h, r1)
    r2 = fp.complete("S", SPECS, h)
    assert r2.stop == "tool_use"
    assert len(fp.requests) == 2
    assert fp.requests[1].history[-1]["role"] == "assistant"
    with pytest.raises(RuntimeError):
        fp.complete("S", SPECS, h)


def test_make_provider_unknown_kind():
    with pytest.raises(ValueError, match="anthropic | openai"):
        make_provider("gemini")
