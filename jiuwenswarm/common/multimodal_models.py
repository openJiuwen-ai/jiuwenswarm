# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Multimodal catalogue rules, independent of transport and persistence."""

from copy import deepcopy
import re
from typing import Any
from uuid import UUID

CAPABILITIES = ("vision", "audio", "video", "visual_gen", "video_gen")
UNDERSTANDING = CAPABILITIES[:3]
PURPOSES = {tool: "understanding" if tool in UNDERSTANDING else "generation" for tool in CAPABILITIES}
CONNECTION_FIELDS = ("provider", "protocol", "api_base", "api_key", "model_name")
UNDERSTANDING_FIELDS = ("endpoint_profile", "vendor_key", "plan", "context_window_tokens")
MAX_SAFE_INTEGER = 9007199254740991


class MultimodalError(Exception):
    """A public error whose payload never includes connection values."""

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.payload = {"code": code, "message_key": f"settingsPanel.multimodal.errors.{code}", **(details or {})}


def valid_model_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("mm_"):
        return False
    try:
        parsed = UUID(value[3:])
        return parsed.version == 4 and str(parsed) == value[3:]
    except ValueError:
        return False


def _length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def validate_model(model: Any) -> dict[str, Any]:
    """Validate new edits; deliberately never used to validate migrated fields."""
    errors = []

    def invalid(field: str, key: str = "invalid") -> None:
        errors.append({"field": field, "key": key})

    if not isinstance(model, dict):
        raise MultimodalError("MM_VALIDATION_FAILED", {"field_errors": [{"field": "model", "key": "invalid"}]})
    result = deepcopy(model)
    if set(result) - {"model_id", "display_name", "purpose", "capabilities", "connection"}:
        invalid("model")
    if not valid_model_id(result.get("model_id")):
        invalid("model_id")
    name = result.get("display_name")
    if not isinstance(name, str) or not 1 <= _length(name.strip()) <= 100:
        invalid("display_name")
    else:
        result["display_name"] = name.strip()
    purpose = result.get("purpose")
    if purpose not in ("understanding", "generation"):
        invalid("purpose")
    caps = result.get("capabilities")
    if (not isinstance(caps, list) or not caps
            or any(not isinstance(cap, str) or PURPOSES.get(cap) != purpose for cap in caps)
            or len(set(caps)) != len(caps)):
        invalid("capabilities")
    else:
        result["capabilities"] = [tool for tool in CAPABILITIES if tool in caps]
    connection = result.get("connection")
    if not isinstance(connection, dict):
        invalid("connection")
        connection = {}
    allowed = CONNECTION_FIELDS + (UNDERSTANDING_FIELDS if purpose == "understanding" else ())
    if set(connection) - set(allowed):
        invalid("connection")
    protocol = connection.get("protocol")
    optional_credentials = purpose == "generation" and isinstance(protocol, str) and protocol.strip() == "vllm-omni"
    for field, maximum in (("provider", 100), ("protocol", 100), ("api_base", 512), ("api_key", 2048), ("model_name", 100)):
        value = connection.get(field, "")
        optional = optional_credentials and field in ("api_key", "model_name")
        if not isinstance(value, str) or _length(value.strip()) > maximum or (not optional and not value.strip()):
            invalid(f"connection.{field}")
        elif field == "api_base" and not re.match(r"^https?://", value.strip(), re.IGNORECASE):
            invalid("connection.api_base")
        else:
            connection[field] = value.strip()
    if purpose == "understanding":
        if connection.get("protocol") != "openai":
            invalid("connection.protocol")
        for field in UNDERSTANDING_FIELDS[:-1]:
            if not isinstance(connection.get(field, ""), str):
                invalid(f"connection.{field}")
        tokens = connection.get("context_window_tokens", 262144)
        if type(tokens) is not int or not 1 <= tokens <= MAX_SAFE_INTEGER:
            invalid("connection.context_window_tokens")
        else:
            connection["context_window_tokens"] = tokens
    elif connection.get("protocol") == "modelark":
        api_base = connection.get("api_base")
        match = re.fullmatch(r"https://ark\.[\w-]+\.(?:volces\.com|bytepluses\.com)(/.*)?",
                             api_base.rstrip("/"), re.IGNORECASE) if isinstance(api_base, str) else None
        if not match or match[1] not in (None, "/api/v3"):
            invalid("connection.api_base")
    result["connection"] = connection
    if errors:
        raise MultimodalError("MM_VALIDATION_FAILED", {"field_errors": errors})
    return result


