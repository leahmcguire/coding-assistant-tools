"""Running setup steps and waiting for services, against a fake sandbox.

The central claims: a failing setup step raises with the command and a
stderr tail (so `cli.py` can print it and kill the sandbox), and readiness
polling stops and raises once its timeout elapses -- checked with a fake
clock so the test doesn't actually wait.
"""

from __future__ import annotations

import pytest
from e2b import CommandExitException, TimeoutException

from e2b_webtest.config import ReadyProbe, ServiceConfig, SetupStep
from e2b_webtest.runner import ReadinessTimeout, SetupFailed, run_setup, wait_ready


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
        self.calls.append(
            {"cmd": cmd, "cwd": cwd, "envs": envs, "timeout": timeout, "background": background}
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeSandbox:
    def __init__(self, responses: list):
        self.commands = FakeCommands(responses)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


# -- run_setup ----------------------------------------------------------------


def test_run_setup_runs_steps_in_order_with_env():
    sandbox = FakeSandbox([FakeResult(), FakeResult()])
    steps = [
        SetupStep(cmd=["supabase", "start"], cwd="supabase"),
        SetupStep(cmd=["echo", "hi"]),
    ]
    run_setup(sandbox, steps, {"FOO": "bar"})
    assert sandbox.commands.calls[0]["cmd"] == "supabase start"
    assert sandbox.commands.calls[0]["cwd"] == "supabase"
    assert sandbox.commands.calls[0]["envs"] == {"FOO": "bar"}


def test_run_setup_captures_env_from_marked_steps():
    sandbox = FakeSandbox([FakeResult(stdout="SUPABASE_URL=http://127.0.0.1:54321\n")])
    steps = [SetupStep(cmd=["supabase", "status", "-o", "env"], capture_env=True)]
    result_env = run_setup(sandbox, steps, {"BASE": "1"})
    assert result_env == {"BASE": "1", "SUPABASE_URL": "http://127.0.0.1:54321"}


def test_run_setup_does_not_capture_env_unless_marked():
    sandbox = FakeSandbox([FakeResult(stdout="SHOULD_NOT=appear\n")])
    steps = [SetupStep(cmd=["echo", "hi"])]
    result_env = run_setup(sandbox, steps, {"BASE": "1"})
    assert result_env == {"BASE": "1"}


def test_run_setup_failure_raises_with_stderr_tail():
    exc = CommandExitException(stderr="line1\nline2\nboom", stdout="", exit_code=17, error=None)
    sandbox = FakeSandbox([exc])
    steps = [SetupStep(cmd=["docker", "build", "."])]
    with pytest.raises(SetupFailed) as excinfo:
        run_setup(sandbox, steps, {})
    message = str(excinfo.value)
    assert "docker build ." in message
    assert "17" in message
    assert "boom" in message
    assert excinfo.value.exit_code == 17


def test_run_setup_stops_at_first_failure():
    exc = CommandExitException(stderr="boom", stdout="", exit_code=1, error=None)
    sandbox = FakeSandbox([exc, FakeResult()])  # second response should never be consumed
    steps = [SetupStep(cmd=["false"]), SetupStep(cmd=["true"])]
    with pytest.raises(SetupFailed):
        run_setup(sandbox, steps, {})
    assert len(sandbox.commands.calls) == 1


# -- wait_ready -----------------------------------------------------------------


def test_wait_ready_returns_once_probe_succeeds():
    sandbox = FakeSandbox([FakeResult()])
    service = ServiceConfig(
        name="backend", cmd=["true"], ready=ReadyProbe(cmd=["curl"], timeout=10, interval=2)
    )
    clock = FakeClock()
    wait_ready(sandbox, service, now=clock.now, sleep=clock.sleep)
    assert clock.sleeps == []


def test_wait_ready_times_out_with_fake_clock():
    failures = [
        CommandExitException(stderr="", stdout="", exit_code=1, error=None) for _ in range(20)
    ]
    sandbox = FakeSandbox(failures)
    service = ServiceConfig(
        name="backend", cmd=["true"], ready=ReadyProbe(cmd=["curl"], timeout=5, interval=2)
    )
    clock = FakeClock()
    with pytest.raises(ReadinessTimeout, match="backend"):
        wait_ready(sandbox, service, now=clock.now, sleep=clock.sleep)
    # Never really slept: the fake clock advanced deterministically.
    assert clock.sleeps == [2, 2, 2]


def test_wait_ready_uses_http_probe_when_configured():
    sandbox = FakeSandbox([FakeResult()])
    service = ServiceConfig(
        name="web",
        cmd=["true"],
        ready=ReadyProbe(http="http://127.0.0.1:8081", timeout=10, interval=2),
    )
    clock = FakeClock()
    wait_ready(sandbox, service, now=clock.now, sleep=clock.sleep)
    assert "curl" in sandbox.commands.calls[0]["cmd"]
    assert "http://127.0.0.1:8081" in sandbox.commands.calls[0]["cmd"]
    assert "--max-time" in sandbox.commands.calls[0]["cmd"]


def test_wait_ready_with_no_probe_does_not_wait():
    """A worker with no port to poll is a real case, not a config mistake."""
    sandbox = FakeSandbox([])
    service = ServiceConfig(name="worker", cmd=["true"], ready=ReadyProbe())
    clock = FakeClock()
    wait_ready(sandbox, service, now=clock.now, sleep=clock.sleep)
    assert sandbox.commands.calls == []


def test_wait_ready_retries_a_probe_that_times_out():
    """A hung probe is a failed attempt, not a reason to abandon the wait."""
    sandbox = FakeSandbox([TimeoutException("probe hung"), FakeResult()])
    service = ServiceConfig(
        name="backend",
        cmd=["true"],
        ready=ReadyProbe(http="http://127.0.0.1:8000", timeout=30, interval=2),
    )
    clock = FakeClock()
    wait_ready(sandbox, service, now=clock.now, sleep=clock.sleep)
    assert len(sandbox.commands.calls) == 2
