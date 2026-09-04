"""Unified read-only catalog for defaults, AgentOS models, and model groups."""

from __future__ import annotations

import json
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
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.snapshot = load_models_config(config or get_config())

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
        return [
            self._safe_model(entry, source)
            for source in ("defaults", "agentos")
            for entry in self.snapshot[source]
            if isinstance(entry, dict) and entry.get("model_id")
        ]

    def list_public_groups(self) -> list[dict[str, Any]]:
        result = []
        for group in self.snapshot["groups"]:
            if not isinstance(group, dict):
                continue
            public = {k: group.get(k) for k in (
                "model_group_id", "display_name", "enabled", "is_default",
                "routes", "request_config", "routing",
            )}
            result.append(public)
        return result

    def find_references(self, selection: ModelSelection) -> list[SelectionReference]:
        refs: list[SelectionReference] = []
        if selection.type == "model":
            for group in self.snapshot["groups"]:
                if any(isinstance(r, dict) and r.get("model_id") == selection.id for r in group.get("routes") or []):
                    refs.append(SelectionReference("model_group", str(group.get("model_group_id") or "")))
        try:
            from jiuwenswarm.common.utils import get_agent_sessions_dir
            root = get_agent_sessions_dir()
            if root.exists():
                for directory in root.iterdir():
                    path = directory / "metadata.json"
                    if not path.is_file():
                        continue
                    try:
                        value = json.loads(path.read_text(encoding="utf-8")).get("model_selection")
                    except Exception:
                        continue
                    if isinstance(value, dict) and value.get("type") == selection.type and value.get("id") == selection.id:
                        refs.append(SelectionReference("session", directory.name))
        except Exception:
            pass
        try:
            from jiuwenswarm.common.utils import get_cron_jobs_path
            path = get_cron_jobs_path()
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            items = data.get("jobs", []) if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                value = item.get("model_selection") if isinstance(item, dict) else None
                if isinstance(value, dict) and value.get("type") == selection.type and value.get("id") == selection.id:
                    refs.append(SelectionReference("cron", str(item.get("id") or "")))
        except Exception:
            pass
        return refs
