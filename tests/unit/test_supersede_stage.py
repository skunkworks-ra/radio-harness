"""
Unit tests for tools/supersede_stage.py — ms_supersede_stage.

Row-marking logic itself (util/stage_log.supersede_stages) has its own
coverage in test_stage_log.py. This file covers only the tool wrapper: input
validation and the response envelope shape.
"""

from __future__ import annotations

import pytest

from ms_inspect.exceptions import ComputationError
from ms_inspect.tools import supersede_stage
from ms_inspect.util.stage_log import read_stage_log, record_stage


def _stage(workdir, stage, name):
    (workdir / name).mkdir()
    record_stage(str(workdir), stage, str(workdir / name))


def test_marks_the_named_stage_and_reports_rows_marked(tmp_path):
    _stage(tmp_path, "initial_bandpass", "BP0.b")
    _stage(tmp_path, "applycal", "t.ms")

    result = supersede_stage.run(str(tmp_path), ["applycal"], "rerun of initial_bandpass")

    assert result["data"]["rows_marked"]["value"] == 1
    assert result["data"]["stages"]["value"] == ["applycal"]
    assert result["data"]["by"]["value"] == "rerun of initial_bandpass"
    entries = read_stage_log(tmp_path)
    assert [e["stage"] for e in entries if "superseded_by" not in e] == ["initial_bandpass"]


def test_missing_workdir_raises(tmp_path):
    with pytest.raises(ComputationError):
        supersede_stage.run(str(tmp_path / "nope"), ["applycal"], "rerun")


def test_empty_stages_raises(tmp_path):
    with pytest.raises(ComputationError):
        supersede_stage.run(str(tmp_path), [], "rerun")


def test_empty_reason_raises(tmp_path):
    _stage(tmp_path, "applycal", "t.ms")
    with pytest.raises(ComputationError):
        supersede_stage.run(str(tmp_path), ["applycal"], "")


def test_no_stage_log_yet_marks_nothing(tmp_path):
    result = supersede_stage.run(str(tmp_path), ["applycal"], "rerun")
    assert result["data"]["rows_marked"]["value"] == 0
