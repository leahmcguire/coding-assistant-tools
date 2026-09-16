# e2b-webtest

Spins up an [E2B](https://e2b.dev) sandbox, uploads a project, runs its setup steps and
services, and lets you poke at the running stack -- all in a disposable VM, never on your
machine. See [`PLAN.md`](PLAN.md) for the design (including the in-progress adaptive-agent
work) and the reasoning behind it.

## Install

Same venv as `scoped` -- see the [root README](../../README.md#install). Once installed,
`e2b-webtest` is on your PATH from any project directory.

```bash
export E2B_API_KEY=...   # your E2B account's API key
```

## Per-project config

`.e2b-webtest.toml`, in the project root (not committed -- same status as `.env`). It has:
sandbox size, a `[template]` section for extra apt packages, `[upload]` for what gets sent into
the sandbox, `[env]` for which environment variables are forwarded (by **name**, approved before
every send), `[[setup]]` steps that run once (e.g. `supabase start`, seed restore, `docker
build`, `npm ci`), and `[[services]]` that stay running with a readiness check.

## Commands

```
e2b-webtest template build [--skip-cache] [--dry-run]   build the sandbox image
e2b-webtest up [-y]                                      create a sandbox, run setup, start services
e2b-webtest status                                       show the recorded sandbox, if any
e2b-webtest logs <service> [--lines N]                   tail a service's log
e2b-webtest exec <command...> [--timeout N]               run a command in the sandbox
e2b-webtest extend [--seconds N]                          extend the sandbox's session timeout
e2b-webtest download <remote> [local]                     pull one file back
e2b-webtest down                                          kill the sandbox, clear local state
e2b-webtest list                                          list this tool's sandboxes, all projects
```

`up` and any command sending environment variables into the sandbox print their **names only**
and wait for your approval first -- `-y`/`--yes` skips the prompt once you trust the list.

## Testing a project

The commands you actually run, in order, against `<YOUR_PROJECT>`:

```bash
cd <YOUR_PROJECT>

# First time, or after a template.py change:
e2b-webtest template build --skip-cache

# Boot the sandbox: uploads the repo, runs supabase start / seed restore /
# backend build / npm ci / expo export, starts backend + web as services.
e2b-webtest up

# Confirm it's actually up, and which sandbox this is.
e2b-webtest status

# Poke at it directly.
e2b-webtest exec df -h
e2b-webtest exec du -sh /home /opt /usr/lib/node_modules
e2b-webtest exec curl -sf http://127.0.0.1:8000/health
e2b-webtest logs backend
e2b-webtest logs web

# Still working after the default hour? Extend before it times out.
e2b-webtest extend --seconds 3600

# Pull a file back (e.g. something a script or command wrote).
e2b-webtest download /home/user/dinner-circle/some-output.png .

# Done -- kill the sandbox, clear local state.
e2b-webtest down
```

`e2b-webtest status` at any point tells you whether a sandbox is still recorded as running, and
`e2b-webtest list` shows every sandbox this tool has created across all your projects, in case
one gets left running.

Static Playwright scripts (fixed, repeatable checks) run **locally**, against your own
already-running dev stack -- not through this tool at all. See `PLAN.md` for why.
