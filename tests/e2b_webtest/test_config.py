"""`.e2b-webtest.toml` parsing and validation.

The central claim: a malformed config fails at load time with a message
naming the file and the key, never three steps into `up`. And a config that
names an env file in `upload.extra` is refused outright.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from e2b_webtest.config import ConfigError, env_like, load

MINIMAL = """
[sandbox]
template = "my-template"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".e2b-webtest.toml"
    path.write_text(text)
    return path


def test_minimal_config_uses_defaults(tmp_path: Path):
    cfg = load(write(tmp_path, MINIMAL))
    assert cfg.sandbox.template == "my-template"
    assert cfg.sandbox.vcpu == 2
    assert cfg.sandbox.memory_mb == 2048
    assert cfg.sandbox.timeout == 3600
    assert cfg.upload.exclude == []
    assert cfg.env.forward == []
    assert cfg.setup == []
    assert cfg.services == []


def test_missing_template_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="sandbox.template"):
        load(write(tmp_path, "[sandbox]\n"))


def test_not_toml_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(write(tmp_path, "this is not [ valid"))


def test_wrong_type_is_a_config_error(tmp_path: Path):
    text = '[sandbox]\ntemplate = "x"\nvcpu = "four"\n'
    with pytest.raises(ConfigError, match="vcpu"):
        load(write(tmp_path, text))


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production"])
def test_upload_extra_rejects_env_files(tmp_path: Path, name: str):
    text = MINIMAL + f'\n[upload]\nextra = ["backend/{name}"]\n'
    with pytest.raises(ConfigError, match="env file"):
        load(write(tmp_path, text))


def test_upload_extra_accepts_non_env_files(tmp_path: Path):
    text = MINIMAL + '\n[upload]\nextra = [".e2b-webtest/seed.sql"]\n'
    cfg = load(write(tmp_path, text))
    assert cfg.upload.extra == [".e2b-webtest/seed.sql"]


def test_setup_steps_parse(tmp_path: Path):
    text = (
        MINIMAL
        + """
[[setup]]
cmd = ["supabase", "start"]
cwd = "supabase"

[[setup]]
cmd = ["supabase", "status", "-o", "env"]
capture_env = true
"""
    )
    cfg = load(write(tmp_path, text))
    assert [s.cmd for s in cfg.setup] == [
        ["supabase", "start"],
        ["supabase", "status", "-o", "env"],
    ]
    assert cfg.setup[0].cwd == "supabase"
    assert cfg.setup[1].capture_env is True
    assert cfg.setup[0].capture_env is False


def test_non_string_cwd_is_a_config_error(tmp_path: Path):
    text = MINIMAL + '\n[[setup]]\ncmd = ["true"]\ncwd = 5\n'
    with pytest.raises(ConfigError, match="cwd"):
        load(write(tmp_path, text))


def test_service_requires_name(tmp_path: Path):
    text = MINIMAL + '\n[[services]]\ncmd = ["true"]\n'
    with pytest.raises(ConfigError, match="name"):
        load(write(tmp_path, text))


def test_duplicate_service_names_rejected(tmp_path: Path):
    text = (
        MINIMAL
        + """
[[services]]
name = "backend"
cmd = ["true"]

[[services]]
name = "backend"
cmd = ["false"]
"""
    )
    with pytest.raises(ConfigError, match="unique"):
        load(write(tmp_path, text))


def test_service_ready_http_and_cmd_are_mutually_exclusive(tmp_path: Path):
    text = (
        MINIMAL
        + """
[[services]]
name = "backend"
cmd = ["true"]
[services.ready]
http = "http://127.0.0.1:8000/health"
cmd = ["pg_isready"]
"""
    )
    with pytest.raises(ConfigError, match="http or cmd"):
        load(write(tmp_path, text))


def test_service_ready_defaults(tmp_path: Path):
    text = (
        MINIMAL
        + """
[[services]]
name = "web"
cmd = ["npx", "serve"]
"""
    )
    cfg = load(write(tmp_path, text))
    ready = cfg.services[0].ready
    assert ready.http is None
    assert ready.cmd is None
    assert ready.timeout == 60
    assert ready.interval == 2


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", True),
        (".env.local", True),
        ("backend/.env", True),
        ("backend/.envrc", True),  # direnv often sources real secrets directly
        (".environment", True),  # over-matching a look-alike name is the safe direction
        ("supabase/.env.example", True),
        ("seed.sql", False),
    ],
)
def test_env_like(path: str, expected: bool):
    assert env_like(path) is expected
