from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop inherited GIT_* variables so tests' throwaway repos stay throwaway.

    Git hooks run with GIT_INDEX_FILE (and friends) pointing at the outer
    commit. A test doing `git add` in a tmp repo would otherwise write entries
    into that index while storing the blobs in the tmp repo, breaking the
    commit with "invalid object ... Error building trees".
    """
    for name in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(name)
