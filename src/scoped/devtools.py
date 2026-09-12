"""Testing and linting without a shell.

An allowlist over Bash means parsing a shell, which is a losing game --
`/bin/cat`, `$(...)`, pipes, expansion. But testing and linting are not a shell,
they are three functions. Each handler builds a fixed argv and runs it with
`shell=False`, so `;` and `|` and backticks in an argument are inert data rather
than syntax. There is no parser to defeat because nothing parses them.

The model supplies `paths` and `expression`. It cannot influence argv[0], which
comes from configuration.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from .scope import Scope

MAX_OUTPUT_LINES = 200
DEFAULT_TIMEOUT = 300


@dataclass
class DevConfig:
    test_command: list[str] = field(default_factory=lambda: ["pytest", "-q"])
    lint_command: list[str] = field(default_factory=lambda: ["ruff", "check"])
    typecheck_command: list[str] = field(default_factory=lambda: ["mypy"])
    timeout: int = DEFAULT_TIMEOUT


def _text(body: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        result["is_error"] = True
    return result


def _tail(output: str) -> str:
    """Keep the end of the output; failures and summaries live there."""
    lines = output.splitlines()
    if len(lines) <= MAX_OUTPUT_LINES:
        return output
    dropped = len(lines) - MAX_OUTPUT_LINES
    return "\n".join([f"[{dropped} earlier lines omitted]", *lines[-MAX_OUTPUT_LINES:]])


async def run_argv(argv: list[str], cwd: str, timeout: int) -> dict[str, Any]:
    """Run a fixed argv with no shell.

    `is_error` is reserved for *could not run it*. A failing test is a
    successful tool call reporting a failure; conflating the two makes the model
    retry something that is working correctly.
    """
    if not shutil.which(argv[0]):
        return _text(f"`{argv[0]}` is not installed or not on PATH.", is_error=True)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return _text(f"Could not start `{argv[0]}`: {exc}", is_error=True)

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _text(f"`{argv[0]}` timed out after {timeout}s.", is_error=True)

    body = _tail(stdout.decode("utf-8", errors="replace").strip())
    header = f"$ {' '.join(argv)}\nexit code {process.returncode}"
    return _text(f"{header}\n\n{body}" if body else header)


class PathRejected(Exception):
    pass


def make_dev_tools(scope: Scope, config: DevConfig) -> list[Any]:
    """Build the tools, closed over the live scope.

    Defined inside a factory so each session's handlers validate against its own
    scope and commands. Returned as a list (rather than only wrapped in a
    server) so the handlers can be exercised directly in tests.
    """

    def validated(raw_paths: Any) -> list[str]:
        """Path arguments must be in scope, exactly as for Read."""
        if not raw_paths:
            return []
        if not isinstance(raw_paths, list):
            raise PathRejected("`paths` must be a list of strings.")
        out = []
        for raw in raw_paths:
            path = scope.resolve(str(raw))
            if scope.mode == "strict" and not (scope.contains(path) or scope.under_root(path)):
                raise PathRejected(
                    f"{scope.relative(path)} is outside the session scope. "
                    f"Ask me to run `/scope add {scope.relative(path)}` if you need it."
                )
            out.append(os.path.relpath(path, scope.cwd))
        return out

    read_only = ToolAnnotations(readOnlyHint=True)

    @tool(
        "run_tests",
        "Run the project's test suite. Optionally restrict to specific paths, or to "
        "tests matching a keyword expression.",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test files or directories to run. Omit to run everything.",
                },
                "expression": {
                    "type": "string",
                    "description": "Keyword expression, passed to the runner as -k.",
                },
            },
            "required": [],
        },
    )
    async def run_tests(args: dict[str, Any]) -> dict[str, Any]:
        try:
            paths = validated(args.get("paths"))
        except PathRejected as exc:
            return _text(str(exc), is_error=True)
        argv = [*config.test_command, *paths]
        if expression := args.get("expression"):
            argv += ["-k", str(expression)]
        return await run_argv(argv, str(scope.cwd), config.timeout)

    @tool(
        "run_lint",
        "Run the project's linter over the given paths, or the whole project.",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "fix": {"type": "boolean", "description": "Apply safe automatic fixes."},
            },
            "required": [],
        },
    )
    async def run_lint(args: dict[str, Any]) -> dict[str, Any]:
        try:
            paths = validated(args.get("paths"))
        except PathRejected as exc:
            return _text(str(exc), is_error=True)
        argv = [*config.lint_command, *paths]
        if args.get("fix"):
            argv.append("--fix")
        return await run_argv(argv, str(scope.cwd), config.timeout)

    @tool(
        "run_typecheck",
        "Run the project's type checker over the given paths, or the whole project.",
        {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": [],
        },
        annotations=read_only,
    )
    async def run_typecheck(args: dict[str, Any]) -> dict[str, Any]:
        try:
            paths = validated(args.get("paths"))
        except PathRejected as exc:
            return _text(str(exc), is_error=True)
        return await run_argv([*config.typecheck_command, *paths], str(scope.cwd), config.timeout)

    return [run_tests, run_lint, run_typecheck]


def make_dev_server(scope: Scope, config: DevConfig):
    """Wrap the dev tools in an in-process MCP server."""
    return create_sdk_mcp_server(
        name="dev",
        version="0.1.0",
        tools=make_dev_tools(scope, config),
    )


DEV_TOOL_NAMES = [
    "mcp__dev__run_tests",
    "mcp__dev__run_lint",
    "mcp__dev__run_typecheck",
]
