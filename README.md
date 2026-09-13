# coding-assistant-tools

Tools for driving coding assistants with explicit, bounded context.

First tool: **`scoped`** — a Claude Code-style REPL where the working set is
exactly the files you named, and nothing else gets in without you saying so.

Also: Claude Code **skills** under `skills/`, installed per project with
`install-skill.sh` (see [Skills](#skills)).

## Why

Stock Claude Code decides for itself what to read. Two mechanisms put context in
front of the model: **discovery** (it calls Glob/Grep/Read/Bash and goes looking)
and **auto-injection** (CLAUDE.md, settings, skills, git status, memory). `scoped`
closes both, and lets you re-open either one mid-session without losing the
conversation.

It is a *second* command, not a replacement. Projects that need full context keep
using `claude`.

## Install

`scoped` is a **separate command**, not a Claude Code plugin, skill, or MCP
server — there is nothing to install *into* Claude Code. It drives the `claude`
CLI as a subprocess via the Agent SDK and inherits that CLI's auth, so if
`claude` works in your terminal, `scoped` works. No API key, no separate login.

**1. Check the prerequisites.**

```bash
claude --version         # the CLI: https://claude.com/claude-code
python3.12 --version     # 3.12 or newer; `brew install python@3.12` on macOS
```

**2. Clone it and build a virtualenv.**

```bash
git clone <this repo> ~/Code/coding-assistant-tools
cd ~/Code/coding-assistant-tools
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`-e` is an editable install: edits to `src/scoped/` take effect on the next run,
with nothing to rebuild. Use `.venv/bin/pip install -e .` to skip the test and
lint tools.

**3. Put the command on your PATH.**

You never activate this venv. Symlink its one entry point instead, so `scoped`
runs from any project directory:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/scoped" ~/.local/bin/scoped
```

Any directory on your PATH works; `~/.local/bin` is just the usual one. If
`which scoped` comes up empty, that directory isn't on your PATH yet:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

**4. Verify, in that order.**

```bash
scoped --help                                     # the command resolves
cd ~/Code/someproject
scoped src/api -p "what does this module do?"     # the whole path works
```

The `-p` form runs one prompt and exits, which is the cheapest way to confirm
every hop: shell → `scoped` → SDK → `claude` → model. If `--help` works but
`-p` hangs or errors, the problem is the `claude` CLI or its auth, not `scoped`
— check `claude -p hello` on its own.

**Update or remove.**

```bash
git -C ~/Code/coding-assistant-tools pull        # editable: usually nothing else
.venv/bin/pip install -e ".[dev]"                # only if dependencies changed
rm ~/.local/bin/scoped                           # remove the command
```

## Use

```bash
cd ~/Code/someproject

scoped src/api/routes.py src/api/db.py   # two files: 100-line previews
scoped src/api --ext .py                 # a folder, Python only: tree + 10-line previews
scoped                                   # whatever .scoped.toml says
scoped src/api --claude-md --skills all  # start with context sources on
scoped src/api -p "why does pagination break?"   # one-shot, no REPL
```

A session:

```
scope: 12 file(s) under src/api (~4.2k tokens)  context: prompt=lean claude-md=off skills=none bash=off

> why does the pagination break on the last page?
  [Read] src/api/routes.py
  The off-by-one is in page_bounds at routes.py:88 ...

> check how this is called from the CLI
  [Read] src/cli/main.py
  That file isn't in scope. Run /scope add src/cli/main.py if you want me to look.

> /scope add src/cli
  + 4 file(s) (~1.1k tokens). scope: 16 file(s) under src/api, src/cli

> fix it and run the tests
  [Edit] src/api/routes.py
  [mcp__dev__run_tests] ['tests/api']
  14 passed.

> /context claude-md on
  reconnected, conversation preserved
```

## How the scope actually holds

Enforcement is a **`PreToolUse` hook**, not the `can_use_tool` callback. This
matters: per the SDK docs, a call approved at an earlier step never reaches
`can_use_tool`, and *a file read inside the working directory is approved on its
own with no rule needed*. A scope check there would be silently skipped for
exactly the case that matters. Hooks run before every other step and a hook deny
holds even in `bypassPermissions` mode.

A second hook, `PostToolUse`, filters Grep and Glob **results**. A search whose
root is legitimately in scope is a legitimate call, but it walks every file under
that root — including ones the filters excluded. This was an observed leak (a
`.env` inside a scoped directory), not a hypothetical one.

There is **no Bash**. An allowlist over a shell means parsing a shell, which is a
losing game. Testing and linting are not a shell, they are three functions:
`run_tests`, `run_lint`, `run_typecheck`, each building a fixed argv and running
it with `shell=False`. Shell syntax in an argument is inert data because nothing
parses it. The model supplies `paths` and `expression`; it cannot touch argv[0].

## Two kinds of toggle

|  | Scope (files, search) | Context (CLAUDE.md, skills, prompt, bash) |
|---|---|---|
| Lives in | a mutable object the hook closes over | `ClaudeAgentOptions`, fixed at connect |
| Changing it | instant, next tool call | reconnects, resuming the same session |
| Conversation | untouched | preserved |

Both are **forward-only**. `/unscope` and `/context claude-md off` do not retract
what is already in the transcript. `--fresh` on a `/context` command is the only
true purge, and it costs you the conversation.

## Commands

```
/scope                       show the manifest
/scope add <paths>           widen (takes effect immediately)
/scope rm <paths>            narrow
/unscope                     lift the file scope
/rescope                     re-apply it
/context                     show context sources
/context claude-md on|off    load or drop CLAUDE.md          (reconnects)
/context skills all|none|a,b which skills are available       (reconnects)
/context prompt lean|preset  lean prompt, or Claude Code's    (reconnects)
/context bash on|off         expose the Bash tool             (reconnects)
    --fresh on any /context command starts a new session instead of resuming
/filters                     show filters and what they excluded
/filters ext .py,.md         change the extension filter and re-expand
/help, /exit
```

## Per-project config

`.scoped.toml`, found in the current directory or any ancestor. Its presence is a
project's declaration that it is a scoped project; absence means you just use
`claude` there.

```toml
default_scope     = ["src/api", "tests/api"]
extensions        = [".py", ".md"]
exclude           = ["**/migrations/**"]
max_bytes         = 256000
test_command      = ["pytest", "-q"]
lint_command      = ["ruff", "check"]
typecheck_command = ["mypy"]

[context]
claude_md = true
skills    = ["dataviz"]
prompt    = "lean"
```

Commands are argv **lists, never strings** — a string would need splitting, and
splitting is the first step back towards parsing a shell. A JS project points the
same three tools at `vitest` / `eslint` / `tsc`.

## Secret guard

Files matching `.env*`, `*.pem`, `*.key`, `id_*`, `*credentials*`, `*secrets*` and
similar are blocked **in every mode** — `/unscope` does not switch them off — and
never enter the manifest. `--no-secret-guard` if you genuinely need one.

## Tests

```bash
.venv/bin/pytest              # 81 unit tests
.venv/bin/pytest -m e2e       # 7 end-to-end tests, slow and billable
```

The e2e suite is the one that matters: it drives a real model and asserts the
bytes never reach the transcript. Unit tests can only prove the guard *returns* a
denial — the `can_use_tool` approach passed unit tests and enforced nothing.

## Lint, lock files, and pre-commit hooks

Commits run ruff (lint + format), mypy, and Biome (the committed JSON files)
through [pre-commit](https://pre-commit.com). Dependencies are pinned with
pip-tools: `requirements.txt` (runtime) and `requirements-dev.txt` (runtime +
`dev` extras) are compiled from `pyproject.toml`, and a hook recompiles them
whenever `pyproject.toml` changes.

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e . --no-deps
.venv/bin/pre-commit install           # once per clone
.venv/bin/pre-commit run --all-files   # check everything by hand
```

To change a dependency, edit `pyproject.toml` and commit; the hook updates the
lock files (re-stage them and commit again). To upgrade pins:

```bash
.venv/bin/pip-compile --upgrade --strip-extras -o requirements.txt pyproject.toml
.venv/bin/pip-compile --upgrade --extra=dev --strip-extras -o requirements-dev.txt pyproject.toml
```

Biome's npm version is pinned twice in `.pre-commit-config.yaml` (`rev` and
`additional_dependencies`); `pre-commit autoupdate` only bumps the first.

## Limits

- Not a sandbox and not a security boundary against a hostile model. It is a
  context-discipline tool; the secret guard is a guardrail against accidents.
- No ad-hoc commands. When you need one, use `claude` — or add a fourth typed
  tool, which is how this is meant to grow.

## Skills

Claude Code skills live in `skills/<name>/` (a `SKILL.md` plus optional
`references/` and `scripts/`). They are installed per project, as a symlink, so
edits here take effect everywhere they're linked:

```bash
./install-skill.sh expo-store-release ~/Code/someproject
./install-skill.sh --uninstall expo-store-release ~/Code/someproject
```

The installer links `.claude/skills/<name>` in the target, adds that path to the
target's `.git/info/exclude` (local-only, nothing to commit), and merges any
`deny` permission rules from the skill's `install.json` into the target's
`.claude/settings.json`. Uninstall removes all three.

| Skill | What it does |
|---|---|
| `expo-store-release` | Audits an Expo/EAS app for the App Store and Google Play (Apple and Google requirements, security review, testing vs final submission), presents findings as a plan, applies only approved fixes, and hands you the build command. The agent never builds or submits: `install.json` denies `eas build/submit/update/credentials`, and `scripts/store-release.sh` refuses to run inside an agent session or without a terminal. |

`skills/expo-store-release/scripts/preflight.py` is the skill's read-only,
offline checker (stdlib only); `tests/test_expo_store_release.py` covers it and
the installer. Eval runs from skill-creator go in `skills/*-workspace/`
(gitignored).
