"""Local record of the running sandbox: `.e2b-webtest/state.json`.

Never committed -- the same way `install-skill.sh` keeps its symlinks out of
git, the directory is added to `.git/info/exclude` the first time it's
written, rather than requiring you to remember a `.gitignore` entry.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR_NAME = ".e2b-webtest"
STATE_FILE_NAME = "state.json"


@dataclass
class State:
    sandbox_id: str
    template: str
    created_at: str
    timeout_at: str
    services: list[str] = field(default_factory=list)


def state_dir(project_root: Path) -> Path:
    return project_root / STATE_DIR_NAME


def state_path(project_root: Path) -> Path:
    return state_dir(project_root) / STATE_FILE_NAME


def save(project_root: Path, state: State) -> None:
    directory = state_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    state_path(project_root).write_text(json.dumps(asdict(state), indent=2) + "\n")
    _git_exclude(project_root, STATE_DIR_NAME)


def load(project_root: Path) -> State | None:
    path = state_path(project_root)
    if not path.is_file():
        return None
    return State(**json.loads(path.read_text()))


def clear(project_root: Path) -> None:
    path = state_path(project_root)
    if path.is_file():
        path.unlink()


def _git_exclude(project_root: Path, entry: str) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return  # not a git repo, or git isn't on PATH -- nothing to exclude
    exclude_file = Path(result.stdout.strip())
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    exclude_file.touch(exist_ok=True)
    line = f"/{entry}"
    if line not in exclude_file.read_text().splitlines():
        with exclude_file.open("a") as handle:
            handle.write(line + "\n")
