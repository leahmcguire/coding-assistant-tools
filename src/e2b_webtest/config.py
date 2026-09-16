"""Per-project settings from `.e2b-webtest.toml`.

Presence of the file is a project's declaration that it is an e2b-webtest
project. There is no default config: every project names its own sandbox
template, setup steps, and services explicitly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = ".e2b-webtest.toml"


class ConfigError(ValueError):
    """A `.e2b-webtest.toml` that cannot be used. The message names the file and the key."""


def env_like(path: str) -> bool:
    """True for a path whose filename looks like an env file (`.env`, `.env.local`, ...).

    Used to refuse config that would upload secrets by name, mirroring the
    always-on exclusion in `upload.py` for git-tracked files.
    """
    return Path(path).name.startswith(".env")


@dataclass
class SandboxConfig:
    template: str
    vcpu: int = 2
    memory_mb: int = 2048
    # Seconds. E2B Hobby plan caps a single sandbox session at 3600; `extend`
    # raises it later without needing this to be right up front.
    timeout: int = 3600


@dataclass
class TemplateConfig:
    """Extras layered onto the tool's baseline template image."""

    apt: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)


@dataclass
class UploadConfig:
    exclude: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    dest: str = "/home/user/app"


@dataclass
class EnvConfig:
    forward: list[str] = field(default_factory=list)
    set: dict[str, str] = field(default_factory=dict)


@dataclass
class SetupStep:
    cmd: list[str]
    cwd: str | None = None
    timeout: int = 300
    # If true, stdout is parsed as `KEY=VALUE` lines and merged into the env
    # used by every later setup step and service (e.g. `supabase status -o env`).
    capture_env: bool = False


@dataclass
class ReadyProbe:
    http: str | None = None
    cmd: list[str] | None = None
    timeout: int = 60
    interval: int = 2


@dataclass
class ServiceConfig:
    name: str
    cmd: list[str]
    cwd: str | None = None
    ready: ReadyProbe = field(default_factory=ReadyProbe)


@dataclass
class Config:
    sandbox: SandboxConfig
    template: TemplateConfig = field(default_factory=TemplateConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    setup: list[SetupStep] = field(default_factory=list)
    services: list[ServiceConfig] = field(default_factory=list)
    source: Path | None = None


def find(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


# Each reader checks the type up front, the same way `scoped/config.py` does:
# a TOML value that parses fine but has the wrong shape should fail loudly at
# load time, not three steps into `up`.


def _argv(table: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    raw = table.get(key)
    if raw is None:
        if required:
            raise ValueError(f'{key} is required and must be a list of strings like ["cmd", "arg"]')
        return []
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise ValueError(f'{key} must be a list of strings like ["cmd", "arg"], got {raw!r}')
    return raw


def _strings(table: dict[str, Any], key: str, example: str) -> list[str]:
    raw = table.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{key} must be a list of strings like {example}, got {raw!r}")
    return raw


def _int(table: dict[str, Any], key: str, default: int) -> int:
    raw = table.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{key} must be a whole number, got {raw!r}")
    return raw


def _str(table: dict[str, Any], key: str, default: str) -> str:
    raw = table.get(key, default)
    if not isinstance(raw, str):
        raise TypeError(f"{key} must be a string, got {raw!r}")
    return raw


def _optional_str(table: dict[str, Any], key: str, label: str) -> str | None:
    raw = table.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"{label} must be a string, got {raw!r}")
    return raw


def _bool(table: dict[str, Any], key: str, default: bool) -> bool:
    raw = table.get(key, default)
    if not isinstance(raw, bool):
        raise TypeError(f"{key} must be true or false (no quotes), got {raw!r}")
    return raw


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    raw = data.get(key, {})
    if not isinstance(raw, dict):
        raise TypeError(f"{key} must be a table: a [{key}] section with keys under it")
    return raw


def load(path: Path) -> Config:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc.strerror or exc}") from exc

    try:
        return _parse(data, path)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _parse(data: dict[str, Any], path: Path) -> Config:
    sandbox_table = _table(data, "sandbox")
    sandbox = SandboxConfig(
        template=_str(sandbox_table, "template", ""),
        vcpu=_int(sandbox_table, "vcpu", SandboxConfig.vcpu),
        memory_mb=_int(sandbox_table, "memory_mb", SandboxConfig.memory_mb),
        timeout=_int(sandbox_table, "timeout", SandboxConfig.timeout),
    )
    if not sandbox.template:
        raise ValueError("sandbox.template is required (the built template's name)")

    template_table = _table(data, "template")
    template = TemplateConfig(
        apt=_strings(template_table, "apt", '["ripgrep"]'),
        run=_strings(template_table, "run", '["echo hi"]'),
    )

    upload_table = _table(data, "upload")
    extra = _strings(upload_table, "extra", '[".e2b-webtest/seed.sql"]')
    for item in extra:
        if env_like(item):
            raise ValueError(f"upload.extra may not name an env file: {item!r}")
    upload = UploadConfig(
        exclude=_strings(upload_table, "exclude", '["**/node_modules"]'),
        extra=extra,
        dest=_str(upload_table, "dest", UploadConfig.dest),
    )

    env_table = _table(data, "env")
    env_set = env_table.get("set", {})
    if not isinstance(env_set, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_set.items()
    ):
        raise ValueError(f"env.set must be a table of string to string, got {env_set!r}")
    env = EnvConfig(
        forward=_strings(env_table, "forward", '["KROGER_CLIENT_ID"]'), set=dict(env_set)
    )

    setup = [_parse_setup_step(raw, i) for i, raw in enumerate(data.get("setup", []))]
    services = [_parse_service(raw, i) for i, raw in enumerate(data.get("services", []))]
    names = [s.name for s in services]
    if len(names) != len(set(names)):
        raise ValueError(f"services[].name must be unique, got {names!r}")

    return Config(
        sandbox=sandbox,
        template=template,
        upload=upload,
        env=env,
        setup=setup,
        services=services,
        source=path,
    )


def _parse_setup_step(raw: Any, index: int) -> SetupStep:
    if not isinstance(raw, dict):
        raise TypeError(f"setup[{index}] must be a table, got {raw!r}")
    return SetupStep(
        cmd=_argv(raw, "cmd", required=True),
        cwd=_optional_str(raw, "cwd", f"setup[{index}].cwd"),
        timeout=_int(raw, "timeout", SetupStep.timeout),
        capture_env=_bool(raw, "capture_env", False),
    )


def _parse_service(raw: Any, index: int) -> ServiceConfig:
    if not isinstance(raw, dict):
        raise TypeError(f"services[{index}] must be a table, got {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"services[{index}].name is required and must be a non-empty string")
    ready_table = raw.get("ready", {})
    if not isinstance(ready_table, dict):
        raise TypeError(f"services[{index}].ready must be a table")
    http = ready_table.get("http")
    cmd = ready_table.get("cmd")
    if http is not None and not isinstance(http, str):
        raise ValueError(f"services[{index}].ready.http must be a string URL")
    if cmd is not None and (not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd)):
        raise ValueError(f"services[{index}].ready.cmd must be a list of strings")
    if http and cmd:
        raise ValueError(f"services[{index}].ready: set http or cmd, not both")
    ready = ReadyProbe(
        http=http,
        cmd=cmd,
        timeout=_int(ready_table, "timeout", ReadyProbe.timeout),
        interval=_int(ready_table, "interval", ReadyProbe.interval),
    )
    return ServiceConfig(
        name=name,
        cmd=_argv(raw, "cmd", required=True),
        cwd=_optional_str(raw, "cwd", f"services[{index}].cwd"),
        ready=ready,
    )
