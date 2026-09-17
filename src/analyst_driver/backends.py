"""Backend contract + claude, opencode, codex adapters.

One contract: a command that takes a prompt non-interactively and returns
text on stdout. Each adapter also does its best to extract the tool calls the
turn made, the model name and the token usage — recorded when the backend can
report them, null when it cannot. A parse failure anywhere degrades to raw
stdout as ``text``; the loop treats an unusable decision as a retryable turn,
never a run failure.

Capability notes per backend:

- ``claude -p --output-format stream-json --verbose`` — full event stream:
  tool calls, tool results, model, token usage.
- ``opencode run --format json`` — raw JSON events; tool events extracted
  best-effort.
- ``codex exec --json`` — event stream (unverified here; codex is not
  installed on the dev machine). codex does not read SKILL.md, so its adapter
  prepends the skill file paths to the prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class BackendResult:
    text: str
    transcript: str | None = None  # raw event stream, journal-only
    model: str | None = None
    #: Uncached input only — the tail the prompt cache didn't cover. Never use
    #: alone for a cost figure; see total_tokens_in.
    tokens_in: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_creation: int | None = None
    tokens_out: int | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Why the backend itself failed — a non-zero exit or no output at all.
    #: Distinct from "the model answered something unusable": without it a
    #: harness that never launched is indistinguishable from a bad answer, and
    #: the loop retries the identical failure until max_turns.
    error: str | None = None
    exit_code: int | None = None

    @property
    def total_tokens_in(self) -> int | None:
        """Every input token the turn was billed for, cached or not.

        None only when the backend reported no input counts at all — distinct
        from 0, which would be a measurement. The three components are kept
        separately as well, because they bill at different rates and a single
        total cannot support a cost figure.
        """
        parts = [self.tokens_in, self.tokens_cache_read, self.tokens_cache_creation]
        if all(p is None for p in parts):
            return None
        return sum(p or 0 for p in parts)

    #: Tool names the harness reported loading, from the system/init event.
    #: None when no such event appeared. Distinct from the tools actually used.
    tool_names_offered: list[str] | None = None
    #: Banned tools the harness offered anyway — the ban silently not applying.
    tools_ban_violated: set[str] = field(default_factory=set)


class Backend(Protocol):
    kind: str

    def run(
        self, prompt: str, workdir: str | Path, *, ms_path: str | None = None
    ) -> BackendResult: ...


def _with_failure(res: BackendResult, out: subprocess.CompletedProcess) -> BackendResult:
    """Record a backend that failed to produce anything, and why.

    Without this a harness that never launched looks exactly like a model that
    answered nothing usable: both give an empty text, the turn records
    "decision did not parse", and the loop retries the identical failure until
    max_turns. The one-line reason sits unread in stderr.
    """
    res.exit_code = out.returncode
    if out.returncode != 0 or not (out.stdout or "").strip():
        stderr = (out.stderr or "").strip()
        res.error = stderr[:2000] or f"exited {out.returncode} with no output"
    return res


def _jsonl(raw: str) -> list[Any]:
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


class StubBackend:
    """Canned responses, for tests and the dry run.

    ``tool_calls``, given, supplies one list per response (parallel to
    ``responses``) so a test can exercise the metric-harvest and citation
    paths, which read ``BackendResult.tool_calls`` — a real turn's decision
    text and its tool calls are two views of the one captured transcript,
    not independent inputs.
    """

    kind = "stub"

    def __init__(self, responses: list[str], tool_calls: list[list[dict]] | None = None):
        self.responses = list(responses)
        self.tool_calls = list(tool_calls) if tool_calls is not None else None
        self.calls: list[str] = []

    def run(self, prompt: str, workdir: str | Path, *, ms_path: str | None = None) -> BackendResult:
        self.calls.append(prompt)
        if not self.responses:
            raise RuntimeError("stub backend ran out of responses")
        calls = self.tool_calls.pop(0) if self.tool_calls is not None else []
        return BackendResult(text=self.responses.pop(0), model="stub", tool_calls=calls)


class ApiBackend:
    """The harness's own inner loop over a model provider's HTTP API.

    Builds the tool registry (the three FastMCP servers in-process plus
    ``read_file`` and ``submit_decision``) and the system prompt once, then
    runs one ``Agent`` turn per ``run`` call and maps it to ``BackendResult``.
    """

    kind = "api"

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key_env: str,
        base_url: str | None = None,
        cache_ttl: str = "5m",
        max_rounds: int | None = None,
        max_tokens: int = 16000,
        temperature: float = 0.2,
        skill_root: str | Path | None = None,
        read_roots: list[str | Path] | None = None,
        _provider: Any = None,
        _registry: Any = None,
    ):
        from analyst_driver.agent import DEFAULT_MAX_ROUNDS, Agent
        from analyst_driver.providers import make_provider
        from analyst_driver.skills import default_skill_root, system_prompt
        from analyst_driver.tools import ToolRegistry

        self.provider_kind = provider
        self.model = model
        root = Path(skill_root) if skill_root else default_skill_root()
        self.skill_root = root
        self.read_roots = [Path(p) for p in (read_roots or [])]
        if _provider is None:
            kwargs: dict[str, Any] = dict(
                model=model, api_key_env=api_key_env, base_url=base_url, max_tokens=max_tokens
            )
            if provider == "anthropic":
                kwargs["cache_ttl"] = cache_ttl
            else:
                kwargs["temperature"] = temperature
            _provider = make_provider(provider, **kwargs)
        self.provider = _provider
        self.registry = _registry or ToolRegistry.default(
            skill_root=root, read_roots=self.read_roots
        )
        self.agent = Agent(
            self.provider,
            self.registry,
            system_prompt(root),
            max_rounds=max_rounds or DEFAULT_MAX_ROUNDS,
        )

    def run(self, prompt: str, workdir: str | Path, *, ms_path: str | None = None) -> BackendResult:
        from analyst_driver.agent import sum_usage, transcript_jsonl

        turn, error = self.agent.run_turn(prompt, Path(workdir))
        texts = [r["text"] for r in turn.transcript if r.get("type") == "assistant" and r["text"]]
        text = texts[-1] if texts else ""
        if turn.decision is not None and turn.decision_source == "tool":
            # The loop reads the decision with parse_decision, which takes the
            # last top-level JSON object in the text.
            text = (text + "\n" if text else "") + json.dumps(turn.decision, sort_keys=True)
        usage = sum_usage(turn)
        return BackendResult(
            text=text,
            transcript=transcript_jsonl(turn),
            model=self.model
            if self.provider is None
            else getattr(self.provider, "model", self.model),
            tokens_in=usage.input_tokens,
            tokens_cache_read=usage.cache_read,
            tokens_cache_creation=usage.cache_write,
            tokens_out=usage.output_tokens,
            tool_calls=list(turn.tool_calls),
            error=error,
            exit_code=None,
        )


#: Removed from every claude turn unless a caller explicitly overrides it.
#: This is a CODE default, not a config default, on purpose: a config written
#: before the ban existed has no disallowed_tools key, and taking the ban from
#: config alone would leave every such run unprotected while the file still
#: claimed Bash was absent. Pass disallowed_tools=[] to turn it off deliberately.
#:
#: Bash is the one that matters — a turn that runs CASA itself leaves no job id,
#: no exit code and no artifact checksum in the journal, so the run becomes
#: unauditable. Write/Edit/NotebookEdit would let a turn change an MS or a
#: caltable outside the tools, where the guards live. Task, WebFetch and
#: WebSearch are removed as unnecessary surface, not because of an observed
#: failure.
DEFAULT_DISALLOWED_TOOLS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Task",
    "WebFetch",
    "WebSearch",
]


class ClaudeBackend:
    kind = "claude"

    def __init__(
        self,
        cmd: str = "claude",
        mcp_config: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        """``allowed_tools`` becomes ``--allowedTools``, ``disallowed_tools``
        ``--disallowedTools``.

        ``claude -p`` is non-interactive, so there is nobody to answer a
        permission prompt: any tool not on the allow list is DENIED, and the
        turn comes back as a refusal the driver can only record and retry.
        Without it the driver cannot call a single ms_modify or ms_create tool,
        which is every tool it exists to call.

        The two flags are NOT opposites. ``--allowedTools`` PRE-APPROVES; it
        does not remove anything. Removing a tool takes ``--disallowedTools``.
        """
        self.cmd = cmd
        self.mcp_config = mcp_config
        self.model = model
        self.timeout = timeout
        self.allowed_tools = list(allowed_tools) if allowed_tools else []
        # None means "not specified" and takes the code default; an explicit
        # empty list means "no ban", which is a different thing and must not be
        # silently upgraded.
        self.disallowed_tools = (
            list(DEFAULT_DISALLOWED_TOOLS) if disallowed_tools is None else list(disallowed_tools)
        )

    def _args(self) -> list[str]:
        """The command line. The prompt is NOT here — it goes on stdin.

        ``--allowedTools <tools...>`` is variadic: it consumes every argument
        that follows it, so a prompt appended as the last positional is eaten
        as another tool name and claude exits 1 with "Input must be provided
        either through stdin or as a prompt argument". stdin also removes any
        argv length limit on a long brief.
        """
        args = [self.cmd, "-p", "--output-format", "stream-json", "--verbose"]
        if self.mcp_config:
            args += ["--mcp-config", self.mcp_config]
        if self.model:
            args += ["--model", self.model]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            args += ["--disallowedTools", ",".join(self.disallowed_tools)]
        return args

    def run(self, prompt: str, workdir: str | Path, *, ms_path: str | None = None) -> BackendResult:
        # The sense hook (hooks/sense.py) reads this to know which MS a
        # workdir-glob fallback can't reliably identify on its own — a run
        # already knows its own ms_path, so pass it rather than let the hook
        # guess. Unset (not "") when there is none yet, e.g. before import.
        env = None
        if ms_path:
            env = {**os.environ, "ANALYST_MS_PATH": ms_path}
        out = subprocess.run(
            self._args(),
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=self.timeout,
            env=env,
        )
        res = _with_failure(self.parse(out.stdout), out)
        leaked = self.banned_tools_offered(res.tool_names_offered)
        if leaked:
            res.tools_ban_violated = leaked
            res.error = (
                f"backend offered banned tools {sorted(leaked)} despite --disallowedTools."
                " The turn could have run CASA itself, so nothing it did is in the journal."
                + (f" ({res.error})" if res.error else "")
            )
        return res

    def banned_tools_offered(self, offered: list[str] | None) -> set[str]:
        """Which banned tools the harness actually offered this turn.

        A flag that is passed but ignored looks exactly like a flag that works:
        the config claims Bash is gone, the transcript says otherwise, and
        nothing reads the transcript. The system/init event lists the tools the
        harness really loaded, so it is checked against the ban rather than
        trusted. None means no init event was seen — reported as no violation,
        because an absent event is not evidence of a leak.
        """
        if not self.disallowed_tools or offered is None:
            return set()
        return {t for t in offered if t in set(self.disallowed_tools)}

    @staticmethod
    def parse(raw: str) -> BackendResult:
        events = _jsonl(raw)
        res = BackendResult(text=raw, transcript=raw or None)
        tool_names: dict[str, str] = {}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "init":
                res.model = ev.get("model") or res.model
                tools = ev.get("tools")
                if isinstance(tools, list):
                    res.tool_names_offered = [t for t in tools if isinstance(t, str)]
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                content = []
            if etype == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names[block.get("id", "")] = block.get("name", "")
            elif etype == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        res.tool_calls.append(
                            {
                                "tool": tool_names.get(block.get("tool_use_id", ""), ""),
                                "result": block.get("content"),
                            }
                        )
            elif etype == "result":
                if isinstance(ev.get("result"), str):
                    res.text = ev["result"]
                usage = ev.get("usage") or {}
                res.tokens_in = usage.get("input_tokens")
                res.tokens_cache_read = usage.get("cache_read_input_tokens")
                res.tokens_cache_creation = usage.get("cache_creation_input_tokens")
                res.tokens_out = usage.get("output_tokens")
        return res


class OpencodeBackend:
    kind = "opencode"

    def __init__(
        self,
        cmd: str = "opencode",
        model: str | None = None,
        agent: str | None = None,
        timeout: float | None = None,
    ):
        self.cmd = cmd
        self.model = model
        self.agent = agent
        self.timeout = timeout

    def _args(self, prompt: str) -> list[str]:
        args = [self.cmd, "run", "--format", "json"]
        if self.model:
            args += ["--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        args.append(prompt)
        return args

    def run(self, prompt: str, workdir: str | Path, *, ms_path: str | None = None) -> BackendResult:
        out = subprocess.run(
            self._args(prompt),
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=self.timeout,
        )
        return _with_failure(self.parse(out.stdout), out)

    @staticmethod
    def parse(raw: str) -> BackendResult:
        events = _jsonl(raw)
        res = BackendResult(text=raw, transcript=raw or None)
        texts: list[str] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
            ptype = part.get("type")
            if ptype == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif ptype == "tool":
                state = part.get("state") or {}
                res.tool_calls.append(
                    {
                        "tool": part.get("tool", ""),
                        "result": state.get("output"),
                    }
                )
        if texts:
            res.text = "\n".join(texts)
        return res


class CodexBackend:
    kind = "codex"

    def __init__(
        self,
        cmd: str = "codex",
        skill_paths: list[str] | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        self.cmd = cmd
        self.skill_paths = list(skill_paths or [])
        self.model = model
        self.timeout = timeout

    def _args(self, prompt: str) -> list[str]:
        args = [self.cmd, "exec", "--json"]
        if self.model:
            args += ["--model", self.model]
        args.append(prompt)
        return args

    def run(self, prompt: str, workdir: str | Path, *, ms_path: str | None = None) -> BackendResult:
        if self.skill_paths:
            preamble = "Read these skill files before deciding:\n" + "\n".join(
                f"- {p}" for p in self.skill_paths
            )
            prompt = f"{preamble}\n\n{prompt}"
        out = subprocess.run(
            self._args(prompt),
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=self.timeout,
        )
        return _with_failure(self.parse(out.stdout), out)

    @staticmethod
    def parse(raw: str) -> BackendResult:
        events = _jsonl(raw)
        res = BackendResult(text=raw, transcript=raw or None)
        for ev in events:
            if not isinstance(ev, dict):
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
            if isinstance(item.get("text"), str) and item.get("type", "agent_message") in (
                "agent_message",
                "message",
            ):
                res.text = item["text"]
        return res


def make_backend(kind: str, **kwargs: Any) -> Backend:
    if kind == "api":
        return ApiBackend(**kwargs)
    if kind == "claude":
        return ClaudeBackend(**kwargs)
    if kind == "opencode":
        return OpencodeBackend(**kwargs)
    if kind == "codex":
        return CodexBackend(**kwargs)
    raise ValueError(f"unknown backend kind {kind!r}")
