"""Unified read-only catalog for defaults, AgentOS models, and model groups."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config, load_models_config
from jiuwenswarm.common.model_errors import MODEL_SELECTION_NOT_FOUND, ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection

logger = logging.getLogger(__name__)

_PUBLIC_GROUP_KEYS = (
    "model_group_id",
    "display_name",
    "enabled",
    "is_default",
    "routes",
    "request_config",
    "routing",
)


def _read_session_selection(path: Path) -> dict[str, Any] | None:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Failed to read session model selection from %s", path, exc_info=True)
        return None
    value = metadata.get("model_selection")
    return value if isinstance(value, dict) else None


def _matches(selection: ModelSelection, value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == selection.type
        and value.get("id") == selection.id
    )


def _find_embedded_selections(
    value: Any,
    selection: ModelSelection,
    *,
    scope: str,
    path: tuple[str, ...] = (),
    owner_id: str = "",
) -> list[SelectionReference]:
    """Find stable selections in Agent/Team configuration trees."""
    refs: list[SelectionReference] = []
    if isinstance(value, dict):
        current_owner = str(value.get("id") or value.get("name") or owner_id)
        if _matches(selection, value.get("model_selection")):
            scope_id = current_owner or ".".join(path) or scope
            refs.append(SelectionReference(scope, scope_id))
        for key, child in value.items():
            refs.extend(_find_embedded_selections(
                child, selection, scope=scope, path=(*path, str(key)), owner_id=current_owner,
            ))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.extend(_find_embedded_selections(
                child, selection, scope=scope, path=(*path, str(index)), owner_id=owner_id,
            ))
    return refs


@dataclass(frozen=True)
class SelectionReference:
    scope: str
    scope_id: str


class ModelCatalog:
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
        result = []
        for source in ("defaults", "agentos"):
            for entry in self.snapshot[source]:
                if isinstance(entry, dict) and entry.get("model_id"):
                    result.append(self._safe_model(entry, source))
        return result

    def get_public_model_detail(self, model_id: str) -> dict[str, Any]:
        """Return an editable model DTO without write-only credentials.

        Updates are merge-based, so omitted write-only fields remain unchanged.
        AgentOS models are intentionally exposed as read-only catalog entries.
        """
        hit = self.get_model(model_id)
        entry = deepcopy(hit["entry"])
        client = entry.get("model_client_config")
        write_only_fields: list[str] = []
        if isinstance(client, dict):
            if "api_key" in client:
                client.pop("api_key", None)
                write_only_fields.append("model_client_config.api_key")
            if "custom_headers" in client:
                client.pop("custom_headers", None)
                write_only_fields.append("model_client_config.custom_headers")
        entry["source"] = hit["source"]
        entry["is_agentos"] = hit["source"] == "agentos"
        entry["read_only"] = hit["source"] == "agentos"
        entry["write_only_fields"] = write_only_fields
        return entry

    def list_public_groups(self) -> list[dict[str, Any]]:
        result = []
        for group in self.snapshot["groups"]:
            if not isinstance(group, dict):
                continue
            public = {}
            for key in _PUBLIC_GROUP_KEYS:
                if key == "routing":
                    continue
                value = deepcopy(group.get(key))
                public[key] = value
            result.append(public)
        return result

    def find_references(self, selection: ModelSelection) -> list[SelectionReference]:
        refs: list[SelectionReference] = []
        if selection.type == "model":
            for group in self.snapshot["groups"]:
                routes = group.get("routes") or []
                if any(isinstance(route, dict) and route.get("model_id") == selection.id for route in routes):
                    refs.append(SelectionReference("model_group", str(group.get("model_group_id") or "")))
        try:
            from jiuwenswarm.common.utils import get_agent_sessions_dir
            root = get_agent_sessions_dir()
            if root.exists():
                for directory in root.iterdir():
                    path = directory / "metadata.json"
                    if not path.is_file():
                        continue
                    value = _read_session_selection(path)
                    if _matches(selection, value):
                        refs.append(SelectionReference("session", directory.name))
        except (OSError, RuntimeError):
            logger.warning("Failed to scan session model references", exc_info=True)
        try:
            from jiuwenswarm.common.utils import get_cron_jobs_path
            path = get_cron_jobs_path()
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            items = data.get("jobs", []) if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                value = item.get("model_selection") if isinstance(item, dict) else None
                if _matches(selection, value):
                    refs.append(SelectionReference("cron", str(item.get("id") or "")))
        except (OSError, ValueError):
            logger.warning("Failed to scan cron model references", exc_info=True)
        refs.extend(_find_embedded_selections(self.config.get("agents"), selection, scope="agent"))
        refs.extend(_find_embedded_selections(self.config.get("team"), selection, scope="team"))
        # A malformed tree can contain the same selection through aliases. Keep
        # the public result deterministic and avoid duplicate delete warnings.
        return list(dict.fromkeys(refs))
