"""Run a project's setup steps and start its services inside the sandbox.

Every probe and every command here executes *inside* the sandbox, over the
E2B control-plane API (`sandbox.commands.run`) -- never as a local HTTP
request or a locally-launched process. The control-plane call itself is just
how this tool tells the sandbox what to do, the same way `git` calls in
`upload.py` are local but touch no web content; the app's own traffic never
reaches your machine.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable

from e2b import CommandExitException, TimeoutException

from .config import ReadyProbe, ServiceConfig, SetupStep
from .envs import parse_env_output

LOG_DIR = "/home/user/.e2b-webtest-logs"


class SetupFailed(RuntimeError):
    def __init__(self, step: SetupStep, exc: CommandExitException):
        cmd = " ".join(step.cmd)
        tail = "\n".join(exc.stderr.strip().splitlines()[-20:])
        message = f"setup step failed (exit {exc.exit_code}): {cmd}"
        if tail:
            message += f"\n{tail}"
        super().__init__(message)
        self.step = step
        self.exit_code = exc.exit_code


class ReadinessTimeout(RuntimeError):
    def __init__(self, service: ServiceConfig, timeout: int):
        super().__init__(f"{service.name!r} did not become ready within {timeout}s")
        self.service = service


def run_setup(sandbox, steps: list[SetupStep], env: dict[str, str]) -> dict[str, str]:
    """Run each step in order, foreground, failing fast.

    Returns the env, grown by any step with `capture_env = True` -- so a
    later step (or a service) can see values a setup step produced, like the
    local Supabase URL and keys from `supabase status -o env`.
    """
    for step in steps:
        cmd = " ".join(shlex.quote(part) for part in step.cmd)
        try:
            result = sandbox.commands.run(cmd, cwd=step.cwd, envs=env, timeout=step.timeout)
        except CommandExitException as exc:
            raise SetupFailed(step, exc) from exc
        if step.capture_env:
            env = {**env, **parse_env_output(result.stdout)}
    return env


def _log_path(service: ServiceConfig) -> str:
    return f"{LOG_DIR}/{service.name}.log"


def start_service(sandbox, service: ServiceConfig, env: dict[str, str]):
    """Start one service in the background; its output is redirected to a log
    file in the sandbox rather than streamed, since it outlives this call."""
    cmd = " ".join(shlex.quote(part) for part in service.cmd)
    log_path = _log_path(service)
    wrapped = f"mkdir -p {LOG_DIR} && ({cmd}) > {shlex.quote(log_path)} 2>&1"
    return sandbox.commands.run(wrapped, background=True, cwd=service.cwd, envs=env)


def read_log(sandbox, service: ServiceConfig, lines: int = 200) -> str:
    result = sandbox.commands.run(f"tail -n {int(lines)} {shlex.quote(_log_path(service))}")
    return result.stdout


def _probe_command(probe: ReadyProbe) -> str | None:
    """None means the service declared no readiness check, so nothing is waited on.

    That is a real case, not a config mistake: a worker with no port to poll
    (Dinner Circle's `ROLE=worker` container, say) has nothing to probe.
    """
    if probe.http:
        # --max-time bounds a probe that connects but never answers, so a
        # stalled service fails this attempt rather than the command's own
        # timeout firing and raising out of the retry loop.
        return f"curl -fsS --max-time {probe.interval} -o /dev/null {shlex.quote(probe.http)}"
    if probe.cmd:
        return " ".join(shlex.quote(part) for part in probe.cmd)
    return None


def wait_ready(
    sandbox,
    service: ServiceConfig,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll `service.ready` inside the sandbox until it succeeds or times out.

    `now`/`sleep` are injectable so tests can drive this with a fake clock
    instead of a real one.
    """
    probe = service.ready
    command = _probe_command(probe)
    if command is None:
        return
    deadline = now() + probe.timeout
    while True:
        try:
            sandbox.commands.run(command, timeout=min(probe.interval, 10) or 1)
            return
        except (CommandExitException, TimeoutException):
            # A nonzero exit and a probe that outran its own timeout mean the
            # same thing here -- this attempt failed -- and the deadline below
            # decides whether to retry. Letting TimeoutException escape would
            # abandon the wait on the first slow probe.
            pass
        if now() >= deadline:
            raise ReadinessTimeout(service, probe.timeout)
        sleep(probe.interval)


def start_services(
    sandbox,
    services: list[ServiceConfig],
    env: dict[str, str],
    *,
    wait: Callable[..., None] = wait_ready,
) -> dict[str, object]:
    handles = {}
    for service in services:
        handles[service.name] = start_service(sandbox, service, env)
        wait(sandbox, service)
    return handles
