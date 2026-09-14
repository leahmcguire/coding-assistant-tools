from __future__ import annotations

from pathlib import Path

import pytest

from scoped.commands import Action, AppState, handle
from scoped.config import ConfigError, load
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


@pytest.mark.parametrize("line", ["/exit", "/quit", "exit", "quit", "EXIT", "exit()", "quit()"])
def test_exit_quits_with_or_without_a_slash(state: AppState, line: str):
    action = handle(state, line)
    assert action is not None and action.quit


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


def test_scope_add_explains_a_missing_path(state: AppState):
    action = act(state, "/scope add /other")
    message = text(action.message)
    assert "could not find '/other'" in message
    assert "did you mean 'other'?" in message
    assert action.inject is None


def test_scope_add_says_when_already_in_scope(state: AppState):
    assert "'in_scope' is already in scope" in text(act(state, "/scope add in_scope").message)


def test_scope_add_flags_typos_alongside_real_additions(state: AppState):
    message = text(act(state, "/scope add other nowhere").message)
    assert message.startswith("+ 2 file(s)")
    assert "could not find 'nowhere'" in message


def test_scope_rm_of_unscoped_path_says_so(state: AppState):
    assert "nothing removed" in text(act(state, "/scope rm other").message)


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


def test_on_off_settings_reject_other_values(state: AppState):
    action = act(state, "/context bash yes")
    assert action.reconnect is False
    assert "must be on or off" in text(action.message)
    assert state.profile.bash is False


def test_unknown_context_setting_lists_the_choices(state: AppState):
    message = text(act(state, "/context nope on").message)
    assert "claude-md" in message and "skills" in message


def test_context_setting_without_value_shows_its_choices(state: AppState):
    assert "lean|preset" in text(act(state, "/context prompt").message)


def test_context_with_no_args_reports(state: AppState):
    action = act(state, "/context")
    assert "claude-md=off" in text(action.message)


# -- model switches live, no reconnect ---------------------------------------

MODELS = [
    {"value": "default", "displayName": "Default (recommended)", "description": "Opus 5"},
    {"value": "sonnet", "displayName": "Sonnet", "description": "Fast"},
]


def test_model_lists_the_choices_and_marks_the_current_one(state: AppState):
    state.models = MODELS
    state.active_model = "claude-opus-5"
    message = text(act(state, "/model").message)
    assert "last reply from: claude-opus-5" in message
    assert "* 1. default" in message
    assert "  2. sonnet" in message


def test_model_before_connecting_says_names_still_work(state: AppState):
    assert "a name or alias works" in text(act(state, "/model").message)


def test_model_by_name_switches_without_reconnecting(state: AppState):
    action = act(state, "/model sonnet")
    assert action.set_model and not action.reconnect
    assert state.model == "sonnet"


def test_model_by_number_picks_from_the_list(state: AppState):
    state.models = MODELS
    handle(state, "/model 2")
    assert state.model == "sonnet"


def test_model_default_clears_the_override(state: AppState):
    state.model = "sonnet"
    action = act(state, "/model default")
    assert action.set_model and state.model is None


def test_model_number_out_of_range_changes_nothing(state: AppState):
    state.models = MODELS
    action = act(state, "/model 9")
    assert not action.set_model
    assert "no model #9" in text(action.message)
    assert state.model is None


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


@pytest.mark.parametrize(
    ("toml", "expected"),
    [
        ('default_scope = "src"\n', "default_scope must be a list of strings"),
        ('secret_guard = "false"\n', "secret_guard must be true or false"),
        ('max_bytes = "big"\n', "max_bytes must be a whole number"),
        ("test_command = []\n", "test_command must be a list of strings"),
        ('[context]\nprompt = "fancy"\n', "context.prompt must be"),
        ('[context]\nskills = "dataviz"\n', "context.skills must be"),
        ("default_scope = [\n", "is not valid TOML"),
    ],
)
def test_bad_config_names_the_key(tmp_path: Path, toml: str, expected: str):
    (tmp_path / ".scoped.toml").write_text(toml)
    with pytest.raises(ConfigError) as caught:
        load(tmp_path)
    assert expected in str(caught.value)
    assert ".scoped.toml" in str(caught.value)


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
