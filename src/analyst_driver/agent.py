"""The inner loop: one turn of model rounds and tool calls.

``Agent.run_turn(brief)`` calls the provider, dispatches every tool call
through the registry, feeds the results back, and repeats until the model
calls ``submit_decision``, stops calling tools, refuses, or ``max_rounds`` is
reached. Tool calls in one reply run sequentially, in order, and their
results go back together. Every round is written to a normalized transcript
that the backend stores in the journal.

``max_rounds`` is a stop condition for this inner loop. The outer loop's
``max_turns`` is a different level and is not merged with it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from analyst_driver.providers import Provider, Reply, ToolResult, Usage
from analyst_driver.tools import ToolRegistry, TurnState

DEFAULT_MAX_ROUNDS = 30


class Agent:
    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        system_prompt: str,
        *,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        self.provider = provider
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_rounds = max_rounds

    def run_turn(self, brief: str, workdir: Path) -> tuple[TurnState, str | None]:
        """Run one turn. Returns the turn state and an error string, or None."""
        turn = TurnState(workdir=Path(workdir))
        specs = self.registry.specs()
        history = self.provider.new_history(brief)
        turn.transcript.append({"type": "system", "text": self.system_prompt})
        turn.transcript.append({"type": "user", "text": brief})
        usages: list[Usage] = []
        last_text = ""
        error: str | None = None
        stop = None

        while turn.rounds < self.max_rounds:
            turn.rounds += 1
            try:
                reply: Reply = self.provider.complete(self.system_prompt, specs, history)
            except Exception as e:  # noqa: BLE001 — any SDK/network failure ends the turn
                error = f"{type(e).__name__}: {e}"[:2000]
                turn.transcript.append({"type": "error", "text": error})
                break
            usages.append(reply.usage)
            turn.transcript.append(
                {
                    "type": "assistant",
                    "round": turn.rounds,
                    "text": reply.text,
                    "stop": reply.stop,
                    "model": reply.model,
                    "usage": asdict(reply.usage),
                }
            )
            for c in reply.tool_calls:
                turn.transcript.append(
                    {"type": "tool_call", "id": c.id, "tool": c.name, "args": c.args}
                )
            if reply.text:
                last_text = reply.text
            self.provider.append_reply(history, reply)
            stop = reply.stop

            if reply.stop == "refusal":
                error = f"model refused: {reply.text[:500]}"
                break
            if not reply.tool_calls:
                break

            results: list[ToolResult] = []
            for call in reply.tool_calls:
                results.append(self.registry.dispatch(call, turn))
            self.provider.append_tool_results(history, results)
            if turn.decision is not None:
                break
        else:
            error = f"no decision after max_rounds={self.max_rounds}"

        if turn.decision is None and error is None:
            from analyst_driver.loop import parse_decision

            parsed = parse_decision(last_text)
            if parsed is not None:
                turn.decision = parsed
                turn.decision_source = "text"
            elif stop == "max_tokens":
                error = "model hit max_tokens before deciding"
            else:
                error = "model ended the turn without a decision"

        self._cache_warning(turn, usages)
        turn.transcript.append(
            {
                "type": "summary",
                "rounds": turn.rounds,
                "n_tool_calls": len(turn.tool_calls),
                "n_rejected": sum(turn.rejections.values()),
                "rejections": dict(turn.rejections),
                "files_read": list(turn.files_read),
                "truncated_results": turn.truncated_results,
                "decision_source": turn.decision_source,
                "error": error,
            }
        )
        return turn, error

    def _cache_warning(self, turn: TurnState, usages: list[Usage]) -> None:
        # A stable prefix must produce cache reads from the second round on.
        # Zero reads across a multi-round turn means something in the prefix
        # varies; that is reported, never silently accepted.
        if getattr(self.provider, "kind", None) != "anthropic" or len(usages) < 2:
            return
        later = [u.cache_read for u in usages[1:]]
        if all(r is not None and r == 0 for r in later):
            turn.transcript.append(
                {"type": "warning", "what": "no prompt-cache reads across rounds 2+"}
            )


def transcript_jsonl(turn: TurnState) -> str:
    return "\n".join(json.dumps(rec, default=str, sort_keys=True) for rec in turn.transcript)


def sum_usage(turn: TurnState) -> Usage:
    """Totals over the turn's rounds; a field is None only if no round reported it."""
    recs = [r["usage"] for r in turn.transcript if r.get("type") == "assistant"]
    out = Usage()
    for key in ("input_tokens", "cache_read", "cache_write", "output_tokens"):
        vals = [r.get(key) for r in recs if r.get(key) is not None]
        setattr(out, key, sum(vals) if vals else None)
    return out
