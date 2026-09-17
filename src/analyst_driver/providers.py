"""Model providers: wire format only.

A provider turns (system prompt, tool specs, history) into one request and
one ``Reply`` back. It knows nothing about CASA, skills, or policy — those
live in ``tools.py`` and ``agent.py``. If two providers differ, the
difference is a row in the mapping below, never a branch in the agent.

The history is a list of provider-native messages. The agent treats it as
opaque and only ever appends through ``append_reply`` /
``append_tool_results``. The assistant reply is appended verbatim (Anthropic
thinking blocks included): a reconstructed assistant message breaks the
prompt cache and, on newer models, is rejected.

Two providers:

- ``AnthropicProvider`` — Messages API. One ``cache_control`` breakpoint
  closes the system prompt; tool results for one assistant message go back
  in ONE user message.
- ``OpenAICompatProvider`` — Chat Completions against any compatible server
  (OpenAI, TACC, vLLM, llama.cpp). No cache marker exists; prefix reuse is
  the server's business. Tool results go back as one ``role: tool`` message
  each, in call order.

``FakeProvider`` replays scripted replies for tests and records every
request it was given.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

StopReason = Literal["tool_use", "end_turn", "max_tokens", "refusal", "other"]


@dataclass(frozen=True)
class ToolSpec:
    """One tool as the model sees it. Produced by ``tools.py``."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = False


@dataclass
class ToolCall:
    """One call the model asked for. ``args`` is None when the arguments did
    not parse; ``raw_args`` keeps the unparsed text for the journal."""

    id: str
    name: str
    args: dict[str, Any] | None
    raw_args: str | None = None


@dataclass
class ToolResult:
    call_id: str
    text: str
    is_error: bool = False


@dataclass
class Usage:
    """Token counts for one request. None means the provider did not report
    the field — distinct from 0, which is a measurement."""

    input_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output_tokens: int | None = None


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop: StopReason
    usage: Usage
    model: str | None
    #: Provider-native assistant message, replayed verbatim by append_reply.
    raw: Any


class Provider(Protocol):
    kind: str
    model: str

    def new_history(self, brief: str) -> list[Any]: ...

    def append_reply(self, history: list[Any], reply: Reply) -> None: ...

    def append_tool_results(self, history: list[Any], results: list[ToolResult]) -> None: ...

    def complete(self, system: str, tools: list[ToolSpec], history: list[Any]) -> Reply: ...


def _api_key(api_key_env: str) -> str:
    """The key comes from the environment, never from config. A missing
    variable fails at construction, not at the first turn."""
    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"environment variable {api_key_env} is not set")
    return key


def _sorted_specs(tools: list[ToolSpec]) -> list[ToolSpec]:
    # Providers cache by byte prefix and render tools first: the order and
    # serialization must not vary between requests.
    return sorted(tools, key=lambda t: t.name)


# ------------------------------------------------------------------ anthropic


class AnthropicProvider:
    kind = "anthropic"

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
        cache_ttl: Literal["5m", "1h"] = "5m",
        max_tokens: int = 16000,
        client: Any = None,
    ):
        self.model = model
        self.cache_ttl = cache_ttl
        self.max_tokens = max_tokens
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=_api_key(api_key_env), base_url=base_url)
        self.client = client

    def new_history(self, brief: str) -> list[Any]:
        return [{"role": "user", "content": [{"type": "text", "text": brief}]}]

    def append_reply(self, history: list[Any], reply: Reply) -> None:
        history.append({"role": "assistant", "content": reply.raw})

    def append_tool_results(self, history: list[Any], results: list[ToolResult]) -> None:
        # All results for one assistant message in ONE user message.
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.text,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )

    def _system(self, system: str) -> list[dict[str, Any]]:
        cache: dict[str, Any] = {"type": "ephemeral"}
        if self.cache_ttl == "1h":
            cache["ttl"] = "1h"
        return [{"type": "text", "text": system, "cache_control": cache}]

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        out = []
        for t in _sorted_specs(tools):
            d: dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            if t.strict:
                d["strict"] = True
            out.append(d)
        return out

    def complete(self, system: str, tools: list[ToolSpec], history: list[Any]) -> Reply:
        # No `thinking` (the model default is adaptive), no `temperature`
        # (rejected on current models).
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system(system),
            tools=self._tools(tools),
            messages=history,
        )
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else None
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        args=args,
                        raw_args=None if args is not None else json.dumps(block.input),
                    )
                )
        stop = self._stop(response.stop_reason)
        text = "".join(texts)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            if details is not None:
                text = (text + "\n" if text else "") + f"refusal: {details}"
        u = response.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", None),
            cache_read=getattr(u, "cache_read_input_tokens", None),
            cache_write=getattr(u, "cache_creation_input_tokens", None),
            output_tokens=getattr(u, "output_tokens", None),
        )
        return Reply(
            text=text,
            tool_calls=calls,
            stop=stop,
            usage=usage,
            model=getattr(response, "model", None),
            raw=response.content,
        )

    @staticmethod
    def _stop(reason: str | None) -> StopReason:
        if reason in ("tool_use", "end_turn", "max_tokens", "refusal"):
            return reason  # type: ignore[return-value]
        return "other"


