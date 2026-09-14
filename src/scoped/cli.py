"""The `scoped` command: a REPL over a bounded working set."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

try:
    # Importing readline is what gives input() arrow keys, history, and
    # Ctrl+A/E; without it the arrows print escape codes. Some Python builds
    # ship without it, and the prompt still works, just without editing.
    import readline  # noqa: F401
except ImportError:
    pass

try:
    import termios
except ImportError:  # not a Unix terminal
    termios = None  # type: ignore[assignment]

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

    def __init__(self, state: AppState, dev_config):
        self.state = state
        self.dev_config = dev_config
        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None
        self._connecting: asyncio.Task[None] | None = None
        # Nothing goes to the model until the user asks something: the manifest
        # and any scope notes ride along with the first question instead of
        # costing a turn (and tokens) of their own.
        self.needs_manifest = True
        self.pending: list[str] = []

    def start(self, resume: str | None = None) -> None:
        """Begin connecting in the background, so the prompt appears at once.

        Connecting spawns the Claude CLI but makes no model call.
        """
        self._connecting = asyncio.create_task(self.connect(resume=resume))

    async def ready(self) -> None:
        if self._connecting is None:
            self.start()
        assert self._connecting is not None
        await self._connecting

    def raise_if_failed(self) -> None:
        """Surface a connect failure that has already happened, e.g. no CLI."""
        task = self._connecting
        if task is not None and task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                raise exc

    def queue(self, text: str) -> None:
        """Hold a note for the model until the next question."""
        if not self.needs_manifest:  # otherwise the manifest will already reflect it
            self.pending.append(text)

    async def ask(self, question: str) -> None:
        if self.needs_manifest:
            parts = [initial_message(self.state)]
        else:
            parts = self.pending
        self.needs_manifest = False
        self.pending = []
        await self.ready()
        await self.send("\n\n".join([*parts, question]) if parts else question)

    async def connect(self, resume: str | None = None) -> None:
        options = build_options(
            self.state.scope, self.state.profile, self.dev_config, resume=resume
        )
        if self.state.model:
            options.model = self.state.model
        # Only keep the client once it is connected, so `close` after a failed
        # connect has nothing half-started to tear down.
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.client = client
        # The initialize response lists the selectable models. Older CLIs do not
        # send it, and /model then takes names without offering a list.
        models = (await client.get_server_info() or {}).get("models")
        self.state.models = (
            [m for m in models if isinstance(m, dict) and not m.get("disabled")]
            if isinstance(models, list)
            else []
        )

    async def set_model(self) -> None:
        """Switch the live session to `state.model`, keeping the conversation.

        Waits for a pending connect first: its options may carry the old model.
        """
        await self.ready()
        assert self.client is not None
        await self.client.set_model(self.state.model)

    async def reconnect(self, fresh: bool) -> None:
        """Apply a changed profile.

        Context sources are ClaudeAgentOptions fields, fixed at connect time, so
        the only way to change one is to rebuild. Resuming by session id carries
        the conversation across; `fresh` deliberately does not, which is the
        only way to drop context that is already in the transcript.
        """
        await self.close()
        if fresh:
            self.session_id = None
            self.needs_manifest = True
            self.pending = []
            note("fresh session -- previous conversation dropped")
        else:
            note("conversation preserved")
        self.start(resume=self.session_id)

    async def send(self, text: str) -> None:
        assert self.client is not None
        await self.client.query(text)
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                self.state.active_model = message.model
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        note(f"  [{block.name}] {_summarize(block.input)}")
                    elif isinstance(block, TextBlock):
                        print(block.text)
            elif isinstance(message, ResultMessage):
                self.session_id = message.session_id
                if message.subtype != "success":
                    detail = "; ".join(message.errors or [])
                    note(f"  [{message.subtype}] {detail}".rstrip())

    async def close(self) -> None:
        if self._connecting is not None:
            # Let a pending connect finish so there is a client to shut down
            # rather than a half-started subprocess; its error no longer matters.
            task, self._connecting = self._connecting, None
            with contextlib.suppress(Exception):
                await task
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


async def read_line(prompt: str) -> str:
    """`input()` off the event loop, on a daemon thread.

    Not `asyncio.to_thread`: a thread blocked in `input()` cannot be cancelled,
    and `asyncio.run` joins the default executor on shutdown, so Ctrl+C would
    hang until the next Enter and the SDK subprocess would be torn down after
    the loop closed. A daemon thread is simply abandoned at exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(result: str | None, exc: BaseException | None) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result or "")

    def target() -> None:
        try:
            result = input(prompt)
        except (EOFError, OSError) as exc:
            loop.call_soon_threadsafe(deliver, None, exc)
        else:
            loop.call_soon_threadsafe(deliver, result, None)

    threading.Thread(target=target, daemon=True).start()
    return await future


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

    state = AppState(
        scope=scope,
        profile=profile,
        preview=preview,
        last_scope_args=list(targets),
        model=args.model,
    )
    return state, cfg


