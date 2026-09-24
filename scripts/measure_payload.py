"""Measure the request payload the harness sends a model: system prompt plus
tool schemas, in the exact OpenAI-compatible wire format.

Local (no key): characters per tool, schema nesting depth, whether the tool
takes its arguments under a ``params`` object. Token counts here are
chars/4 estimates, labelled as such.

Remote (``--model``, repeatable): one request per model with max_tokens=1;
reports the server's own ``prompt_tokens``, or the error body (context
ceiling, rejected ``strict``, no tool support). Nothing is executed.

    python scripts/measure_payload.py
    python scripts/measure_payload.py --base-url https://llm.jetstream-cloud.org/api \\
        --api-key-env JETSTREAM_API_KEY --model gpt-oss-120b --model llama-4-scout
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from analyst_driver.providers import OpenAICompatProvider
from analyst_driver.skills import default_skill_root, system_prompt
from analyst_driver.tools import ToolRegistry


def depth(schema: Any) -> int:
    """Levels of nested object properties; a flat object is 1."""
    if not isinstance(schema, dict):
        return 0
    children = list((schema.get("properties") or {}).values())
    if "items" in schema:
        children.append(schema["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        children.extend(schema.get(key) or [])
    inner = max((depth(c) for c in children), default=0)
    return inner + (1 if "properties" in schema else 0)


def local_report(registry: ToolRegistry, system: str) -> dict[str, Any]:
    wire = OpenAICompatProvider._tools(registry.specs())
    rows = []
    for tool in wire:
        fn = tool["function"]
        props = fn["parameters"].get("properties") or {}
        rows.append(
            {
                "name": fn["name"],
                "class": registry.classify(fn["name"]),
                "chars": len(json.dumps(tool)),
                "depth": depth(fn["parameters"]),
                "params_wrapper": list(props) == ["params"],
            }
        )
    tools_chars = len(json.dumps(wire))
    return {
        "n_tools": len(rows),
        "system_chars": len(system),
        "tools_chars": tools_chars,
        "total_chars": len(system) + tools_chars,
        "est_tokens_chars_over_4": (len(system) + tools_chars) // 4,
        "n_params_wrapper": sum(r["params_wrapper"] for r in rows),
        "tools": sorted(rows, key=lambda r: -r["chars"]),
    }


def remote_report(args: argparse.Namespace, registry: ToolRegistry, system: str) -> list[dict]:
    out = []
    for model in args.model:
        provider = OpenAICompatProvider(
            model=model, api_key_env=args.api_key_env, base_url=args.base_url, max_tokens=1
        )
        row: dict[str, Any] = {"model": model}
        try:
            reply = provider.complete(system, registry.specs(), provider.new_history("hi"))
            row["prompt_tokens"] = reply.usage.input_tokens
            row["stop"] = reply.stop
        except Exception as exc:  # the error body is the measurement
            row["error"] = f"{type(exc).__name__}: {exc}"[:500]
        out.append(row)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", action="append", default=[], help="repeatable")
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = p.parse_args()

    root = default_skill_root()
    registry = ToolRegistry.default(skill_root=root, read_roots=[])
    system = system_prompt(root)
    report = local_report(registry, system)
    if args.model:
        report["remote"] = remote_report(args, registry, system)

    if args.json:
        print(json.dumps(report, indent=1))
        return
    print(
        f"tools={report['n_tools']}  params_wrapper={report['n_params_wrapper']}"
        f"  system_chars={report['system_chars']}  tools_chars={report['tools_chars']}"
        f"  est_tokens(chars/4)={report['est_tokens_chars_over_4']}"
    )
    print(f"{'tool':34} {'class':12} {'chars':>7} {'depth':>5} params")
    for r in report["tools"]:
        print(
            f"{r['name']:34} {r['class']:12} {r['chars']:7d} {r['depth']:5d} {r['params_wrapper']}"
        )
    for r in report.get("remote", []):
        print(json.dumps(r))


if __name__ == "__main__":
    main()
