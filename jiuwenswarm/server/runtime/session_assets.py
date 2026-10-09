# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session (task) named assets.

A task's files - uploads from the composer and media the agent generated - can be
given short names so they can be referenced with ``@name`` in the chat box. The
registry is independent of any other feature: it lives next to the session's own
data as ``<sessions>/<session_id>/session_assets.json`` and is served by three
stateless RPC methods:

- ``session.assets.list``: the session's assets.
- ``session.assets.register``: add files (idempotent per file path; the default
  name is the file name without extension, made unique).
- ``session.assets.rename``: change an asset's name.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir

logger = logging.getLogger(__name__)

_STATE_FILE = "session_assets.json"
_NAME_MAX = 60
_FORBIDDEN_NAME_CHARS = re.compile(r"[@\r\n\t]")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
}


class SessionAssetError(Exception):
    """A request the registry rejects; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _kind_of(path: str) -> str:
    suffix = Path(path).suffix.lower()
    for kind, suffixes in _KINDS.items():
        if suffix in suffixes:
            return kind
    return "document"


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def validate_asset_name(name: str) -> str:
    """Return the cleaned name, or raise SessionAssetError."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise SessionAssetError("INVALID_NAME", "名称不能为空")
    if len(cleaned) > _NAME_MAX:
        raise SessionAssetError("INVALID_NAME", f"名称不能超过 {_NAME_MAX} 个字符")
    if _FORBIDDEN_NAME_CHARS.search(name or ""):
        raise SessionAssetError("INVALID_NAME", "名称不能包含 @ 或换行")
    return cleaned


class SessionAssetManager:
    """Stateless RPC handlers; each call is a full load -> change -> save round trip."""

    @staticmethod
    def _state_file(session_id: str) -> Path:
        sid = (session_id or "").strip()
        if not _SESSION_ID.match(sid):
            raise SessionAssetError("INVALID_PARAMS", "缺少或非法的 session_id")
        base = get_agent_sessions_dir().resolve()
        file = (base / sid / _STATE_FILE).resolve()
        if not file.is_relative_to(base):
            raise SessionAssetError("INVALID_PARAMS", "缺少或非法的 session_id")
        return file

    def _load(self, session_id: str) -> list[dict[str, Any]]:
        file = self._state_file(session_id)
        try:
            if file.exists():
                raw = json.loads(file.read_text(encoding="utf-8"))
                items = raw.get("assets") if isinstance(raw, dict) else None
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict) and item.get("asset_id")]
        except (OSError, ValueError):
            logger.exception("[SessionAssets] failed to read %s, starting empty", file)
        return []

    def _save(self, session_id: str, assets: list[dict[str, Any]]) -> None:
        file = self._state_file(session_id)
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"assets": assets}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(file)

    @staticmethod
    def _unique_name(base: str, taken: set[str]) -> str:
        candidate, index = base, 2
        while candidate.casefold() in taken:
            suffix = f" {index}"
            candidate = base[: _NAME_MAX - len(suffix)] + suffix
            index += 1
        return candidate

    async def handle_session_assets_list(self, params: dict) -> dict:
        session_id = str(params.get("session_id") or "")
        return {"assets": self._load(session_id)}

    async def handle_session_assets_register(self, params: dict) -> dict:
        session_id = str(params.get("session_id") or "")
        items = params.get("items")
        if not isinstance(items, list):
            raise SessionAssetError("INVALID_PARAMS", "缺少 items")
        assets = self._load(session_id)
        known_paths = {_path_key(str(a.get("path") or "")) for a in assets}
        taken = {str(a.get("name") or "").casefold() for a in assets}
        added = False
        for item in items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path or _path_key(path) in known_paths or not Path(path).is_file():
                continue
            wanted = str(item.get("name") or "").strip() or Path(path).stem
            try:
                base = validate_asset_name(wanted)
            except SessionAssetError:
                cleaned_stem = re.sub(r"[@\r\n\t]", "", Path(path).stem).strip()
                base = validate_asset_name(cleaned_stem[:_NAME_MAX] or "asset")
            name = self._unique_name(base, taken)
            taken.add(name.casefold())
            known_paths.add(_path_key(path))
            source = str(item.get("source") or "upload")
            assets.append(
                {
                    "asset_id": f"asset_{secrets.token_hex(4)}",
                    "name": name,
                    "kind": _kind_of(path),
                    "path": path,
                    "source": source if source in ("upload", "generated") else "upload",
                    "created_at": time.time(),
                }
            )
            added = True
        if added:
            self._save(session_id, assets)
        return {"assets": assets}

    async def handle_session_assets_rename(self, params: dict) -> dict:
        session_id = str(params.get("session_id") or "")
        asset_id = str(params.get("asset_id") or "").strip()
        name = validate_asset_name(str(params.get("name") or ""))
        assets = self._load(session_id)
        target = next((a for a in assets if a["asset_id"] == asset_id), None)
        if target is None:
            raise SessionAssetError("ASSET_NOT_FOUND", f"未找到素材: {asset_id}")
        if any(a is not target and str(a.get("name") or "").casefold() == name.casefold() for a in assets):
            raise SessionAssetError("NAME_CONFLICT", f"名称已被使用: {name}")
        target["name"] = name
        self._save(session_id, assets)
        return {"assets": assets}


_MANAGER: SessionAssetManager | None = None


def get_session_asset_manager() -> SessionAssetManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SessionAssetManager()
    return _MANAGER
