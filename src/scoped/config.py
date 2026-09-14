"""Per-project defaults from `.scoped.toml`.

Presence of the file is a project's declaration that it is a scoped project.
Absence means you use stock `claude` there and never think about this tool.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .devtools import DevConfig
from .filters import DEFAULT_EXCLUDES, Filters
from .preview import PreviewConfig
from .profile import ContextProfile

CONFIG_NAME = ".scoped.toml"


class ConfigError(ValueError):
    """A `.scoped.toml` that cannot be used. The message names the file and the key."""


@dataclass
class Config:
    default_scope: list[str] = field(default_factory=list)
    filters: Filters = field(default_factory=Filters)
    dev: DevConfig = field(default_factory=DevConfig)
    profile: ContextProfile = field(default_factory=ContextProfile)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    source: Path | None = None


def find(start: Path) -> Path | None:
    """Look for the config in this directory or any ancestor."""
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


# Each reader checks the type up front. Without that, TOML values that parse fine
# but have the wrong shape fail quietly -- `default_scope = "src"` becomes the
# three paths s, r, c, and `secret_guard = "false"` is truthy.


def _argv(table: dict[str, Any], key: str, fallback: list[str]) -> list[str]:
    """Commands must be argv lists, never strings.

    A string would need splitting, and splitting is the first step back towards
    parsing a shell. Rejecting it here keeps that door shut.
    """
    raw = table.get(key)
    if raw is None:
        return fallback
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise ValueError(f'{key} must be a list of strings like ["pytest", "-q"], got {raw!r}')
    return raw


def _strings(table: dict[str, Any], key: str, example: str) -> list[str] | None:
    raw = table.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{key} must be a list of strings like {example}, got {raw!r}")
    return raw


def _int(table: dict[str, Any], key: str, default: int) -> int:
    raw = table.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{key} must be a whole number, got {raw!r}")
    return raw


def _bool(table: dict[str, Any], key: str, default: bool, label: str | None = None) -> bool:
    raw = table.get(key, default)
    if not isinstance(raw, bool):
        raise TypeError(f"{label or key} must be true or false (no quotes), got {raw!r}")
    return raw


def load(cwd: Path) -> Config:
    path = find(cwd)
    if path is None:
        return Config()

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
    extensions = _strings(data, "extensions", '[".py", ".md"]')
    filters = Filters(
        extensions={e.lower() for e in extensions} if extensions else None,
        exclude=[*DEFAULT_EXCLUDES, *(_strings(data, "exclude", '["**/fixtures/**"]') or [])],
        max_bytes=_int(data, "max_bytes", Filters.max_bytes),
        secret_guard=_bool(data, "secret_guard", True),
    )

    defaults = DevConfig()
    dev = DevConfig(
        test_command=_argv(data, "test_command", defaults.test_command),
        lint_command=_argv(data, "lint_command", defaults.lint_command),
        typecheck_command=_argv(data, "typecheck_command", defaults.typecheck_command),
        timeout=_int(data, "timeout", defaults.timeout),
    )

    context = data.get("context", {})
    if not isinstance(context, dict):
        raise TypeError("context must be a table: a [context] section with keys under it")
    skills = context.get("skills")
    if not (
        skills in (None, "all")
        or (isinstance(skills, list) and all(isinstance(s, str) for s in skills))
    ):
        raise ValueError(f'context.skills must be "all" or a list of skill names, got {skills!r}')
    prompt = context.get("prompt", "lean")
    if prompt not in ("lean", "preset"):
        raise ValueError(f'context.prompt must be "lean" or "preset", got {prompt!r}')
    profile = ContextProfile(
        claude_md=_bool(context, "claude_md", False, "context.claude_md"),
        skills=skills if skills in (None, "all") else list(skills),
        prompt=prompt,
        bash=_bool(context, "bash", False, "context.bash"),
    )

    preview = PreviewConfig(
        max_preview_files=_int(data, "max_preview_files", PreviewConfig().max_preview_files),
    )

    return Config(
        default_scope=_strings(data, "default_scope", '["src", "tests"]') or [],
        filters=filters,
        dev=dev,
        profile=profile,
        preview=preview,
        source=path,
    )
