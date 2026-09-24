"""Run one stage brief (ORIENTATION_BRIEF.md) through radio-harness's own agent loop.

The system prompt is the brief's section 1 plus the pasted skill files of
sections 3 and 4. The task is section 2 with placeholders filled. The tool
list is the brief's section 5 names, served by the harness ToolRegistry over
the in-process ms_inspect server. No skill index, no read_file, no
submit_decision: the model ends by writing the report and calling no tool.

Run from the radio-harness repo root:

    pixi run python scripts/run_brief.py --brief scripts/ORIENTATION_BRIEF.md \
        --ms <ms> --workdir <dir> \
        --goal "<owner's words>" --out <run dir>

The API key is read from the environment variable named by --api-key-env.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from analyst_driver.agent import Agent
from analyst_driver.providers import OpenAICompatProvider
from analyst_driver.tools import ToolRegistry

HARNESS = Path(__file__).resolve().parents[1]


def parse_brief(text: str) -> tuple[str, str, list[str]]:
    """Return (system prompt, task template, tool names) from the brief."""
    sections = re.split(r"^## (\d)\. .*$", text, flags=re.M)
    by_num = {sections[i]: sections[i + 1] for i in range(1, len(sections) - 1, 2)}
    system = re.search(r"```text\n(.*?)\n```", by_num["1"], re.S).group(1)
    task = re.search(r"```text\n(.*?)\n```", by_num["2"], re.S).group(1)
    stage = re.findall(r"````markdown\n(.*?)\n````", by_num["3"], re.S)
    contract = re.findall(r"````markdown\n(.*?)\n````", by_num["4"], re.S)
    tools = re.findall(r"^### `mcp__ms-inspect__(\w+)`", by_num["5"], re.M)
    system += "\n\n# Stage instructions\n\n" + "\n\n".join(stage)
    system += "\n\n# Output contract\n\n" + "\n\n".join(contract)
    return system, task, tools


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", type=Path, required=True)
    ap.add_argument("--ms", required=True)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="muse-glimmer")
    ap.add_argument("--base-url", default="https://llm.jetstream-cloud.org/api")
    ap.add_argument("--api-key-env", default="JETSTREAM_API_KEY")
    ap.add_argument("--max-rounds", type=int, default=30)
    args = ap.parse_args()

    system, task, tool_names = parse_brief(args.brief.read_text())
    task = task.format(MS_PATH=args.ms, WORKDIR=args.workdir, GOAL=args.goal)
    args.workdir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    registry = ToolRegistry.default(skill_root=HARNESS / "skills", read_roots=[args.workdir])
    missing = [n for n in tool_names if n not in registry._entries]
    if missing:
        raise SystemExit(f"brief names tools the registry does not have: {missing}")
    registry._entries = {n: registry._entries[n] for n in tool_names}

    provider = OpenAICompatProvider(
        model=args.model, api_key_env=args.api_key_env, base_url=args.base_url
    )
    agent = Agent(provider, registry, system, max_rounds=args.max_rounds)
    (args.out / "system_prompt.txt").write_text(system)
    (args.out / "task.txt").write_text(task)
    turn, error = agent.run_turn(task, args.workdir)

    (args.out / "transcript.json").write_text(json.dumps(turn.transcript, indent=1, default=str))
    texts = [e["text"] for e in turn.transcript if e["type"] == "assistant" and e.get("text")]
    (args.out / "report.md").write_text(texts[-1] if texts else "(no text)")
    calls = [e["tool"] for e in turn.transcript if e["type"] == "tool_call"]
    print(f"rounds={turn.rounds} tool_calls={len(calls)} error={error}")
    print("calls:", ", ".join(calls))
    print("not called:", ", ".join(n for n in tool_names if n not in calls) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
