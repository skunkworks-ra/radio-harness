"""The system prompt: a fixed preamble plus every skill's ``SKILL.md`` body.

Skills are the directories under the skill root that hold a ``SKILL.md``.
Their bodies go into the system prompt in a fixed order; sub-files are read
on demand through the ``read_file`` tool. The result must be byte-identical
from one call to the next — providers cache by prefix — so file lists are
sorted and nothing time- or run-dependent is rendered.

The ``allowed-tools`` frontmatter is advisory and not read here.
"""

from __future__ import annotations

from pathlib import Path

#: Skills the harness loads, in this order. Orchestration first: it decides
#: the stage; the driver skill executes it.
SKILL_ORDER = ("stage-orchestration", "radio-interferometry-driver")

PREAMBLE = """\
You are one decision point in a CASA reduction driven by an external loop.
Tools measure; you reason. The skills below are always in context; their
sub-files are read with the read_file tool by bare file name. A
script-producing tool must be called with execute=false and the work
directory as workdir: it writes a script, and the loop executes the script
after you are gone. Call at most one script-producing tool per turn. Finish
every turn by calling submit_decision.
"""


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML block delimited by ``---`` lines, if present."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            return "".join(lines[i + 1 :]).lstrip("\n")
    return text


def skill_dirs(skill_root: Path) -> list[Path]:
    """The skill directories in load order. Unknown skills follow, sorted."""
    root = Path(skill_root)
    present = {p.name: p for p in root.iterdir() if (p / "SKILL.md").is_file()}
    ordered = [present.pop(name) for name in SKILL_ORDER if name in present]
    return ordered + [present[k] for k in sorted(present)]


def default_skill_root() -> Path:
    """``<repo>/skills`` — two levels above this package's ``src/``."""
    return Path(__file__).resolve().parents[2] / "skills"


def system_prompt(skill_root: Path) -> str:
    parts = [PREAMBLE, f"Skill root: {Path(skill_root)}\n"]
    for d in skill_dirs(skill_root):
        body = strip_frontmatter((d / "SKILL.md").read_text()).rstrip("\n")
        siblings = sorted(p.name for p in d.iterdir() if p.is_file() and p.name != "SKILL.md")
        parts.append(f"\n\n## Skill: {d.name}\n\n{body}\n")
        if siblings:
            parts.append("\nSub-files (read with read_file): " + ", ".join(siblings) + "\n")
    return "".join(parts)
