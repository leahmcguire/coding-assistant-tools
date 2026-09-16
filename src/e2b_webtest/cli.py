"""The `e2b-webtest` command.

Every subcommand here is something **you** run -- Claude never invokes this
CLI itself. It creates, drives, and tears down E2B sandboxes; that's exactly
the live, billable, network-touching activity Claude stays out of. Claude
writes `.e2b-webtest.toml` and any Playwright scripts, and reads back
whatever a command you ran prints or downloads.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from e2b import (
    AuthenticationException,
    CommandExitException,
    Sandbox,
    SandboxException,
    SandboxQuery,
    Template,
)

from . import config as config_module
from . import envs, runner, scripts, state
from .config import Config, ConfigError
from .template import build_template
from .upload import UploadError, upload_to_sandbox

TOOL_METADATA_KEY = "tool"
TOOL_METADATA_VALUE = "e2b-webtest"
PROJECT_METADATA_KEY = "project"


def _project_root() -> Path:
    return Path.cwd().resolve()


def _load_config(args: argparse.Namespace) -> tuple[Path, Config]:
    root = _project_root()
    explicit = getattr(args, "config", None)
    path = Path(explicit).resolve() if explicit else config_module.find(root)
    if path is None:
        raise ConfigError(f"no {config_module.CONFIG_NAME} found in {root} or its parents")
    return root, config_module.load(path)


def _require_state(root: Path) -> state.State:
    current = state.load(root)
    if current is None:
        print("e2b-webtest: no running sandbox here -- run `e2b-webtest up` first", file=sys.stderr)
        raise SystemExit(1)
    return current


def _connect(current: state.State) -> Sandbox:
    try:
        return Sandbox.connect(current.sandbox_id)
    except (SandboxException, AuthenticationException) as exc:
        print(
            f"e2b-webtest: cannot reach sandbox {current.sandbox_id}: {exc}\n"
            "it may have timed out or been killed -- `e2b-webtest down` clears the local record",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def _approve_env(cfg: Config, *, assume_yes: bool) -> dict[str, str] | None:
    """Show the names (never the values) crossing into the sandbox, and get an OK.

    Returns None when you decline, so the caller aborts before anything is
    created or run. Nothing to send means nothing to approve.
    """
    env_plan = envs.plan(cfg.env)
    print(envs.describe(env_plan))
    if not env_plan.sent:
        return {}
    if assume_yes:
        return envs.base_env(cfg.env)
    if not sys.stdin.isatty():
        print(
            "e2b-webtest: no terminal to ask for approval at -- "
            "re-run with --yes if you meant to send these",
            file=sys.stderr,
        )
        return None
    if input("send these into the sandbox? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return None
    return envs.base_env(cfg.env)


def cmd_template_build(args: argparse.Namespace) -> int:
    _root, cfg = _load_config(args)
    builder = build_template(cfg.template)
    name = args.name or cfg.sandbox.template

    if args.dry_run:
        print(Template.to_dockerfile(builder))
        return 0

    print(f"building template {name!r} ({cfg.sandbox.vcpu} vCPU, {cfg.sandbox.memory_mb} MiB)...")
    info = Template.build(
        builder,
        name=name,
        cpu_count=cfg.sandbox.vcpu,
        memory_mb=cfg.sandbox.memory_mb,
        skip_cache=args.skip_cache,
        on_build_logs=lambda entry: print(getattr(entry, "message", str(entry))),
    )
    print(f"built {name!r}: {info}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    root, cfg = _load_config(args)
    if state.load(root) is not None:
        print("e2b-webtest: a sandbox is already recorded here -- `down` it first", file=sys.stderr)
        return 1

    env = _approve_env(cfg, assume_yes=args.yes)
    if env is None:
        return 1

    print(f"creating sandbox from template {cfg.sandbox.template!r}...")
    sandbox = Sandbox.create(
        template=cfg.sandbox.template,
        timeout=cfg.sandbox.timeout,
        metadata={TOOL_METADATA_KEY: TOOL_METADATA_VALUE, PROJECT_METADATA_KEY: root.name},
        network={"allow_public_traffic": False},
    )
    print(f"sandbox {sandbox.sandbox_id}")

    try:
        print(f"uploading project to {cfg.upload.dest}...")
        upload_to_sandbox(sandbox, root, cfg.upload)

        print(f"running {len(cfg.setup)} setup step(s)...")
        env = runner.run_setup(sandbox, cfg.setup, env)

        print(f"starting {len(cfg.services)} service(s)...")
        runner.start_services(sandbox, cfg.services, env)
    # SandboxException covers CommandExitException and TimeoutException alike:
    # any sandbox-side failure during bring-up must still reach the kill below,
    # or an unreachable sandbox keeps billing with no local record of it.
    except (runner.SetupFailed, runner.ReadinessTimeout, SandboxException, UploadError) as exc:
        print(f"e2b-webtest: {exc}", file=sys.stderr)
        if not args.keep_on_failure:
            print("killing sandbox (pass --keep-on-failure to leave it running for debugging)")
            sandbox.kill()
        return 1

    now = datetime.now(UTC)
    new_state = state.State(
        sandbox_id=sandbox.sandbox_id,
        template=cfg.sandbox.template,
        created_at=now.isoformat(),
        timeout_at=(now + timedelta(seconds=cfg.sandbox.timeout)).isoformat(),
        services=[s.name for s in cfg.services],
    )
    state.save(root, new_state)
    print(f"up. services: {', '.join(new_state.services) or '(none)'}")
    print("next: write a Playwright script, then `e2b-webtest run-script <path>`")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root, _cfg = _load_config(args)
    current = state.load(root)
    if current is None:
        print("no running sandbox here")
        return 0
    print(f"sandbox:   {current.sandbox_id}")
    print(f"template:  {current.template}")
    print(f"created:   {current.created_at}")
    print(f"times out: {current.timeout_at}")
    print(f"services:  {', '.join(current.services) or '(none)'}")
    try:
        _connect(current)
    except Exception as exc:  # noqa: BLE001 -- reported, not raised
        print(f"reachable: no ({exc})")
        return 0
    print("reachable: yes")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    root, cfg = _load_config(args)
    current = _require_state(root)
    service = next((s for s in cfg.services if s.name == args.service), None)
    if service is None:
        names = ", ".join(s.name for s in cfg.services) or "(none configured)"
        print(f"e2b-webtest: no service named {args.service!r} (have: {names})", file=sys.stderr)
        return 2
    sandbox = _connect(current)
    print(runner.read_log(sandbox, service, lines=args.lines), end="")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    root, cfg = _load_config(args)
    current = _require_state(root)
    sandbox = _connect(current)
    cmd = " ".join(args.command)
    try:
        result = sandbox.commands.run(cmd, cwd=cfg.upload.dest, timeout=args.timeout)
    except CommandExitException as exc:
        sys.stdout.write(exc.stdout)
        sys.stderr.write(exc.stderr)
        return exc.exit_code
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return 0


def cmd_run_script(args: argparse.Namespace) -> int:
    root, cfg = _load_config(args)
    current = _require_state(root)
    sandbox = _connect(current)
    env = _approve_env(cfg, assume_yes=args.yes)
    if env is None:
        return 1

    local_path = Path(args.script).resolve()
    if not local_path.is_file():
        print(f"e2b-webtest: {local_path} does not exist", file=sys.stderr)
        return 2

    exit_code = scripts.run_script(sandbox, local_path, cfg.upload, env, args.args)

    download_dir = (
        Path(args.download_dir).resolve()
        if args.download_dir
        else root / ".e2b-webtest" / "downloads"
    )
    names = scripts.download_outputs(sandbox, cfg.upload, download_dir)
    if names:
        print(f"downloaded {len(names)} file(s) to {download_dir}:")
        for name in names:
            print(f"  {name}")
    return exit_code


def cmd_extend(args: argparse.Namespace) -> int:
    root, _cfg = _load_config(args)
    current = _require_state(root)
    sandbox = _connect(current)
    sandbox.set_timeout(args.seconds)
    now = datetime.now(UTC)
    current.timeout_at = (now + timedelta(seconds=args.seconds)).isoformat()
    state.save(root, current)
    print(f"extended: times out at {current.timeout_at}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    root, _cfg = _load_config(args)
    current = _require_state(root)
    sandbox = _connect(current)
    local_dir = Path(args.local or ".").resolve()
    local_dir.mkdir(parents=True, exist_ok=True)
    data = sandbox.files.read(args.remote, format="bytes")
    target = local_dir / Path(args.remote).name
    target.write_bytes(data)
    print(f"downloaded {args.remote} -> {target}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    root, _cfg = _load_config(args)
    current = state.load(root)
    if current is None:
        print("no running sandbox here")
        return 0
    try:
        Sandbox.kill(current.sandbox_id)
    except Exception as exc:  # noqa: BLE001 -- local state is cleared regardless
        print(f"e2b-webtest: could not kill {current.sandbox_id}: {exc}", file=sys.stderr)
    state.clear(root)
    print(f"down: {current.sandbox_id}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    paginator = Sandbox.list(query=SandboxQuery(metadata={TOOL_METADATA_KEY: TOOL_METADATA_VALUE}))
    found = False
    while paginator.has_next:
        for info in paginator.next_items():
            found = True
            project = info.metadata.get(PROJECT_METADATA_KEY, "?")
            print(f"{info.sandbox_id}  {project}  {info.state.value}")
    if not found:
        print("no e2b-webtest sandboxes running")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e2b-webtest",
        description="Run a project's web stack and Playwright tests inside an E2B sandbox.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--config", help=f"path to {config_module.CONFIG_NAME} (default: search upward)"
        )
        sub.set_defaults(func=func)
        return sub

    template_parser = subparsers.add_parser("template", help="template subcommands")
    template_sub = template_parser.add_subparsers(dest="template_command", required=True)
    build = template_sub.add_parser("build", help="build the sandbox template image")
    build.add_argument(
        "--config", help=f"path to {config_module.CONFIG_NAME} (default: search upward)"
    )
    build.add_argument("--name", help="template name (default: sandbox.template from config)")
    build.add_argument("--skip-cache", action="store_true")
    build.add_argument("--dry-run", action="store_true", help="print the Dockerfile, build nothing")
    build.set_defaults(func=cmd_template_build)

    up = add("up", cmd_up, "create a sandbox and start the project's services")
    up.add_argument(
        "--keep-on-failure", action="store_true", help="don't kill the sandbox if setup fails"
    )
    up.add_argument("-y", "--yes", action="store_true", help="skip the env-var approval prompt")

    add("status", cmd_status, "show the recorded sandbox, if any")

    logs = add("logs", cmd_logs, "show a service's log tail")
    logs.add_argument("service")
    logs.add_argument("--lines", type=int, default=200)

    ex = add("exec", cmd_exec, "run a command in the sandbox")
    ex.add_argument("command", nargs=argparse.REMAINDER)
    ex.add_argument("--timeout", type=float, default=60)

    rs = add("run-script", cmd_run_script, "upload and run a Playwright script in the sandbox")
    rs.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the env-var approval prompt (must come before the script path)",
    )
    rs.add_argument("script", help="local path to the .py script")
    rs.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the script")
    rs.add_argument(
        "--download-dir", help="where to save script outputs (default: .e2b-webtest/downloads)"
    )

    extend = add("extend", cmd_extend, "extend the sandbox's session timeout")
    extend.add_argument("--seconds", type=int, default=3600)

    dl = add("download", cmd_download, "download one file from the sandbox")
    dl.add_argument("remote")
    dl.add_argument("local", nargs="?", default=".")

    add("down", cmd_down, "kill the sandbox and clear local state")

    list_parser = subparsers.add_parser(
        "list", help="list this tool's sandboxes across all projects"
    )
    list_parser.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, UploadError) as exc:
        print(f"e2b-webtest: {exc}", file=sys.stderr)
        return 2
    except (SandboxException, AuthenticationException) as exc:
        print(f"e2b-webtest: E2B error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