LOGIN_CHECK_TIMEOUT = 3.0


async def auth_status() -> dict[str, Any] | None:
    """`claude auth status`: reads the saved login and makes no model call.

    None means the check could not be made -- no CLI on PATH, a CLI too old to
    have the command, a hang, or output that is not JSON. None never blocks.
    """
    cli = shutil.which("claude")
    if cli is None:
        return None  # connect reports a missing CLI with a proper message
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), LOGIN_CHECK_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        # The exit code is not checked: a logged-out status is still an answer.
        status = json.loads(out)
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def login_problem(status: dict[str, Any] | None) -> str | None:
    if status is None or status.get("loggedIn") is not False:
        return None
    # Bedrock and Vertex authenticate outside claude.ai, so "not logged in"
    # says nothing about whether their sessions will work.
    if status.get("apiProvider", "firstParty") != "firstParty":
        return None
    return (
        "scoped: you are not logged in to Claude, so questions will fail. "
        "Exit, run `claude auth login`, then start scoped again."
    )


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
        f"{BOLD}context:{RESET} {state.profile.describe()}  "
        f"{BOLD}model:{RESET} {state.model or 'default'}"
    )
    if cfg.source:
        note(f"config: {cfg.source}")
    if state.scope.skipped:
        note(f"{len(state.scope.skipped)} file(s) filtered out -- /filters to see why")

    session = ScopedSession(state, cfg.dev)
    try:
        session.start()
        # Warn only: a false alarm must not lock anyone out of a working session.
        if problem := login_problem(await auth_status()):
            print(f"{WARN}{problem}{RESET}", file=sys.stderr)
        if args.one_shot:
            await session.ask(args.one_shot)
            return 0

        note("/help for commands, exit or Ctrl+D to quit")
        while True:
            await asyncio.sleep(0)  # let an instant connect failure land first
            session.raise_if_failed()
            try:
                line = (await read_line("\n> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue

            previous_model = state.model
            action = handle(state, line)
            if action is None:
                await session.ask(line)
                continue

            if action.message:
                colour = WARN if line.startswith("/unscope") else DIM
                print(f"{colour}{action.message}{RESET}")
            if action.quit:
                return 0
            if action.reconnect:
                await session.reconnect(action.fresh)
            elif action.set_model:
                try:
                    await session.set_model()
                except ClaudeSDKError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- the SDK raises a rejected switch as a plain Exception
                    state.model = previous_model
                    print(
                        f"{WARN}could not switch model: {exc}. "
                        f"still on {previous_model or 'default'}{RESET}"
                    )
            elif action.inject:
                session.queue(action.inject)
    except ClaudeSDKError as exc:
        print(sdk_failure(exc), file=sys.stderr)
        return 1
    finally:
        await session.close()


def save_terminal() -> list | None:
    if termios is None or not sys.stdin.isatty():
        return None
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except termios.error:
        return None


def restore_terminal(saved: list | None) -> None:
    """Undo readline's terminal modes if we exit while it is mid-prompt.

    On Ctrl+C the reader thread is abandoned inside readline, which never gets
    to switch echo and line editing back on for the shell.
    """
    if saved is None or termios is None:
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    except termios.error:
        pass


def main() -> int:
    args = build_parser().parse_args()
    saved = save_terminal()
    try:
        return asyncio.run(run(args))
    except config_module.ConfigError as exc:
        print(f"scoped: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        restore_terminal(saved)


if __name__ == "__main__":
    raise SystemExit(main())
