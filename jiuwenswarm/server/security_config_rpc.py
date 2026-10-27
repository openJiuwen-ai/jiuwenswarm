"""AgentServer adapter for the security configuration facade."""

import json
import os
import uuid
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.security_config_service import SecurityConfigService


_METHODS = frozenset({
    ReqMethod.SECURITY_CONFIG_GET,
    ReqMethod.SECURITY_CONFIG_INITIALIZE,
    ReqMethod.SECURITY_CONFIG_SETTINGS_UPDATE,
    ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
    ReqMethod.SECURITY_CONFIG_MODULE_UPDATE,
    ReqMethod.SECURITY_CONFIG_PROFILE_UPDATE,
    ReqMethod.SECURITY_CONFIG_APPLY,
    ReqMethod.SECURITY_CONFIG_SANDBOX_GET,
    ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE,
    ReqMethod.SECURITY_BUILTIN_GET,
})

_MANAGED_SANDBOX_METHODS = frozenset({
    ReqMethod.SANDBOX_ENABLED_GET, ReqMethod.SANDBOX_ENABLED_SET,
    ReqMethod.SANDBOX_STARTUP_MODE_GET, ReqMethod.SANDBOX_STARTUP_MODE_SET,
    ReqMethod.SANDBOX_LIMITS_GET, ReqMethod.SANDBOX_LIMITS_SET,
    ReqMethod.SANDBOX_FILES_GET, ReqMethod.SANDBOX_FILES_SET,
    ReqMethod.SANDBOX_NETWORK_GET, ReqMethod.SANDBOX_NETWORK_SET,
})


def is_security_config_method(method: ReqMethod) -> bool:
    return method in _METHODS


def is_managed_sandbox_method(method: ReqMethod) -> bool:
    return method in _MANAGED_SANDBOX_METHODS


def _native_sandbox_state(service: SecurityConfigService) -> dict[str, Any] | None:
    path = service.config_path.parent / "security.sandbox-apply-state.json"
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("status") not in {"pending", "failed", "applied"}:
        raise ValueError("invalid native sandbox apply state")
    return state


def _write_native_sandbox_state(service: SecurityConfigService, state: dict[str, Any]) -> None:
    path = service.config_path.parent / "security.sandbox-apply-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _check_sandbox_coverage(
    permissions: dict[str, Any], live: dict[str, Any], *, retry_axes: dict[str, bool] | None = None,
) -> None:
    """Reject rules the sandbox cannot apply with the same meaning."""
    from jiuwenswarm.common.behavior_security import runtime_permission_config
    from jiuwenswarm.common.file_guard_config import project_file_guard_to_sandbox, sandbox_file_buckets
    from jiuwenswarm.common.net_guard_config import project_net_guard_to_sandbox

    previous = live.get("permissions") or {}
    retry_axes = retry_axes or {}
    file_changed = retry_axes.get("file", False) or _sandbox_rule_fields_changed(previous, permissions, "file_guard", "paths")
    net_changed = retry_axes.get("network", False) or _sandbox_rule_fields_changed(previous, permissions, "net_guard", "urls")
    sandbox = live.get("sandbox") or {}
    if not sandbox.get("enabled") or not (file_changed or net_changed):
        return
    if sandbox.get("type", "jiuwenbox") != "jiuwenbox":
        raise ValueError("UNSUPPORTED_COVERAGE sandbox type")
    runtime = runtime_permission_config({**live, "permissions": permissions})
    if not runtime.get("enabled"):
        raise ValueError("UNSUPPORTED_COVERAGE permissions disabled for sandbox sync")
    if file_changed:
        guard = permissions.get("file_guard") or {}
        if guard.get("enabled") is False:
            raise ValueError("UNSUPPORTED_COVERAGE disabled FileGuard cannot replace sandbox rules")
        try:
            files, _ = project_file_guard_to_sandbox(guard)
        except NotImplementedError as exc:
            raise ValueError(f"UNSUPPORTED_COVERAGE file: {exc}") from exc
        sandbox_file_buckets(files)
    if net_changed:
        guard = permissions.get("net_guard") or {}
        if not (runtime.get("net_guard") or {}).get("enabled"):
            raise ValueError("UNSUPPORTED_COVERAGE NetGuard must be enabled before sandbox sync")
        projected, _ = project_net_guard_to_sandbox(guard)
        projected_domains = (*projected["allow_domains"], *projected["deny_domains"])
        if any("*" in domain or "?" in domain for domain in projected_domains):
            raise ValueError("UNSUPPORTED_COVERAGE wildcard domains differ between Rail and sandbox")


