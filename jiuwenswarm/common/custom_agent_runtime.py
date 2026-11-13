# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Install user-uploaded third-party harness provider packages.

A custom agent is an openjiuwen harness provider wheel the user uploads from
the settings panel, optionally with the vendor CLI executable it drives. The
wheel is extracted (never pip-installed) into a private ``site-packages`` under
the user workspace, so frozen and source runs behave the same and the Python
environment stays untouched. openjiuwen finds the provider through the
``openjiuwen.harness_providers`` entry point once that directory is on
``sys.path``; :func:`activate_custom_agent_paths` puts it there.

Layout under ``<user workspace>/runtime/custom-agents``::

    agents.json                 registry: name -> distribution, version, summary, cli_path
    <name>/site-packages/       the extracted provider wheel
    <name>/bin/<executable>     the uploaded CLI executable, when one was given
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.external_cli_runtime import _safe_extract_wheel
from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

PROVIDER_ENTRY_POINT_GROUP = "openjiuwen.harness_providers"
_REGISTRY_FILE = "agents.json"
# Agent names become directory names and config keys.
_AGENT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Names the team already resolves to another backend; an uploaded provider
# under one of them would never be reached.
_BUILTIN_AGENT_NAMES = frozenset({"claude", "codex"})
_REGISTRY_LOCK = threading.Lock()


class CustomAgentError(ValueError):
    """An uploaded package cannot be installed as a custom agent."""


@dataclass(frozen=True)
class CustomAgent:
    """One installed custom agent.

    Attributes:
        name: Provider entry point name; also the team ``cli_agent`` name.
        distribution: Wheel distribution name.
        version: Wheel version.
        summary: Wheel summary line, shown as the settings hint.
        cli_path: Absolute path of the uploaded CLI executable, if any.
    """

    name: str
    distribution: str
    version: str
    summary: str = ""
    cli_path: str = ""


def custom_agents_root() -> Path:
    """Return the directory holding every custom agent and the registry."""
    return get_user_workspace_dir() / "runtime" / "custom-agents"


def custom_agent_site_packages(name: str) -> Path:
    """Return the private ``site-packages`` of custom agent ``name``."""
    return custom_agents_root() / name / "site-packages"


def list_custom_agents() -> list[CustomAgent]:
    """Return the installed custom agents in registry order."""
    return list(_read_registry().values())


def custom_agent_names() -> tuple[str, ...]:
    """Return the installed custom agent names."""
    return tuple(_read_registry())


def get_custom_agent(name: str) -> CustomAgent | None:
    """Return custom agent ``name``, or ``None`` when it is not installed."""
    return _read_registry().get(name)


def activate_custom_agent_paths() -> None:
    """Append every installed custom agent's ``site-packages`` to ``sys.path``.

    Idempotent; call it before openjiuwen resolves harness providers so a
    provider installed after process start becomes resolvable too.
    """
    added = False
    for name in custom_agent_names():
        path = str(custom_agent_site_packages(name))
        if path not in sys.path:
            sys.path.append(path)
            added = True
    if added:
        importlib.invalidate_caches()


def install_custom_agent(wheel_path: Path, *, cli_executable: Path | None = None) -> CustomAgent:
    """Install an uploaded provider wheel, replacing an agent of the same name.

    Args:
        wheel_path: The uploaded ``.whl`` file.
        cli_executable: Optional vendor CLI executable to install beside it.

    Returns:
        The installed agent.

    Raises:
        CustomAgentError: When the wheel declares no single provider entry
            point, the name is reserved or malformed, or the executable is
            missing.
    """
    if cli_executable is not None and not cli_executable.is_file():
        raise CustomAgentError(f"CLI executable not found: {cli_executable.name}")
    root = custom_agents_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".install-{uuid.uuid4().hex}"
    staging_site_packages = staging / "site-packages"
    staging_site_packages.mkdir(parents=True)
    try:
        try:
            _safe_extract_wheel(wheel_path, staging_site_packages)
        except RuntimeError as exc:
            raise CustomAgentError(f"not a valid wheel: {exc}") from exc
        agent = _describe_wheel(staging_site_packages)
        if cli_executable is not None:
            installed_cli = staging / "bin" / cli_executable.name
            installed_cli.parent.mkdir()
            shutil.copyfile(cli_executable, installed_cli)
            installed_cli.chmod(0o755)
            final_cli = root / agent.name / "bin" / cli_executable.name
            agent = CustomAgent(**{**asdict(agent), "cli_path": str(final_cli)})
        with _REGISTRY_LOCK:
            _replace_directory(root / agent.name, staging)
            registry = _read_registry()
            registry[agent.name] = agent
            _write_registry(registry)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info("[custom-agent] installed %s (%s %s)", agent.name, agent.distribution, agent.version)
    return agent


