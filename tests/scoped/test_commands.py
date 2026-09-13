from __future__ import annotations

from pathlib import Path

import pytest

from scoped.commands import Action, AppState, handle
from scoped.config import load
from scoped.profile import ContextProfile
from scoped.scope import Scope


def act(state: AppState, line: str) -> Action:
    """Run a line that must be a command."""
    action = handle(state, line)
    assert action is not None
    return action


def text(value: str | None) -> str:
    assert value is not None
    return value


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "in_scope").mkdir()
    (tmp_path / "in_scope" / "a.py").write_text("print('a')\n")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "b.py").write_text("print('b')\n")
    (tmp_path / "other" / "notes.md").write_text("# notes\n")
    return tmp_path


@pytest.fixture
def state(repo: Path) -> AppState:
    scope = Scope(cwd=repo)
    scope.add(["in_scope"])
    return AppState(scope=scope, profile=ContextProfile(), last_scope_args=["in_scope"])


def test_plain_text_is_not_a_command(state: AppState):
    assert handle(state, "why is this broken?") is None


def test_unknown_command_is_reported(state: AppState):
    action = act(state, "/nope")
    assert "unknown command" in text(action.message)


# -- scope commands take effect immediately, no reconnect --------------------


def test_scope_add_widens_without_reconnecting(state: AppState, repo: Path):
    action = act(state, "/scope add other")
    assert action.reconnect is False
    assert state.scope.contains(repo / "other" / "b.py")
    assert "just been added" in text(action.inject)


def test_scope_add_injects_only_the_new_files(state: AppState):
    action = act(state, "/scope add other")
    assert "b.py" in text(action.inject)
    assert "a.py" not in text(action.inject)  # already in context from the first render


def test_scope_rm_narrows(state: AppState, repo: Path):
    handle(state, "/scope add other")
    handle(state, "/scope rm other")
    assert not state.scope.contains(repo / "other" / "b.py")


def test_unscope_and_rescope_flip_the_mode(state: AppState):
    action = act(state, "/unscope")
    assert state.scope.mode == "open"
    assert "does not retract" in text(action.message)  # the honest caveat is surfaced
    handle(state, "/rescope")
    assert state.scope.mode == "strict"


def test_scope_shows_the_manifest(state: AppState):
    action = act(state, "/scope")
    assert "a.py" in text(action.message)
    assert "tokens" in text(action.message)


# -- context commands need a reconnect ---------------------------------------


def test_context_toggle_requests_a_reconnect(state: AppState):
    action = act(state, "/context claude-md on")
    assert action.reconnect is True
    assert action.fresh is False
    assert state.profile.claude_md is True


def test_fresh_flag_is_passed_through(state: AppState):
    action = act(state, "/context claude-md on --fresh")
    assert action.reconnect is True and action.fresh is True


def test_skills_values(state: AppState):
    handle(state, "/context skills all")
    assert state.profile.skills == "all"
    handle(state, "/context skills dataviz,design")
    assert state.profile.skills == ["dataviz", "design"]
    handle(state, "/context skills none")
    assert state.profile.skills is None


def test_prompt_must_be_valid(state: AppState):
    action = act(state, "/context prompt nonsense")
    assert action.reconnect is False
    assert state.profile.prompt == "lean"


def test_context_with_no_args_reports(state: AppState):
    action = act(state, "/context")
    assert "claude-md=off" in text(action.message)


# -- filters -----------------------------------------------------------------


def test_filters_ext_re_expands_the_scope(state: AppState, repo: Path):
    handle(state, "/scope add other")
    state.last_scope_args = ["in_scope", "other"]
    handle(state, "/filters ext .md")
    names = {p.name for p in state.scope.files}
    assert names == {"notes.md"}


def test_filters_reports_why_files_were_skipped(state: AppState, repo: Path):
    (repo / "in_scope" / ".env").write_text("K=v\n")
    state.scope.add(["in_scope"])
    action = act(state, "/filters")
    assert "secret" in text(action.message)


# -- config ------------------------------------------------------------------


def test_config_is_optional(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg.source is None
    assert cfg.default_scope == []


def test_config_is_read(tmp_path: Path):
    (tmp_path / ".scoped.toml").write_text(
        'default_scope = ["src"]\n'
        'extensions = [".py"]\n'
        'test_command = ["pytest", "-x"]\n'
        '\n[context]\nclaude_md = true\nskills = "all"\n'
    )
    cfg = load(tmp_path)
    assert cfg.default_scope == ["src"]
    assert cfg.filters.extensions == {".py"}
    assert cfg.dev.test_command == ["pytest", "-x"]
    assert cfg.profile.claude_md is True
    assert cfg.profile.skills == "all"


def test_config_is_found_in_a_parent_directory(tmp_path: Path):
    (tmp_path / ".scoped.toml").write_text('default_scope = ["src"]\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert load(nested).default_scope == ["src"]


def test_string_commands_are_rejected(tmp_path: Path):
    """A command string would need splitting, and splitting leads back to a shell."""
    (tmp_path / ".scoped.toml").write_text('test_command = "pytest -q; rm -rf /"\n')
    with pytest.raises(ValueError, match="list of strings"):
        load(tmp_path)
