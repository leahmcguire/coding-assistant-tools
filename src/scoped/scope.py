"""The mutable session scope.

One `Scope` instance is created at startup and closed over by the PreToolUse
hook. Slash commands mutate it in place, so `/scope add` and `/unscope` take
effect on the very next tool call with no reconnect and no lost conversation.
Never rebind the instance or the hook's closure goes stale.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .filters import Filters

Mode = Literal["strict", "open"]


def normcase(path: Path) -> str:
    """Case-fold for comparison.

    APFS is case-insensitive by default, so `SRC/x.py` and `src/x.py` are one
    file but compare unequal as strings. Every containment test goes through
    this.
    """
    return os.path.normcase(str(path))


def is_under(path: Path, root: Path) -> bool:
    """Containment test that respects case-insensitive filesystems.

    `Path.is_relative_to` compares case-sensitively, which would let
    `/scope/SRC/secret.py` escape a `/scope/src` root on macOS.
    """
    path_key, root_key = normcase(path), normcase(root)
    if path_key == root_key:
        return True
    return path_key.startswith(root_key.rstrip(os.sep) + os.sep)


@dataclass
class Skipped:
    path: Path
    reason: str


@dataclass
class Scope:
    cwd: Path
    filters: Filters = field(default_factory=Filters)
    mode: Mode = "strict"
    roots: set[Path] = field(default_factory=set)
    files: set[Path] = field(default_factory=set)
    skipped: list[Skipped] = field(default_factory=list)

    # -- resolution -------------------------------------------------------

    def resolve(self, raw: str | Path) -> Path:
        """Absolute, symlink-free path. Relative inputs resolve against cwd.

        `Path.resolve()` collapses `..` and follows symlinks for the portion
        that exists, which is what stops `../../etc/passwd` and a symlink
        pointing out of scope from slipping past the containment test. It does
        not require the path to exist.
        """
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()

    # -- predicates -------------------------------------------------------

    def contains(self, raw: str | Path) -> bool:
        """Is this file in the manifest? The read/edit predicate."""
        target = normcase(self.resolve(raw))
        return any(normcase(known) == target for known in self.files)

    def under_root(self, raw: str | Path) -> bool:
        """Is this path inside a scoped directory? The search-root predicate."""
        path = self.resolve(raw)
        return any(is_under(path, root) for root in self.roots)

    def can_create(self, raw: str | Path) -> bool:
        """May a file that does not exist yet be created here?

        Scoping to a directory should let the agent add a test file to it, but
        must not thereby permit reading files the filters rejected -- so this
        is a separate, narrower predicate than `contains`.
        """
        path = self.resolve(raw)
        if not self.under_root(path):
            return False
        accepted, _ = self.filters.accept_name(path)
        return accepted

    # -- mutation ---------------------------------------------------------

    def note_created(self, raw: str | Path) -> None:
        """Fold a newly written file into the manifest so it can be read back."""
        self.files.add(self.resolve(raw))

    def add(self, args: list[str]) -> list[Path]:
        """Expand and merge. Returns only the newly added files."""
        before = {normcase(f) for f in self.files}
        for arg in args:
            path = self.resolve(arg)
            if path.is_dir():
                self.roots.add(path)
                for found in enumerate_dir(path):
                    self._offer(found)
            else:
                self._offer(path)
        return sorted(f for f in self.files if normcase(f) not in before)

    def remove(self, args: list[str]) -> list[Path]:
        """Narrow the scope. Removing a directory drops everything under it."""
        dropped: list[Path] = []
        for arg in args:
            path = self.resolve(arg)
            self.roots = {r for r in self.roots if not is_under(r, path)}
            for known in sorted(self.files):
                if is_under(known, path):
                    self.files.discard(known)
                    dropped.append(known)
        return dropped

    def _offer(self, path: Path) -> None:
        accepted, reason = self.filters.accept(path)
        if accepted:
            self.files.add(path)
        else:
            self.skipped.append(Skipped(path, reason))

    def set_mode(self, mode: Mode) -> None:
        self.mode = mode

    # -- reporting --------------------------------------------------------

    def summary(self) -> str:
        roots = ", ".join(sorted(str(self.relative(r)) for r in self.roots))
        where = f" under {roots}" if roots else ""
        return f"{len(self.files)} file(s){where}"

    def relative(self, path: Path) -> Path:
        try:
            return path.relative_to(self.cwd)
        except ValueError:
            return path

    def sorted_files(self) -> list[Path]:
        return sorted(self.files)


def enumerate_dir(directory: Path) -> list[Path]:
    """List candidate files in a directory, honouring .gitignore where possible.

    `git ls-files --cached --others --exclude-standard` gets exact gitignore
    semantics for free and still includes new untracked files. Outside a git
    repo it fails and we fall back to walking.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=directory,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return _walk(directory)

    paths = []
    for rel in result.stdout.split("\0"):
        if rel:
            candidate = (directory / rel).resolve()
            if candidate.is_file():
                paths.append(candidate)
    return paths


def _walk(directory: Path) -> list[Path]:
    found = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".venv"}]
        for name in names:
            found.append((Path(root) / name).resolve())
    return found
