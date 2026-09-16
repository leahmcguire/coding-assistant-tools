"""Uploading and running a Playwright script inside the sandbox, and pulling
back whatever it wrote -- against a fake sandbox, so no network, container,
or real Playwright involved.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from e2b import CommandExitException

from e2b_webtest.config import UploadConfig
from e2b_webtest.scripts import OUTPUT_DIR_ENV_VAR, download_outputs, run_script


class FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class FakeCommands:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def run(
        self,
        cmd,
        *,
        cwd=None,
        envs=None,
        timeout=None,
        background=None,
        on_stdout=None,
        on_stderr=None,
    ):
        self.calls.append({"cmd": cmd, "cwd": cwd, "envs": envs, "timeout": timeout})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeFiles:
    def __init__(self):
        self.written: dict[str, bytes] = {}
        self.to_read: dict[str, bytes] = {}

    def write(self, path, data):
        self.written[path] = data

    def read(self, path, format="text"):
        return self.to_read[path]


class FakeSandbox:
    def __init__(self, command_responses: list):
        self.commands = FakeCommands(command_responses)
        self.files = FakeFiles()


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "test_flow.py"
    path.write_text("print('hello')\n")
    return path


def test_run_script_uploads_and_runs(script: Path):
    sandbox = FakeSandbox([FakeResult(), FakeResult()])  # mkdir, then the script itself
    upload = UploadConfig(dest="/home/user/app")

    exit_code = run_script(sandbox, script, upload, {"BASE": "1"}, args=["--flag"])

    assert exit_code == 0
    remote_path = "/home/user/app/.e2b-webtest-scripts/test_flow.py"
    assert sandbox.files.written[remote_path] == script.read_bytes()

    run_call = sandbox.commands.calls[-1]
    assert remote_path in run_call["cmd"]
    assert "--flag" in run_call["cmd"]
    assert run_call["envs"][OUTPUT_DIR_ENV_VAR] == "/home/user/app/.e2b-webtest-out"
    assert run_call["envs"]["BASE"] == "1"


def test_run_script_returns_exit_code_on_failure(script: Path):
    failure = CommandExitException(stderr="boom", stdout="", exit_code=3, error=None)
    sandbox = FakeSandbox([FakeResult(), failure])
    upload = UploadConfig(dest="/home/user/app")

    exit_code = run_script(sandbox, script, upload, {}, args=[])

    assert exit_code == 3


def test_download_outputs_returns_empty_when_nothing_written(tmp_path: Path):
    sandbox = FakeSandbox([FakeResult(stdout="no\n")])
    upload = UploadConfig(dest="/home/user/app")

    names = download_outputs(sandbox, upload, tmp_path / "downloads")

    assert names == []


def test_download_outputs_extracts_files(tmp_path: Path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"fake png bytes"
        info = tarfile.TarInfo(name="screenshot.png")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive_bytes = buffer.getvalue()

    sandbox = FakeSandbox([FakeResult(stdout="yes\n"), FakeResult()])  # check, then the final rm
    upload = UploadConfig(dest="/home/user/app")
    sandbox.files.to_read["/home/user/app/.e2b-webtest-out.tar.gz"] = archive_bytes

    local_dir = tmp_path / "downloads"
    names = download_outputs(sandbox, upload, local_dir)

    assert names == ["screenshot.png"]
    assert (local_dir / "screenshot.png").read_bytes() == b"fake png bytes"
