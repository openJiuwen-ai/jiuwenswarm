# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral, credential-free Runtime model catalog contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

_RESERVED_MULTIMODAL_NAMES = frozenset({"video", "audio", "vision"})


class ModelCatalogError(RuntimeError):
    """A stable model catalog or selection failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeModelDescriptor:
    """Safe model facts whose selection key maps to the Adapter global index."""

    selection_key: str
    display_name: str
    model_name: str
    provider: str = ""
    reasoning_level: str = ""
    is_default: bool = False
    is_agentos: bool = False
    is_current: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh, serialization-safe projection."""
        return {
            "selection_key": self.selection_key,
            "display_name": self.display_name,
            "model_name": self.model_name,
            "provider": self.provider,
            "reasoning_level": self.reasoning_level,
            "is_default": self.is_default,
            "is_agentos": self.is_agentos,
            "is_current": self.is_current,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCatalogResult:
    """Configured chat models plus the effective request selection."""

    models: tuple[RuntimeModelDescriptor, ...]
    current_selection: str = ""
    current_display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh representation without model connection secrets."""
        return {
            "models": [item.to_dict() for item in self.models],
            "current_selection": self.current_selection,
            "current_display_name": self.current_display_name,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSelectionResult:
    """A validated, request-scoped model selection."""

    model: RuntimeModelDescriptor
    session_id: str = ""
    persisted: bool = False


def _safe_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _reasoning_level(value: Any) -> str:
    if value is False:
        return "off"
    return _safe_text(value)


def _descriptor(
    global_index: int, entry: Mapping[str, Any]
) -> RuntimeModelDescriptor | None:
    client_config = entry.get("model_client_config")
    if not isinstance(client_config, Mapping):
        return None
    model_name = _safe_text(client_config.get("model_name"))
    alias = _safe_text(entry.get("alias"))
    display_name = alias or model_name
    if (
        not model_name
        or model_name.lower() in _RESERVED_MULTIMODAL_NAMES
        or display_name.lower() in _RESERVED_MULTIMODAL_NAMES
    ):
        return None

    model_config = entry.get("model_config_obj")
    model_config = model_config if isinstance(model_config, Mapping) else {}
    is_agentos = model_config.get("_source") == "agentos"
    return RuntimeModelDescriptor(
        # Agent Adapter interprets the suffix as the position in the complete
        # get_default_models() sequence, not as a per-name occurrence index.
        selection_key=f"{model_name}#{global_index}",
        display_name=display_name,
        model_name=model_name,
        provider=_safe_text(client_config.get("client_provider")),
        reasoning_level=_reasoning_level(model_config.get("reasoning_level")),
        is_default=bool(entry.get("is_default")) and not is_agentos,
        is_agentos=is_agentos,
    )


def _resolve_from_catalog(
    models: Sequence[RuntimeModelDescriptor],
    requested: str,
) -> RuntimeModelDescriptor | None:
    exact = next((item for item in models if item.selection_key == requested), None)
    if exact is not None:
        return exact

    named = tuple(item for item in models if item.model_name == requested)
    if named:
        return next((item for item in named if item.is_default), named[0])

    aliases = tuple(
        item
        for item in models
        if item.display_name == requested and item.display_name != item.model_name
    )
    return aliases[0] if len(aliases) == 1 else None


def build_model_catalog(
    entries: Iterable[Mapping[str, Any] | object],
    *,
    current_selection: str = "",
) -> ModelCatalogResult:
    """Build a safe catalog while preserving original global list indexes.

    ``entries`` may be a one-shot iterable. It is consumed exactly once, and
    skipped entries still retain their position in subsequent selection keys.
    """
    model_items: list[RuntimeModelDescriptor] = []
    for global_index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        descriptor = _descriptor(global_index, entry)
        if descriptor is not None:
            model_items.append(descriptor)
    models = tuple(model_items)
    if not models:
        return ModelCatalogResult(models=())

    requested = _safe_text(current_selection)
    current = _resolve_from_catalog(models, requested) if requested else None
    if current is None:
        current = next((item for item in models if item.is_default), models[0])
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
    """Resolve an exact key or an unambiguous human-facing model reference."""
    target = _safe_text(requested)
    if not target:
        raise ModelCatalogError("model selection is required", code="BAD_REQUEST")
    if target.lower() in _RESERVED_MULTIMODAL_NAMES:
        raise ModelCatalogError(
            "video, audio, and vision are multimodal-only model profiles",
            code="BAD_REQUEST",
        )
    selected = _resolve_from_catalog(catalog.models, target)
    if selected is None:
        matching_aliases = tuple(
            item
            for item in catalog.models
            if item.display_name == target and item.display_name != item.model_name
        )
        if len(matching_aliases) > 1:
            raise ModelCatalogError(
                "multiple models match; use a selection key from the catalog",
                code="BAD_REQUEST",
            )
        raise ModelCatalogError("model not found", code="NOT_FOUND")
    return replace(selected, is_current=True)


def _resolve_configured_model_entry(
    entries: Iterable[Mapping[str, Any] | object],
    requested: str,
) -> Mapping[str, Any] | None:
    """Resolve the original entry using the Adapter's global-index grammar.

    This internal helper can return credentials. It is intentionally separate
    from the safe descriptor and must never be serialized as catalog output.
    """
    materialized = tuple(entries)
    target = _safe_text(requested)
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
        if not 0 <= global_index < len(materialized):
            return None
        entry = materialized[global_index]
        if not isinstance(entry, Mapping):
            return None
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, Mapping):
            return None
        configured_name = _safe_text(client_config.get("model_name"))
        return entry if configured_name == bare_name else None

    named: list[Mapping[str, Any]] = []
    for entry in materialized:
        if not isinstance(entry, Mapping):
            continue
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, Mapping):
            continue
        if _safe_text(client_config.get("model_name")) == target:
            named.append(entry)
    if named:
        return next(
            (entry for entry in named if entry.get("is_default") is True), named[0]
        )
    aliases = tuple(
        entry
        for entry in materialized
        if isinstance(entry, Mapping) and _safe_text(entry.get("alias")) == target
    )
    return aliases[0] if len(aliases) == 1 else None


__all__ = [
    "ModelCatalogError",
    "ModelCatalogResult",
    "ModelSelectionResult",
    "RuntimeModelDescriptor",
    "build_model_catalog",
    "resolve_model_selection",
]
