"""What ends up in the sandbox.

The central claim: `.env*` never appears in the upload manifest, no matter
what the config says -- not from the git-tracked set, and not from an
`upload.extra` entry that slips past `config.py`'s own check (e.g. a config
built directly, bypassing `load()`).
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from e2b_webtest.config import UploadConfig
from e2b_webtest.upload import UploadError, build_archive, manifest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text("print('hi')\n")
    (tmp_path / "backend" / ".env").write_text("SECRET=1\n")
    (tmp_path / ".env.local").write_text("SECRET=2\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("// dep\n")
    (tmp_path / "README.md").write_text("# hi\n")
    return tmp_path


def test_manifest_always_excludes_env_files(repo: Path):
    result = manifest(repo, UploadConfig())
    assert "backend/.env" not in result
    assert ".env.local" not in result
    assert "backend/app.py" in result
    assert "README.md" in result


def test_manifest_respects_exclude_patterns(repo: Path):
    result = manifest(repo, UploadConfig(exclude=["**/node_modules/**"]))
    assert "node_modules/pkg.js" not in result
    assert "backend/app.py" in result


def test_manifest_includes_extra_files_not_tracked_by_git(repo: Path):
    (repo / ".e2b-webtest").mkdir()
    (repo / ".e2b-webtest" / "seed.sql").write_text("insert into x values (1);\n")
    result = manifest(repo, UploadConfig(extra=[".e2b-webtest/seed.sql"]))
    assert ".e2b-webtest/seed.sql" in result


def test_manifest_excludes_env_file_named_in_extra_even_if_config_bypassed_validation(repo: Path):
    """`config.load` already rejects this; `manifest` enforces it again independently."""
    cfg = UploadConfig(extra=["backend/.env"])
    result = manifest(repo, cfg)
    assert "backend/.env" not in result


def test_manifest_deduplicates_a_file_listed_both_ways(repo: Path):
    result = manifest(repo, UploadConfig(extra=["README.md"]))
    assert result.count("README.md") == 1


def test_git_not_a_repo_raises_upload_error(tmp_path: Path):
    with pytest.raises(UploadError):
        manifest(tmp_path, UploadConfig())


def test_build_archive_contains_exactly_the_manifest(repo: Path):
    paths = manifest(repo, UploadConfig())
    data = build_archive(repo, paths)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
    assert names == set(paths)
    assert "backend/.env" not in names