def _sandbox_rule_fields_changed(before: dict[str, Any], after: dict[str, Any], module: str, field: str) -> bool:
    old = before.get(module) or {}
    new = after.get(module) or {}
    return (
        old.get(field) != new.get(field)
        or old.get("enabled") != new.get("enabled")
        or (module == "net_guard" and old.get("defaults", "allow") != new.get("defaults", "allow"))
    )


def _get_native_sandbox_config(service: SecurityConfigService) -> dict[str, Any]:
    from jiuwenswarm.common.config import _ensure_sandbox_runtime_shape
    from jiuwenswarm.server.sandbox_policy_render import (
        get_native_sandbox_network_config, get_sandbox_files_axes,
    )

    sandbox = service._read_yaml(service.config_path).get("sandbox") or {}
    startup_mode = sandbox.get("startup_mode")
    return {
        "enabled": bool(sandbox.get("enabled", False)),
        "startup_mode": startup_mode if startup_mode in {"internal", "external"} else "internal",
        "limits": _ensure_sandbox_runtime_shape(sandbox)["limits"],
        "filesystem": get_sandbox_files_axes(),
        "network": get_native_sandbox_network_config(),
    }


async def _update_native_sandbox_config(
    params: dict[str, Any], *, restart_sandboxes: Any, service: SecurityConfigService,
) -> dict[str, Any]:
    from jiuwenswarm.common.config import _ensure_sandbox_runtime_shape, update_config, update_sandbox_runtime
    from jiuwenswarm.server.sandbox_config_rpc import _apply_sandbox_change
    from jiuwenswarm.server.sandbox_policy_render import (
        _is_windows, get_sandbox_files_axes, initialize_native_sandbox_network,
        set_native_sandbox_network_config, set_sandbox_files_axes,
        validate_native_sandbox_network, validate_sandbox_files_axes,
    )

    if set(params) != {"config"} or not isinstance(params["config"], dict):
        raise ValueError("security.config.sandbox.update requires config")
    value = params["config"]
    if not {"enabled", "filesystem", "network"} <= set(value) or set(value) - {
        "enabled", "startup_mode", "limits", "filesystem", "network",
    } or not isinstance(value["enabled"], bool):
        raise ValueError("sandbox config requires enabled/filesystem/network")
    files = validate_sandbox_files_axes(value["filesystem"])
    if not _is_windows() and any(files.values()):
        raise NotImplementedError("native filesystem ACL is supported on Windows only")
    network = validate_native_sandbox_network(value["network"])
    previous = _get_native_sandbox_config(service)
    limits = _ensure_sandbox_runtime_shape({"limits": value.get("limits", previous["limits"])})["limits"]
    value = {**value, "startup_mode": value.get("startup_mode", previous["startup_mode"]), "limits": limits}
    if not isinstance(value["startup_mode"], str) or value["startup_mode"] not in {"internal", "external"}:
        raise ValueError("startup_mode must be internal or external")
    state = _native_sandbox_state(service)
    live = service._read_yaml(service.config_path)
    if (live.get("sandbox") or {}).get("type", "jiuwenbox") != "jiuwenbox":
        raise ValueError("native sandbox config supports jiuwenbox only")
    if value == previous and state is not None and state["status"] == "applied" and state.get("config") == value:
        return {"saved": True, "applied": True, "targets": {"sandbox": "not_required"}, "config": previous}
    initialize_native_sandbox_network(service.config_path, (live.get("sandbox") or {}).get("urls") or {})
    _write_native_sandbox_state(service, {"status": "pending", "config": value, "target": "pending"})
    if files != previous["filesystem"]:
        set_sandbox_files_axes(files)
        if get_sandbox_files_axes() != files:
            raise OSError("sandbox filesystem configuration could not be saved")
    if network != previous["network"]:
        set_native_sandbox_network_config(network)
    if value["enabled"] != previous["enabled"]:
        update_sandbox_runtime({"enabled": value["enabled"]})
    if limits != previous["limits"]:
        update_sandbox_runtime({"limits": limits})
    if value["startup_mode"] != previous["startup_mode"]:
        def set_startup_mode(data):
            data.setdefault("sandbox", {})["startup_mode"] = value["startup_mode"]
            return data

        update_config(set_startup_mode)
    if value["enabled"]:
        def mark_explicit(data):
            data.setdefault("sandbox", {})["security_config_user_set"] = True
            return data

        update_config(mark_explicit)
    try:
        if value["startup_mode"] != previous["startup_mode"]:
            await _apply_sandbox_change("startup_mode", strict=True)
        if limits != previous["limits"]:
            await _apply_sandbox_change("limits", strict=True)
        if not value["enabled"]:
            # Retrying a failed disable must revisit pending cleanup even though
            # the saved enabled flag is already false.
            await _apply_sandbox_change("enabled", strict=True)
            status = "not_required"
        else:
            if not previous["enabled"]:
                await _apply_sandbox_change("enabled", strict=True)
            restarted = await restart_sandboxes({})
            needs_network_confirmation = network != previous["network"] or not previous["enabled"]
            status = (
                "external_pending"
                if needs_network_confirmation and restarted.get("network_status") == "externally_managed"
                else "applied"
            )
    except Exception as exc:
        status = f"failed: {exc}"
    applied = status in {"applied", "not_required"}
    _write_native_sandbox_state(service, {
        "status": "applied" if applied else "failed", "config": value, "target": status,
    })
    return {
        "saved": True, "applied": applied,
        "targets": {"sandbox": status}, "config": _get_native_sandbox_config(service),
    }


