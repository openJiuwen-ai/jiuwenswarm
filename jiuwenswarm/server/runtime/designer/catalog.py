# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load the Designer node catalog from designer_catalog_skills_reports_trajectory/."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from jiuwenswarm.server.runtime.designer.paths import catalog_json_path, designer_package_dir

logger = logging.getLogger(__name__)


def _candidate_paths():
    primary = catalog_json_path()
    package = designer_package_dir()
    # Fallbacks for older checkouts that still keep the file at repo root / legacy folder.
    return [
        primary,
        package.parent / "designer_node_catalog.json",
        package.parent / "designer_catalog_skills_reports_trajectory" / "designer_node_catalog.json",
        package.parent / "designer_catalog_and_skills" / "designer_node_catalog.json",
    ]


@lru_cache(maxsize=1)
def load_node_catalog() -> dict[str, Any]:
    for path in _candidate_paths():
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "nodes" not in data:
                raise ValueError(f"invalid designer catalog at {path}")
            logger.info(
                "Loaded designer node catalog from %s (%s nodes)",
                path,
                data.get("node_count"),
            )
            return data
    raise FileNotFoundError(
        "designer_node_catalog.json not found under designer_catalog_skills_reports_trajectory/; "
        "run scripts/generate_designer_node_catalog.py"
    )


def catalog_nodes_by_id() -> dict[str, dict[str, Any]]:
    catalog = load_node_catalog()
    nodes = catalog.get("nodes") or []
    return {str(n["id"]): n for n in nodes if isinstance(n, dict) and n.get("id")}


def scenario_template(scenario: str) -> list[str]:
    catalog = load_node_catalog()
    templates = catalog.get("scenario_templates") or {}
    raw = templates.get(scenario) or templates.get("video") or []
    return [str(item) for item in raw if isinstance(item, str)]
