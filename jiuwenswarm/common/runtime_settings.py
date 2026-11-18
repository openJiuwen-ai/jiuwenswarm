# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Apply backend-neutral launch bindings supplied by the application Gateway."""
from __future__ import annotations

import copy
import json
import os
from urllib.parse import urlsplit

RUNTIME_SETTINGS_ENV = "JIUWENSWARM_RUNTIME_SETTINGS"


def runtime_settings() -> dict:
    if os.getenv("JIUWENSWARM_RUNTIME_PROFILE", "").strip().lower() != "agentos":
        return {}
    value = json.loads(os.getenv(RUNTIME_SETTINGS_ENV, "{}"))
    if not isinstance(value, dict):
        raise ValueError("Runtime settings must be a mapping")
    return value


def prepare_runtime_environment() -> None:
    """Consume launch credentials before loading the user's shared dotenv."""
    for source, target in runtime_settings().get("credentials", {}).items():
        value = os.environ.pop(source, None)
        if value is not None:
            if not value.strip():
                raise ValueError("AgentOS launch credential is empty")
            os.environ[target] = value


def bind_runtime_sandbox(sandbox: dict | None, deployment: dict) -> dict:
    """Retain static user settings while applying managed node connectivity."""
    managed = deployment.get("managed_connection")
    if not isinstance(managed, dict):
        return {**deployment, **sandbox} if isinstance(sandbox, dict) else deployment
    if not isinstance(sandbox, dict) or sandbox.get("type") != deployment.get("type"):
        raise ValueError("AgentOS workspace sandbox requires deployment migration before startup")
    context_file = os.getenv(managed["context_file_env"], "").strip()
    if context_file:
        with open(context_file, encoding="utf-8") as stream:
            node = json.load(stream)
        ingress = node.get("ingress_url")
    else:
        ingress = os.getenv(managed["url_env"], "").strip()
    if not isinstance(ingress, str) or not ingress:
        raise ValueError("Managed sandbox requires a launcher-injected node endpoint")
    return bind_sandbox_connection(sandbox, ingress, context_file, managed,
                                   deployment.get("launcher_config"))


def bind_sandbox_connection(sandbox: dict, ingress: str, context_file: str, managed: dict,
                            launcher_config: dict | None = None) -> dict:
    parsed = urlsplit(ingress)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Managed sandbox ingress must be an HTTP(S) origin")
    result = copy.deepcopy(sandbox)
    connection = result.get("connection") or {}
    if not isinstance(connection, dict):
        raise ValueError("sandbox.connection must be a mapping")
    connection = {key: value for key, value in connection.items()
                  if key not in managed.get("replace_connection_fields", [])}
    use_tls = parsed.scheme == "https"
    connection.update(use_tls=use_tls, verify_tls=use_tls)
    result.update(url=ingress, connection=connection)
    result.pop("endpoint_file", None)
    if context_file:
        result["endpoint_file"] = context_file
    if isinstance(result.get("env"), dict):
        result["env"] = {key: value for key, value in result["env"].items()
                         if key not in managed.get("credential_fields", [])}
    if launcher_config is not None:
        result["launcher_config"] = copy.deepcopy(launcher_config)
    return result
