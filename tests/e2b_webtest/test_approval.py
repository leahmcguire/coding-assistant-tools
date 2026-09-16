"""The environment-variable approval gate.

The central claim: nothing crosses into the sandbox without its name being
shown first, and declining -- or having no terminal to be asked at -- sends
nothing at all.
"""

from __future__ import annotations

import pytest

from e2b_webtest.cli import _approve_env
from e2b_webtest.config import Config, EnvConfig, SandboxConfig


class FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def make_config(**env_kwargs) -> Config:
    return Config(sandbox=SandboxConfig(template="t"), env=EnvConfig(**env_kwargs))


def refuse_input(prompt: str = "") -> str:
    raise AssertionError("should not have prompted")


def test_nothing_to_send_does_not_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr("sys.stdin", FakeStdin(True))
    monkeypatch.setattr("builtins.input", refuse_input)
    assert _approve_env(make_config(), assume_yes=False) == {}
    assert "none" in capsys.readouterr().out


def test_declining_sends_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOKEN", "real-value")
    monkeypatch.setattr("sys.stdin", FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _approve_env(make_config(forward=["TOKEN"]), assume_yes=False) is None


def test_accepting_sends_the_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOKEN", "real-value")
    monkeypatch.setattr("sys.stdin", FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _approve_env(make_config(forward=["TOKEN"]), assume_yes=False) == {"TOKEN": "real-value"}


def test_no_terminal_without_yes_refuses(monkeypatch: pytest.MonkeyPatch):
    """Unattended runs must fail closed, not send silently."""
    monkeypatch.setenv("TOKEN", "real-value")
    monkeypatch.setattr("sys.stdin", FakeStdin(False))
    monkeypatch.setattr("builtins.input", refuse_input)
    assert _approve_env(make_config(forward=["TOKEN"]), assume_yes=False) is None


def test_yes_flag_skips_the_prompt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOKEN", "real-value")
    monkeypatch.setattr("sys.stdin", FakeStdin(False))
    monkeypatch.setattr("builtins.input", refuse_input)
    assert _approve_env(make_config(forward=["TOKEN"]), assume_yes=True) == {"TOKEN": "real-value"}


def test_prompt_output_has_no_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("TOKEN", "super-secret-value")
    monkeypatch.setattr("sys.stdin", FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    _approve_env(make_config(forward=["TOKEN"]), assume_yes=False)
    assert "super-secret-value" not in capsys.readouterr().out