def validate_catalogue(state: Any) -> None:
    """Check stored identity/references without imposing new form requirements."""
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise MultimodalError("MM_UNSUPPORTED_SCHEMA")
    models, tools = state.get("models"), state.get("tools")
    if not isinstance(models, list) or not isinstance(tools, dict) or set(tools) != set(CAPABILITIES):
        raise MultimodalError("MM_CONFIG_READ_FAILED")
    ids = set()
    for model in models:
        if not isinstance(model, dict) or not valid_model_id(model.get("model_id")):
            raise MultimodalError("MM_CONFIG_READ_FAILED")
        caps = model.get("capabilities")
        if (model["model_id"] in ids or model.get("purpose") not in ("understanding", "generation")
                or not isinstance(caps, list) or not caps
                or any(not isinstance(cap, str) or PURPOSES.get(cap) != model["purpose"] for cap in caps)
                or len(set(caps)) != len(caps) or not isinstance(model.get("connection"), dict)
                or not isinstance(model.get("display_name"), str) or not isinstance(model.get("legacy_options", {}), dict)):
            raise MultimodalError("MM_CONFIG_READ_FAILED")
        ids.add(model["model_id"])
    for tool in CAPABILITIES:
        binding = tools[tool]
        if not isinstance(binding, dict) or type(binding.get("enabled")) is not bool:
            raise MultimodalError("MM_CONFIG_READ_FAILED")
        model_id = binding.get("model_id")
        if model_id is not None:
            model = next((m for m in models if m["model_id"] == model_id), None)
            if model is None or tool not in model["capabilities"]:
                raise MultimodalError("MM_CONFIG_READ_FAILED")


def references(state: dict, model_id: str) -> list[str]:
    return [tool for tool in CAPABILITIES if state["tools"][tool]["model_id"] == model_id]


def normalize_legacy_model(model: dict) -> dict:
    """Keep effective legacy addresses/names in the editable connection fields."""
    result = deepcopy(model)
    if result["purpose"] != "understanding":
        return result
    legacy = result.get("legacy_options", {})
    overrides = legacy.get("environment_overrides", {})
    for alias, field in (("base_url", "api_base"), ("model", "model_name")):
        value = overrides.pop(alias, None)
        if value:
            result["connection"][field] = value
    if not overrides:
        legacy.pop("environment_overrides", None)
    return result


def _confirm(params: dict, affected: list[str]) -> None:
    confirmed = params.get("confirmed_tools", [])
    if affected and (not isinstance(confirmed, list) or any(not isinstance(tool, str) for tool in confirmed)
                     or sorted(confirmed) != sorted(affected)):
        raise MultimodalError("MM_CONFIRM_REQUIRED", {"affected_tools": affected})


