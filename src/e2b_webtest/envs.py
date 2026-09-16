"""Environment variables that reach the sandbox.

`env.forward` names are read from this process's own `os.environ` -- never
from a file, so `.env*` never enters the picture. But `os.environ` is *your*
real shell environment: if you use direnv (or just export things in
`~/.zshrc`), names like `KROGER_CLIENT_ID` or `RESEND_API_KEY` may already
hold real, live credentials there, not sandbox-safe test values. `forward` is
an allowlist by design -- a name only crosses into the sandbox if a config
explicitly lists it -- so the safety property depends entirely on projects
not listing live-service credentials there. The Dinner Circle config keeps
`env.forward` empty for exactly this reason: nothing about the Kroger or
Resend integrations is needed to exercise the anon store-selection and
sign-in flows, so those names are simply never on the list.

`env.set` supplies literal values from the config. `capture_env` setup steps
(see `runner.py`) add to this at runtime by parsing output produced *inside*
the sandbox, e.g. `supabase status -o env` -- those are the sandbox's own
freshly generated local credentials, not anything from your machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import EnvConfig


def base_env(cfg: EnvConfig) -> dict[str, str]:
    """Forwarded names read from the local process env, plus literal `set` values.

    A forwarded name that isn't set locally is silently skipped -- the sandbox
    simply won't have it, same as leaving a shell variable unset.
    """
    forwarded = {name: os.environ[name] for name in cfg.forward if name in os.environ}
    return {**forwarded, **cfg.set}


@dataclass(frozen=True)
class EnvPlan:
    """What would cross into the sandbox, as names only.

    This deliberately carries no values. It exists to be shown to you before
    anything is sent, so a plan object that could hold a secret would defeat
    its own purpose.
    """

    forwarded: list[str]
    """Listed in `env.forward` and set locally: real values from your shell."""
    missing: list[str]
    """Listed in `env.forward` but not set locally, so nothing is sent."""
    literal: list[str]
    """Names from `env.set`, whose values are written in the config file."""

    @property
    def sent(self) -> list[str]:
        return sorted({*self.forwarded, *self.literal})


def plan(cfg: EnvConfig) -> EnvPlan:
    return EnvPlan(
        forwarded=[name for name in cfg.forward if name in os.environ],
        missing=[name for name in cfg.forward if name not in os.environ],
        literal=sorted(cfg.set),
    )


def describe(env_plan: EnvPlan) -> str:
    """Render a plan for display. Values must never appear in this output.

    Variables captured inside the sandbox (`capture_env` steps) aren't listed:
    they're generated there and never come from your machine.
    """
    if not env_plan.sent:
        return "environment variables to be sent into the sandbox: none"
    lines = ["environment variables to be sent into the sandbox (names only, no values):"]
    if env_plan.forwarded:
        lines.append("  from your shell (env.forward) -- real values from your environment:")
        lines += [f"    {name}" for name in env_plan.forwarded]
    if env_plan.literal:
        lines.append("  literal values from the config file (env.set):")
        lines += [f"    {name}" for name in env_plan.literal]
    if env_plan.missing:
        lines.append("  listed in env.forward but not set locally, so not sent:")
        lines += [f"    {name}" for name in env_plan.missing]
    return "\n".join(lines)


def parse_env_output(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines, optionally `export KEY=VALUE`, quoted or not.

    Matches what `supabase status -o env` and similar tools print. Blank
    lines, comments, and lines without `=` are skipped rather than raising --
    this reads a command's own stdout, so it should degrade gracefully.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result