async def dispatch_managed_sandbox_request(
    method: ReqMethod, params: dict[str, Any], *, restart_sandboxes: Any,
    service: SecurityConfigService | None = None,
) -> dict[str, Any]:
    """Keep the old sandbox RPC shapes while routing writes through the facade."""
    service = service or SecurityConfigService()
    if method not in _MANAGED_SANDBOX_METHODS or not isinstance(params, dict):
        raise ValueError("unsupported managed sandbox request")
    current = _get_native_sandbox_config(service)
    if method == ReqMethod.SANDBOX_ENABLED_GET:
        return {"enabled": current["enabled"]}
    if method == ReqMethod.SANDBOX_STARTUP_MODE_GET:
        return {"startup_mode": current["startup_mode"]}
    if method == ReqMethod.SANDBOX_LIMITS_GET:
        return {"limits": current["limits"]}
    if method == ReqMethod.SANDBOX_FILES_GET:
        files = current["filesystem"]
        return {"files": {"allow": files["allow_read"], "deny": files["deny_read"]}}
    if method == ReqMethod.SANDBOX_NETWORK_GET:
        return {"network": current["network"]}
    if method == ReqMethod.SANDBOX_ENABLED_SET:
        if set(params) != {"enabled"} or not isinstance(params["enabled"], bool):
            raise ValueError("enabled must be boolean")
        current["enabled"] = params["enabled"]
    elif method == ReqMethod.SANDBOX_STARTUP_MODE_SET:
        if set(params) != {"startup_mode"}:
            raise ValueError("startup_mode is required")
        current["startup_mode"] = params["startup_mode"]
    elif method == ReqMethod.SANDBOX_LIMITS_SET:
        limits = params.get("limits") if isinstance(params.get("limits"), dict) else params
        if not isinstance(limits, dict):
            raise ValueError("limits must be an object")
        current["limits"] = {**current["limits"], **limits}
    elif method == ReqMethod.SANDBOX_FILES_SET:
        if set(params) != {"allow", "deny"} or not isinstance(params["allow"], list) or not isinstance(params["deny"], list):
            raise ValueError("allow and deny must be lists")
        current["filesystem"] = {
            "allow_read": params["allow"], "allow_write": params["allow"],
            "deny_read": params["deny"], "deny_write": params["deny"],
        }
    elif method == ReqMethod.SANDBOX_NETWORK_SET:
        if set(params) != {"disable_all", "allow_domains", "deny_domains"}:
            raise ValueError("network requires disable_all/allow_domains/deny_domains")
        current["network"] = params
    result = await _update_native_sandbox_config(
        {"config": current}, restart_sandboxes=restart_sandboxes, service=service,
    )
    if method == ReqMethod.SANDBOX_ENABLED_SET:
        result["enabled"] = result["config"]["enabled"]
    elif method == ReqMethod.SANDBOX_STARTUP_MODE_SET:
        result["startup_mode"] = result["config"]["startup_mode"]
    elif method == ReqMethod.SANDBOX_LIMITS_SET:
        result["limits"] = result["config"]["limits"]
    elif method == ReqMethod.SANDBOX_FILES_SET:
        files = result["config"]["filesystem"]
        result["files"] = {"allow": files["allow_read"], "deny": files["deny_read"]}
    else:
        result["network"] = result["config"]["network"]
    return result