def transition(state: dict, operation: str, params: dict) -> tuple[dict, dict]:
    """Calculate a complete candidate; no mutation happens before confirmation."""
    candidate = deepcopy(state)
    models, tools = candidate["models"], candidate["tools"]
    result = {}
    if operation == "models.upsert":
        model = validate_model(params.get("model"))
        model_id = model["model_id"]
        old = next((item for item in models if item["model_id"] == model_id), None)
        mode = params.get("mode")
        if mode not in ("create", "update"):
            raise MultimodalError("MM_VALIDATION_FAILED", {"field_errors": [{"field": "mode", "key": "invalid"}]})
        if mode == "update" and old is None:
            raise MultimodalError("MM_MODEL_NOT_FOUND", {"model_id": model_id})
        previous = normalize_legacy_model(old) if old and old["purpose"] == model["purpose"] else None
        model["legacy_options"] = deepcopy(previous.get("legacy_options", {})) if previous else {}
        if previous and previous["connection"].get("model_name") != model["connection"].get("model_name"):
            overrides = model["legacy_options"].get("environment_overrides", {})
            for alias in ("transcription_model", "question_answering_model"):
                overrides.pop(alias, None)
            if not overrides:
                model["legacy_options"].pop("environment_overrides", None)
        if mode == "create" and old is not None and old != model:
            raise MultimodalError("MM_MODEL_ID_CONFLICT", {"model_id": model_id})
        affected = [tool for tool in references(state, model_id) if tool not in model["capabilities"]]
        _confirm(params, affected)
        if old is None:
            models.append(model)
        else:
            models[models.index(old)] = model
        for tool in affected:
            replacement = next((m for m in models if tool in m["capabilities"]), None)
            tools[tool]["model_id"] = replacement["model_id"] if replacement else None
            if replacement is None:
                tools[tool]["enabled"] = False
        added = set(model["capabilities"]) - set(old["capabilities"] if old else [])
        for tool in added:
            if tools[tool]["model_id"] is None:
                tools[tool] = {"model_id": model_id, "enabled": True}
        result["model_id"] = model_id
    elif operation == "models.delete":
        model_id = params.get("model_id")
        if not valid_model_id(model_id):
            raise MultimodalError("MM_VALIDATION_FAILED")
        affected = references(state, model_id)
        _confirm(params, affected)
        candidate["models"] = models = [m for m in models if m["model_id"] != model_id]
        for tool in affected:
            replacement = next((m for m in models if tool in m["capabilities"]), None)
            tools[tool]["model_id"] = replacement["model_id"] if replacement else None
            if replacement is None:
                tools[tool]["enabled"] = False
        result["deleted"] = True
    elif operation == "tools.update":
        tool = params.get("tool_id")
        if tool not in CAPABILITIES or not ({"model_id", "enabled"} & params.keys()):
            raise MultimodalError("MM_VALIDATION_FAILED")
        binding = tools[tool]
        if "model_id" in params:
            model = next((m for m in models if m["model_id"] == params["model_id"]), None)
            if model is None:
                raise MultimodalError("MM_MODEL_NOT_FOUND")
            if tool not in model["capabilities"]:
                raise MultimodalError("MM_CAPABILITY_MISMATCH", {"tool_id": tool, "model_id": model["model_id"]})
            binding["model_id"] = model["model_id"]
        if "enabled" in params:
            if type(params["enabled"]) is not bool:
                raise MultimodalError("MM_VALIDATION_FAILED")
            if params["enabled"] and binding["model_id"] is None:
                raise MultimodalError("MM_MODEL_REQUIRED", {"tool_id": tool})
            binding["enabled"] = params["enabled"]
    elif operation != "config.apply":
        raise MultimodalError("MM_VALIDATION_FAILED")
    result["changed_tools"] = [tool for tool in CAPABILITIES if tools[tool] != state["tools"][tool]
                               or next((m for m in models if m["model_id"] == tools[tool]["model_id"]), None)
                               != next((m for m in state["models"] if m["model_id"] == state["tools"][tool]["model_id"]), None)]
    result["changed"] = candidate != state
    validate_catalogue(candidate)
    return candidate, result


def public_snapshot(state: dict) -> dict:
    result = {key: deepcopy(state[key]) for key in ("schema_version", "models", "tools")}
    result["models"] = [
        {"model_id": m["model_id"], "display_name": m["display_name"], "purpose": m["purpose"],
         "capabilities": m["capabilities"], "references": references(state, m["model_id"]),
         **{key: m["connection"].get(key, "") for key in ("provider", "protocol", "api_base", "model_name")},
         "api_key_configured": bool(m["connection"].get("api_key"))}
        for m in state["models"]
    ]
    return result
