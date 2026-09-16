"""Build the E2B template image.

Every project shares the same baseline (Docker, Node 20, the Supabase CLI, and
a Python venv with Playwright + Chromium for running `webapp-testing`-style
scripts), then a project's own `template.apt`/`template.run` extras are
layered on top. Building the `TemplateBuilder` object here is pure, local,
and network-free -- only `Template.build()` (called from `cli.py`) talks to
E2B.
"""

from __future__ import annotations

from e2b import Template
from e2b.template.main import TemplateBuilder

from .config import TemplateConfig

VENV_DIR = "/home/user/.venv"
VENV_PYTHON = f"{VENV_DIR}/bin/python3"

_BASE_APT = [
    "curl",
    "ca-certificates",
    "git",
    "build-essential",
    "python3",
    "python3-venv",
    "python3-pip",
]

# Nodesource's setup script adds the NodeSource apt repo for Node 20, then the
# normal `apt-get install` pulls the pinned version from it.
_INSTALL_NODE = (
    "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs"
)

_INSTALL_DOCKER = "curl -fsSL https://get.docker.com | sh && usermod -aG docker user"

# The Supabase CLI dropped npm-global installs; this is their documented
# Linux binary release.
_INSTALL_SUPABASE = (
    "curl -fsSL "
    "https://github.com/supabase/cli/releases/latest/download/supabase_linux_amd64.tar.gz "
    "| tar -xz -C /usr/local/bin"
)

_INSTALL_PLAYWRIGHT = (
    f"python3 -m venv {VENV_DIR} "
    f"&& {VENV_DIR}/bin/pip install --upgrade pip playwright "
    f"&& {VENV_DIR}/bin/playwright install --with-deps chromium"
)


def build_template(extra: TemplateConfig) -> TemplateBuilder:
    builder = (
        Template()
        .from_ubuntu_image("24.04")
        # Unlike a Dockerfile, `run_cmd`'s default user isn't root --
        # apt/usermod/system-package installs need it explicitly. `set_user`
        # is a standing instruction (like Dockerfile's USER), so this applies
        # to every command below until it's set back.
        .set_user("root")
        .apt_install(_BASE_APT)
        .run_cmd(_INSTALL_DOCKER)
        .run_cmd(_INSTALL_NODE)
        .run_cmd(_INSTALL_SUPABASE)
        .run_cmd(_INSTALL_PLAYWRIGHT)
    )
    # A project's own apt packages need root too -- installed here, while
    # still root, rather than after the switch back below.
    if extra.apt:
        builder = builder.apt_install(extra.apt)
    # Back to the sandbox's normal runtime user before any project `run`
    # extras, so ownership of what those commands create matches the user
    # `up`/`run-script` actually run commands as.
    builder = builder.set_user("user")
    for command in extra.run:
        builder = builder.run_cmd(command)
    return builder


def dockerfile_preview(extra: TemplateConfig) -> str:
    """What `build_template` would produce, without contacting E2B. Used by tests and `template build --dry-run`."""
    return Template.to_dockerfile(build_template(extra))
