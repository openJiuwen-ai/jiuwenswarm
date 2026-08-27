# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Early --dotenv/--name parsing for multi-instance isolation.

This module MUST be imported BEFORE any other jiuwenswarm modules,
because it sets JIUWENSWARM_DATA_DIR environment variable that affects
path resolution in jiuwenswarm.utils.

Usage in entry point files:
    from jiuwenswarm.dotenv_early import parse_dotenv_early
    parse_dotenv_early()

    # Now safe to import other jiuwenswarm modules
    from jiuwenswarm.common.utils import ...

The parsing happens before any jiuwenswarm imports:
- sys.argv is scanned for --dotenv <path> and --name <name>
- If --dotenv found: load that file
- If --name found (no --dotenv): load instance bootstrap .env
- JIUWENSWARM_DATA_DIR is injected into os.environ
- Then get_user_workspace_dir() returns the correct instance path

IMPORTANT: This ensures module-level code uses correct workspace path.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# gRPC C-core hygiene — must be set BEFORE grpc initializes (it is imported
# lazily by the OTLP/otel exporter and chromadb). This module is the first
# jiuwenswarm import in every entrypoint, so setting it here guarantees grpc
# reads it at init.
#
# When the agent server forks for tool subprocesses (e.g. the bash tool running
# ``curl | python3``), grpc's pthread_atfork child handler floods the child's
# stderr with INFO lines like::
#
#   I.... ev_poll_posix.cc:593] FD from fork parent still in poll list: fd(26, ...)
#
# which the tool captures via 2>&1 and mixes into its result. The forked child
# immediately exec()s away and never touches the inherited grpc channel, so
# disabling fork support removes the handler (and the noise) safely; VERBOSITY
# is lowered as defense in depth against other INFO-level C-core chatter.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

# Early logger for startup diagnostics (outputs to stderr)
_early_logger = logging.getLogger("jiuwenswarm.early")
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
# must not clobber them with stale values from ~/.jiuwenswarm/config/.env
# (e.g. a prior CLI port-fallback residue such as GATEWAY_PORT=20001).
DESKTOP_PRESERVED_ENV_KEYS = (
    "WEB_HOST",
    "WEB_PORT",
    "GATEWAY_PORT",
    "AGENT_SERVER_PORT",
    "AGENT_PORT",
    "FRONTEND_PORT",
)

