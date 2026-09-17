"""Unit tests for analyst_driver/skills.py — the system prompt is complete,
ordered, and byte-stable."""

from __future__ import annotations

from pathlib import Path

from analyst_driver.skills import (
    PREAMBLE,
    SKILL_ORDER,
    default_skill_root,
    skill_dirs,
    strip_frontmatter,
    system_prompt,
)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name, body in (
        ("radio-interferometry-driver", "# Driver\nexecute detail\n"),
        ("stage-orchestration", "# Orchestration\nsequence detail\n"),
        ("zz-extra", "# Extra\n"),
    ):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\ndescription: >\n  {name}\nallowed-tools: a, b\n---\n\n{body}"
        )
        (d / "02-b.md").write_text("b")
        (d / "01-a.md").write_text("a")
    (root / "no-skill-here").mkdir()
    (root / "README.md").write_text("not a skill")
    return root


def test_strip_frontmatter():
    assert strip_frontmatter("---\nx: 1\n---\n\nbody\n") == "body\n"
    assert strip_frontmatter("body only\n") == "body only\n"
    assert strip_frontmatter("---\nunterminated\n") == "---\nunterminated\n"


def test_skill_dirs_order_and_filter(tmp_path):
    names = [d.name for d in skill_dirs(_tree(tmp_path))]
    assert names == [*SKILL_ORDER, "zz-extra"]


def test_system_prompt_content_and_order(tmp_path):
    root = _tree(tmp_path)
    text = system_prompt(root)
    assert text.startswith(PREAMBLE)
    assert f"Skill root: {root}" in text
    i_orch = text.index("## Skill: stage-orchestration")
    i_drv = text.index("## Skill: radio-interferometry-driver")
    assert i_orch < i_drv
    assert "sequence detail" in text and "execute detail" in text
    assert "allowed-tools" not in text and "description: >" not in text
    assert "Sub-files (read with read_file): 01-a.md, 02-b.md" in text


def test_system_prompt_is_byte_stable(tmp_path):
    root = _tree(tmp_path)
    assert system_prompt(root) == system_prompt(root)


def test_real_skill_root_loads():
    root = default_skill_root()
    assert root.is_dir(), root
    text = system_prompt(root)
    assert "## Skill: stage-orchestration" in text
    assert "## Skill: radio-interferometry-driver" in text
    assert "01-macro-stages.md" in text and "07-calibration-execution.md" in text
    # the two bodies plus preamble stay small; sub-files are read on demand
    assert len(text) < 12_000, len(text)
