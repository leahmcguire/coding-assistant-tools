from __future__ import annotations

from pathlib import Path

from scoped.preview import PreviewConfig, estimate_tokens, render
from scoped.scope import Scope


def make_repo(tmp_path: Path, count: int, lines: int = 200) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    for i in range(count):
        (src / f"mod{i:02d}.py").write_text("".join(f"line {n}\n" for n in range(lines)))
    return tmp_path


def scope_for(tmp_path: Path, count: int, lines: int = 200) -> Scope:
    repo = make_repo(tmp_path, count, lines)
    s = Scope(cwd=repo)
    s.add(["src"])
    return s


def test_five_or_fewer_files_get_full_previews(tmp_path: Path):
    out = render(scope_for(tmp_path, 3))
    assert out.count("=== ") == 3
    assert "line 99" in out  # 100th line present
    assert "line 100" not in out  # 101st is not
    assert "100 more lines" in out


def test_more_than_five_files_get_a_tree(tmp_path: Path):
    out = render(scope_for(tmp_path, 8))
    assert "=== " not in out
    assert "src/" in out
    assert "    | line 9" in out  # 10th line present
    assert "    | line 10\n" not in out  # 11th is not
    assert "190 more lines" in out


def test_short_files_are_not_marked_truncated(tmp_path: Path):
    out = render(scope_for(tmp_path, 2, lines=4))
    assert "more lines" not in out


def test_very_wide_scope_degrades_to_a_file_list(tmp_path: Path):
    out = render(scope_for(tmp_path, 12), PreviewConfig(max_preview_files=10))
    assert "too many to preview inline" in out
    assert "| line 0" not in out
    assert "mod11.py" in out


def test_empty_scope(tmp_path: Path):
    assert render(Scope(cwd=tmp_path)) == "No files are in scope."


def test_header_states_previews_are_truncated(tmp_path: Path):
    out = render(scope_for(tmp_path, 3))
    assert "entire working set" in out
    assert "use Read" in out


def test_token_estimate_scales_with_width(tmp_path: Path):
    narrow = estimate_tokens(render(scope_for(tmp_path / "a", 3)))
    wide = estimate_tokens(render(scope_for(tmp_path / "b", 20)))
    assert narrow > 0 and wide > narrow
