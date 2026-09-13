"""The `scoped` command: a REPL over a bounded working set."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from . import config as config_module
from .commands import AppState, handle
from .filters import parse_extensions
from .preview import estimate_tokens, render
from .scope import Scope
from .session import build_options

DIM = "\033[2m"
BOLD = "\033[1m"
WARN = "\033[33m"
RESET = "\033[0m"


def note(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


class ScopedSession:
    """Owns the client, and can rebuild it without losing the conversation."""

    def __init__(self, state: AppState, dev_config, model: str | None):
        self.state = state
        self.dev_config = dev_config
        self.model = model
        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None

    async def connect(self, resume: str | None = None) -> None:
        options = build_options(
            self.state.scope, self.state.profile, self.dev_config, resume=resume
        )
        if self.model:
            options.model = self.model
        # Only keep the client once it is connected, so `close` after a failed
        # connect has nothing half-started to tear down.
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.client = client

    async def reconnect(self, fresh: bool) -> None:
        """Apply a changed profile.

        Context sources are ClaudeAgentOptions fields, fixed at connect time, so
        the only way to change one is to rebuild. Resuming by session id carries
        the conversation across; `fresh` deliberately does not, which is the
        only way to drop context that is already in the transcript.
        """
        resume = None if fresh else self.session_id
        await self.close()
        await self.connect(resume=resume)
        if fresh:
            self.session_id = None
            note("started a fresh session -- previous conversation dropped")
            await self.send(initial_message(self.state), echo=False)
        else:
            note("reconnected, conversation preserved")

    async def send(self, text: str, echo: bool = True) -> None:
        assert self.client is not None
        await self.client.query(text)
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        note(f"  [{block.name}] {_summarize(block.input)}")
                    elif isinstance(block, TextBlock) and echo:
                        print(block.text)
            elif isinstance(message, ResultMessage):
                self.session_id = message.session_id
                if message.subtype != "success":
                    detail = "; ".join(message.errors or [])
                    note(f"  [{message.subtype}] {detail}".rstrip())

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


def _summarize(tool_input: dict) -> str:
    for key in ("file_path", "notebook_path", "path", "pattern", "paths"):
        if key in tool_input:
            return str(tool_input[key])
    return ""


def initial_message(state: AppState) -> str:
    return render(state.scope, state.preview)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scoped",
        description="Run a coding assistant over exactly the files you name.",
    )
    parser.add_argument("paths", nargs="*", help="files or directories to put in scope")
    parser.add_argument("--ext", help="restrict to these extensions, e.g. .py,.md")
    parser.add_argument("--exclude", action="append", default=[], help="extra exclude glob")
    parser.add_argument("--max-bytes", type=int, help="skip files larger than this")
    parser.add_argument("--no-secret-guard", action="store_true", help="allow credential files")
    parser.add_argument("--claude-md", action="store_true", help="load CLAUDE.md at startup")
    parser.add_argument("--skills", help="'all', or a comma-separated list")
    parser.add_argument("--prompt", choices=["lean", "preset"], help="which system prompt")
    parser.add_argument("--bash", action="store_true", help="expose the Bash tool")
    parser.add_argument("--max-preview-files", type=int, help="cap inline previews")
    parser.add_argument("--model", help="model override")
    parser.add_argument("-p", "--print", dest="one_shot", help="run one prompt and exit")
    return parser


def build_state(args, cwd: Path) -> tuple[AppState, config_module.Config]:
    cfg = config_module.load(cwd)

    filters = cfg.filters
    if args.ext:
        filters.extensions = parse_extensions(args.ext)
    filters.exclude = [*filters.exclude, *args.exclude]
    if args.max_bytes:
        filters.max_bytes = args.max_bytes
    if args.no_secret_guard:
        filters.secret_guard = False

    profile = cfg.profile
    if args.claude_md:
        profile.claude_md = True
    if args.skills:
        profile.skills = (
            "all" if args.skills == "all" else [s.strip() for s in args.skills.split(",")]
        )
    if args.prompt:
        profile.prompt = args.prompt
    if args.bash:
        profile.bash = True

    preview = cfg.preview
    if args.max_preview_files:
        preview.max_preview_files = args.max_preview_files

    targets = args.paths or cfg.default_scope
    scope = Scope(cwd=cwd, filters=filters)
    scope.add(targets)

    state = AppState(scope=scope, profile=profile, preview=preview, last_scope_args=list(targets))
    return state, cfg


def sdk_failure(exc: ClaudeSDKError) -> str:
    if isinstance(exc, CLINotFoundError):
        return (
            "scoped: Claude Code is not installed, or `claude` is not on your PATH. "
            "Install it, check that `claude` runs in this shell, then try again."
        )
    return (
        f"scoped: the Claude session failed: {exc}\n"
        "Check that `claude` starts on its own in this directory -- you may need to log in."
    )


async def run(args) -> int:
    cwd = Path.cwd().resolve()
    state, cfg = build_state(args, cwd)

    if not state.scope.files:
        if state.last_scope_args:
            lines = state.scope.explain(state.last_scope_args)
            print(
                "\n".join(line if line.startswith(" ") else f"scoped: {line}" for line in lines),
                file=sys.stderr,
            )
        else:
            print(
                "Nothing in scope. Name files or directories, or add a .scoped.toml "
                "with default_scope.\n",
                file=sys.stderr,
            )
            build_parser().print_usage(sys.stderr)
        return 2

    body = initial_message(state)
    print(
        f"{BOLD}scope:{RESET} {state.scope.summary()} (~{estimate_tokens(body)} tokens)  "
        f"{BOLD}context:{RESET} {state.profile.describe()}"
    )
    if cfg.source:
        note(f"config: {cfg.source}")
    if state.scope.skipped:
        note(f"{len(state.scope.skipped)} file(s) filtered out -- /filters to see why")

    session = ScopedSession(state, cfg.dev, args.model)
    try:
        await session.connect()
        await session.send(body, echo=False)
        if args.one_shot:
            await session.send(args.one_shot)
            return 0

        note("/help for commands, /exit to quit")
        while True:
            try:
                line = (await asyncio.to_thread(input, "\n> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue

            action = handle(state, line)
            if action is None:
                await session.send(line)
                continue

            if action.message:
                colour = WARN if line.startswith("/unscope") else DIM
                print(f"{colour}{action.message}{RESET}")
            if action.quit:
                return 0
            if action.reconnect:
                await session.reconnect(action.fresh)
            elif action.inject:
                await session.send(action.inject, echo=False)
    except ClaudeSDKError as exc:
        print(sdk_failure(exc), file=sys.stderr)
        return 1
    finally:
        await session.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except config_module.ConfigError as exc:
        print(f"scoped: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
