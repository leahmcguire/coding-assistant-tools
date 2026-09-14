"""Startup messages when scoped cannot begin a session.

`run` returns before connecting a client in these cases, or has `connect` stubbed
out, so no model is involved.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
from claude_agent_sdk import CLINotFoundError, ProcessError

from scoped.cli import (
    ScopedSession,
    auth_status,
    build_parser,
    build_state,
    login_problem,
    main,
    read_line,
    run,
)


@pytest.fixture(autouse=True)
def no_login_check(monkeypatch: pytest.MonkeyPatch):
    """Never shell out to the real `claude auth status` from `run`."""

    async def unknown():
        return None

    monkeypatch.setattr("scoped.cli.auth_status", unknown)


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


@pytest.fixture
def repl(monkeypatch: pytest.MonkeyPatch):
    """Stub the client out and script what the user types; returns what was sent."""
    sent: list[str] = []
    lines: list[str] = []

    async def connect(self, resume=None):
        pass

    async def send(self, text):
        sent.append(text)

    async def typed(prompt):
        return lines.pop(0) if lines else "exit"

    monkeypatch.setattr(ScopedSession, "connect", connect)
    monkeypatch.setattr(ScopedSession, "send", send)
    monkeypatch.setattr("scoped.cli.read_line", typed)
    return lines, sent


async def test_exiting_without_a_question_sends_nothing(repo: Path, repl):
    _, sent = repl
    assert await start("in_scope") == 0
    assert sent == []


async def test_first_question_carries_the_manifest(repo: Path, repl):
    lines, sent = repl
    lines += ["what does a do?", "and again?"]
    assert await start("in_scope") == 0
    assert len(sent) == 2
    assert "entire working set" in sent[0] and sent[0].endswith("what does a do?")
    assert sent[1] == "and again?"


async def test_scope_changes_before_the_first_question_are_in_the_manifest(repo: Path, repl):
    (repo / "b.py").write_text("print('b')\n")
    lines, sent = repl
    lines += ["/scope add b.py", "hi"]
    assert await start("in_scope") == 0
    assert len(sent) == 1
    assert sent[0].count("b.py") == 1  # rendered once, not also as a separate note


async def test_scope_notes_ride_along_with_the_next_question(repo: Path, repl):
    (repo / "b.py").write_text("print('b')\n")
    lines, sent = repl
    lines += ["hi", "/scope add b.py", "now?"]
    assert await start("in_scope") == 0
    assert len(sent) == 2
    assert "just been added" in sent[1] and sent[1].endswith("now?")


async def test_model_switch_applies_live_without_reconnecting(repo: Path, repl, monkeypatch):
    lines, sent = repl
    switched: list[str | None] = []
    reconnects: list[bool] = []

    async def set_model(self):
        switched.append(self.state.model)

    async def reconnect(self, fresh):
        reconnects.append(fresh)

    monkeypatch.setattr(ScopedSession, "set_model", set_model)
    monkeypatch.setattr(ScopedSession, "reconnect", reconnect)
    lines += ["/model sonnet", "hi"]
    assert await start("in_scope") == 0
    assert switched == ["sonnet"]
    assert reconnects == []
    assert len(sent) == 1


async def test_rejected_model_switch_keeps_the_old_model(repo: Path, repl, capsys, monkeypatch):
    lines, _ = repl

    async def set_model(self):
        raise RuntimeError("unknown model nope")

    monkeypatch.setattr(ScopedSession, "set_model", set_model)
    lines += ["/model nope", "/model"]
    assert await start("in_scope", "--model", "opus") == 0
    out = capsys.readouterr().out
    assert "could not switch model: unknown model nope. still on opus" in out
    assert "model: opus\n" in out  # the listing afterwards


async def test_connect_applies_the_model_and_records_the_choices(repo: Path, monkeypatch):
    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def get_server_info(self):
            return {
                "models": [
                    {"value": "default", "displayName": "Default"},
                    {"value": "legacy", "displayName": "Legacy", "disabled": True},
                    "not a model",
                ]
            }

    monkeypatch.setattr("scoped.cli.ClaudeSDKClient", FakeClient)
    state, cfg = build_state(build_parser().parse_args(["in_scope", "--model", "opus"]), repo)
    session = ScopedSession(state, cfg.dev)
    await session.connect()
    assert session.client is not None and session.client.options.model == "opus"
    assert state.models == [{"value": "default", "displayName": "Default"}]


@pytest.mark.parametrize(
    ("status", "warns"),
    [
        ({"loggedIn": False, "apiProvider": "firstParty"}, True),
        ({"loggedIn": False}, True),
        ({"loggedIn": True, "apiProvider": "firstParty"}, False),
        ({"loggedIn": False, "apiProvider": "bedrock"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_login_problem_only_flags_a_definite_first_party_logout(status, warns):
    assert (login_problem(status) is not None) == warns


async def test_logged_out_warns_before_the_prompt_but_carries_on(
    repo: Path, repl, capsys, monkeypatch
):
    async def logged_out():
        return {"loggedIn": False, "apiProvider": "firstParty"}

    monkeypatch.setattr("scoped.cli.auth_status", logged_out)
    assert await start("in_scope") == 0
    assert "claude auth login" in capsys.readouterr().err


def fake_claude(bin_dir: Path, script: str) -> None:
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(0o755)


async def test_auth_status_reads_json_even_on_a_failing_exit(tmp_path: Path, monkeypatch):
    fake_claude(tmp_path / "bin", "echo '{\"loggedIn\": false}'; exit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert await auth_status() == {"loggedIn": False}


async def test_auth_status_gives_up_on_output_it_cannot_read(tmp_path: Path, monkeypatch):
    fake_claude(tmp_path / "bin", "echo 'error: unknown command auth'; exit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert await auth_status() is None


async def test_auth_status_gives_up_on_a_hang(tmp_path: Path, monkeypatch):
    fake_claude(tmp_path / "bin", "exec sleep 10")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr("scoped.cli.LOGIN_CHECK_TIMEOUT", 0.2)
    assert await auth_status() is None


async def test_auth_status_without_a_cli_is_unknown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert await auth_status() is None


async def test_read_line_does_not_hold_up_shutdown(monkeypatch):
    """A reader still blocked in input() must not be joined when the loop exits."""
    release = threading.Event()
    monkeypatch.setattr("builtins.input", lambda prompt: release.wait(5) and "")
    task = asyncio.ensure_future(read_line("> "))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    readers = [t for t in threading.enumerate() if t.daemon and t.is_alive()]
    assert readers, "reader should be a daemon thread, abandoned at exit"
    release.set()


def test_bad_config_names_the_file_and_key(repo: Path, capsys, monkeypatch):
    (repo / ".scoped.toml").write_text('default_scope = "in_scope"\n')
    monkeypatch.setattr(sys, "argv", ["scoped"])
    assert main() == 2
    err = capsys.readouterr().err
    assert ".scoped.toml" in err
    assert "default_scope must be a list of strings" in err
