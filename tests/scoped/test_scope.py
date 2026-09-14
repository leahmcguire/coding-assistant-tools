"""Tests for containment and filtering.

`Scope.contains` is the predicate the whole tool rests on -- if it is wrong,
nothing above it matters.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scoped.filters import Filters, parse_extensions
from scoped.scope import Scope, is_under


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "in_scope").mkdir()
    (tmp_path / "in_scope" / "a.py").write_text("print('a')\n")
    (tmp_path / "in_scope" / "notes.md").write_text("# notes\n")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "b.py").write_text("TOKEN = 'hunter2'\n")
    (tmp_path / ".env").write_text("API_KEY=abc123\n")
    return tmp_path


@pytest.fixture
def scope(repo: Path) -> Scope:
    s = Scope(cwd=repo)
    s.add(["in_scope"])
    return s


def test_in_scope_file_is_contained(scope: Scope, repo: Path):
    assert scope.contains(repo / "in_scope" / "a.py")
    assert scope.contains("in_scope/a.py")


def test_out_of_scope_file_is_not(scope: Scope, repo: Path):
    assert not scope.contains(repo / "secret" / "b.py")


def test_dotdot_traversal_is_rejected(scope: Scope):
    assert not scope.contains("in_scope/../secret/b.py")
    assert not scope.contains("in_scope/../../etc/passwd")


def test_symlink_pointing_out_of_scope_is_rejected(scope: Scope, repo: Path):
    link = repo / "in_scope" / "escape.py"
    link.symlink_to(repo / "secret" / "b.py")
    # Even though the link itself sits inside the scoped directory, resolving it
    # lands outside, and the manifest never contained the target.
    assert not scope.contains(link)


def test_absolute_path_outside_is_rejected(scope: Scope):
    assert not scope.contains("/etc/passwd")


def test_case_insensitive_paths_compare_equal(scope: Scope, repo: Path):
    """On APFS, SRC/x.py and src/x.py are one file. Cheap to get wrong."""
    shouty = repo / "IN_SCOPE" / "a.py"
    assert is_under(shouty, repo / "in_scope") == (os.path.normcase("A") == os.path.normcase("a"))


def test_new_file_in_scoped_dir_can_be_created(scope: Scope, repo: Path):
    target = repo / "in_scope" / "test_new.py"
    assert not scope.contains(target)  # does not exist yet
    assert scope.can_create(target)


def test_new_file_outside_scope_cannot_be_created(scope: Scope, repo: Path):
    assert not scope.can_create(repo / "secret" / "test_new.py")


def test_created_file_becomes_readable(scope: Scope, repo: Path):
    target = repo / "in_scope" / "test_new.py"
    scope.note_created(target)
    assert scope.contains(target)


def test_secret_files_never_enter_the_manifest(repo: Path):
    s = Scope(cwd=repo)
    s.add(["."])
    assert not any(f.name == ".env" for f in s.files)
    assert s.filters.is_secret(repo / ".env")


def test_can_create_refuses_secret_names(scope: Scope, repo: Path):
    assert not scope.can_create(repo / "in_scope" / ".env")


def test_extension_filter(repo: Path):
    s = Scope(cwd=repo, filters=Filters(extensions={".py"}))
    s.add(["in_scope"])
    names = {f.name for f in s.files}
    assert names == {"a.py"}


def test_size_cap(repo: Path):
    big = repo / "in_scope" / "big.py"
    big.write_text("x = 1\n" * 50_000)
    s = Scope(cwd=repo, filters=Filters(max_bytes=1000))
    s.add(["in_scope"])
    assert big not in s.files
    assert any("over the" in skip.reason for skip in s.skipped)


def test_binary_files_are_skipped(repo: Path):
    blob = repo / "in_scope" / "data.py"
    blob.write_bytes(b"\x89PNG\x00\x00binary")
    s = Scope(cwd=repo)
    s.add(["in_scope"])
    assert blob not in s.files


def test_gitignored_files_are_skipped(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "kept.py").write_text("1\n")
    (tmp_path / "ignored.py").write_text("2\n")
    s = Scope(cwd=tmp_path)
    s.add(["."])
    names = {f.name for f in s.files}
    assert "kept.py" in names
    assert "ignored.py" not in names


def test_remove_drops_a_directory(scope: Scope, repo: Path):
    assert scope.files
    scope.remove(["in_scope"])
    assert not scope.files
    assert not scope.contains(repo / "in_scope" / "a.py")


def test_add_returns_only_new_files(scope: Scope, repo: Path):
    added = scope.add(["in_scope"])
    assert added == []  # already there
    added = scope.add(["secret"])
    assert [p.name for p in added] == ["b.py"]


def test_missing_path_is_skipped_as_nonexistent(repo: Path):
    s = Scope(cwd=repo)
    s.add(["nowhere"])
    assert not s.files
    assert [skip.reason for skip in s.skipped] == ["does not exist"]


def test_leading_slash_suggests_the_relative_path(repo: Path):
    s = Scope(cwd=repo)
    s.add(["/in_scope"])
    assert not s.files
    assert s.skipped[0].reason == "does not exist (did you mean 'in_scope'?)"


def test_no_slash_hint_when_relative_path_is_also_missing(repo: Path):
    assert Scope(cwd=repo).slash_hint("/nowhere") == ""


def test_parse_extensions():
    assert parse_extensions(".py,md") == {".py", ".md"}
    assert parse_extensions("") is None
    assert parse_extensions(None) is None
