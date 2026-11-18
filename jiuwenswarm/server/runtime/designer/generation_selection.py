# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design mode's own image and video model choice.

The choice is global for Design: every canvas uses the same selected model
for a modality. It is stored beside the multimodal catalogue and resolved
against that catalogue. Settings → Agent → Tools (the enabled switch and the
single bound model) is not consulted. A saved id is used only while that model
is still listed and its protocol is one Design can call. Otherwise the first
supported model of that modality is used. Unsupported rows stay in the
catalogue; they are only left out of this choice. When none are supported,
the modality resolves to no model.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from jiuwenswarm.common.config import config_write_lock, dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.multimodal_models import MultimodalError, valid_model_id
from jiuwenswarm.common.multimodal_store import MultimodalStore
from jiuwenswarm.common.utils import get_config_file

DESIGN_GENERATION_SLOTS = ("video_gen", "visual_gen")
# Protocols whose native payload Design can call, including reference images.
# OpenAI-compatible chat completions are not in this set. Add a protocol here
# only together with the matching frontend whitelist.
DESIGN_GENERATION_PROTOCOLS = frozenset({"minimax", "modelark", "dashscope", "vllm-omni"})
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
    ``model`` is none when the catalogue exists but has no selectable model for the slot.
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


def design_generation_protocol_supported(protocol: object) -> bool:
    """True when Design can call this generation protocol."""
    return str(protocol or "").strip().lower() in DESIGN_GENERATION_PROTOCOLS


def _connection_protocol(model: dict) -> str:
    connection = model.get("connection")
    if not isinstance(connection, dict):
        return ""
    return str(connection.get("protocol") or "")


def _selectable_models(state: dict, slot: str) -> list[dict]:
    return [
        model for model in _models_for(state, slot)
        if design_generation_protocol_supported(_connection_protocol(model))
    ]


def resolve_model(state: dict, saved: dict[str, str], slot: str) -> dict | None:
    """The saved model when it is still selectable, otherwise the first selectable one.

    Rows whose protocol Design cannot call stay in the catalogue and keep a
    previously saved id, but they are not a choice. No selectable row resolves
    to no model, the same as an empty modality.
    """
    models = _selectable_models(state, slot)
    chosen = next((model for model in models if model.get("model_id") == saved.get(slot)), None)
    if chosen is not None:
        return chosen
    return models[0] if models else None


def _load_catalogue(path) -> tuple[str, dict | None, dict | None]:
    """One locked read and validate. Status is absent, error, or present."""
    if not path.is_file():
        return "absent", None, None
    try:
        with config_write_lock(config_path=path):
            raw, state = MultimodalStore()._load(path)
    except MultimodalError as exc:
        if exc.code == "MM_NOT_CONFIGURED":
            return "absent", None, None
        return "error", None, None
    except Exception:
        # Lock acquisition (recovery, timeout) must not fail the design operation.
        return "error", None, None
    return "present", raw, state


@dataclass(frozen=True)
class _CatalogueSnapshot:
    """One catalogue read, reused until the current operation ends."""

    status: str
    raw: dict | None = None
    state: dict | None = None


_CATALOGUE_SNAPSHOT: ContextVar[_CatalogueSnapshot | None] = ContextVar(
    "design_catalogue_snapshot",
    default=None,
)


def _snapshot_from_disk() -> _CatalogueSnapshot:
    status, raw, state = _load_catalogue(get_config_file())
    return _CatalogueSnapshot(status, raw, state)


def _current_snapshot() -> _CatalogueSnapshot:
    snap = _CATALOGUE_SNAPSHOT.get()
    if snap is not None:
        return snap
    return _snapshot_from_disk()


def _refresh_snapshot(raw: dict, state: dict) -> None:
    if _CATALOGUE_SNAPSHOT.get() is not None:
        _CATALOGUE_SNAPSHOT.set(_CatalogueSnapshot("present", raw, state))


@contextmanager
def reuse_catalogue_snapshot() -> Iterator[None]:
    """Load the multimodal catalogue once and reuse it for this operation.

    Nested calls keep the outer snapshot. The next operation loads again, so a
    model switch is visible afterwards. Node loops should enter this context
    once instead of re-parsing the YAML for every node.
    """
    if _CATALOGUE_SNAPSHOT.get() is not None:
        yield
        return
    token = _CATALOGUE_SNAPSHOT.set(_snapshot_from_disk())
    try:
        yield
    finally:
        _CATALOGUE_SNAPSHOT.reset(token)


def _model_from_snapshot(snap: _CatalogueSnapshot, slot: str) -> CatalogueModel:
    if snap.status == "absent":
        return CatalogueModel(False)
    if snap.status != "present" or snap.raw is None or snap.state is None:
        return CatalogueModel(True)
    return CatalogueModel(True, resolve_model(snap.state, _saved_ids(snap.raw), slot))


def catalogue_generation_model(slot: str) -> CatalogueModel:
    """Resolve one design modality from the multimodal catalogue."""
    if slot not in DESIGN_GENERATION_SLOTS:
        raise DesignSelectionError("Unknown design generation slot.")
    return _model_from_snapshot(_current_snapshot(), slot)


def generation_selection_view() -> dict[str, str | None]:
    """Resolved model ids. Missing modalities are null, not the tool binding."""
    snap = _current_snapshot()
    if snap.status != "present" or snap.raw is None or snap.state is None:
        return {slot: None for slot in DESIGN_GENERATION_SLOTS}
    saved = _saved_ids(snap.raw)
    view: dict[str, str | None] = {}
    for slot in DESIGN_GENERATION_SLOTS:
        model = resolve_model(snap.state, saved, slot)
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
    """Remember one design-wide choice. The id must be a selectable model of that modality."""
    if slot not in DESIGN_GENERATION_SLOTS:
        raise DesignSelectionError("Unknown design generation slot.")
    if not isinstance(model_id, str) or not valid_model_id(model_id):
        raise DesignSelectionError("model_id is invalid.")
    path = get_config_file()
    if not path.is_file():
        raise DesignSelectionError("Multimodal models are not available.")
    with config_write_lock(config_path=path):
        try:
            raw, state = MultimodalStore()._load(path)
        except MultimodalError as exc:
            raise DesignSelectionError("Multimodal models are not available.") from exc
        match = next((model for model in _models_for(state, slot) if model.get("model_id") == model_id), None)
        if match is None:
            raise DesignSelectionError("That model is not available for this design generation type.")
        if not design_generation_protocol_supported(_connection_protocol(match)):
            raise DesignSelectionError("That model's protocol is not supported for Design.")
        block = raw.get("design_generation")
        if not isinstance(block, dict):
            block = {}
            raw["design_generation"] = block
        block[slot] = model_id
        dump_yaml_round_trip(path, raw)
    _refresh_snapshot(raw, state)
    return generation_selection_view()
