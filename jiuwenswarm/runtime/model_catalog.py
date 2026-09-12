# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral model catalog and selection contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

_RESERVED_MULTIMODAL_NAMES = frozenset({"video", "audio", "vision"})


class ModelCatalogError(RuntimeError):
    """A stable model catalog or selection failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeModelDescriptor:
    """Safe model facts; credentials and endpoints are intentionally absent."""

    selection_key: str
    display_name: str
    model_name: str
    provider: str = ""
    reasoning_level: str = ""
    is_default: bool = False
    is_agentos: bool = False
    is_current: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCatalogResult:
    """Configured chat models plus the effective local selection."""

    models: tuple[RuntimeModelDescriptor, ...]
    current_selection: str = ""
    current_display_name: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSelectionResult:
    """A validated request-scoped model selection."""

    model: RuntimeModelDescriptor
    session_id: str = ""
    persisted: bool = False


def _build_model_descriptors(
    entries: list[dict[str, Any]],
) -> tuple[RuntimeModelDescriptor, ...]:
    normalized: list[tuple[int, dict[str, Any], str, str, bool]] = []
    model_name_counts: dict[str, int] = {}
    alias_counts: dict[str, int] = {}
    model_names: set[str] = set()
    for global_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, dict):
            continue
        model_name = str(client_config.get("model_name") or "").strip()
        alias = str(entry.get("alias") or "").strip()
        display_name = alias or model_name
        if (
            not model_name
            or model_name.lower() in _RESERVED_MULTIMODAL_NAMES
            or display_name.lower() in _RESERVED_MULTIMODAL_NAMES
        ):
            continue
        model_config = entry.get("model_config_obj")
        is_agentos = isinstance(model_config, dict) and (
            model_config.get("_source") == "agentos"
        )
        normalized.append((global_index, entry, model_name, display_name, is_agentos))
        model_names.add(model_name)
        model_name_counts[model_name] = model_name_counts.get(model_name, 0) + 1
        if alias:
            alias_counts[alias] = alias_counts.get(alias, 0) + 1

    descriptors: list[RuntimeModelDescriptor] = []
    for global_index, entry, model_name, display_name, is_agentos in normalized:
        client_config = entry.get("model_client_config") or {}
        model_config = entry.get("model_config_obj") or {}
        alias = str(entry.get("alias") or "").strip()
        alias_does_not_shadow_model = alias == model_name or alias not in model_names
        if (
            alias
            and alias_counts.get(alias) == 1
            and alias_does_not_shadow_model
        ):
            selection_key = alias
        elif model_name_counts[model_name] > 1:
            selection_key = f"{model_name}#{global_index}"
        else:
            selection_key = model_name
        descriptors.append(
            RuntimeModelDescriptor(
                selection_key=selection_key,
                display_name=display_name,
                model_name=model_name,
                provider=str(client_config.get("client_provider") or "").strip(),
                reasoning_level=str(model_config.get("reasoning_level") or "").strip(),
                is_default=bool(entry.get("is_default")) and not is_agentos,
                is_agentos=is_agentos,
            )
        )
    return tuple(descriptors)


def build_model_catalog(
    entries: list[dict[str, Any]],
    *,
    current_selection: str = "",
) -> ModelCatalogResult:
    """Build a credential-free catalog from resolved model entries."""
    models = _build_model_descriptors(entries)
    requested_current = str(current_selection or "").strip()
    current = next(
        (item for item in models if item.selection_key == requested_current),
        None,
    )
    if current is None and requested_current:
        candidates = tuple(
            item
            for item in models
            if requested_current in {item.display_name, item.model_name}
        )
        if len(candidates) == 1:
            current = candidates[0]
    if current is None:
        current = next((item for item in models if item.is_default), None)
    if current is None and models:
        current = models[0]
    if current is None:
        return ModelCatalogResult(models=())
    marked = tuple(
        replace(item, is_current=item.selection_key == current.selection_key)
        for item in models
    )
    return ModelCatalogResult(
        models=marked,
        current_selection=current.selection_key,
        current_display_name=current.display_name,
    )


def resolve_model_selection(
    catalog: ModelCatalogResult,
    requested: str,
) -> RuntimeModelDescriptor:
    """Resolve an exact key or unambiguous configured name."""
    target = str(requested or "").strip()
    if not target:
        raise ModelCatalogError("model selection is required", code="BAD_REQUEST")
    if target.lower() in _RESERVED_MULTIMODAL_NAMES:
        raise ModelCatalogError(
            "video, audio, and vision are multimodal-only model profiles",
            code="BAD_REQUEST",
        )
    exact = tuple(item for item in catalog.models if item.selection_key == target)
    if len(exact) == 1:
        return replace(exact[0], is_current=True)
    candidates = tuple(
        item
        for item in catalog.models
        if target in {item.display_name, item.model_name}
    )
    if not candidates:
        raise ModelCatalogError("model not found", code="NOT_FOUND")
    if len(candidates) > 1:
        raise ModelCatalogError(
            "multiple models match; use the selection key shown by /model",
            code="BAD_REQUEST",
        )
    return replace(candidates[0], is_current=True)


def resolve_configured_model_entry(
    entries: list[dict[str, Any]],
    requested: str,
) -> dict[str, Any] | None:
    """Resolve one executable entry using the Agent Runtime selector rules.

    The returned mapping is the original configured entry and can contain
    credentials, so this helper is intentionally absent from the lazy Runtime
    public exports. Presentation code must use ``RuntimeModelDescriptor``.
    """
    target = str(requested or "").strip()
    if not target:
        return None

    if "#" in target:
        bare_name, separator, raw_index = target.rpartition("#")
        if not separator or not bare_name:
            return None
        try:
            global_index = int(raw_index)
        except ValueError:
            return None
        if not 0 <= global_index < len(entries):
            return None
        entry = entries[global_index]
        if not isinstance(entry, dict):
            return None
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, dict):
            return None
        configured_name = str(client_config.get("model_name") or "").strip()
        return entry if configured_name == bare_name else None

    name_matches: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, dict):
            continue
        if str(client_config.get("model_name") or "").strip() == target:
            name_matches.append(entry)
    if name_matches:
        return next(
            (entry for entry in name_matches if entry.get("is_default") is True),
            name_matches[0],
        )

    alias_matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("alias") or "").strip() == target
    ]
    return alias_matches[0] if len(alias_matches) == 1 else None


__all__ = [
    "ModelCatalogError",
    "ModelCatalogResult",
    "ModelSelectionResult",
    "RuntimeModelDescriptor",
    "build_model_catalog",
    "resolve_configured_model_entry",
    "resolve_model_selection",
]
