# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway-owned early --dotenv/--name loading.

This is an independent copy of the startup loader. Gateway entry points
import it before other jiuwenswarm modules so ``JIUWENSWARM_DATA_DIR`` is
set before path resolution. The copy only loads an existing ``.env``.

``jiuwenswarm.dotenv_early`` stays the writer for a missing instance
bootstrap file (via ``instance_manager``). This module must not import
``instance_manager`` and must not create that file: two processes sharing
an instance would otherwise be able to emit different data directories.
While both processes are pointed at the same argv and the same existing
file, ``JIUWENSWARM_DATA_DIR`` is the line inside that file.

Preserved session keys and the gRPC defaults start identical to the mother
module so a desktop launch that spawns gateway keeps the injected ports.
Later divergence of those keys, the gRPC defaults, or the log component
name is local to this file.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# gRPC C-core hygiene — must be set BEFORE grpc initializes (it is imported
# lazily by the OTLP/otel exporter and chromadb). Gateway entry points import
# this module first, so setting it here covers the gateway process.
#
# When a process forks for tool subprocesses, grpc's pthread_atfork child
# handler floods the child's stderr with INFO lines. The forked child
# immediately exec()s away and never touches the inherited grpc channel, so
# disabling fork support removes the handler safely; VERBOSITY is lowered as
# defense in depth against other INFO-level C-core chatter.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

_early_logger = logging.getLogger("gateway.early")
if not _early_logger.handlers:
    _early_logger.addHandler(logging.StreamHandler(sys.stderr))
    _early_logger.setLevel(logging.WARNING)


def _early_warning(component_name: str, message: str) -> None:
    """Log early warning message to stderr."""
    _early_logger.warning("[%s] %s", component_name, message)


def _early_error(component_name: str, message: str) -> None:
    """Log early error message to stderr."""
    _early_logger.error("[%s] %s", component_name, message)


# Port / bind keys injected for the current launch session (desktop or
# jiuwenswarm-start). When a session flag is set, load_dotenv(override=True)
# must not clobber them with stale values from the instance .env.
DESKTOP_PRESERVED_ENV_KEYS = (
    "WEB_HOST",
    "WEB_PORT",
    "GATEWAY_PORT",
    "AGENT_SERVER_PORT",
    "AGENT_PORT",
    "FRONTEND_PORT",
)

# Electron owns these values for the lifetime of a desktop launch.
DESKTOP_BROWSER_PRESERVED_ENV_KEYS = (
    "BROWSER_DRIVER",
    "BROWSER_SHARED_CONTROL",
    "PLAYWRIGHT_MCP_COMMAND",
    "PLAYWRIGHT_MCP_ARGS",
    "PLAYWRIGHT_MCP_CDP_ENDPOINT",
    "PLAYWRIGHT_MCP_TARGET_ID",
    "PLAYWRIGHT_MCP_TARGET_RESOLVER",
    "PLAYWRIGHT_MCP_ENV_JSON",
)
ELECTRON_ENV_FLAG = "JIUWENSWARM_ELECTRON"

# Flag set by jiuwenswarm-start when it injects the resolved port group into
# child env. Mirrors JIUWENSWARM_DESKTOP=1 for the CLI launcher path.
CLI_PORTS_ENV_FLAG = "JIUWENSWARM_CLI_PORTS"


def _should_preserve_session_ports() -> bool:
    """True when this process was launched with an explicit session port remap."""
    return (
        os.environ.get("JIUWENSWARM_DESKTOP") == "1"
        or os.environ.get(CLI_PORTS_ENV_FLAG) == "1"
    )


def load_dotenv_runtime(dotenv_path: str | Path | None, *, override: bool = True) -> bool:
    """load_dotenv wrapper that keeps session-injected port env vars.

    Plain processes behave exactly like ``load_dotenv``. Under
    ``JIUWENSWARM_DESKTOP=1`` or ``JIUWENSWARM_CLI_PORTS=1``, any of
    ``DESKTOP_PRESERVED_ENV_KEYS`` already present in ``os.environ`` are
    restored after loading so the launcher's resolved port group survives
    ``override=True``. Electron launches additionally restore
    ``DESKTOP_BROWSER_PRESERVED_ENV_KEYS``.

    Also drops ``AGENT_SERVER_URL`` in those modes: Gateway prefers that URL
    over ``AGENT_SERVER_PORT``, so a stale value from .env/shell would bypass
    the remapped agent port. Without the URL, Gateway builds
    ``ws://{host}:{AGENT_SERVER_PORT}`` from the injected port.
    """
    from dotenv import load_dotenv

    preserve = _should_preserve_session_ports()
    preserved_keys = DESKTOP_PRESERVED_ENV_KEYS
    if os.environ.get(ELECTRON_ENV_FLAG) == "1":
        preserved_keys += DESKTOP_BROWSER_PRESERVED_ENV_KEYS
    saved = (
        {k: os.environ[k] for k in preserved_keys if k in os.environ}
        if preserve
        else {}
    )
    loaded = load_dotenv(dotenv_path=dotenv_path, override=override)
    if saved:
        os.environ.update(saved)
    if preserve:
        os.environ.pop("AGENT_SERVER_URL", None)
    return loaded


