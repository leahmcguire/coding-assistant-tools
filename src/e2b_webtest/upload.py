"""What gets uploaded into the sandbox.

The manifest is the project's git-tracked (and not-yet-ignored) files, taken
from `git ls-files` and filtered by `upload.exclude`, plus `upload.extra`
files named explicitly (e.g. a seed dump you made yourself). `.env*` is
always dropped, regardless of what the config says -- this is the one place
that enforces it for real, since `config.py` only catches it when a file is
named directly in `upload.extra`.
"""

from __future__ import annotations

import fnmatch
import io
import subprocess
import tarfile
from pathlib import Path

from .config import UploadConfig, env_like


class UploadError(RuntimeError):
    """A file the manifest wants can't be read, or `git` failed."""


def _match_path(pattern: str, path: str) -> bool:
    """Same recursive-glob-over-fnmatch approach as `scoped/filters.py`."""
    if "/" not in pattern:
        return fnmatch.fnmatch(Path(path).name.lower(), pattern.lower())
    collapsed = pattern.replace("**/", "*").replace("/**", "/*")
    return fnmatch.fnmatch(path.lower(), collapsed.lower())


def git_tracked_files(project_root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files, as project-relative posix paths."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise UploadError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise UploadError(f"git ls-files failed: {exc.stderr.strip()}") from exc
    return [line for line in result.stdout.splitlines() if line]


def manifest(project_root: Path, cfg: UploadConfig) -> list[str]:
    """The final list of project-relative paths to upload, `.env*` always excluded."""
    tracked = git_tracked_files(project_root)
    kept = [
        path
        for path in tracked
        if not env_like(path) and not any(_match_path(p, path) for p in cfg.exclude)
    ]
    extra = [path for path in cfg.extra if not env_like(path)]
    # A file named twice (tracked and also listed in `extra`) is only packed once.
    return sorted(set(kept) | set(extra))


def build_archive(project_root: Path, paths: list[str]) -> bytes:
    """Tar the given project-relative paths, reading them from `project_root`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel_path in paths:
            full_path = project_root / rel_path
            if not full_path.is_file():
                raise UploadError(f"{rel_path} is in the manifest but is not a file")
            tar.add(full_path, arcname=rel_path)
    return buffer.getvalue()


def upload_to_sandbox(sandbox, project_root: Path, cfg: UploadConfig) -> None:
    """Build the archive locally, write it into the sandbox, and extract it there.

    The only sandbox-touching function in this module; everything above is
    pure and covered by local unit tests.
    """
    paths = manifest(project_root, cfg)
    archive = build_archive(project_root, paths)
    archive_path = f"{cfg.dest}.tar.gz"
    sandbox.files.write(archive_path, archive)
    sandbox.commands.run(
        f"mkdir -p {cfg.dest} && tar -xzf {archive_path} -C {cfg.dest} && rm {archive_path}",
        timeout=120,
    )
