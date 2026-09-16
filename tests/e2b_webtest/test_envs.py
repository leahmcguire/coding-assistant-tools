"""Which environment variables reach the sandbox.

The central claim: only names explicitly listed in `env.forward` are ever
read from the local process environment. A real secret sitting in
`os.environ` under an unlisted name (the way a direnv-loaded Kroger or Resend
key would be) never appears in the result.
"""

from __future__ import annotations

import pytest

from e2b_webtest.config import EnvConfig
from e2b_webtest.envs import base_env, describe, parse_env_output, plan


def test_forward_reads_only_listed_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOWED_NAME", "visible")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "super-secret-real-key")
    result = base_env(EnvConfig(forward=["ALLOWED_NAME"]))
    assert result == {"ALLOWED_NAME": "visible"}
    assert "KROGER_CLIENT_SECRET" not in result


def test_forward_skips_unset_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    result = base_env(EnvConfig(forward=["NOT_SET_ANYWHERE"]))
    assert result == {}


def test_set_literals_are_included_and_override_forwarded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PORT", "1234")
    result = base_env(EnvConfig(forward=["PORT"], set={"PORT": "9000", "MODE": "test"}))
    assert result == {"PORT": "9000", "MODE": "test"}


def test_empty_config_forwards_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANYTHING", "value")
    assert base_env(EnvConfig()) == {}


def test_plan_categorizes_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PRESENT", "value")
    monkeypatch.delenv("ABSENT", raising=False)
    result = plan(EnvConfig(forward=["PRESENT", "ABSENT"], set={"LITERAL": "x"}))
    assert result.forwarded == ["PRESENT"]
    assert result.missing == ["ABSENT"]
    assert result.literal == ["LITERAL"]
    assert result.sent == ["LITERAL", "PRESENT"]


def test_describe_never_prints_values(monkeypatch: pytest.MonkeyPatch):
    """The whole point of the approval gate: names, never values."""
    monkeypatch.setenv("SHELL_SECRET", "super-secret-shell-value")
    text = describe(plan(EnvConfig(forward=["SHELL_SECRET"], set={"CONFIG_NAME": "config-value"})))
    assert "SHELL_SECRET" in text
    assert "CONFIG_NAME" in text
    assert "super-secret-shell-value" not in text
    assert "config-value" not in text


def test_describe_with_nothing_to_send():
    assert describe(plan(EnvConfig())) == "environment variables to be sent into the sandbox: none"


def test_describe_marks_unset_names_as_not_sent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NEVER_SET", raising=False)
    text = describe(plan(EnvConfig(forward=["NEVER_SET"], set={"SOMETHING": "1"})))
    assert "NEVER_SET" in text
    assert "not sent" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FOO=bar\n", {"FOO": "bar"}),
        ("export FOO=bar\n", {"FOO": "bar"}),
        ('FOO="bar baz"\n', {"FOO": "bar baz"}),
        ("FOO='bar'\n", {"FOO": "bar"}),
        ("# comment\n\nFOO=bar\n", {"FOO": "bar"}),
        ("not a valid line\nFOO=bar\n", {"FOO": "bar"}),
        (
            "API_URL=http://127.0.0.1:54321\nANON_KEY=abc.def\n",
            {
                "API_URL": "http://127.0.0.1:54321",
                "ANON_KEY": "abc.def",
            },
        ),
    ],
)
def test_parse_env_output(text: str, expected: dict[str, str]):
    assert parse_env_output(text) == expected