def parse_dotenv_early(component_name: str = "jiuwenswarm-gateway") -> Path | None:
    """Parse --dotenv/--name and load an existing env file.

    Scans ``sys.argv`` and does not remove the arguments. ``--dotenv`` wins
    over ``--name``. ``JIUWENSWARM_DATA_DIR`` comes from the loaded file.

    A missing bootstrap ``.env`` is reported and left uncreated. The mother
    loader remains the only writer of that file.

    Args:
        component_name: Name included in warning messages.

    Returns:
        Path to the loaded .env file, or None when nothing was loaded.
    """
    global _parsed_dotenv, _component_name
    _component_name = component_name
    dotenv_path = None
    name_value = None

    for i, arg in enumerate(sys.argv):
        if arg == "--dotenv" and i + 1 < len(sys.argv):
            dotenv_path = sys.argv[i + 1]
        elif arg == "--name" and i + 1 < len(sys.argv):
            name_value = sys.argv[i + 1]

    result: Path | None = None
    if dotenv_path is not None:
        dotenv_file = Path(dotenv_path).expanduser().resolve()
        if dotenv_file.exists():
            load_dotenv_runtime(dotenv_file, override=True)
            result = dotenv_file
        else:
            _early_warning(component_name, f"--dotenv file not found: {dotenv_file}")
    elif name_value is not None:
        result = _load_bootstrap_by_name_early(name_value, component_name)

    _parsed_dotenv = result
    return result


def _load_bootstrap_by_name_early(name: str, component_name: str) -> Path | None:
    """Load an existing instance bootstrap .env. Never creates one."""
    if not name or name.lower() in ("default", "config", "tmp"):
        _early_error(component_name, f"Invalid instance name '{name}'")
        return None

    user_home = os.environ.get("JIUWENSWARM_HOME") or Path.home()
    yaml_path = Path(user_home) / ".jiuwenswarm" / "instances.yaml"

    if not yaml_path.exists():
        _early_error(component_name, f"instances.yaml not found: {yaml_path}")
        _early_error(component_name, f"Run 'jiuwenswarm-init --name {name}' to create it.")
        return None

    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        instances = data.get("instances", {}) if data else {}

        if name not in instances:
            _early_error(component_name, f"Instance '{name}' not found in instances.yaml")
            _early_error(component_name, f"Run 'jiuwenswarm-init --name {name}' to create it.")
            return None

        inst_data = instances.get(name) or {}
        workspace_str = inst_data.get("workspace")

        if workspace_str:
            workspace = Path(workspace_str).expanduser().resolve()
        else:
            instances_dir = Path(user_home) / ".jiuwenswarm-instances"
            workspace = instances_dir / name

    except Exception as exc:
        _early_error(component_name, f"Failed to parse instances.yaml: {exc}")
        return None

    if not workspace.exists():
        _early_error(component_name, f"Workspace directory not found: {workspace}")
        _early_error(component_name, f"Run 'jiuwenswarm-init --name {name}' to create it.")
        return None

    bootstrap_env = workspace / ".env"
    if not bootstrap_env.exists():
        _early_warning(component_name, f"Bootstrap .env not found: {bootstrap_env}")
        _early_warning(
            component_name,
            f"Run 'jiuwenswarm-init --name {name}' to create it.",
        )
        return None

    load_dotenv_runtime(bootstrap_env, override=True)
    return bootstrap_env


_parsed_dotenv: Path | None = None
_component_name: str = "jiuwenswarm-gateway"


def get_parsed_dotenv() -> Path | None:
    """Get the path that was parsed, if any."""
    return _parsed_dotenv


__all__ = [
    "CLI_PORTS_ENV_FLAG",
    "DESKTOP_BROWSER_PRESERVED_ENV_KEYS",
    "DESKTOP_PRESERVED_ENV_KEYS",
    "ELECTRON_ENV_FLAG",
    "parse_dotenv_early",
    "load_dotenv_runtime",
    "get_parsed_dotenv",
]
