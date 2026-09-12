"""Building ClaudeAgentOptions, and reconnecting when the profile changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from .devtools import DEV_TOOL_NAMES, DevConfig, make_dev_server
from .guard import make_guard, make_post_guard
from .profile import SCOPE_CONTRACT, SCOPED_PROMPT, ContextProfile
from .scope import Scope

BASE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "TodoWrite"]


def build_options(
    scope: Scope,
    profile: ContextProfile,
    dev_config: DevConfig,
    resume: str | None = None,
    audit: list[str] | None = None,
) -> ClaudeAgentOptions:
    tools = list(BASE_TOOLS)
    if profile.bash:
        tools.append("Bash")
    if profile.skills:
        # The SDK adds the Skill tool to allowed_tools automatically when
        # `skills` is set -- but not when `tools` is also passed, in which case
        # omitting it here silently disables skills.
        tools.append("Skill")

    disallowed = ["WebFetch", "WebSearch", "Agent"]
    if not profile.bash:
        disallowed.append("Bash")

    if profile.prompt == "lean":
        system_prompt: Any = SCOPED_PROMPT
    else:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": SCOPE_CONTRACT,
        }

    extra_roots: list[str | Path] = [
        str(r) for r in scope.roots if not str(r).startswith(str(scope.cwd))
    ]

    return ClaudeAgentOptions(
        cwd=str(scope.cwd),
        add_dirs=extra_roots,
        # The single switch for CLAUDE.md, settings, and project skills.
        setting_sources=["project"] if profile.claude_md else [],
        system_prompt=system_prompt,
        skills=profile.skills,
        tools=tools,
        disallowed_tools=disallowed,
        mcp_servers={"dev": make_dev_server(scope, dev_config)},
        allowed_tools=[*DEV_TOOL_NAMES, *BASE_TOOLS],
        # Safe despite being permissive: hooks run before the permission-mode
        # step, so a scope denial still wins. bypassPermissions would be
        # needlessly broader for no gain.
        permission_mode="acceptEdits",
        hooks={
            "PreToolUse": [HookMatcher(hooks=[make_guard(scope, audit)])],
            # Repairs the one hole PreToolUse cannot: a legitimate search whose
            # results include files the filters excluded.
            "PostToolUse": [HookMatcher(hooks=[make_post_guard(scope)])],
        },
        resume=resume,
    )
