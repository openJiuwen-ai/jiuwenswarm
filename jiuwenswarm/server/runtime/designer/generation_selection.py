# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design mode's own image and video model choice.

The choice is global for Design: every canvas uses the same selected model
for a modality. It is stored beside the multimodal catalogue and resolved
against that catalogue. Settings → Agent → Tools (the enabled switch and the
single bound model) is not consulted. When no choice has been saved, or the
saved model is no longer in the catalogue, the first model of that modality
is used.
"""

from __future__ import annotations

from dataclasses import dataclass

from jiuwenswarm.common.config import config_write_lock, dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.multimodal_models import MultimodalError, valid_model_id
from jiuwenswarm.common.multimodal_store import MultimodalStore
from jiuwenswarm.common.utils import get_config_file

DESIGN_GENERATION_SLOTS = ("video_gen", "visual_gen")
# Flat config.set keys. The gateway already forwards config.set; these names
# do not match the agent tool slots, so they do not rewrite that binding.
DESIGN_GENERATION_CONFIG_KEYS = {
    "design_generation_video_gen": "video_gen",
    "design_generation_visual_gen": "visual_gen",
}


class DesignSelectionError(Exception):
    """The design generation choice could not be saved."""


@dataclass(frozen=True)
class CatalogueModel:
    """A modality resolved against the stored multimodal catalogue.

    ``present`` is false when the catalogue has not been created yet. Callers
    that still have to run without one (unit tests, a process that has not
    migrated settings) keep their previous environment-slot behavior.
    ``model`` is none when the catalogue exists but has no model for the slot.
    """

    present: bool
    model: dict | None = None


def _saved_ids(raw: dict) -> dict[str, str]:
    block = raw.get("design_generation")
    if not isinstance(block, dict):
        return {}
    saved: dict[str, str] = {}
    for slot in DESIGN_GENERATION_SLOTS:
        value = block.get(slot)
        if isinstance(value, str) and value.strip():
            saved[slot] = value.strip()
    return saved


def _models_for(state: dict, slot: str) -> list[dict]:
    models = state.get("models")
    if not isinstance(models, list):
        return []
    return [
        model for model in models
        if isinstance(model, dict) and slot in (model.get("capabilities") or [])
    ]


def resolve_model(state: dict, saved: dict[str, str], slot: str) -> dict | None:
    """The saved model when it is still listed, otherwise the first of that slot."""
    models = _models_for(state, slot)
    chosen = next((model for model in models if model.get("model_id") == saved.get(slot)), None)
    if chosen is not None:
        return chosen
    return models[0] if models else None


def _catalogue_is_stored(path) -> bool:
    try:
        raw = load_yaml_round_trip(path) or {}
    except Exception:
        return False
    return isinstance(raw, dict) and isinstance(raw.get("multimodal"), dict)


def _load_locked(path) -> tuple[dict, dict]:
    with config_write_lock(config_path=path):
        return MultimodalStore()._load(path)


def catalogue_generation_model(slot: str) -> CatalogueModel:
    """Resolve one design modality from the multimodal catalogue."""
    if slot not in DESIGN_GENERATION_SLOTS:
        raise DesignSelectionError("Unknown design generation slot.")
    path = get_config_file()
    if not path.is_file() or not _catalogue_is_stored(path):
        return CatalogueModel(False)
    try:
        raw, state = _load_locked(path)
    except MultimodalError:
        return CatalogueModel(True)
    return CatalogueModel(True, resolve_model(state, _saved_ids(raw), slot))


def generation_selection_view() -> dict[str, str | None]:
    """Resolved model ids. Missing modalities are null, not the tool binding."""
    path = get_config_file()
    if not path.is_file() or not _catalogue_is_stored(path):
        return {slot: None for slot in DESIGN_GENERATION_SLOTS}
    try:
        raw, state = _load_locked(path)
    except MultimodalError:
        return {slot: None for slot in DESIGN_GENERATION_SLOTS}
    saved = _saved_ids(raw)
    view: dict[str, str | None] = {}
    for slot in DESIGN_GENERATION_SLOTS:
        model = resolve_model(state, saved, slot)
        model_id = model.get("model_id") if model else None
        view[slot] = model_id if isinstance(model_id, str) and model_id else None
    return view


def saved_generation_config() -> dict[str, str]:
    """Raw saved ids for config.get. Empty when that modality has no choice yet."""
    path = get_config_file()
    raw: dict = {}
    if path.is_file():
        try:
            loaded = load_yaml_round_trip(path) or {}
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            raw = loaded
    saved = _saved_ids(raw)
    return {key: saved.get(slot, "") for key, slot in DESIGN_GENERATION_CONFIG_KEYS.items()}


def apply_design_generation_config(params: dict) -> list[str]:
    """Persist design choices carried by a config.set payload. Blank values are left unchanged."""
    updated: list[str] = []
    for key, slot in DESIGN_GENERATION_CONFIG_KEYS.items():
        if key not in params:
            continue
        value = params[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        set_generation_selection(slot, value)
        updated.append(key)
    return updated


def set_generation_selection(slot: object, model_id: object) -> dict[str, str | None]:
    """Remember one design-wide choice. The id must already be in that modality."""
    if slot not in DESIGN_GENERATION_SLOTS:
        raise DesignSelectionError("Unknown design generation slot.")
    if not isinstance(model_id, str) or not valid_model_id(model_id):
        raise DesignSelectionError("model_id is invalid.")
    path = get_config_file()
    if not path.is_file() or not _catalogue_is_stored(path):
        raise DesignSelectionError("Multimodal models are not available.")
    with config_write_lock(config_path=path):
        try:
            raw, state = MultimodalStore()._load(path)
        except MultimodalError as exc:
            raise DesignSelectionError("Multimodal models are not available.") from exc
        match = next((model for model in _models_for(state, slot) if model.get("model_id") == model_id), None)
        if match is None:
            raise DesignSelectionError("That model is not available for this design generation type.")
        block = raw.get("design_generation")
        if not isinstance(block, dict):
            block = {}
            raw["design_generation"] = block
        block[slot] = model_id
        dump_yaml_round_trip(path, raw)
    return generation_selection_view()
