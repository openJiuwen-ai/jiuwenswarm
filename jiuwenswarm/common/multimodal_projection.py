# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One-time import and legacy execution-field projection for media models."""

from copy import deepcopy
import re
from uuid import uuid4

from jiuwenswarm.common.multimodal_models import CAPABILITIES, PURPOSES, UNDERSTANDING, normalize_legacy_model

FIELDS = {
    "api_base": "API_BASE", "api_key": "API_KEY", "model_name": "MODEL_NAME", "provider": "PROVIDER",
    "endpoint_profile": "ENDPOINT_PROFILE", "vendor_key": "VENDOR_KEY", "plan": "PLAN",
    "context_window_tokens": "CONTEXT_WINDOW_TOKENS", "protocol": "PROTOCOL",
}
ALIASES = {"vision": ("BASE_URL", "MODEL"), "audio": ("BASE_URL", "TRANSCRIPTION_MODEL", "QUESTION_ANSWERING_MODEL")}
TEMPLATE_MODEL_NAMES = {"vision": "your-vision-model-name", "audio": "your-audio-model-name", "video": "your-video-model-name"}
CLIENT_FIELDS = {"api_base", "base_url", "api_key", "model_name", "model", "client_provider", "model_provider",
                 "endpoint_profile", "context_window_tokens", "vendor_key", "plan", "protocol"}


def model_block(data: dict, tool: str, *, create: bool = False) -> dict:
    models = data.get("models")
    if models is None and create:
        data["models"] = models = {}
    if isinstance(models, dict):
        if create:
            return models.setdefault(tool, {})
        return models.get(tool, {})
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and isinstance(item.get(tool), dict):
                return item[tool]
        if create:
            block = {}
            models.append({tool: block})
            return block
    return {}


def resolve_legacy(value, env: dict):
    """Resolve old YAML placeholders against the effective legacy environment."""
    if isinstance(value, str):
        return re.sub(r"\$\{([^:}]+)(?::-([^}]*))?\}",
                      lambda m: str(env.get(m[1]) or m[2] or ""), value)
    if isinstance(value, dict):
        return {key: resolve_legacy(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_legacy(item, env) for item in value]
    return value


def _enabled(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def migrate_catalogue(raw: dict, env: dict) -> dict:
    """Import each old slot; merge only equal effective model configurations."""
    resolved = resolve_legacy(raw, env)
    state = {"schema_version": 1, "models": [], "tools": {}}
    for tool in CAPABILITIES:
        prefix = tool.upper()
        block = model_block(resolved, tool) if tool in UNDERSTANDING else {}
        client = block.get("model_config") or block.get("model_client_config") or {}
        connection = {}
        for field, suffix in FIELDS.items():
            if tool in UNDERSTANDING and field == "protocol":
                connection[field] = "openai"
                continue
            if tool not in UNDERSTANDING and field not in ("api_base", "api_key", "model_name", "provider", "protocol"):
                continue
            value = client.get({"provider": "client_provider"}.get(field, field))
            if field == "provider":
                value = value or client.get("model_provider")
            elif field == "model_name":
                value = value or client.get("model")
            elif field == "api_base":
                value = value or client.get("base_url")
            if value is None or value == "":
                value = env.get(f"{prefix}_{suffix}")
            if value is not None:
                connection[field] = value
        legacy = {}
        options = {key: value for key, value in client.items() if key not in CLIENT_FIELDS}
        if options:
            legacy["client_options"] = options
        if block.get("model_config_obj"):
            legacy["model_options"] = deepcopy(block["model_config_obj"])
        overrides = {suffix.lower(): env[f"{prefix}_{suffix}"] for suffix in ALIASES.get(tool, ())
                     if env.get(f"{prefix}_{suffix}")}
        if overrides:
            legacy["environment_overrides"] = overrides
        context_key = f"{prefix}_CONTEXT_WINDOW_TOKENS"
        if tool not in UNDERSTANDING and context_key in env:
            legacy["generation_context_window_tokens"] = env[context_key]
        # Template defaults and explicit switches alone are not model configurations.
        present = any(connection.get(key) for key in ("api_base", "api_key", "model_name", "provider")) or bool(overrides)
        explicit = env.get(f"{prefix}_ENABLED", block.get("enabled"))
        enabled = (_enabled(explicit) if explicit is not None else
                   tool in UNDERSTANDING and all(connection.get(key) for key in ("api_base", "api_key", "model_name", "provider")))
        binding = {"model_id": None, "enabled": enabled}
        if present:
            imported = normalize_legacy_model({
                "model_id": f"mm_{uuid4()}", "display_name": "", "purpose": PURPOSES[tool],
                "capabilities": [], "connection": connection, "legacy_options": legacy,
            })
            if tool in TEMPLATE_MODEL_NAMES and imported["connection"].get("model_name") == TEMPLATE_MODEL_NAMES[tool]:
                state["tools"][tool] = {"model_id": None, "enabled": False}
                continue
            if explicit is None and tool in UNDERSTANDING:
                binding["enabled"] = all(imported["connection"].get(key) for key in ("api_base", "api_key", "model_name", "provider"))
            imported["display_name"] = str(imported["connection"].get("model_name") or tool)
            match = next((model for model in state["models"] if model["purpose"] == PURPOSES[tool]
                          and model["connection"] == imported["connection"]
                          and model["legacy_options"] == imported["legacy_options"]), None)
            if match is None:
                match = imported
                state["models"].append(match)
            match["capabilities"].append(tool)
            binding["model_id"] = match["model_id"]
        state["tools"][tool] = binding
    return state


def project_catalogue(raw: dict, state: dict) -> tuple[dict, dict[str, str | None]]:
    """Compute all five slots before writing either file."""
    data = deepcopy(raw)
    data["multimodal"] = deepcopy(state)
    updates = {}
    by_id = {model["model_id"]: normalize_legacy_model(model) for model in state["models"]}
    data["multimodal"]["models"] = list(by_id.values())
    for tool in CAPABILITIES:
        prefix = tool.upper()
        binding = state["tools"][tool]
        model = by_id.get(binding["model_id"], {})
        connection = model.get("connection", {})
        legacy = model.get("legacy_options", {})
        updates[f"{prefix}_ENABLED"] = str(binding["enabled"]).lower()
        for field, suffix in FIELDS.items():
            if field == "protocol" and tool in UNDERSTANDING:
                continue
            if tool not in UNDERSTANDING and field in ("endpoint_profile", "vendor_key", "plan"):
                continue
            value = connection.get(field, "")
            if tool not in UNDERSTANDING and field == "context_window_tokens":
                value = legacy.get("generation_context_window_tokens", "")
            updates[f"{prefix}_{suffix}"] = str(value) if value is not None else ""
        for suffix in ALIASES.get(tool, ()):
            updates[f"{prefix}_{suffix}"] = legacy.get("environment_overrides", {}).get(suffix.lower())
        if tool in UNDERSTANDING:
            block = model_block(data, tool, create=True)
            client = deepcopy(legacy.get("client_options", {}))
            for field in ("api_base", "api_key", "model_name", "provider", "endpoint_profile", "context_window_tokens"):
                client["client_provider" if field == "provider" else field] = "${" + f"{prefix}_{FIELDS[field]}" + "}"
            block["enabled"] = "${" + f"{prefix}_ENABLED" + "}"
            # Both historical forms must resolve to the same model, including callers
            # that prefer model_config over model_client_config.
            block["model_client_config"] = client
            if "model_config" in block:
                block["model_config"] = deepcopy(client)
            block["model_config_obj"] = deepcopy(legacy.get("model_options", {}))
    return data, updates
