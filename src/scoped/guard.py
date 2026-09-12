"""The PreToolUse hook -- the only real enforcement point.

`can_use_tool` cannot do this job. Per the SDK permission docs, a call approved
at an earlier step never reaches that callback, and a file read inside the
working directory is approved on its own with no rule needed -- so a scope check
there would be silently skipped for exactly the case that matters. Hooks run
before every other step and a hook deny holds even in bypassPermissions mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk.types import HookCallback

from .scope import Scope

# Tools that read or write a single file.
FILE_PATH_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

# Tools that search a directory.
SEARCH_PATH_TOOLS = {"Glob", "Grep"}

# Tools with no place in a scoped session. These are also in `disallowed_tools`,
# which removes them from the model's context entirely; the hook denies them too
# so that re-enabling one in a profile cannot quietly widen the scope.
ESCAPE_TOOLS = {"WebFetch", "WebSearch", "Agent", "Task"}

HookResult = dict[str, Any]


def _deny(event: str, reason: str) -> HookResult:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


ALLOW: HookResult = {}


def candidate_paths(tool: str, tool_input: dict[str, Any]) -> list[str]:
    """Every value in this call that names a file, for the secret check."""
    keys = ("file_path", "notebook_path", "path", "pattern")
    return [str(tool_input[k]) for k in keys if tool_input.get(k)]


def make_guard(scope: Scope, audit: list[str] | None = None) -> HookCallback:
    """Build the hook callback, closed over the mutable scope.

    The closure is why `/scope add` and `/unscope` work mid-session: the hook
    reads the same object the slash commands mutate, so a change lands on the
    very next tool call without rebuilding options or reconnecting.
    """

    async def guard(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        event = input_data.get("hook_event_name", "PreToolUse")
        tool = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}

        if audit is not None:
            audit.append(tool)

        # 1. Secrets, independent of mode. `/unscope` does not switch this off.
        for raw in candidate_paths(tool, tool_input):
            path = scope.resolve(raw)
            if scope.filters.is_secret(path):
                return _deny(
                    event,
                    f"{path.name} looks like a credentials file and is blocked in every mode. "
                    "Do not try to read it another way.",
                )

        # 2. Bash is off unless the profile enabled it, and then only in open mode.
        if tool == "Bash":
            if scope.mode == "open":
                return ALLOW
            return _deny(
                event,
                "Bash is not available in this session. Use the run_tests, run_lint, "
                "and run_typecheck tools instead.",
            )

        if tool in ESCAPE_TOOLS:
            return _deny(event, f"{tool} is not available in this session.")

        # 3. Open mode lifts the file scope. This line is `/unscope`.
        if scope.mode == "open":
            return ALLOW

        # 4. Single-file tools.
        if tool in FILE_PATH_KEYS:
            named = tool_input.get(FILE_PATH_KEYS[tool])
            if not named:
                return _deny(event, f"{tool} called without a file path.")
            path = scope.resolve(str(named))
            if scope.contains(path):
                return ALLOW
            if tool in {"Write", "NotebookEdit"} and scope.can_create(path):
                # Creating a new file in a scoped directory is allowed; fold it
                # into the manifest so it can be read back afterwards.
                scope.note_created(path)
                return ALLOW
            return _deny(event, _out_of_scope_message(scope, path))

        # 5. Directory searches.
        if tool in SEARCH_PATH_TOOLS:
            searched = tool_input.get("path") or str(scope.cwd)
            path = scope.resolve(str(searched))
            if scope.under_root(path):
                return ALLOW
            if not scope.roots:
                return _deny(
                    event,
                    "This session is scoped to individual files, not directories, so there "
                    "is nothing to search. The files in scope are already in context.",
                )
            return _deny(event, _out_of_scope_message(scope, path))

        # 6. Everything else (TodoWrite, Skill, the dev tools) passes; the dev
        #    tool handlers validate their own path arguments.
        return ALLOW

    return guard


def _out_of_scope_message(scope: Scope, path: Path) -> str:
    shown = scope.relative(path)
    return (
        f"{shown} is outside the session scope. Do not try another path or another tool "
        f"to reach it -- ask me to run `/scope add {shown}` if you need it."
    )


# -- PostToolUse: filtering search results ----------------------------------
#
# The PreToolUse hook cannot close this hole. A Grep whose search root is
# legitimately in scope is a legitimate call, but it walks every file under that
# root -- including ones the filters kept out of the manifest. Observed
# leaking a .env sitting inside a scoped directory. The leak is in the output,
# so it has to be repaired in the output.

FILTER_NOTE = "[some matches were outside the session scope and have been removed]"


def _line_path(line: str) -> str | None:
    """Pull the filename off a `path:line:text` (or `path-line-text`) result."""
    for separator in (":", "-"):
        head, found, _ = line.partition(separator)
        if found and head and not head.isdigit():
            return head
    return None


def filter_grep_content(scope: Scope, cwd: Path, content: str) -> tuple[str, int]:
    """Drop result lines that name a file outside the manifest."""
    kept: list[str] = []
    removed = 0
    last_kept = False
    for line in content.splitlines():
        name = _line_path(line)
        if name is None:
            # Context or separator lines belong to whichever file came before.
            if last_kept:
                kept.append(line)
            continue
        if scope.contains(cwd / name):
            kept.append(line)
            last_kept = True
        else:
            removed += 1
            last_kept = False
    return "\n".join(kept), removed


def make_post_guard(scope: Scope) -> HookCallback:
    """Strip out-of-scope files from Grep and Glob results before the model sees them."""

    async def post_guard(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        tool = input_data.get("tool_name", "")
        if tool not in SEARCH_PATH_TOOLS or scope.mode == "open":
            return ALLOW

        response = input_data.get("tool_response")
        if not isinstance(response, dict):
            return ALLOW

        cwd = Path(input_data.get("cwd") or scope.cwd)
        updated = dict(response)
        removed = 0

        names = response.get("filenames")
        if isinstance(names, list):
            kept = [n for n in names if scope.contains(cwd / str(n))]
            removed += len(names) - len(kept)
            updated["filenames"] = kept
            updated["numFiles"] = len(kept)
            if "totalMatches" in updated:
                updated["totalMatches"] = len(kept)

        content = response.get("content")
        if isinstance(content, str) and content:
            filtered, dropped = filter_grep_content(scope, cwd, content)
            removed += dropped
            if dropped:
                filtered = f"{filtered}\n{FILTER_NOTE}".strip()
            updated["content"] = filtered
            updated["numLines"] = len(filtered.splitlines())

        if not removed:
            return ALLOW

        return {
            "hookSpecificOutput": {
                "hookEventName": input_data.get("hook_event_name", "PostToolUse"),
                "updatedToolOutput": updated,
            }
        }

    return post_guard
