"""One-time, idempotent migration from unambiguous model names to stable IDs."""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict

import portalocker

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.utils import get_agent_sessions_dir, get_cron_jobs_path


def _name_map() -> dict[str, str]:
    candidates: dict[str, list[str]] = defaultdict(list)
    for model in ModelCatalog().list_public_models():
        for name in {str(model.get("model_name") or ""), str(model.get("alias") or "")}:
            if name and model["model_id"] not in candidates[name]:
                candidates[name].append(model["model_id"])
    return {name: ids[0] for name, ids in candidates.items() if len(ids) == 1}


def _write(path, data) -> None:
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def migrate_legacy_model_selections() -> dict[str, int]:
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
                data["model_selection"] = {"type": "model", "id": model_id}
                _write(path, data)
                report["sessions"] += 1
            except (OSError, ValueError):
                continue
    cron_path = get_cron_jobs_path()
    if cron_path.is_file():
        lock_path = cron_path.with_suffix(cron_path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=10):
            data = json.loads(cron_path.read_text(encoding="utf-8"))
            items = data.get("jobs", []) if isinstance(data, dict) else data
            changed = False
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or isinstance(item.get("model_selection"), dict):
                    continue
                old = str(item.get("model_name") or "").strip()
                if not old:
                    continue
                model_id = mapping.get(old)
                if model_id:
                    item["model_selection"] = {"type": "model", "id": model_id}
                    report["cron"] += 1
                    changed = True
                else:
                    report["ambiguous"] += 1
            if changed:
                _write(cron_path, data)
    return report

