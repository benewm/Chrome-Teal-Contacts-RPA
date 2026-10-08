import json

import pytest

from teal_rpa.state import DONE, FAILED, PENDING, RunState, status_path_for


def test_status_file_sits_next_to_spreadsheet(tmp_path):
    assert status_path_for(tmp_path / "c.xlsx") == tmp_path / "c.xlsx.status.json"


def test_mark_persists_and_reloads(tmp_path):
    state = RunState.for_spreadsheet(tmp_path / "c.xlsx")
    assert state.status("https://www.linkedin.com/in/a") == PENDING
    state.mark("https://www.linkedin.com/in/a", DONE, row_number=2, name="A B")
    state.mark("https://www.linkedin.com/in/b", FAILED, row_number=3, reason="login wall")

    reloaded = RunState.for_spreadsheet(tmp_path / "c.xlsx")
    assert reloaded.is_done("https://www.linkedin.com/in/a")
    assert not reloaded.is_done("https://www.linkedin.com/in/b")
    entry = reloaded.entries["https://www.linkedin.com/in/b"]
    assert (entry["row"], entry["reason"], entry["attempts"]) == (3, "login wall", 1)
    assert json.loads(state.path.read_text())["version"] == 1


def test_failed_rows_are_retried_and_count_attempts(tmp_path):
    state = RunState(tmp_path / "s.json")
    state.mark("k", FAILED, reason="timeout")
    state.mark("k", DONE)
    assert state.entries["k"]["attempts"] == 2
    assert state.entries["k"]["reason"] is None


def test_counts(tmp_path):
    state = RunState(tmp_path / "s.json")
    state.mark("a", DONE)
    state.mark("b", FAILED)
    assert state.counts(["a", "b", "c"]) == {
        "pending": 1, "done": 1, "failed": 1, "skipped": 0, "needs attention": 0}


def test_reset(tmp_path):
    state = RunState(tmp_path / "s.json")
    state.mark("a", DONE)
    state.reset()
    assert not state.path.exists()
    assert RunState(tmp_path / "s.json").status("a") == PENDING


def test_rejects_unknown_status(tmp_path):
    with pytest.raises(ValueError):
        RunState(tmp_path / "s.json").mark("a", "finished")
