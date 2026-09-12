"""First-contact rendering of the manifest.

Few files get generous previews; many files get a tree with just enough of each
to orient. Either way the model pulls full contents through the gated Read tool
on demand, so the first message stays cheap no matter how wide the scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .scope import Scope

FULL_PREVIEW_MAX_FILES = 5
FULL_PREVIEW_LINES = 100
TREE_PREVIEW_LINES = 10
DEFAULT_MAX_PREVIEW_FILES = 60


@dataclass
class PreviewConfig:
    full_preview_max_files: int = FULL_PREVIEW_MAX_FILES
    full_preview_lines: int = FULL_PREVIEW_LINES
    tree_preview_lines: int = TREE_PREVIEW_LINES
    max_preview_files: int = DEFAULT_MAX_PREVIEW_FILES


def read_lines(path: Path, limit: int) -> tuple[list[str], int]:
    """First `limit` lines, plus the true total."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return [], 0
    return lines[:limit], len(lines)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def render(scope: Scope, config: PreviewConfig | None = None) -> str:
    config = config or PreviewConfig()
    files = scope.sorted_files()

    if not files:
        return "No files are in scope."

    if len(files) <= config.full_preview_max_files:
        body = _render_full(scope, files, config)
    elif len(files) > config.max_preview_files:
        body = _render_tree_only(scope, files)
    else:
        body = _render_tree(scope, files, config)

    header = (
        f"These {len(files)} file(s) are the entire working set for this session. "
        "Previews are truncated -- use Read on any of them for the full contents."
    )
    return f"{header}\n\n{body}"


def _render_full(scope: Scope, files: list[Path], config: PreviewConfig) -> str:
    chunks = []
    for path in files:
        lines, total = read_lines(path, config.full_preview_lines)
        shown = "\n".join(lines)
        chunk = f"=== {scope.relative(path)} ({total} lines) ===\n{shown}"
        if total > config.full_preview_lines:
            chunk += (
                f"\n... {total - config.full_preview_lines} more lines -- use Read for the rest"
            )
        chunks.append(chunk)
    return "\n\n".join(chunks)


def _render_tree(scope: Scope, files: list[Path], config: PreviewConfig) -> str:
    out = []
    for directory, group in _group(scope, files):
        out.append(f"{directory}/")
        for path in group:
            lines, total = read_lines(path, config.tree_preview_lines)
            out.append(f"  {path.name} ({total} lines)")
            for line in lines:
                out.append(f"    | {line}")
            if total > config.tree_preview_lines:
                out.append(f"    | ... {total - config.tree_preview_lines} more lines")
    return "\n".join(out)


def _render_tree_only(scope: Scope, files: list[Path]) -> str:
    out = [
        (
            f"{len(files)} files -- too many to preview inline, so this is the file list "
            "only. Use Read on whichever ones you need."
        ),
        "",
    ]
    for directory, group in _group(scope, files):
        out.append(f"{directory}/")
        out.extend(f"  {path.name}" for path in group)
    return "\n".join(out)


def _group(scope: Scope, files: list[Path]) -> list[tuple[str, list[Path]]]:
    grouped: dict[str, list[Path]] = {}
    for path in files:
        parent = str(scope.relative(path).parent)
        grouped.setdefault(parent, []).append(path)
    return sorted(grouped.items())
