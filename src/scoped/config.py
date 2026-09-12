"""Per-project defaults from `.scoped.toml`.

Presence of the file is a project's declaration that it is a scoped project.
Absence means you use stock `claude` there and never think about this tool.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .devtools import DevConfig
from .filters import DEFAULT_EXCLUDES, Filters
from .preview import PreviewConfig
from .profile import ContextProfile

CONFIG_NAME = ".scoped.toml"


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


def _argv(raw: object, fallback: list[str]) -> list[str]:
    """Commands must be argv lists, never strings.

    A string would need splitting, and splitting is the first step back towards
    parsing a shell. Rejecting it here keeps that door shut.
    """
    if raw is None:
        return fallback
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f'expected a list of strings like ["pytest", "-q"], got {raw!r}')
    return raw


def load(cwd: Path) -> Config:
    path = find(cwd)
    if path is None:
        return Config()

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    extensions = data.get("extensions")
    filters = Filters(
        extensions={e.lower() for e in extensions} if extensions else None,
        exclude=[*DEFAULT_EXCLUDES, *data.get("exclude", [])],
        max_bytes=int(data.get("max_bytes", Filters.max_bytes)),
        secret_guard=bool(data.get("secret_guard", True)),
    )

    dev = DevConfig(
        test_command=_argv(data.get("test_command"), DevConfig().test_command),
        lint_command=_argv(data.get("lint_command"), DevConfig().lint_command),
        typecheck_command=_argv(data.get("typecheck_command"), DevConfig().typecheck_command),
        timeout=int(data.get("timeout", DevConfig().timeout)),
    )

    context = data.get("context", {})
    skills = context.get("skills")
    profile = ContextProfile(
        claude_md=bool(context.get("claude_md", False)),
        skills=skills if skills in (None, "all") else list(skills),
        prompt=context.get("prompt", "lean"),
        bash=bool(context.get("bash", False)),
    )

    preview = PreviewConfig(
        max_preview_files=int(data.get("max_preview_files", PreviewConfig().max_preview_files)),
    )

    return Config(
        default_scope=list(data.get("default_scope", [])),
        filters=filters,
        dev=dev,
        profile=profile,
        preview=preview,
        source=path,
    )
