"""Host-side composition and lifecycle for the bundled memory plugin."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_gateway.common.mode_matrix import resolve_request_mode

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
    _build_security_provider,
    get_jiuwen_memory_scope,
    get_jiuwen_builtin_config,
)
from jiuwenswarm.common.config import _merge_config_dict, get_config
from jiuwenswarm.common.team_memory_binding import uses_jiuwen_team_memory
from jiuwenswarm.enterprise import is_enterprise
from jiuwenswarm.runtime.request import resolve_agent_request_mode

MEMORY_PLUGIN_ID = "agent-memory-plugin"


def ensure_memory_plugin_installed() -> None:
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    if packages.plugin_package_exists(MEMORY_PLUGIN_ID):
        return
    root = packages.get_equipment_resources_plugin_packages_dir()
    if root is not None and (root / MEMORY_PLUGIN_ID / "manifest.json").is_file():
        packages.install_plugin_package({"id": MEMORY_PLUGIN_ID})


@dataclass(frozen=True)
class HostMemoryContext:
    agent_name: str | None
    project_dir: str | None
    security_provider_factory: Callable[[], Any]
    sdk_media_globals: Callable[[dict[str, Any]], dict[str, Any]]
    team_project: str = ""
    team_binding: Mapping[str, str] | None = None
    team_enabled: Callable[[], bool] | None = None
    team_agent: str = ""


def host_memory_context(
    config: dict[str, Any], *, agent_name: str | None, project_dir: str | None,
    sdk_media_globals: Callable[[dict[str, Any]], dict[str, Any]],
) -> HostMemoryContext:
    scope = get_jiuwen_memory_scope(config)
    return HostMemoryContext(
        agent_name, project_dir, lambda: _build_security_provider(scope["org"], scope["user"]),
        sdk_media_globals,
    )


def supported_memory_session(params: dict[str, Any]) -> bool:
    mode = resolve_request_mode(params, resolve_agent_request_mode)
    return (
        mode.manager_mode in {"agent", "code"}
        and not mode.is_team
        and not params.get("team")
        and params.get("sub_mode") != "team"
        and params.get("submode") != "team"
    )


def supported_team_memory_session(params: dict[str, Any]) -> bool:
    mode = resolve_request_mode(params, resolve_agent_request_mode)
    return uses_jiuwen_team_memory(get_config(), mode.canonical_mode)


def team_memory_plugin_enabled(session_id: str, config: dict[str, Any]) -> bool:
    from jiuwenswarm.server.runtime.extension_package_manager import is_plugin_allowed
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    if is_enterprise() or not is_plugin_allowed(MEMORY_PLUGIN_ID):
        return False
    saved = get_session_equipment(session_id, cache_bust=True)
    if "plugin_names" in saved:
        return MEMORY_PLUGIN_ID in saved["plugin_names"]
    return (config.get("memory", {}).get("plugin", {}).get("default_enabled", True) is True)


def persist_team_memory_selection(session_id: str, params: dict[str, Any]) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment, save_session_equipment

    if "plugin_names" not in params or not supported_team_memory_session(params):
        return
    names = params["plugin_names"]
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("plugin_names must be a list of strings")
    saved = get_session_equipment(session_id, cache_bust=True)
    enabled = MEMORY_PLUGIN_ID in names
    previous = saved.get("plugin_names", [])
    if "plugin_names" in saved and enabled == (MEMORY_PLUGIN_ID in previous):
        return
    selected = [name for name in previous if name != MEMORY_PLUGIN_ID]
    if enabled:
        selected.append(MEMORY_PLUGIN_ID)
    save_session_equipment(
        session_id,
        agent_template_name=saved.get("agent_template_name", ""),
        plugin_names=selected,
        mcp=saved.get("mcp"),
    )


def default_memory_plugins(
    session_id: str | None, params: dict[str, Any], mounted: list[str],
) -> list[str]:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    if not session_id or not supported_memory_session(params):
        return mounted
    saved = get_session_equipment(session_id)
    if "plugin_names" in saved:
        return saved["plugin_names"]
    if memory_plugin_selected_by_default(params):
        return [*mounted, MEMORY_PLUGIN_ID] if MEMORY_PLUGIN_ID not in mounted else mounted
    return [name for name in mounted if name != MEMORY_PLUGIN_ID]


def memory_plugin_default_enabled() -> bool:
    config = get_config().get("memory", {}).get("plugin", {})
    return not is_enterprise() and config.get("default_enabled", True) is True


def memory_plugin_selected_by_default(params: dict[str, Any]) -> bool:
    from jiuwenswarm.server.runtime.extension_package_manager import is_plugin_allowed

    return (
        (supported_memory_session(params) or supported_team_memory_session(params))
        and memory_plugin_default_enabled()
        and is_plugin_allowed(MEMORY_PLUGIN_ID)
    )


def resolve_memory_plugin_options(
    host_config: Mapping[str, Any], package_config: Mapping[str, Any], overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the live listener for both shipped and previously installed Rails."""
    from jiuwenswarm.server.runtime.managed_memory_http import managed_memory_http

    if is_enterprise():
        raise ValueError("Built-in memory HTTP is unavailable in multi-tenant deployments")
    settings = _merge_memory_plugin_config(host_config, package_config, overrides)
    routing = {
        name: settings[name] for name in ("user_id", "scope_id", "agent_id", "session_id", "session_end_extract")
        if name in settings
    }
    return {**routing, "mode": "http", **managed_memory_http.connection_options()}


def memory_http_server_options(
    host_config: Mapping[str, Any], package_config: Mapping[str, Any], overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep legacy model configuration for the server, not client transport settings."""
    options = _merge_memory_plugin_config(host_config, package_config, overrides)
    return {name: options[name] for name in ("sdk", "identity_org", "identity_user") if name in options}


def _merge_memory_plugin_config(
    host_config: Mapping[str, Any], package_config: Mapping[str, Any], overrides: Mapping[str, Any],
) -> dict[str, Any]:
    options = mutable_config(package_config)
    _merge_config_dict(options, get_jiuwen_builtin_config(mutable_config(host_config)))
    _merge_config_dict(options, mutable_config(overrides))
    return options


def mutable_config(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: mutable_config(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [mutable_config(item) for item in value]
    return deepcopy(value)
