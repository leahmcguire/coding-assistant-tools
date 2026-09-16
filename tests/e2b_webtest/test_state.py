"""`.e2b-webtest/state.json`: save, load, clear, and the git-exclude side effect."""

from __future__ import annotations

import subprocess
from pathlib import Path

from e2b_webtest.state import STATE_DIR_NAME, State, clear, load, save


def make_state() -> State:
    return State(
        sandbox_id="sbx_123",
        template="my-template",
        created_at="2026-09-16T00:00:00+00:00",
        timeout_at="2026-09-16T01:00:00+00:00",
        services=["backend", "web"],
    )


def test_round_trip(tmp_path: Path):
    original = make_state()
    save(tmp_path, original)
    loaded = load(tmp_path)
    assert loaded == original


def test_load_returns_none_when_absent(tmp_path: Path):
    assert load(tmp_path) is None


def test_clear_removes_the_file(tmp_path: Path):
    save(tmp_path, make_state())
    clear(tmp_path)
    assert load(tmp_path) is None


def test_clear_on_absent_state_is_a_no_op(tmp_path: Path):
    clear(tmp_path)  # must not raise


def test_save_adds_git_exclude_entry_once(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    save(tmp_path, make_state())
    save(tmp_path, make_state())  # a second save must not duplicate the entry

    exclude_file = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_file.read_text().splitlines()
    assert lines.count(f"/{STATE_DIR_NAME}") == 1


def test_save_without_a_git_repo_does_not_raise(tmp_path: Path):
    save(tmp_path, make_state())  # tmp_path is not a git repo
    assert load(tmp_path) == make_state()
