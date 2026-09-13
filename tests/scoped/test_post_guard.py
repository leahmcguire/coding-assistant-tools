"""Tests for the search-result filter.

This exists because of an observed leak, not a hypothetical one: a Grep whose
search root was legitimately in scope returned the contents of a .env sitting
inside that directory. PreToolUse cannot catch it -- the request is valid, only
the response is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scoped.guard import filter_grep_content, make_post_guard
from scoped.scope import Scope


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "in_scope").mkdir()
    (tmp_path / "in_scope" / "a.py").write_text("API_KEY = 1\n")
    (tmp_path / "in_scope" / ".env").write_text("API_KEY=leak\n")
    return tmp_path


@pytest.fixture
def scope(repo: Path) -> Scope:
    s = Scope(cwd=repo)
    s.add(["in_scope"])
    return s


async def post(guard, tool: str, response, cwd: Path):
    return await guard(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": {},
            "tool_response": response,
            "cwd": str(cwd),
        },
        "tool_1",
        None,
    )


def test_grep_content_drops_out_of_scope_lines(scope: Scope, repo: Path):
    content = "in_scope/.env:1:API_KEY=leak\nin_scope/a.py:1:API_KEY = 1"
    filtered, removed = filter_grep_content(scope, repo, content)
    assert "leak" not in filtered
    assert "a.py" in filtered
    assert removed == 1


def test_grep_content_keeps_everything_in_scope(scope: Scope, repo: Path):
    content = "in_scope/a.py:1:API_KEY = 1"
    filtered, removed = filter_grep_content(scope, repo, content)
    assert filtered == content
    assert removed == 0


def test_context_lines_follow_their_file(scope: Scope, repo: Path):
    """`-A/-B` context lines have no path of their own; they inherit the last one."""
    content = "in_scope/.env:1:API_KEY=leak\n--\nin_scope/a.py:1:API_KEY = 1\n    trailing"
    filtered, _ = filter_grep_content(scope, repo, content)
    assert "leak" not in filtered
    assert "    trailing" in filtered


async def test_post_guard_rewrites_grep_output(scope: Scope, repo: Path):
    guard = make_post_guard(scope)
    result = await post(
        guard,
        "Grep",
        {"mode": "content", "content": "in_scope/.env:1:API_KEY=leak", "numLines": 1},
        repo,
    )
    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert "leak" not in updated["content"]
    assert "removed" in updated["content"]


async def test_post_guard_filters_glob_filenames(scope: Scope, repo: Path):
    guard = make_post_guard(scope)
    result = await post(
        guard, "Glob", {"filenames": ["in_scope/a.py", "in_scope/.env"], "numFiles": 2}, repo
    )
    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["filenames"] == ["in_scope/a.py"]
    assert updated["numFiles"] == 1


async def test_post_guard_passes_clean_results_through(scope: Scope, repo: Path):
    guard = make_post_guard(scope)
    result = await post(guard, "Glob", {"filenames": ["in_scope/a.py"], "numFiles": 1}, repo)
    assert result == {}


async def test_post_guard_is_inert_in_open_mode(scope: Scope, repo: Path):
    scope.set_mode("open")
    guard = make_post_guard(scope)
    result = await post(guard, "Glob", {"filenames": ["in_scope/.env"], "numFiles": 1}, repo)
    assert result == {}


async def test_post_guard_ignores_other_tools(scope: Scope, repo: Path):
    guard = make_post_guard(scope)
    assert await post(guard, "Read", {"content": "anything"}, repo) == {}
