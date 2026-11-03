# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for user-uploaded custom agents (third-party harness providers)."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.custom_agent_upload_http import handle_custom_agent_upload_http
from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import custom_agent_runtime as runtime
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.agents.harness.team.config_loader import _prepare_external_cli_agents


def _wheel(directory: Path, *, entry_points: tuple[str, ...] = ("acme",), summary: str = "Acme agent") -> Path:
    """Build a minimal provider wheel declaring ``entry_points`` in the provider group."""
    wheel = directory / "openjiuwen_harness_acme-0.2.0-py3-none-any.whl"
    dist_info = "openjiuwen_harness_acme-0.2.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openjiuwen_harness_acme/__init__.py", "")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: openjiuwen-harness-acme\nVersion: 0.2.0\nSummary: {summary}\n",
        )
        if entry_points:
            lines = "".join(f"{name} = openjiuwen_harness_acme:Provider\n" for name in entry_points)
            archive.writestr(f"{dist_info}/entry_points.txt", f"[{runtime.PROVIDER_ENTRY_POINT_GROUP}]\n{lines}")
    return wheel


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from jiuwenswarm.common import external_cli_catalog

    home = tmp_path / "home"
    monkeypatch.setattr(runtime, "get_user_workspace_dir", lambda: home)
    monkeypatch.setattr(external_cli_catalog, "get_user_workspace_dir", lambda: home)
    return home


def test_install_extracts_the_wheel_and_registers_the_agent(workspace: Path, tmp_path: Path) -> None:
    cli = tmp_path / "acme-cli"
    cli.write_bytes(b"#!/bin/sh\n")

    agent = runtime.install_custom_agent(_wheel(tmp_path), cli_executable=cli)

    assert agent.name == "acme"
    assert agent.distribution == "openjiuwen-harness-acme"
    assert agent.version == "0.2.0"
    assert agent.summary == "Acme agent"
    assert Path(agent.cli_path) == runtime.custom_agents_root() / "acme" / "bin" / "acme-cli"
    if os.name != "nt":
        assert os.stat(agent.cli_path).st_mode & 0o111
    assert (runtime.custom_agent_site_packages("acme") / "openjiuwen_harness_acme" / "__init__.py").is_file()
    assert runtime.list_custom_agents() == [agent]
    assert not list(runtime.custom_agents_root().glob(".install-*"))