async def dispatch_security_config_request(
    method: ReqMethod, params: dict[str, Any], *, agent_manager: Any,
    restart_sandboxes: Any, service: SecurityConfigService | None = None,
) -> dict[str, Any]:
    """Save and apply a revision; never report success for an unacknowledged target."""
    service = service or SecurityConfigService()
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if method == ReqMethod.SECURITY_CONFIG_GET:
        if params:
            raise ValueError("security.config.get accepts no parameters")
        if not service.is_initialized():
            return {"initialized": False}
        return {"initialized": True, **service.get_candidate(include_disabled_sources=True)}
    if method == ReqMethod.SECURITY_BUILTIN_GET:
        if set(params) != {"module"}:
            raise ValueError("security.builtin.get requires module only")
        return service.get_builtin(params["module"])
    if method == ReqMethod.SECURITY_CONFIG_SANDBOX_GET:
        if params:
            raise ValueError("security.config.sandbox.get accepts no parameters")
        return {"config": _get_native_sandbox_config(service), "apply": _native_sandbox_state(service)}
    if method == ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE:
        return await _update_native_sandbox_config(params, restart_sandboxes=restart_sandboxes, service=service)

    expected = params.get("expected_revision")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("expected_revision must be a string")
    prior_state = service.get_candidate()["apply"] if method != ReqMethod.SECURITY_CONFIG_INITIALIZE else {}
    retry_axes = prior_state.get("sandbox_retry") or {}
    rollback_snapshot = (
        service.snapshot_for_rollback()
        if method == ReqMethod.SECURITY_CONFIG_INITIALIZE
        or (method != ReqMethod.SECURITY_CONFIG_APPLY and prior_state.get("applied")) else None
    )

    def validate_coverage(permissions, live):
        _check_sandbox_coverage(permissions, live, retry_axes=retry_axes)

    if method == ReqMethod.SECURITY_CONFIG_INITIALIZE:
        if params:
            raise ValueError("security.config.initialize accepts no parameters")
        state = service.initialize_from_live(validator=validate_coverage)
    elif method == ReqMethod.SECURITY_CONFIG_SETTINGS_UPDATE:
        state = service.update_settings(params.get("patch"), expected_revision=expected, validator=validate_coverage)
    elif method == ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE:
        state = service.update_local(params.get("module"), params.get("value"), expected_revision=expected, validator=validate_coverage)
    elif method == ReqMethod.SECURITY_CONFIG_MODULE_UPDATE:
        state = service.update_module_settings(params.get("module"), params.get("patch"), expected_revision=expected, validator=validate_coverage)
    elif method == ReqMethod.SECURITY_CONFIG_PROFILE_UPDATE:
        state = service.update_permission_profile(params.get("patch"), expected_revision=expected, validator=validate_coverage)
    elif method == ReqMethod.SECURITY_CONFIG_APPLY:
        state = service.get_candidate()["apply"]
        if expected is not None and state["revision"] != expected:
            raise ValueError("VERSION_CONFLICT")
    else:
        raise ValueError(f"unsupported security config method: {method}")

    revision = state["revision"]
    targets = {"rail": "pending", "host_exit": "pending", "sandbox": "pending"}
    try:
        published = service.publish_candidate(
            expected_revision=revision, validator=validate_coverage,
        )
    except Exception as exc:
        if str(exc) == "VERSION_CONFLICT":
            raise
        targets["rail"] = f"failed: publication: {exc}"
        targets["host_exit"] = "not_published"
        targets["sandbox"] = f"not_published: {exc}"
        result = service.record_apply(revision, targets)
        return {"saved": True, "applied": False, "apply": result}
    config = published["config"]
    previous = published["previous"]
    before = previous.get("permissions") or {}
    after = config.get("permissions") or {}
    sandbox = config.get("sandbox") or {}
    planned_axes = published["apply"].get("sandbox_pending") or {}
    file_changed = (planned_axes.get("file", False) or retry_axes.get("file", False)
                    or _sandbox_rule_fields_changed(before, after, "file_guard", "paths"))
    net_changed = (planned_axes.get("network", False) or retry_axes.get("network", False)
                   or _sandbox_rule_fields_changed(before, after, "net_guard", "urls"))
    sandbox_file_skipped = []
    sandbox_network_skipped = []

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        publish_host_exit_policy_from_config,
    )

    try:
        publish_host_exit_policy_from_config(config)
        targets["host_exit"] = "applied"
    except Exception as exc:
        targets["host_exit"] = f"failed: {exc}"
    try:
        await agent_manager.reload_agents_config(config, None)
        targets["rail"] = "applied"
    except Exception as exc:
        targets["rail"] = f"failed: {exc}"

    if not sandbox.get("enabled") or not (file_changed or net_changed):
        targets["sandbox"] = "not_required"
    elif sandbox.get("type", "jiuwenbox") != "jiuwenbox":
        targets["sandbox"] = "unsupported: sandbox type"
    else:
        try:
            if file_changed:
                from jiuwenswarm.common.file_guard_config import sync_file_guard_to_sandbox

                file_result = sync_file_guard_to_sandbox()
                sandbox_file_skipped = file_result["skipped"]
            if net_changed:
                from jiuwenswarm.common.net_guard_config import sync_net_guard_to_sandbox

                net_result = sync_net_guard_to_sandbox()
                # The sandbox applies the domain projection; NetGuard still owns
                # entries that the sandbox cannot express. Preserve the omitted
                # entries in the apply receipt instead of failing the projection.
                sandbox_network_skipped = net_result["skipped"]
            restarted = await restart_sandboxes({})
            if restarted.get("network_status") == "externally_managed" and net_changed:
                targets["sandbox"] = "external_pending"
            else:
                targets["sandbox"] = "applied"
        except Exception as exc:
            targets["sandbox"] = f"failed: {exc}"

    if rollback_snapshot is not None and targets["sandbox"].startswith("failed:"):
        try:
            restored = service.restore_snapshot(rollback_snapshot, expected_revision=revision)
            old_config = service._read_yaml(service.config_path)
            rollback_targets = {"rail": "applied", "host_exit": "applied", "sandbox": "applied"}
            try:
                publish_host_exit_policy_from_config(old_config)
            except Exception as exc:
                rollback_targets["host_exit"] = f"failed: {exc}"
            try:
                await agent_manager.reload_agents_config(old_config, None)
            except Exception as exc:
                rollback_targets["rail"] = f"failed: {exc}"
            try:
                await restart_sandboxes({})
            except Exception as exc:
                rollback_targets["sandbox"] = f"failed: {exc}"
            rollback_applied = all(value in {"applied", "not_required"} for value in rollback_targets.values())
            if restored is not None and not rollback_applied:
                restored = service.record_apply(
                    restored["revision"], rollback_targets,
                    sandbox_retry={"file": bool(file_changed), "network": bool(net_changed)}
                    if rollback_targets["sandbox"].startswith("failed:") else {},
                )
            if restored is None:
                restored = {
                    "revision": None, "status": "uninitialized", "saved": False,
                    "applied": False, "targets": rollback_targets,
                }
            return {
                "saved": False, "applied": False,
                "rolled_back": rollback_applied,
                "failed_targets": targets, "apply": restored,
            }
        except Exception as exc:
            targets["sandbox"] = f"{targets['sandbox']}; rollback failed: {exc}"

    result = service.record_apply(
        revision, targets,
        sandbox_file_skipped=sandbox_file_skipped,
        sandbox_network_skipped=sandbox_network_skipped,
        sandbox_retry={"file": bool(file_changed), "network": bool(net_changed)}
        if targets["sandbox"].startswith("failed:") else {},
    )
    return {"saved": True, "applied": result["applied"], "apply": result}