# --------------------------------------------------------- openai-compatible


class OpenAICompatProvider:
    kind = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        max_tokens: int = 16000,
        temperature: float = 0.2,
        client: Any = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        if client is None:
            import openai

            client = openai.OpenAI(api_key=_api_key(api_key_env), base_url=base_url)
        self.client = client

    def new_history(self, brief: str) -> list[Any]:
        # The system message is part of the history so the prefix the server
        # sees is identical from one request to the next; complete() fills
        # its content from the system argument on every call.
        return [{"role": "system", "content": ""}, {"role": "user", "content": brief}]

    def append_reply(self, history: list[Any], reply: Reply) -> None:
        history.append(reply.raw)

    def append_tool_results(self, history: list[Any], results: list[ToolResult]) -> None:
        # This API has no is_error flag; the prefix is the only signal.
        for r in results:
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": r.call_id,
                    "content": f"ERROR: {r.text}" if r.is_error else r.text,
                }
            )

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        out = []
        for t in _sorted_specs(tools):
            fn: dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            if t.strict:
                fn["strict"] = True
            out.append({"type": "function", "function": fn})
        return out

    def complete(self, system: str, tools: list[ToolSpec], history: list[Any]) -> Reply:
        if not history or history[0].get("role") != "system":
            raise ValueError("history must start with the system message from new_history()")
        history[0]["content"] = system
        # max_tokens, not max_completion_tokens: llama.cpp and older vLLM
        # accept only the former.
        response = self.client.chat.completions.create(
            model=self.model,
            messages=history,
            tools=self._tools(tools),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        choice = response.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            raw = fn.arguments if isinstance(fn.arguments, str) else json.dumps(fn.arguments)
            try:
                args = json.loads(raw) if raw else {}
                if not isinstance(args, dict):
                    args = None
            except json.JSONDecodeError:
                args = None
            calls.append(ToolCall(id=tc.id, name=fn.name, args=args, raw_args=raw))
        # Reasoning models can spend the whole budget before emitting content,
        # leaving content None with finish_reason "length".
        text = msg.content or ""
        u = getattr(response, "usage", None)
        details = getattr(u, "prompt_tokens_details", None) if u is not None else None
        usage = Usage(
            input_tokens=getattr(u, "prompt_tokens", None) if u is not None else None,
            cache_read=getattr(details, "cached_tokens", None) if details is not None else None,
            cache_write=(
                getattr(details, "cache_write_tokens", None) if details is not None else None
            ),
            output_tokens=getattr(u, "completion_tokens", None) if u is not None else None,
        )
        raw = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
        raw.setdefault("role", "assistant")
        return Reply(
            text=text,
            tool_calls=calls,
            stop=self._stop(choice.finish_reason),
            usage=usage,
            model=getattr(response, "model", None) or self.model,
            raw=raw,
        )

    @staticmethod
    def _stop(reason: str | None) -> StopReason:
        return {
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "stop": "end_turn",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(reason or "", "other")  # type: ignore[return-value]


# -------------------------------------------------------------------- fake


@dataclass
class FakeRequest:
    system: str
    tools: list[ToolSpec]
    history: list[Any]


class FakeProvider:
    """Scripted replies, one per ``complete`` call. Records every request.

    History entries are plain dicts in an Anthropic-like shape so tests can
    read them; nothing else depends on the shape.
    """

    kind = "fake"

    def __init__(self, replies: list[Reply], model: str = "fake"):
        self.replies = list(replies)
        self.model = model
        self.requests: list[FakeRequest] = []

    def new_history(self, brief: str) -> list[Any]:
        return [{"role": "user", "content": brief}]

    def append_reply(self, history: list[Any], reply: Reply) -> None:
        history.append({"role": "assistant", "content": reply.raw})

    def append_tool_results(self, history: list[Any], results: list[ToolResult]) -> None:
        history.append(
            {
                "role": "user",
                "content": [
                    {"tool_use_id": r.call_id, "content": r.text, "is_error": r.is_error}
                    for r in results
                ],
            }
        )

    def complete(self, system: str, tools: list[ToolSpec], history: list[Any]) -> Reply:
        self.requests.append(FakeRequest(system=system, tools=list(tools), history=list(history)))
        if not self.replies:
            raise RuntimeError("fake provider ran out of replies")
        return self.replies.pop(0)


def reply(
    text: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    stop: StopReason | None = None,
    usage: Usage | None = None,
    model: str = "fake",
) -> Reply:
    """Build a ``Reply`` for tests: stop defaults to tool_use when there are
    calls, end_turn otherwise."""
    calls = list(tool_calls or [])
    return Reply(
        text=text,
        tool_calls=calls,
        stop=stop or ("tool_use" if calls else "end_turn"),
        usage=usage or Usage(),
        model=model,
        raw={"text": text, "tool_calls": [c.__dict__ for c in calls]},
    )


def make_provider(kind: str, **kwargs: Any) -> Provider:
    if kind == "anthropic":
        return AnthropicProvider(**kwargs)
    if kind == "openai":
        return OpenAICompatProvider(**kwargs)
    raise ValueError(f"unknown provider {kind!r} (expected anthropic | openai)")