def remove_custom_agent(name: str) -> None:
    """Uninstall custom agent ``name``.

    Raises:
        CustomAgentError: When no custom agent has that name.
    """
    with _REGISTRY_LOCK:
        registry = _read_registry()
        if registry.pop(name, None) is None:
            raise CustomAgentError(f"custom agent not found: {name}")
        _write_registry(registry)
    shutil.rmtree(custom_agents_root() / name, ignore_errors=True)
    logger.info("[custom-agent] removed %s", name)


def team_mcp_server_command() -> list[str]:
    """Return the argv starting the team MCP stdio server for this process layout."""
    if getattr(sys, "frozen", False):
        executable = "openjiuwen-team-mcp.exe" if os.name == "nt" else "openjiuwen-team-mcp"
        return [str(Path(sys.executable).resolve().parent / executable)]
    return [sys.executable, "-m", "openjiuwen.agent_teams.mcp"]


def _describe_wheel(site_packages: Path) -> CustomAgent:
    providers: list[tuple[str, importlib.metadata.Distribution]] = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        distribution = importlib.metadata.Distribution.at(dist_info)
        for entry in distribution.entry_points.select(group=PROVIDER_ENTRY_POINT_GROUP):
            providers.append((entry.name, distribution))
    if len(providers) != 1:
        raise CustomAgentError(
            f"the wheel must declare exactly one {PROVIDER_ENTRY_POINT_GROUP} entry point, found {len(providers)}"
        )
    name, distribution = providers[0]
    if not _AGENT_NAME_PATTERN.fullmatch(name):
        raise CustomAgentError(f"provider name {name!r} must match {_AGENT_NAME_PATTERN.pattern}")
    if name in _reserved_agent_names():
        raise CustomAgentError(f"provider name {name!r} is reserved by a built-in agent")
    metadata = distribution.metadata
    return CustomAgent(
        name=name,
        distribution=str(metadata["Name"] or ""),
        version=str(metadata["Version"] or ""),
        summary=str(metadata["Summary"] or ""),
    )


def _reserved_agent_names() -> frozenset[str]:
    from openjiuwen.agent_teams.external.cli_agent.adapters import available_adapters
    from openjiuwen.harness_providers.factory import PROVIDER_NAMES

    return _BUILTIN_AGENT_NAMES | frozenset(available_adapters()) | frozenset(PROVIDER_NAMES)


def _replace_directory(target: Path, staging: Path) -> None:
    backup = target.parent / f".backup-{uuid.uuid4().hex}"
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except OSError:
        if backup.exists():
            os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _read_registry() -> dict[str, CustomAgent]:
    path = custom_agents_root() / _REGISTRY_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[custom-agent] ignoring unreadable registry %s: %s", path, exc)
        return {}
    agents: dict[str, CustomAgent] = {}
    if not isinstance(payload, dict):
        return agents
    for name, entry in payload.items():
        if isinstance(entry, dict) and _AGENT_NAME_PATTERN.fullmatch(str(name)):
            agents[name] = CustomAgent(**_registry_fields(name, entry))
    return agents


def _registry_fields(name: str, entry: dict[str, Any]) -> dict[str, str]:
    return {
        "name": name,
        "distribution": str(entry.get("distribution") or ""),
        "version": str(entry.get("version") or ""),
        "summary": str(entry.get("summary") or ""),
        "cli_path": str(entry.get("cli_path") or ""),
    }


def _write_registry(registry: dict[str, CustomAgent]) -> None:
    path = custom_agents_root() / _REGISTRY_FILE
    payload = {name: {k: v for k, v in asdict(agent).items() if k != "name"} for name, agent in registry.items()}
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "PROVIDER_ENTRY_POINT_GROUP",
    "CustomAgent",
    "CustomAgentError",
    "activate_custom_agent_paths",
    "custom_agent_names",
    "custom_agent_site_packages",
    "custom_agents_root",
    "get_custom_agent",
    "install_custom_agent",
    "list_custom_agents",
    "remove_custom_agent",
    "team_mcp_server_command",
]
