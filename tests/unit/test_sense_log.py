"""
Unit tests for ms_inspect/util/sense_log.py.

No CASA required — this module only touches sqlite3.
"""

from __future__ import annotations

import sqlite3

from ms_inspect.util.sense_log import ANALYST_DB_NAME, has_sensed, record_sense


class TestHasSensedOnEmptyWorkdir:
    def test_no_db_file_reads_as_not_sensed(self, tmp_path):
        assert has_sensed(tmp_path, "sess-1") is False

    def test_db_file_present_but_no_sense_log_table_reads_as_not_sensed(self, tmp_path):
        con = sqlite3.connect(tmp_path / ANALYST_DB_NAME)
        con.execute("CREATE TABLE unrelated (id INTEGER)")
        con.commit()
        con.close()
        assert has_sensed(tmp_path, "sess-1") is False


class TestRecordAndCheck:
    def test_recorded_session_reads_as_sensed(self, tmp_path):
        record_sense(tmp_path, "sess-1", "delay_bandpass_gain")
        assert has_sensed(tmp_path, "sess-1") is True

    def test_unrecorded_session_reads_as_not_sensed(self, tmp_path):
        record_sense(tmp_path, "sess-1", "delay_bandpass_gain")
        assert has_sensed(tmp_path, "sess-2") is False

    def test_two_sessions_are_independent(self, tmp_path):
        record_sense(tmp_path, "sess-1", "apply_preflag")
        assert has_sensed(tmp_path, "sess-1") is True
        assert has_sensed(tmp_path, "sess-2") is False
        record_sense(tmp_path, "sess-2", "generate_priorcals")
        assert has_sensed(tmp_path, "sess-2") is True

    def test_repeated_sense_in_the_same_session_is_fine(self, tmp_path):
        record_sense(tmp_path, "sess-1", "apply_preflag")
        record_sense(tmp_path, "sess-1", "generate_priorcals")
        assert has_sensed(tmp_path, "sess-1") is True

    def test_row_content(self, tmp_path):
        record_sense(tmp_path, "sess-1", "delay_bandpass_gain")
        con = sqlite3.connect(tmp_path / ANALYST_DB_NAME)
        rows = con.execute("SELECT session_id, next_recommended_step FROM sense_log").fetchall()
        con.close()
        assert rows == [("sess-1", "delay_bandpass_gain")]


class TestConcurrentDatabaseFile:
    def test_coexists_with_other_tables_in_the_same_db(self, tmp_path):
        # sense_log shares analyst.db with stage_log/reduction_log —
        # CREATE TABLE IF NOT EXISTS must not disturb an existing, unrelated
        # table in the same file.
        con = sqlite3.connect(tmp_path / ANALYST_DB_NAME)
        con.execute("CREATE TABLE stage_log (id INTEGER PRIMARY KEY, stage TEXT)")
        con.execute("INSERT INTO stage_log (stage) VALUES ('gaincal')")
        con.commit()
        con.close()

        record_sense(tmp_path, "sess-1", "delay_bandpass_gain")
        assert has_sensed(tmp_path, "sess-1") is True

        con = sqlite3.connect(tmp_path / ANALYST_DB_NAME)
        assert con.execute("SELECT stage FROM stage_log").fetchall() == [("gaincal",)]
        con.close()
