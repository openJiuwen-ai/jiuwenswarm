# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent state for GitCode issue auto-harness runs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _write_json(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


class IssueStateStore:
    """Stores issue processing state to avoid duplicate auto-harness runs."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "issue" / "gitcode-issues-status.json"
        self._cache: Optional[dict[str, Any]] = None
        self._save_lock = asyncio.Lock()

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {"issues": {}, "last_updated": None}
            return self._cache
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {"issues": {}, "last_updated": None}
        if not isinstance(loaded, dict):
            loaded = {"issues": {}, "last_updated": None}
        loaded.setdefault("issues", {})
        self._cache = loaded
        return loaded

    async def _save(self, data: dict[str, Any]) -> None:
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._cache = data
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        async with self._save_lock:
            await asyncio.to_thread(_write_json, self._path, payload)

    @staticmethod
    def issue_key(owner: str, repo: str, number: int) -> str:
        return f"{owner}/{repo}#{number}"

    def get(self, owner: str, repo: str, number: int) -> Optional[dict[str, Any]]:
        return self._load().get("issues", {}).get(self.issue_key(owner, repo, number))

    def list(self) -> list[dict[str, Any]]:
        issues = self._load().get("issues", {})
        if not isinstance(issues, dict):
            return []
        return list(issues.values())

    async def update(self, owner: str, repo: str, number: int, updates: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        issues = data.setdefault("issues", {})
        key = self.issue_key(owner, repo, number)
        record = dict(issues.get(key) or {})
        record.update(updates)
        record["key"] = key
        record["owner"] = owner
        record["repo"] = repo
        record["number"] = number
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        issues[key] = record
        await self._save(data)
        return record

    async def delete(self, owner: str, repo: str, number: int) -> bool:
        """删除指定 issue 的处理记录。"""
        data = self._load()
        issues = data.setdefault("issues", {})
        key = self.issue_key(owner, repo, number)
        if key not in issues:
            return False
        del issues[key]
        await self._save(data)
        return True
