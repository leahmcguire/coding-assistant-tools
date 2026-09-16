"""Run a Playwright test script inside the sandbox, and pull back what it wrote.

Claude writes an ordinary local `.py` file, following the patterns from
Anthropic's `webapp-testing` skill (synchronous Playwright, headless
Chromium). Writing that file is just editing code -- nothing executes on
your machine. `run_script` is the only thing that executes it: it uploads
the file into the sandbox and runs it with the sandbox's own Python (the venv
`template.py` builds, with Chromium already installed), streaming output back
live so you can watch it run. The script should write anything it wants kept
(screenshots, HTML dumps) into `$E2B_WEBTEST_OUTPUT_DIR`, which `run_script`
sets and `download_outputs` pulls back afterwards.
"""

from __future__ import annotations

import io
import shlex
import sys
import tarfile
from pathlib import Path

from e2b import CommandExitException

from .config import UploadConfig
from .template import VENV_PYTHON

SCRIPTS_DIR_NAME = ".e2b-webtest-scripts"
OUTPUT_DIR_NAME = ".e2b-webtest-out"
OUTPUT_DIR_ENV_VAR = "E2B_WEBTEST_OUTPUT_DIR"


def remote_scripts_dir(upload: UploadConfig) -> str:
    return f"{upload.dest}/{SCRIPTS_DIR_NAME}"


def remote_output_dir(upload: UploadConfig) -> str:
    return f"{upload.dest}/{OUTPUT_DIR_NAME}"


def run_script(
    sandbox,
    local_path: Path,
    upload: UploadConfig,
    env: dict[str, str],
    args: list[str] | None = None,
) -> int:
    """Upload and run one script. Returns its exit code.

    A nonzero exit is an expected outcome (a failing test), not a tool
    error, so this returns rather than raising.
    """
    scripts_dir = remote_scripts_dir(upload)
    output_dir = remote_output_dir(upload)
    sandbox.commands.run(f"mkdir -p {shlex.quote(scripts_dir)} {shlex.quote(output_dir)}")

    remote_path = f"{scripts_dir}/{local_path.name}"
    sandbox.files.write(remote_path, local_path.read_bytes())

    arg_str = " ".join(shlex.quote(a) for a in (args or []))
    cmd = f"{VENV_PYTHON} {shlex.quote(remote_path)}"
    if arg_str:
        cmd += f" {arg_str}"
    script_env = {**env, OUTPUT_DIR_ENV_VAR: output_dir}
    try:
        sandbox.commands.run(
            cmd,
            cwd=upload.dest,
            envs=script_env,
            timeout=None,
            on_stdout=lambda line: print(line, end=""),
            on_stderr=lambda line: print(line, end="", file=sys.stderr),
        )
        return 0
    except CommandExitException as exc:
        return exc.exit_code


def download_outputs(sandbox, upload: UploadConfig, local_dir: Path) -> list[str]:
    """Tar the script output directory out of the sandbox, extract it locally, clear it.

    Returns the extracted file paths (relative to `local_dir`), or an empty
    list if the script wrote nothing.
    """
    output_dir = remote_output_dir(upload)
    archive_path = f"{output_dir}.tar.gz"
    check = sandbox.commands.run(
        f"if [ -d {shlex.quote(output_dir)} ] && "
        f'[ -n "$(ls -A {shlex.quote(output_dir)} 2>/dev/null)" ]; '
        f"then tar -czf {shlex.quote(archive_path)} -C {shlex.quote(output_dir)} . && echo yes; "
        f"else echo no; fi"
    )
    if check.stdout.strip() != "yes":
        return []

    data = sandbox.files.read(archive_path, format="bytes")
    local_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        tar.extractall(local_dir, members=members, filter="data")
        names = [m.name for m in members if m.isfile()]

    sandbox.commands.run(f"rm -rf {shlex.quote(output_dir)} {shlex.quote(archive_path)}")
    return names
