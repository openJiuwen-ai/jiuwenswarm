"""Read-only catalog for configured models and model groups."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.config import get_config, load_models_config
from jiuwenswarm.common.model_errors import MODEL_SELECTION_NOT_FOUND, ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection


@dataclass(frozen=True)
class SelectionReference:
    scope: str
    scope_id: str


class ModelCatalog:
    """Business catalog facade; credentials are only exposed to the resolver."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else get_config()
        self.snapshot = load_models_config(self.config)

    def get_model(self, model_id: str) -> dict[str, Any]:
        hit = self.snapshot["by_id"].get(model_id)
        if hit is None:
            raise ModelSelectionError(MODEL_SELECTION_NOT_FOUND, f"unknown model_id {model_id!r}")
        return hit

    def get_group(self, group_id: str) -> dict[str, Any]:
        for group in self.snapshot["groups"]:
            if isinstance(group, dict) and group.get("model_group_id") == group_id:
                return group
        raise ModelSelectionError(MODEL_SELECTION_NOT_FOUND, f"unknown model_group_id {group_id!r}")

    @staticmethod
    def _safe_model(entry: dict[str, Any], source: str) -> dict[str, Any]:
        mcc = entry.get("model_client_config") or {}
        mco = entry.get("model_config_obj") or {}
        return {
            "model_id": entry.get("model_id"), "alias": entry.get("alias", ""),
            "model_name": mcc.get("model_name", ""), "provider": mcc.get("client_provider", ""),
            "source": source, "is_agentos": source == "agentos", "is_default": bool(entry.get("is_default")),
            "enabled": bool(mcc.get("model_name")), "context_window": mco.get("context_window"),
        }

    def list_public_models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for source in ("defaults", "agentos"):
            for entry in self.snapshot[source]:
                if isinstance(entry, dict) and entry.get("model_id"):
                    result.append(self._safe_model(entry, source))
        return result

    def list_public_groups(self) -> list[dict[str, Any]]:
        keys = ("model_group_id", "display_name", "enabled", "is_default", "routes", "request_config", "routing")
        return [{key: group.get(key) for key in keys} for group in self.snapshot["groups"] if isinstance(group, dict)]

    def get_public_model_detail(self, model_id: str) -> dict[str, Any]:
        hit = self.get_model(model_id)
        entry = deepcopy(hit["entry"])
        client = entry.get("model_client_config")
        write_only: list[str] = []
        if isinstance(client, dict):
            if "api_key" in client:
                client.pop("api_key", None)
                write_only.append("model_client_config.api_key")
            if "custom_headers" in client:
                client.pop("custom_headers", None)
                write_only.append("model_client_config.custom_headers")
        entry.update(source=hit["source"], is_agentos=hit["source"] == "agentos", read_only=hit["source"] == "agentos", write_only_fields=write_only)
        return entry

    def find_references(self, selection: ModelSelection) -> list[SelectionReference]:
        refs: list[SelectionReference] = []
        if selection.type == "model":
            for group in self.snapshot["groups"]:
                routes = group.get("routes") or [] if isinstance(group, dict) else []
                if any(isinstance(route, dict) and route.get("model_id") == selection.id for route in routes):
                    refs.append(SelectionReference("model_group", str(group.get("model_group_id") or "")))
        return refs

