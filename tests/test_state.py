import json
import os
from pathlib import Path

import pytest

from teal_rpa.state import DONE, FAILED, PENDING, RunState, status_path_for


def test_progress_file_lives_outside_the_spreadsheet_folder(tmp_path):
    path = status_path_for(tmp_path / "OneDrive" / "Upload 4.xlsx")
    assert path.parent == tmp_path / "app-data" / "progress"
    assert path.name.startswith("Upload 4.xlsx-") and path.name.endswith(".status.json")
    # Same file name in another folder gets its own progress file.
    assert status_path_for(tmp_path / "other" / "Upload 4.xlsx") != path


def test_old_progress_file_is_carried_over(tmp_path):
    sheet = tmp_path / "Upload 4.xlsx"
    old = tmp_path / "Upload 4.xlsx.status.json"
    old.write_text(json.dumps({"version": 1, "contacts": {"k": {"status": "done"}}}))
    state = RunState.for_spreadsheet(sheet)
    assert state.is_done("k")
    assert not old.exists() and (tmp_path / "Upload 4.xlsx.status.json.moved").exists()


def test_save_waits_out_a_lock(tmp_path, monkeypatch):
    real_replace = os.replace
    calls = []

    def locked_twice(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr("teal_rpa.state.os.replace", locked_twice)
    monkeypatch.setattr("teal_rpa.state.time.sleep", lambda s: None)
    state = RunState(tmp_path / "s.json")
    state.mark("a", DONE)
    assert len(calls) == 3 and not state.unsaved
    assert RunState(tmp_path / "s.json").is_done("a")


def test_save_never_crashes_when_locked(tmp_path, monkeypatch):
    def always_locked(*args, **kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr("teal_rpa.state.os.replace", always_locked)
    monkeypatch.setattr("teal_rpa.state.time.sleep", lambda s: None)
    path = tmp_path / "s.json"
    path.write_text("{}")
    monkeypatch.setattr(type(path), "write_text", lambda self, *a, **k: always_locked()
                        if self == path else Path.__dict__["write_text"](self, *a, **k), raising=True)
    state = RunState(path)
    state.mark("a", DONE)  # no exception
    assert state.unsaved and state.is_done("a")

    monkeypatch.undo()
    state.mark("b", DONE)  # next change writes everything
    assert not state.unsaved
    reloaded = RunState(path)
    assert reloaded.is_done("a") and reloaded.is_done("b")


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
