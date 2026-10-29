# Gateway 侧副本，源文件 jiuwenswarm/common/model_migration.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
"""One-time, idempotent migration from unambiguous model names to stable IDs."""

from __future__ import annotations

from collections import defaultdict

from gateway.common.model_catalog import ModelCatalog


def _name_map(catalog: ModelCatalog | None = None) -> dict[str, str]:
    candidates: dict[str, list[str]] = defaultdict(list)
    for model in (catalog or ModelCatalog()).list_public_models():
        for name in {str(model.get("model_name") or ""), str(model.get("alias") or "")}:
            if name and model["model_id"] not in candidates[name]:
                candidates[name].append(model["model_id"])
    return {name: ids[0] for name, ids in candidates.items() if len(ids) == 1}


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
