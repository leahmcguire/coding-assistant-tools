"""Tests for the shell-free dev tools.

The central claim is that there is no shell, so shell syntax in an argument is
inert data. That is what these assert.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scoped.devtools import DevConfig, make_dev_tools, run_argv
from scoped.scope import Scope

ECHO_ARGV = [sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "b.py").write_text("x = 1\n")
    return tmp_path


@pytest.fixture
def scope(repo: Path) -> Scope:
    s = Scope(cwd=repo)
    s.add(["tests"])
    return s


def tools(scope: Scope, config: DevConfig | None = None) -> dict:
    return {t.name: t for t in make_dev_tools(scope, config or DevConfig())}


def body(result) -> str:
    return "\n".join(block.get("text", "") for block in result["content"])


# -- no shell ---------------------------------------------------------------


async def test_shell_metacharacters_are_inert(repo: Path):
    """`; echo pwned` must arrive as a literal argument, not run as a command."""
    result = await run_argv([*ECHO_ARGV, "; echo pwned"], str(repo), 30)
    out = body(result)
    assert "'; echo pwned'" in out  # passed through as one literal argv entry
    assert "\npwned" not in out  # never executed


async def test_command_substitution_is_inert(repo: Path):
    result = await run_argv([*ECHO_ARGV, "$(whoami)", "`whoami`", "a|b", "c>d"], str(repo), 30)
    out = body(result)
    assert "$(whoami)" in out
    assert "`whoami`" in out


async def test_missing_binary_is_a_tool_error(repo: Path):
    result = await run_argv(["definitely-not-a-real-binary-xyz"], str(repo), 30)
    assert result.get("is_error") is True
    assert "not installed" in body(result)


async def test_failing_command_is_not_a_tool_error(repo: Path):
    """A failing test is a successful call reporting a failure."""
    result = await run_argv([sys.executable, "-c", "import sys; sys.exit(1)"], str(repo), 30)
    assert result.get("is_error") is None
    assert "exit code 1" in body(result)


async def test_output_is_capped(repo: Path):
    result = await run_argv([sys.executable, "-c", "print('x\\n' * 5000)"], str(repo), 30)
    out = body(result)
    assert "earlier lines omitted" in out
    assert len(out.splitlines()) < 250


# -- path validation --------------------------------------------------------


async def test_out_of_scope_path_is_rejected(scope: Scope):
    result = await tools(scope)["run_tests"].handler({"paths": ["secret/b.py"]})
    assert result.get("is_error") is True
    assert "/scope add" in body(result)


async def test_in_scope_path_is_accepted(scope: Scope, repo: Path):
    config = DevConfig(test_command=ECHO_ARGV)
    result = await tools(scope, config)["run_tests"].handler({"paths": ["tests/test_a.py"]})
    assert result.get("is_error") is None
    assert "tests/test_a.py" in body(result)


async def test_path_argument_containing_shell_syntax_stays_literal(scope: Scope):
    """An out-of-scope path is rejected before a process is ever started."""
    result = await tools(scope)["run_tests"].handler({"paths": ["; rm -rf /"]})
    assert result.get("is_error") is True


async def test_expression_is_passed_as_a_k_argument(scope: Scope):
    config = DevConfig(test_command=ECHO_ARGV)
    result = await tools(scope, config)["run_tests"].handler({"expression": "not slow"})
    assert "'-k', 'not slow'" in body(result)


async def test_model_cannot_change_the_binary(scope: Scope):
    """Only `paths` and `expression` are accepted; argv[0] comes from config."""
    schema = tools(scope)["run_tests"].input_schema
    assert set(schema["properties"]) == {"paths", "expression"}


async def test_lint_fix_flag(scope: Scope):
    config = DevConfig(lint_command=ECHO_ARGV)
    result = await tools(scope, config)["run_lint"].handler({"fix": True})
    assert "'--fix'" in body(result)
