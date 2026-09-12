"""Tests for the PreToolUse gate.

These assert the exact dict shape the SDK expects, because a malformed hook
result fails open -- the tool would run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scoped.guard import make_guard
from scoped.scope import Scope


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "in_scope").mkdir()
    (tmp_path / "in_scope" / "a.py").write_text("print('a')\n")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "b.py").write_text("TOKEN = 'hunter2'\n")
    (tmp_path / ".env").write_text("API_KEY=abc\n")
    return tmp_path


@pytest.fixture
def scope(repo: Path) -> Scope:
    s = Scope(cwd=repo)
    s.add(["in_scope"])
    return s


@pytest.fixture
def guard(scope: Scope):
    return make_guard(scope)


async def call(guard, tool: str, **tool_input):
    return await guard(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input},
        "tool_1",
        None,
    )


def is_denial(result) -> bool:
    out = result.get("hookSpecificOutput", {})
    return out.get("permissionDecision") == "deny" and bool(out.get("permissionDecisionReason"))


async def test_read_in_scope_is_allowed(guard, repo):
    assert await call(guard, "Read", file_path=str(repo / "in_scope" / "a.py")) == {}


async def test_read_out_of_scope_is_denied(guard, repo):
    result = await call(guard, "Read", file_path=str(repo / "secret" / "b.py"))
    assert is_denial(result)


async def test_denial_has_the_exact_shape_the_sdk_expects(guard, repo):
    result = await call(guard, "Read", file_path=str(repo / "secret" / "b.py"))
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert isinstance(out["permissionDecisionReason"], str)


async def test_denial_tells_the_model_not_to_retry(guard, repo):
    result = await call(guard, "Read", file_path=str(repo / "secret" / "b.py"))
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "/scope add" in reason


async def test_traversal_is_denied(guard):
    assert is_denial(await call(guard, "Read", file_path="in_scope/../secret/b.py"))


async def test_edit_out_of_scope_is_denied(guard, repo):
    assert is_denial(await call(guard, "Edit", file_path=str(repo / "secret" / "b.py")))


async def test_write_creates_new_file_in_scoped_dir(guard, scope, repo):
    target = repo / "in_scope" / "test_new.py"
    assert await call(guard, "Write", file_path=str(target)) == {}
    # and it is readable afterwards
    assert scope.contains(target)
    assert await call(guard, "Read", file_path=str(target)) == {}


async def test_write_outside_scope_is_denied(guard, repo):
    assert is_denial(await call(guard, "Write", file_path=str(repo / "secret" / "new.py")))


async def test_grep_under_root_is_allowed(guard, repo):
    assert await call(guard, "Grep", pattern="token", path=str(repo / "in_scope")) == {}


async def test_grep_outside_root_is_denied(guard, repo):
    assert is_denial(await call(guard, "Grep", pattern="token", path=str(repo / "secret")))


async def test_grep_defaults_to_cwd_and_is_denied(guard):
    """No path argument means the whole working directory -- wider than scope."""
    assert is_denial(await call(guard, "Grep", pattern="token"))


async def test_bash_is_denied_in_strict_mode(guard):
    result = await call(guard, "Bash", command="pytest")
    assert is_denial(result)
    assert "run_tests" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_escape_tools_are_denied(guard):
    assert is_denial(await call(guard, "WebFetch", url="https://example.com"))
    assert is_denial(await call(guard, "Agent", prompt="go read everything"))


async def test_dev_tools_pass_through(guard):
    assert await call(guard, "mcp__dev__run_tests", paths=["in_scope"]) == {}


# -- open mode ---------------------------------------------------------------


async def test_unscope_lifts_file_scope(guard, scope, repo):
    assert is_denial(await call(guard, "Read", file_path=str(repo / "secret" / "b.py")))
    scope.set_mode("open")
    assert await call(guard, "Read", file_path=str(repo / "secret" / "b.py")) == {}


async def test_unscope_takes_effect_without_rebuilding_the_hook(guard, scope, repo):
    """The hook closes over the scope, so a mutation lands on the next call."""
    scope.set_mode("open")
    assert await call(guard, "Grep", pattern="x") == {}
    scope.set_mode("strict")
    assert is_denial(await call(guard, "Grep", pattern="x"))


async def test_secret_guard_survives_unscope(guard, scope, repo):
    scope.set_mode("open")
    assert is_denial(await call(guard, "Read", file_path=str(repo / ".env")))


async def test_secret_guard_catches_any_path_argument(guard, scope, repo):
    scope.set_mode("open")
    assert is_denial(await call(guard, "Grep", pattern="KEY", path=str(repo / ".env")))


async def test_bash_allowed_only_in_open_mode(guard, scope):
    scope.set_mode("open")
    assert await call(guard, "Bash", command="ls") == {}
