"""One-time, idempotent migration from unambiguous model names to stable IDs."""

from __future__ import annotations

import json
from collections import defaultdict

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.utils import get_agent_sessions_dir


def _name_map(catalog: ModelCatalog | None = None) -> dict[str, str]:
    candidates: dict[str, list[str]] = defaultdict(list)
    for model in (catalog or ModelCatalog()).list_public_models():
        for name in {str(model.get("model_name") or ""), str(model.get("alias") or "")}:
            if name and model["model_id"] not in candidates[name]:
                candidates[name].append(model["model_id"])
    return {name: ids[0] for name, ids in candidates.items() if len(ids) == 1}


def migrate_legacy_model_selections() -> dict[str, int]:
    from jiuwenswarm.common.external_providers import provider

    update_session_model_selection = (
        provider("session_metadata").update_session_model_selection
    )

    mapping = _name_map()
    report = {"sessions": 0, "cron": 0, "ambiguous": 0}
    session_root = get_agent_sessions_dir()
    if session_root.exists():
        for directory in session_root.iterdir():
            path = directory / "metadata.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data.get("model_selection"), dict):
                    continue
                old = str(data.get("model") or "").strip()
                if not old:
                    continue
                model_id = mapping.get(old)
                if not model_id:
                    report["ambiguous"] += 1
                    continue
                update_session_model_selection(
                    directory.name, {"type": "model", "id": model_id},
                    expected_selection=("model_selection" in data, data.get("model_selection")),
                )
                report["sessions"] += 1
            except (OSError, ValueError):
                continue
    return report


async def migrate_legacy_cron_selections(store, *, catalog=None, user_id="") -> int:
    """Migrate only this user's jobs through the configured store's atomic patch."""
    mapping = _name_map(catalog)
    changed = 0
    for job in await store.list_jobs(strict=True):
        if user_id is not None and job.user_id != user_id:
            continue
        if job.model_selection is not None:
            continue
        model_id = mapping.get(job.model_name or "")
        if model_id:
            await store.update_job(job.id, {
                "model_selection": {"type": "model", "id": model_id},
                "_expected_legacy_model_name": job.model_name,
            })
            changed += 1
    return changed
