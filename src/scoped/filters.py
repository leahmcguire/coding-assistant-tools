"""File-type, size, and secret filtering.

Filtering runs at manifest-expansion time, so it shapes what is in scope. Since
the guard tests membership in the manifest, a file rejected here is unreadable
without any separate enforcement -- with one exception: `is_secret` is consulted
by the guard directly on every call, so secrets stay blocked even in open mode.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDES: list[str] = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/*.lock",
    "**/*.min.js",
    "**/*.min.css",
]

# Matched against the file's *name* only, case-insensitively. Enforced
# independently of scope mode -- `/unscope` does not switch these off.
SECRET_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*credentials*",
    "*secrets*",
    ".netrc",
    ".npmrc",
    ".pypirc",
]

SNIFF_BYTES = 8192


def _match_path(pattern: str, path: Path) -> bool:
    """Glob a pattern against a whole path.

    `fnmatch`'s `*` crosses directory separators, so collapsing `**/` to `*`
    gives us recursive-glob semantics that are permissive enough for exclusion
    rules without pulling in a pathspec dependency.
    """
    if "/" not in pattern:
        return fnmatch.fnmatch(path.name.lower(), pattern.lower())
    collapsed = pattern.replace("**/", "*").replace("/**", "/*")
    return fnmatch.fnmatch(path.as_posix().lower(), collapsed.lower())


@dataclass
class Filters:
    extensions: set[str] | None = None
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_bytes: int = 256_000
    secret_guard: bool = True

    def is_secret(self, path: Path) -> bool:
        """Name looks like a credential file. Checked on every tool call."""
        if not self.secret_guard:
            return False
        name = path.name.lower()
        return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)

    def accept_name(self, path: Path) -> tuple[bool, str]:
        """Path-only checks. Safe for files that do not exist yet."""
        if self.is_secret(path):
            return False, "looks like a secret"
        for pattern in self.exclude:
            if _match_path(pattern, path):
                return False, f"excluded by {pattern}"
        if self.extensions is not None and path.suffix.lower() not in self.extensions:
            return False, f"extension {path.suffix or '(none)'} not in filter"
        return True, ""

    def accept(self, path: Path) -> tuple[bool, str]:
        """Full check, including size and a binary sniff. Requires the file to exist."""
        ok, reason = self.accept_name(path)
        if not ok:
            return ok, reason
        try:
            size = path.stat().st_size
        except OSError as exc:
            return False, f"unreadable: {exc.strerror or exc}"
        if size > self.max_bytes:
            return False, f"{size:,} bytes over the {self.max_bytes:,} limit"
        if is_binary(path):
            return False, "binary"
        return True, ""


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(SNIFF_BYTES)
    except OSError:
        return True


def parse_extensions(raw: str | None) -> set[str] | None:
    """Turn `--ext .py,md` into {'.py', '.md'}."""
    if not raw:
        return None
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip().lower()
        if chunk:
            out.add(chunk if chunk.startswith(".") else f".{chunk}")
    return out or None
