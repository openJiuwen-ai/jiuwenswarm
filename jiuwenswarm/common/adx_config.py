# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project historical workspace settings onto the managed ADX tool backend."""
from __future__ import annotations

import copy
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

ADX_TOOL_KEYS = (
    "image", "runtime", "workdir", "mounts", "cpu", "cpu_limit", "memory", "mem_limit",
    "idle_timeout", "user", "group", "network", "connection", "env", "create_timeout",
    "schedule_timeout",
)


@lru_cache(maxsize=64)
def _warn_legacy_fields(fields: tuple[str, ...]) -> None:
    # Log field names only; configuration values may contain credentials.
    logger.warning("Managed ADX tools replace legacy sandbox fields: %s", ", ".join(fields))


def _native_mount(mount: dict) -> dict:
    if not isinstance(mount, dict):
        raise ValueError("sandbox.mounts entries must be mappings")
    result = copy.deepcopy(mount)
    if "source" in result:
        source = result.pop("source")
        if not isinstance(source, str) or not source.startswith("/"):
            raise ValueError("legacy sandbox.mounts.source must be an absolute local path; configure an ADX mount explicitly")
        if "host_path" in result and result["host_path"] != source:
            raise ValueError("sandbox mount source conflicts with host_path")
        result["host_path"] = source
        result.setdefault("target", source)
        if "readonly" not in result:
            result.setdefault("options", ["rw"])
    if "readonly" in result:
        readonly = result.pop("readonly")
        if not isinstance(readonly, bool):
            raise ValueError("legacy sandbox.mounts.readonly must be a boolean")
        options = ["ro" if readonly else "rw"]
        if "options" in result and list(result["options"]) != options:
            raise ValueError("sandbox mount readonly conflicts with options")
        result["options"] = options
    return result


def managed_adx_sandbox(local: dict | None, deployment: dict) -> dict:
    """Use deployment routing, retaining portable user options without rewriting YAML.

    Called only for the managed ADX profile. Other backends keep their own merge
    rules. Docker images and Jiuwenbox policies are not ADX image bindings/policies.
    """
    if local is None:
        local = {}
    if not isinstance(local, dict):
        raise ValueError("sandbox must be a mapping")
    legacy = local.get("type") not in (None, "", "adx-environment")
    for key in ("files", "excluded_commands", "policy_file"):
        value = local.get(key)
        if key == "files" and isinstance(value, dict):
            value = any(value.values())
        if value:
            raise ValueError(f"sandbox.{key} requires explicit migration to ADX policy/mounts")
    result = {key: copy.deepcopy(deployment[key]) for key in ADX_TOOL_KEYS if key in deployment}
    for key in ADX_TOOL_KEYS:
        if key in local and not (legacy and key in ("image", "runtime", "connection")):
            result[key] = copy.deepcopy(local[key])
    for request, limit in (("cpu", "cpu_limit"), ("memory", "mem_limit")):
        if request in local and limit not in local:
            result[limit] = local[request]
    replaced = tuple(sorted(key for key in ("executor", "template_name", "conch", "token", "use_random_token")
                            if local.get(key)))
    if legacy and local.get("image"):
        replaced += ("image",)
    if replaced:
        _warn_legacy_fields(replaced)
    rootfs = local.get("rootfs")
    if rootfs is None:
        rootfs = {}
    if not isinstance(rootfs, dict):
        raise ValueError("sandbox.rootfs must be a mapping")
    unsupported = set(rootfs) - {"workdir", "mounts", "user"}
    if unsupported:
        raise ValueError("sandbox.rootfs fields require explicit ADX migration: " + ", ".join(sorted(unsupported)))
    if "workdir" not in local and rootfs.get("workdir"):
        result["workdir"] = rootfs["workdir"]
    if "user" not in local and rootfs.get("user") is not None:
        result["user"] = rootfs["user"]
    user = result.get("user")
    if isinstance(user, str) and ":" in user:
        user, group = user.split(":", 1)
        if not user or not group:
            raise ValueError("sandbox.user must contain both user and group")
        if "group" in local and str(local["group"]) != group:
            raise ValueError("sandbox.user conflicts with sandbox.group")
        result.update(user=user, group=group)
    mounts = []
    for entries in (local.get("mounts"), rootfs.get("mounts")):
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError("sandbox.mounts must be a list")
        for mount in entries:
            native = _native_mount(mount)
            if any(item.get("target") == native.get("target") and item != native for item in mounts):
                raise ValueError("sandbox.mounts contain conflicting entries for the same target")
            if native not in mounts:
                mounts.append(native)
    if "mounts" in local or "mounts" in rootfs:
        result["mounts"] = mounts
    # User YAML cannot redirect tools to an old node or disable deployment TLS.
    connection = result.get("connection") or {}
    if not isinstance(connection, dict):
        raise ValueError("sandbox.connection must be a mapping")
    connection = {key: value for key, value in connection.items()
                  if key not in ("server_address", "gateway_address", "token", "token_provider", "use_tls", "gateway_use_tls", "verify_tls")}
    connection.update(deployment.get("connection") or {})
    connection["verify_tls"] = True
    result.update(type="adx-environment", enabled=True, connection=connection)
    if deployment.get("url"):
        result["url"] = deployment["url"]
    else:
        result.pop("url", None)
    result.pop("startup_mode", None)
    return result