# Flag set by jiuwenswarm-start when it injects the resolved port group into
# child env. Mirrors JIUWENSWARM_DESKTOP=1 for the CLI launcher path (issue #2749).
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
    ``override=True`` (avoids banner vs Gateway bind mismatch, issue #2749).

    Also drops ``AGENT_SERVER_URL`` in those modes: Gateway prefers that URL
    over ``AGENT_SERVER_PORT``, so a stale value from .env/shell would bypass
    the remapped agent port. Without the URL, Gateway builds
    ``ws://{host}:{AGENT_SERVER_PORT}`` from the injected port.
    """
    from dotenv import load_dotenv

    preserve = _should_preserve_session_ports()
    saved = (
        {k: os.environ[k] for k in DESKTOP_PRESERVED_ENV_KEYS if k in os.environ}
        if preserve
        else {}
    )
    loaded = load_dotenv(dotenv_path=dotenv_path, override=override)
    if saved:
        os.environ.update(saved)
    if preserve:
        # Prefer remapped AGENT_SERVER_PORT over any URL from .env/parent env.
        os.environ.pop("AGENT_SERVER_URL", None)
    return loaded


def parse_dotenv_early(component_name: str = "jiuwenswarm") -> Path | None:
    """Parse --dotenv/--name arguments and load env before jiuwenswarm imports.

    This function scans sys.argv for '--dotenv <path>' and '--name <name>' patterns,
    and loads the appropriate .env file with override=True.

    NOTE: This function does NOT remove arguments from sys.argv.
    - argparse will still see and parse them normally
    - But JIUWENSWARM_DATA_DIR is set BEFORE module-level code executes

    Priority:
    1. --dotenv <path>: Use specified file directly
    2. --name <name>: Load instance bootstrap .env from instances.yaml

    Args:
        component_name: Name for warning messages (e.g., "jiuwenswarm-app")

    Returns:
        Path to the loaded .env file if found and loaded, None otherwise

    Usage:
        from jiuwenswarm.dotenv_early import parse_dotenv_early
        parse_dotenv_early("jiuwenswarm-app")

        # Now safe to import jiuwenswarm modules
        from jiuwenswarm.common.utils import get_user_workspace_dir
    """
    global _parsed_dotenv, _component_name
    _component_name = component_name
    dotenv_path = None
    name_value = None

    # Scan sys.argv for --dotenv and --name patterns (DO NOT remove)
    for i, arg in enumerate(sys.argv):
        if arg == "--dotenv" and i + 1 < len(sys.argv):
            dotenv_path = sys.argv[i + 1]
        elif arg == "--name" and i + 1 < len(sys.argv):
            name_value = sys.argv[i + 1]

    # Load .env file
    result: Path | None = None
    if dotenv_path is not None:
        # --dotenv takes priority
        dotenv_file = Path(dotenv_path).expanduser().resolve()
        if dotenv_file.exists():
            load_dotenv_runtime(dotenv_file, override=True)
            result = dotenv_file
        else:
            _early_warning(component_name, f"--dotenv file not found: {dotenv_file}")

    elif name_value is not None:
        # --name: load instance bootstrap .env
        result = load_named_instance_env(name_value, component_name)

    # Store result for get_parsed_dotenv()
    _parsed_dotenv = result
    return result


def load_named_instance_env(name: str, component_name: str = "jiuwenswarm") -> Path | None:
    """Load a named instance's bootstrap .env into os.environ.

    Public wrapper so CLI chat can resolve GATEWAY_PORT after argparse, not
    only from the early sys.argv scan.
    """
    return _load_bootstrap_by_name_early(name, component_name)


def _load_bootstrap_by_name_early(name: str, component_name: str) -> Path | None:
    """Load bootstrap .env for named instance during early parsing.

    This is called before any jiuwenswarm imports, so it needs to:
    1. Validate instance name (basic check, full validation later)
    2. Find instances.yaml and read instance workspace
    3. Load bootstrap .env if exists

    Args:
        name: Instance name
        component_name: Component name for error messages

    Returns:
        Path to loaded .env if successful, None otherwise
    """

    # Basic instance name validation (just check it's not empty/reserved)
    if not name or name.lower() in ("default", "config", "tmp"):
        _early_error(component_name, f"Invalid instance name '{name}'")
        return None

    # Find instances.yaml path (same logic as instance_manager but without imports)
    user_home = os.environ.get("JIUWENSWARM_HOME") or Path.home()
    yaml_path = Path(user_home) / ".jiuwenswarm" / "instances.yaml"

    if not yaml_path.exists():
        _early_error(component_name, f"instances.yaml not found: {yaml_path}")
        _early_error(component_name, f"Run 'jiuwenswarm-init --name {name}' to create it.")
        return None

    # Parse YAML to find instance workspace (minimal parsing without full imports)
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

    # Check workspace exists
    if not workspace.exists():
        _early_error(component_name, f"Workspace directory not found: {workspace}")
        _early_error(component_name, f"Run 'jiuwenswarm-init --name {name}' to create it.")
        return None

    # Load bootstrap .env
    bootstrap_env = workspace / ".env"
    if bootstrap_env.exists():
        load_dotenv_runtime(bootstrap_env, override=True)
        return bootstrap_env
    else:
        # Bootstrap .env doesn't exist - need to create it
        # Import bootstrap module (safe now that we're past early parsing)
        from jiuwenswarm.instance_manager.bootstrap import _create_basic_bootstrap_env
        _create_basic_bootstrap_env(name, workspace, component_name)
        if bootstrap_env.exists():
            load_dotenv_runtime(bootstrap_env, override=True)
            return bootstrap_env
        return None


# Self-contained early parsing (no function call needed)
# This is the simplest usage pattern: just import this module
# and the parsing happens automatically.
_parsed_dotenv: Path | None = None
_component_name: str = "jiuwenswarm"


def set_component_name(name: str) -> None:
    """Set the component name for warning messages.

    Call this before importing the module if you want custom warnings:
        from jiuwenswarm import dotenv_early
        dotenv_early.set_component_name("jiuwenswarm-app")
        # Now import triggers parsing with custom name

    However, the simpler pattern is to just call parse_dotenv_early() directly.
    """
    global _component_name
    _component_name = name


def get_parsed_dotenv() -> Path | None:
    """Get the path that was parsed, if any."""
    return _parsed_dotenv


def load_instance_bootstrap_by_name(name: str) -> Path | None:
    """Load bootstrap .env for a named instance after argparse parsing.

    This function is a wrapper that delegates to
    jiuwenswarm.instance_manager.bootstrap.load_instance_bootstrap_by_name.

    NOTE: This function is deprecated. Use the function from
    jiuwenswarm.instance_manager.bootstrap directly for new code.

    Args:
        name: Instance name (must exist in instances.yaml)

    Returns:
        Path to loaded .env if successful, None otherwise
    """
    from jiuwenswarm.instance_manager.bootstrap import (
        load_instance_bootstrap_by_name as _load_bootstrap,
    )
    return _load_bootstrap(name)


__all__ = [
    "CLI_PORTS_ENV_FLAG",
    "DESKTOP_PRESERVED_ENV_KEYS",
    "parse_dotenv_early",
    "load_dotenv_runtime",
    "get_parsed_dotenv",
    "set_component_name",
    "load_instance_bootstrap_by_name",
]