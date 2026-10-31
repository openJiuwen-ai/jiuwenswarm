# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session (task) named assets.

A task's files - uploads from the composer and media the agent generated - can be
given short names so they can be referenced with ``@name`` in the chat box. The
registry is independent of any other feature: it lives next to the session's own
data as ``<sessions>/<session_id>/session_assets.json`` and is served by four
stateless RPC methods:

- ``session.assets.list``: the session's assets.
- ``session.assets.register``: add files, one new asset per item (not deduped by
  path -- the same path attached again deliberately gets its own asset, the way
  re-attaching an image always does); the default name is the file name without
  extension, made unique. Callers are responsible for not re-registering a file
  they already registered themselves (see InputArea.tsx's per-draft tracking).
- ``session.assets.rename``: change an asset's name.
- ``session.assets.remove``: drop one asset by id (idempotent; does not touch the
  underlying file, only the registry entry).
"""
from __future__ import annotations

import json
import logging
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
        # Same "-N" convention as atomic_write_unique/unique_upload_path
        # (upload_storage.py), so a duplicate name reads the same way
        # whether the collision was resolved on disk (browser upload) or
        # only here (desktop local-path attachment, which never renames the
        # file itself).
        candidate, index = base, 1
        while candidate.casefold() in taken:
            suffix = f"-{index}"
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
        # A default name must stay unique against every name this session ever handed
        # out, not just the ones currently in use: renaming "1" to "2" (see
        # handle_session_assets_rename) must not free up "1" for the next unrelated
        # file that happens to share that stem, or the two would look like the same
        # file split across two names.
        taken = {str(a.get("name") or "").casefold() for a in assets}
        for asset in assets:
            taken.update(str(old).casefold() for old in asset.get("name_history") or ())
        added = False
        # Parallel to ``items``: the asset each input item became, or None when
        # skipped (missing path/file). One element per input, in order, so a
        # caller that registers several items in one call (InputArea.tsx) can
        # tell exactly which of its own drafts became which asset_id -- it
        # cannot infer that from ``assets`` alone once the same path is allowed
        # to back more than one asset (see below).
        created: list[dict[str, Any] | None] = []
        for item in items:
            if not isinstance(item, dict):
                created.append(None)
                continue
            path = str(item.get("path") or "").strip()
            # No path-based dedup here: a document re-attached from the same
            # local file (desktop path, never rewritten to a fresh file --
            # unlike an image, which is always re-encoded to a fresh unique
            # path on every attach, see media_attachments._store_image_item)
            # must still get its own new, distinctly-named asset each time it
            # is deliberately attached again, the same as an image would. The
            # caller (InputArea.tsx) is what guarantees a single draft is
            # never registered twice; every item that reaches here is a
            # deliberate, distinct attach action.
            if not path or not Path(path).is_file():
                created.append(None)
                continue
            wanted = str(item.get("name") or "").strip() or Path(path).stem
            try:
                base = validate_asset_name(wanted)
            except SessionAssetError:
                cleaned_stem = re.sub(r"[@\r\n\t]", "", Path(path).stem).strip()
                base = validate_asset_name(cleaned_stem[:_NAME_MAX] or "asset")
            name = self._unique_name(base, taken)
            taken.add(name.casefold())
            source = str(item.get("source") or "upload")
            asset = {
                "asset_id": f"asset_{secrets.token_hex(4)}",
                "name": name,
                "kind": _kind_of(path),
                "path": path,
                "source": source if source in ("upload", "generated") else "upload",
                "created_at": time.time(),
            }
            assets.append(asset)
            created.append(asset)
            added = True
        if added:
            self._save(session_id, assets)
        return {"assets": assets, "created": created}

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
        old_name = str(target.get("name") or "")
        if old_name and old_name.casefold() != name.casefold():
            target.setdefault("name_history", []).append(old_name)
        target["name"] = name
        self._save(session_id, assets)
        return {"assets": assets}

    async def handle_session_assets_remove(self, params: dict) -> dict:
        session_id = str(params.get("session_id") or "")
        asset_id = str(params.get("asset_id") or "").strip()
        assets = self._load(session_id)
        remaining = [a for a in assets if a["asset_id"] != asset_id]
        if len(remaining) != len(assets):
            self._save(session_id, remaining)
        return {"assets": remaining}


_MANAGER: SessionAssetManager | None = None


def get_session_asset_manager() -> SessionAssetManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SessionAssetManager()
    return _MANAGER