def test_activation_makes_the_provider_entry_point_visible(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    runtime.install_custom_agent(_wheel(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))

    runtime.activate_custom_agent_paths()
    runtime.activate_custom_agent_paths()

    site_packages = str(runtime.custom_agent_site_packages("acme"))
    assert sys.path.count(site_packages) == 1
    found = importlib.metadata.entry_points(group=runtime.PROVIDER_ENTRY_POINT_GROUP, name="acme")
    assert [entry.value for entry in found] == ["openjiuwen_harness_acme:Provider"]


def test_reinstall_replaces_the_agent_and_drops_a_stale_cli(workspace: Path, tmp_path: Path) -> None:
    cli = tmp_path / "acme-cli"
    cli.write_bytes(b"#!/bin/sh\n")
    runtime.install_custom_agent(_wheel(tmp_path), cli_executable=cli)

    agent = runtime.install_custom_agent(_wheel(tmp_path, summary="Acme v2"))

    assert agent.cli_path == ""
    assert runtime.get_custom_agent("acme").summary == "Acme v2"
    assert not (runtime.custom_agents_root() / "acme" / "bin").exists()


@pytest.mark.parametrize(
    ("entry_points", "message"),
    [
        ((), "exactly one"),
        (("acme", "other"), "exactly one"),
        (("claude",), "reserved"),
        (("gemini",), "reserved"),
        (("Bad.Name",), "must match"),
    ],
)
def test_install_rejects_wheels_the_team_cannot_address(
    workspace: Path,
    tmp_path: Path,
    entry_points: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(runtime.CustomAgentError, match=message):
        runtime.install_custom_agent(_wheel(tmp_path, entry_points=entry_points))

    assert runtime.list_custom_agents() == []


def test_remove_uninstalls_the_agent(workspace: Path, tmp_path: Path) -> None:
    runtime.install_custom_agent(_wheel(tmp_path))

    runtime.remove_custom_agent("acme")

    assert runtime.list_custom_agents() == []
    assert not (runtime.custom_agents_root() / "acme").exists()
    with pytest.raises(runtime.CustomAgentError, match="not found"):
        runtime.remove_custom_agent("acme")


def test_settings_panel_lists_custom_agent_switches(workspace: Path, tmp_path: Path) -> None:
    cli = tmp_path / "acme-cli"
    cli.write_bytes(b"#!/bin/sh\n")
    agent = runtime.install_custom_agent(_wheel(tmp_path), cli_executable=cli)
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": [{"cli_agent": "acme", "cli_path": agent.cli_path}]}}}}

    flat = handlers.flatten_external_cli_agents_for_config_panel(raw)

    # Running the uploaded CLI is the agent's "built-in CLI" choice.
    assert flat["external_cli_agent_acme_enabled"] == "true"
    assert flat["external_cli_agent_acme_use_builtin"] == "true"
    assert flat["external_cli_agent_acme_cli_path"] == ""
    assert flat["external_cli_agent_claude_enabled"] == "false"
    assert handlers.is_external_cli_agent_config_key("external_cli_agent_acme_cli_path")
    assert not handlers.is_external_cli_agent_config_key("external_cli_agent_other_cli_path")
    assert '"has_cli": true' in handlers.custom_agents_config_value()


def test_enabling_a_custom_agent_with_its_uploaded_cli_stores_that_path(workspace: Path, tmp_path: Path) -> None:
    cli = tmp_path / "acme-cli"
    cli.write_bytes(b"#!/bin/sh\n")
    agent = runtime.install_custom_agent(_wheel(tmp_path), cli_executable=cli)

    agents = handlers.external_cli_agents_from_switches(
        {},
        {"external_cli_agent_acme_enabled": "true", "external_cli_agent_acme_use_builtin": "true"},
    )

    assert agents == [{"cli_agent": "acme", "cli_path": agent.cli_path}]


def test_custom_agent_without_a_cli_leaves_the_choice_to_the_provider(workspace: Path, tmp_path: Path) -> None:
    runtime.install_custom_agent(_wheel(tmp_path))

    result = handlers.detect_external_cli_agent("acme")
    agents = handlers.external_cli_agents_from_switches(
        {},
        {"external_cli_agent_acme_enabled": "true", "external_cli_agent_acme_use_builtin": "true"},
    )

    assert result["status"] == "ok"
    assert result["reason"] == "provider_managed"
    assert agents == [{"cli_agent": "acme"}]


def test_team_config_accepts_installed_custom_agents_and_needs_the_hybrid_transport(
    workspace: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be one of: claude, codex"):
        config_module._normalize_external_cli_agents([{"cli_agent": "acme"}], "external_cli_agents")
    runtime.install_custom_agent(_wheel(tmp_path))

    normalized = config_module._normalize_external_cli_agents(
        [{"cli_agent": "acme", "cli_path": "/opt/acme"}],
        "external_cli_agents",
    )

    assert normalized == [{"cli_agent": "acme", "cli_path": "/opt/acme"}]
    assert config_module._requires_external_transport(normalized)
    assert not config_module._requires_external_transport([{"cli_agent": "claude"}])


def test_team_spec_gets_the_custom_agent_paths_and_team_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    runtime.install_custom_agent(_wheel(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = {"external_cli_agents": [{"cli_agent": "acme"}, {"cli_agent": "claude"}]}

    _prepare_external_cli_agents(spec)

    assert spec["external_cli_agents"][0]["mcp_server_command"] == runtime.team_mcp_server_command()
    assert "mcp_server_command" not in spec["external_cli_agents"][1]
    assert str(runtime.custom_agent_site_packages("acme")) in sys.path


def _multipart(files: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = "custom-agent-boundary"
    body = b""
    for field, (filename, content) in files.items():
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def test_upload_installs_the_wheel_and_cli(workspace: Path, tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    content_type, body = _multipart({"wheel": (wheel.name, wheel.read_bytes()), "cli": ("acme", b"#!/bin/sh\n")})

    status, payload = handle_custom_agent_upload_http(content_type=content_type, body=body)

    assert status == 200
    assert payload == {"agent": {"name": "acme", "version": "0.2.0", "summary": "Acme agent", "has_cli": True}}


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"cli": ("acme", b"x")}, "missing wheel"),
        ({"wheel": ("acme.zip", b"x")}, ".whl"),
        ({"wheel": ("acme.whl", b"not a zip")}, "not a valid wheel"),
    ],
)
def test_upload_rejects_invalid_packages(
    workspace: Path,
    files: dict[str, tuple[str, bytes]],
    message: str,
) -> None:
    content_type, body = _multipart(files)

    status, payload = handle_custom_agent_upload_http(content_type=content_type, body=body)

    assert status == 400
    assert message in payload["error"]
    assert runtime.list_custom_agents() == []
