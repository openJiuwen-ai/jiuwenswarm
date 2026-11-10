# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Files the task pane inserts into the document: an image, or a .pptx to import slides from.

``publish_office_asset`` copies a file of the conversation (its project,
the agent workspace, its uploads) into the conversation's ``office_assets``
directory and gives the model its descriptor (an opaque handle, and the size
and SHA-256 the task pane checks the download against). A task pane tool call
that names the handle gets a ``/file-api/download`` link for it in the request
to the task pane (``client_tools``); the model never sees the link.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import zipfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure

from jiuwenswarm.office.runtime import asset_roots, session_dir, session_id_of

MAX_ASSET_BYTES = 50 * 1024 * 1024
PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PURPOSES = {"image/png": "input_image", "image/jpeg": "input_image", PPTX_MIME_TYPE: "input_pptx"}
_HANDLE = re.compile(r"^oa_[A-Za-z0-9_-]{20,80}$")


@dataclass(frozen=True)
class OfficeAsset:
    handle: str
    purpose: str
    mime_type: str
    size: int
    sha256: str

    def descriptor(self) -> dict[str, Any]:
        """The reference the model passes to a task pane tool, which the task pane checks the download against."""
        return {"handle": self.handle, "purpose": self.purpose, "mime_type": self.mime_type,
                "size": self.size, "sha256": self.sha256}


def is_asset_handle(value: Any) -> bool:
    return isinstance(value, str) and _HANDLE.fullmatch(value) is not None


class OfficeAssetStore:
    """Each asset is ``<handle>.bin`` in the conversation's ``office_assets``."""

    def put(self, session_id: str, data: bytes) -> OfficeAsset:
        mime_type = detect_mime_type(data)
        handle = f"oa_{secrets.token_urlsafe(24)}"
        data_path = self._path(session_id, handle)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        asset = OfficeAsset(handle=handle, purpose=_PURPOSES[mime_type], mime_type=mime_type,
                            size=len(data), sha256=hashlib.sha256(data).hexdigest())
        _write_exclusive(data_path, data)
        return asset

    def download_url(self, session_id: str, handle: str) -> str:
        """A ``/file-api/download`` link to an asset this conversation published.

        The task pane checks the download against the descriptor's size and SHA-256.
        """
        from jiuwenswarm.common.file_download_token import WebFileDownloadManager, generate_file_download_token

        data_path = self._path(session_id, handle)
        if not data_path.is_file():
            raise ValueError(f"no asset {handle} in this conversation")
        token = generate_file_download_token(str(data_path), session_id)
        return WebFileDownloadManager.get_instance().generate_download_url(token)

    @staticmethod
    def _path(session_id: str, handle: str) -> Path:
        if not is_asset_handle(handle):
            raise ValueError("invalid asset handle")
        return session_dir(session_id) / "office_assets" / f"{handle}.bin"


def referenced_handles(value: Any) -> set[str]:
    """The asset handles anywhere in a tool call's arguments."""
    if isinstance(value, dict):
        found = {value["handle"]} if is_asset_handle(value.get("handle")) else set()
        return found.union(*(referenced_handles(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(referenced_handles(item) for item in value))
    return set()


def detect_mime_type(data: bytes) -> str:
    if not data or len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"an asset must hold 1 byte to {MAX_ASSET_BYTES // (1024 * 1024)} MB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04") and {"[Content_Types].xml", "ppt/presentation.xml"} <= _zip_names(data):
        return PPTX_MIME_TYPE
    raise ValueError("an asset must be a PNG or JPEG image or a .pptx presentation")


class PublishOfficeAssetTool(Tool):
    def __init__(self, store: OfficeAssetStore | None = None) -> None:
        super().__init__(ToolCard(
            name="publish_office_asset",
            description=(
                "Publish one PNG, JPEG or .pptx file from the active backend project/workspace so this "
                "document can use it; it is the only way image bytes reach the document. Obtain or generate the file "
                "in the backend first: it must already exist under the active project/workspace or this Session's "
                "uploads directory (images the user attached); this tool does not download, generate, or modify it. "
                "Pass the returned descriptor to the tool that inserts the file as that tool's schema says. "
                "Inspect an image's pixels, source, crop and watermark before inserting it, and never substitute a "
                "rectangle for a requested picture. Treat a logo's visible canvas as part of the asset: do not shrink "
                "a wordmark into an unreadable tile or put an opaque white logo box on a dark field; use a compatible "
                "placement or a transparent variant."
            ),
            input_params={
                "type": "object",
                "properties": {"absolute_path": {
                    "type": "string",
                    "description": "Absolute path to an existing image or .pptx inside the active backend project "
                                   "or workspace.",
                }},
                "required": ["absolute_path"],
            },
            exposure=ToolExposure.DEFERRED,
            idempotent=False,
        ))
        self._store = store or OfficeAssetStore()

    async def invoke(self, inputs: Any, **kwargs: Any) -> str:
        session_id = session_id_of(kwargs.get("session"))
        source = Path(str((inputs or {}).get("absolute_path") or ""))
        if not source.is_absolute():
            raise ValueError("absolute_path must be an absolute path")
        if source.is_symlink():
            raise ValueError("a symbolic link cannot be published")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{source} does not exist") from exc
        if not resolved.is_file():
            raise ValueError(f"{source} is not a regular file")
        if not any(resolved.is_relative_to(root) for root in _usable_roots(asset_roots(session_id))):
            raise ValueError(f"{source} is outside the project, the workspace and this Session's uploads")
        if resolved.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError(f"an asset must hold at most {MAX_ASSET_BYTES // (1024 * 1024)} MB")
        asset = self._store.put(session_id, resolved.read_bytes())
        return json.dumps(asset.descriptor(), ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)


def _usable_roots(roots: Iterable[Path]) -> list[Path]:
    """Existing roots, without a filesystem root or the home directory that would open every file."""
    resolved = (root.expanduser().resolve() for root in roots)
    return [root for root in resolved if root.is_dir() and root not in (Path(root.anchor), Path.home().resolve())]


def _zip_names(data: bytes) -> set[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return set(archive.namelist())
    except zipfile.BadZipFile:
        return set()


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with open(path, "xb", opener=_owner_only) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _owner_only(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)
