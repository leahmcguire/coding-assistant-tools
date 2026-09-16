"""The sandbox template definition.

Building the `TemplateBuilder` and rendering it to a Dockerfile is pure and
local -- `Template.to_dockerfile` never contacts E2B -- so this checks the
baseline (Docker, Node, Supabase CLI, Playwright + Chromium) and a project's
own extras are actually in there, without building or spending anything.
"""

from __future__ import annotations

from e2b_webtest.config import TemplateConfig
from e2b_webtest.template import dockerfile_preview


def test_baseline_includes_docker_node_playwright():
    dockerfile = dockerfile_preview(TemplateConfig())
    assert "FROM ubuntu:24.04" in dockerfile
    assert "get.docker.com" in dockerfile
    assert "nodesource" in dockerfile
    assert "supabase" in dockerfile.lower()
    assert "playwright install" in dockerfile
    assert "chromium" in dockerfile


def test_project_apt_extras_are_appended():
    dockerfile = dockerfile_preview(TemplateConfig(apt=["ripgrep", "jq"]))
    assert "ripgrep" in dockerfile
    assert "jq" in dockerfile


def test_project_apt_extras_install_before_dropping_root():
    """`apt-get install` needs root; installing after `USER user` fails at build time."""
    dockerfile = dockerfile_preview(TemplateConfig(apt=["postgresql-client"]))
    apt_line = next(line for line in dockerfile.splitlines() if "postgresql-client" in line)
    user_line = next(line for line in dockerfile.splitlines() if line == "USER user")
    assert dockerfile.index(apt_line) < dockerfile.index(user_line)


def test_project_run_extras_are_appended():
    dockerfile = dockerfile_preview(TemplateConfig(run=["echo project-specific-setup"]))
    assert "echo project-specific-setup" in dockerfile


def test_project_run_extras_run_after_dropping_root():
    """Run extras get the sandbox's normal runtime user, not root."""
    dockerfile = dockerfile_preview(TemplateConfig(run=["echo project-specific-setup"]))
    run_line = next(line for line in dockerfile.splitlines() if "project-specific-setup" in line)
    user_line = next(line for line in dockerfile.splitlines() if line == "USER user")
    assert dockerfile.index(user_line) < dockerfile.index(run_line)


def test_no_extras_still_builds():
    dockerfile = dockerfile_preview(TemplateConfig())
    assert dockerfile  # doesn't raise, produces something
