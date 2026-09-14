"""Slash commands.

Two kinds, and the difference is the whole design. Scope commands mutate the
object the hook closes over, so they land on the next tool call. Context
commands change ClaudeAgentOptions, which is fixed at connect time, so they ask
for a reconnect that resumes the same session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .preview import PreviewConfig, estimate_tokens, render
from .profile import ContextProfile
from .scope import Scope

HELP = """\
  /scope                       show the manifest
  /scope add <paths>           widen scope (takes effect immediately)
  /scope rm <paths>            narrow scope
  /unscope                     lift the file scope for this session
  /rescope                     re-apply the file scope
  /context                     show context sources
  /context claude-md on|off    load or drop CLAUDE.md         (reconnects)
  /context skills all|none|a,b which skills are available      (reconnects)
  /context prompt lean|preset  swap the system prompt          (reconnects)
  /context bash on|off         expose the Bash tool            (reconnects)
      add --fresh to any /context command to start a new session instead of
      resuming, which is the only way to drop what is already in the transcript
  /model                       show the model and the ones you can pick
  /model <name|number|default> switch model (live, keeps the conversation)
  /filters                    show active file filters
  /filters ext .py,.md         change the extension filter and re-expand
  /help
  /exit, exit, Ctrl+D          quit
"""


CONTEXT_VALUES = {
    "claude-md": "on|off",
    "skills": "all|none|name1,name2",
    "prompt": "lean|preset",
    "bash": "on|off",
}


@dataclass
class AppState:
    scope: Scope
    profile: ContextProfile
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    last_scope_args: list[str] = field(default_factory=list)
    # The requested model; None leaves the choice to the CLI.
    model: str | None = None
    # What the CLI reports as selectable once connected: value, displayName, description.
    models: list[dict[str, Any]] = field(default_factory=list)
    # The model that actually answered the last question.
    active_model: str | None = None


@dataclass
class Action:
    quit: bool = False
    reconnect: bool = False
    set_model: bool = False
    fresh: bool = False
    inject: str | None = None
    message: str | None = None


def handle(state: AppState, line: str) -> Action | None:
    """Return None if this is not a command and should go to the model."""
    # Bare `exit` is what people type in any other REPL; sent to the model it
    # just gets a polite goodbye and the session carries on.
    if line.lower().removesuffix("()") in {"exit", "quit"}:
        return Action(quit=True)
    if not line.startswith("/"):
        return None

    parts = line.split()
    command, args = parts[0], parts[1:]
    fresh = "--fresh" in args
    args = [a for a in args if a != "--fresh"]

    match command:
        case "/help":
            return Action(message=HELP)
        case "/exit" | "/quit":
            return Action(quit=True)
        case "/scope":
            return _scope(state, args)
        case "/unscope":
            state.scope.set_mode("open")
            return Action(
                message=(
                    "!! file scope lifted -- the assistant can now read anything under "
                    f"{state.scope.cwd}. Credential files stay blocked. /rescope to restore.\n"
                    "   Note: this does not retract anything already in the transcript."
                )
            )
        case "/rescope":
            state.scope.set_mode("strict")
            return Action(message=f"file scope restored: {state.scope.summary()}")
        case "/context":
            return _context(state, args, fresh)
        case "/model":
            return _model(state, args)
        case "/filters":
            return _filters(state, args)
        case _:
            return Action(message=f"unknown command {command}. /help for the list.")


def _scope(state: AppState, args: list[str]) -> Action:
    scope = state.scope
    if not args:
        return Action(message=_manifest(state))

    action, targets = args[0], args[1:]
    if action == "add":
        if not targets:
            return Action(message="usage: /scope add <paths>")
        added = scope.add(targets)
        if not added:
            return Action(message="nothing added:\n" + "\n".join(scope.explain(targets)))
        preview = render_subset(state, added)
        # Some targets may have landed while others were typos; say which.
        missing = scope.explain([t for t in targets if not scope.resolve(t).exists()])
        return Action(
            message=f"+ {len(added)} file(s) (~{estimate_tokens(preview)} tokens). "
            f"scope: {scope.summary()}" + "".join(f"\n{line}" for line in missing),
            inject=f"These files have just been added to your scope:\n\n{preview}",
        )
    if action in {"rm", "remove"}:
        if not targets:
            return Action(message="usage: /scope rm <paths>")
        dropped = scope.remove(targets)
        if not dropped:
            return Action(
                message=f"nothing removed -- no files in scope under {', '.join(targets)}. "
                "/scope lists what is."
            )
        return Action(
            message=f"- {len(dropped)} file(s). scope: {scope.summary()}",
            inject=(
                "These files have been removed from your scope; do not rely on them:\n"
                + "\n".join(f"  {scope.relative(p)}" for p in dropped)
                if dropped
                else None
            ),
        )
    return Action(message=f"unknown /scope action '{action}'. usage: /scope [add|rm] <paths>")


def render_subset(state: AppState, paths: list[Path]) -> str:
    """Preview just these files, using the same rules as the initial render."""
    subset = Scope(cwd=state.scope.cwd, filters=state.scope.filters)
    subset.files = set(paths)
    subset.roots = set(state.scope.roots)
    return render(subset, state.preview)


def _manifest(state: AppState) -> str:
    scope = state.scope
    lines = [f"mode: {scope.mode}    {scope.summary()}"]
    for path in scope.sorted_files():
        lines.append(f"  {scope.relative(path)}")
    if scope.skipped:
        lines.append(f"  ({len(scope.skipped)} file(s) filtered out -- /filters to see why)")
    body = render(scope, state.preview)
    lines.append(f"~{estimate_tokens(body)} tokens if re-sent in full")
    return "\n".join(lines)


def _context(state: AppState, args: list[str], fresh: bool) -> Action:
    profile = state.profile
    if not args:
        return Action(message=profile.describe())

    setting, value = (args[0], args[1] if len(args) > 1 else None)
    if setting not in CONTEXT_VALUES:
        return Action(
            message=f"unknown context setting {setting}. Choose from: {', '.join(CONTEXT_VALUES)}."
        )
    if value is None:
        return Action(message=f"usage: /context {setting} {CONTEXT_VALUES[setting]}")
    if setting in {"claude-md", "bash"} and value not in {"on", "off"}:
        return Action(message=f"{setting} must be on or off, got '{value}'")

    match setting:
        case "claude-md":
            profile.claude_md = value == "on"
        case "bash":
            profile.bash = value == "on"
        case "prompt":
            if value == "lean":
                profile.prompt = "lean"
            elif value == "preset":
                profile.prompt = "preset"
            else:
                return Action(message=f"prompt must be lean or preset, got '{value}'")
        case "skills":
            if value == "none":
                profile.skills = None
            elif value == "all":
                profile.skills = "all"
            else:
                profile.skills = [s.strip() for s in value.split(",") if s.strip()]

    return Action(
        reconnect=True,
        fresh=fresh,
        message=f"{profile.describe()}",
    )


def _model(state: AppState, args: list[str]) -> Action:
    """Unlike context sources, the model can change on a live connection: no reconnect."""
    if not args:
        return Action(message=_model_list(state))

    choice = args[0]
    if choice.isdigit():
        index = int(choice) - 1
        if not 0 <= index < len(state.models):
            return Action(message=f"no model #{choice}. /model lists the choices.")
        choice = str(state.models[index].get("value"))
    # "default" is the CLI's own name for no override, and set_model spells it None.
    state.model = None if choice == "default" else choice
    return Action(
        set_model=True,
        message=f"model: {state.model or 'default'} -- applies from the next question",
    )


def _model_list(state: AppState) -> str:
    current = state.model or "default"
    header = f"model: {current}"
    if state.active_model:
        header += f"    last reply from: {state.active_model}"
    lines = [header]
    if not state.models:
        lines.append(
            "  the list of models appears once the session has connected; "
            "a name or alias works meanwhile, e.g. /model sonnet"
        )
        return "\n".join(lines)
    for number, entry in enumerate(state.models, start=1):
        value = str(entry.get("value"))
        marker = "*" if value == current else " "
        label = entry.get("displayName") or value
        description = entry.get("description")
        detail = f" -- {description}" if description else ""
        lines.append(f" {marker}{number:>2}. {value:<16} {label}{detail}")
    lines.append("/model <number|name> to switch")
    return "\n".join(lines)


def _filters(state: AppState, args: list[str]) -> Action:
    filters = state.scope.filters
    if not args:
        lines = [
            f"extensions: {sorted(filters.extensions) if filters.extensions else 'any text file'}",
            f"max size:   {filters.max_bytes:,} bytes",
            f"secrets:    {'blocked' if filters.secret_guard else 'NOT blocked'}",
            f"excludes:   {len(filters.exclude)} pattern(s)",
        ]
        if state.scope.skipped:
            lines.append("filtered out of this scope:")
            for skip in state.scope.skipped[:20]:
                lines.append(f"  {state.scope.relative(skip.path)} -- {skip.reason}")
        return Action(message="\n".join(lines))

    if args[0] == "ext" and len(args) > 1:
        from .filters import parse_extensions

        filters.extensions = parse_extensions(args[1])
        state.scope.files.clear()
        state.scope.skipped.clear()
        state.scope.add(state.last_scope_args)
        message = f"extension filter updated. scope: {state.scope.summary()}"
        if not state.scope.files:
            message += " -- nothing matches this filter; /filters shows why each file was skipped"
        return Action(message=message)

    return Action(message="usage: /filters [ext .py,.md]")
