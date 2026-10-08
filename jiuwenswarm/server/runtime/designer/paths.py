# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve Designer catalog + skills package and trajectory directories."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from jiuwenswarm.common.utils import get_user_workspace_dir

# Local catalog package name; trajectories live under the data dir instead.
_PACKAGE_DIRNAME = "designer_catalog_skills_reports_trajectory"
# Older checkouts may still use the previous folder name.
_LEGACY_PACKAGE_DIRNAMES = ("designer_catalog_and_skills",)
_CATALOG_JSON = "designer_node_catalog.json"
_CATALOG_TXT = "designer_node_catalog.txt"
_SAFE_TRAJECTORY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve()
    # .../jiuwenswarm/server/runtime/designer/paths.py
    repo_root = here.parents[4]
    package_root = here.parents[3]
    return [repo_root, package_root.parent, Path.cwd()]


@lru_cache(maxsize=1)
def designer_package_dir() -> Path:
    """Directory holding the local catalog JSON/TXT."""
    names = (_PACKAGE_DIRNAME, *_LEGACY_PACKAGE_DIRNAMES)
    for root in _repo_roots():
        for name in names:
            candidate = root / name
            if (candidate / _CATALOG_JSON).is_file() or (candidate / "skills").is_dir():
                return candidate
    return _repo_roots()[0] / _PACKAGE_DIRNAME


def catalog_json_path() -> Path:
    return designer_package_dir() / _CATALOG_JSON


def catalog_txt_path() -> Path:
    return designer_package_dir() / _CATALOG_TXT


def skills_dir() -> Path:
    """In-repo Designer skill markdown.

    Scenario, director, agent, subject, and style files live next to this
    package. The gitignored ``designer_catalog_skills_reports_trajectory``
    directory is for local catalogs only.
    """
    return Path(__file__).resolve().parent / "skills"


def design_trajectory_dir() -> Path:
    """Sibling of the session trajectory store under ``<data dir>/.trace``."""
    return get_user_workspace_dir() / ".trace" / "designer"


def design_trajectory_key(project_id: str) -> str:
    """File stem shared by every run of one design project.

    A design project owns exactly one graph, so the project id (visible in the
    URL) identifies the trajectory files for all agent actions in that project.
    """
    raw = str(project_id).strip()
    if _SAFE_TRAJECTORY_KEY.fullmatch(raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def design_otlp_trajectory_path(key: str) -> Path:
    return design_trajectory_dir() / f"{key}.otlp.jsonl"


def design_record_trajectory_path(key: str) -> Path:
    return design_trajectory_dir() / f"{key}.design.jsonl"
