# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue matrix storage for tracking analyzed GitCode issues."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class IssueMatrixStore:
    """Manages issue matrix data for a specific repository.

    Storage layout:
        ~/.jiuwenswarm/auto-harness/issue/
        └── <owner>_<repo>.json

    Each file contains:
        {
            "owner": "...",
            "repo": "...",
            "last_scan": "2026-06-11T10:00:00Z",
            "total_open": 127,
            "matrix": [
                {
                    "number": 1272,
                    "title": "...",
                    "body": "...",
                    "labels": ["bug"],
                    "difficulty": "medium",
                    "updated_at": "2026-06-11T...",
                    "first_seen": "2026-06-01T...",
                    "last_analyzed": "2026-06-11T...",
                }
            ]
        }
    """

    def __init__(self, data_dir: Path):
        self._matrix_dir = data_dir / "issue"
        self._matrix_dir.mkdir(parents=True, exist_ok=True)

    def _matrix_path(self, owner: str, repo: str) -> Path:
        """Get matrix file path for a repository."""
        safe_owner = owner.replace("/", "_").replace("\\", "_")
        safe_repo = repo.replace("/", "_").replace("\\", "_")
        return self._matrix_dir / f"{safe_owner}_{safe_repo}_issue_matrix.json"

    def load(self, owner: str, repo: str) -> dict[str, Any]:
        """Load matrix for a repository, returns empty structure if not exists."""
        path = self._matrix_path(owner, repo)
        if not path.exists():
            return {
                "owner": owner,
                "repo": repo,
                "last_scan": None,
                "total_open": 0,
                "matrix": [],
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
        except Exception as e:
            logger.warning("[IssueMatrixStore] Failed to load matrix: %s", e)
            return {
                "owner": owner,
                "repo": repo,
                "last_scan": None,
                "total_open": 0,
                "matrix": [],
            }

    def save(self, owner: str, repo: str, data: dict[str, Any]) -> None:
        """Save matrix for a repository."""
        path = self._matrix_path(owner, repo)
        data["owner"] = owner
        data["repo"] = repo
        data["last_scan"] = datetime.now(timezone.utc).isoformat()
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(json_str, encoding="utf-8")
        logger.info("[IssueMatrixStore] Saved matrix for %s/%s: %d issues", owner, repo, len(data.get("matrix", [])))

    def get_entry(self, owner: str, repo: str, number: int) -> dict[str, Any] | None:
        """Get a specific issue entry from the matrix."""
        data = self.load(owner, repo)
        for entry in data.get("matrix", []):
            if entry.get("number") == number:
                return entry
        return None

    def remove_entry(self, owner: str, repo: str, number: int) -> bool:
        """Remove an issue entry from the matrix."""
        data = self.load(owner, repo)
        matrix = data.get("matrix", [])
        new_matrix = [e for e in matrix if e.get("number") != number]
        if len(new_matrix) == len(matrix):
            return False
        data["matrix"] = new_matrix
        data["total_open"] = len(new_matrix)
        self.save(owner, repo, data)
        return True
