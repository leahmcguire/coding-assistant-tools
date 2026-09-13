"""Startup messages when scoped cannot begin a session.

`run` returns before connecting a client in these cases, or has `connect` stubbed
out, so no model is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from claude_agent_sdk import CLINotFoundError, ProcessError

from scoped.cli import ScopedSession, build_parser, main, run


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve()
    (root / "in_scope").mkdir()
    (root / "in_scope" / "a.py").write_text("print('a')\n")
    monkeypatch.chdir(root)
    return root


async def start(*argv: str) -> int:
    return await run(build_parser().parse_args(list(argv)))


async def test_missing_path_says_it_could_not_be_found(repo: Path, capsys):
    assert await start("/in_scope") == 2
    err = capsys.readouterr().err
    assert "scoped: could not find '/in_scope'" in err
    assert "did you mean 'in_scope'?" in err
    assert "usage:" not in err


async def test_fully_filtered_directory_explains_why(repo: Path, capsys):
    assert await start("in_scope", "--ext", ".rs") == 2
    err = capsys.readouterr().err
    assert "scoped: found no usable files in 'in_scope'" in err
    assert "in_scope/a.py --" in err
    assert "usage:" not in err


async def test_no_paths_still_prints_usage(repo: Path, capsys):
    assert await start() == 2
    err = capsys.readouterr().err
    assert "Nothing in scope" in err
    assert "usage:" in err


async def test_missing_claude_cli_is_explained(repo: Path, capsys, monkeypatch):
    async def not_installed(self, resume=None):
        raise CLINotFoundError()

    monkeypatch.setattr(ScopedSession, "connect", not_installed)
    assert await start("in_scope") == 1
    assert "Claude Code is not installed" in capsys.readouterr().err


async def test_session_failure_is_explained(repo: Path, capsys, monkeypatch):
    async def fails(self, resume=None):
        raise ProcessError("Command failed", exit_code=1, stderr="not logged in")

    monkeypatch.setattr(ScopedSession, "connect", fails)
    assert await start("in_scope") == 1
    err = capsys.readouterr().err
    assert "not logged in" in err
    assert "Traceback" not in err


def test_bad_config_names_the_file_and_key(repo: Path, capsys, monkeypatch):
    (repo / ".scoped.toml").write_text('default_scope = "in_scope"\n')
    monkeypatch.setattr(sys, "argv", ["scoped"])
    assert main() == 2
    err = capsys.readouterr().err
    assert ".scoped.toml" in err
    assert "default_scope must be a list of strings" in err