async def apply_trusted_cloud_policy(
    policy_version: int, permissions: dict[str, Any], *, agent_manager: Any,
    restart_sandboxes: Any, service: SecurityConfigService | None = None,
) -> dict[str, Any]:
    """Trusted cloud ingress only; intentionally absent from public ReqMethod."""
    service = service or SecurityConfigService()
    prior = service.get_candidate()["apply"] if service.is_initialized() else {}
    retry_axes = prior.get("sandbox_retry") or {}
    state = service.replace_cloud_policy(
        policy_version, permissions,
        validator=lambda candidate, live: _check_sandbox_coverage(candidate, live, retry_axes=retry_axes),
    )
    if state["applied"]:
        return {"saved": True, "applied": True, "apply": state}
    return await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_APPLY,
        {"expected_revision": state["revision"]},
        agent_manager=agent_manager,
        restart_sandboxes=restart_sandboxes,
        service=service,
    )


async def resume_managed_security_on_start(
    *, agent_manager: Any, restart_sandboxes: Any,
    service: SecurityConfigService | None = None,
) -> dict[str, Any] | None:
    """Restore the latest source revision before AgentServer accepts requests."""
    service = service or SecurityConfigService()
    source_state = None
    if service.is_initialized():
        from openjiuwen.harness.security.permission_engine import core as permission_core

        if getattr(permission_core, "COMPOSITION_SCHEMA_VERSION", None) != 1:
            raise RuntimeError("installed core does not support security composition v1")
        source_state = service.get_candidate()["apply"]
        if not source_state["applied"]:
            result = await dispatch_security_config_request(
                ReqMethod.SECURITY_CONFIG_APPLY,
                {"expected_revision": source_state["revision"]},
                agent_manager=agent_manager,
                restart_sandboxes=restart_sandboxes,
                service=service,
            )
            if not result["applied"]:
                raise RuntimeError(f"managed security configuration is not ready: {result['apply']['targets']}")
            source_state = result["apply"]
    native_state = _native_sandbox_state(service)
    if native_state is not None and native_state["status"] != "applied":
        result = await _update_native_sandbox_config(
            {"config": native_state["config"]},
            restart_sandboxes=restart_sandboxes, service=service,
        )
        if not result["applied"]:
            raise RuntimeError(f"native sandbox configuration is not ready: {result['targets']}")
    return source_state
