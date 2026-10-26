# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway MessageHandler mixin: chunked file transfer (upload + download).

Download (Agent→Gateway via ``file.download.*``): only when YAML
``file_transfer.enabled`` is explicitly true or
``JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH`` (escape hatch). Enterprise default
download is OBS URL → Gateway proxy (not this mixin).
Upload (Gateway→Agent): skipped when ``is_enterprise()`` (MinIO URLs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jiuwenswarm.common.e2a.constants import (
    FILE_DOWNLOAD_CHUNK,
    FILE_DOWNLOAD_COMPLETE,
    FILE_DOWNLOAD_START,
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
    FILE_TRANSFER_START,
)
from jiuwenswarm.common.file_transfer_types import FileTransferStartParams
from jiuwenswarm.edition import is_enterprise

if TYPE_CHECKING:
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.common.schema.message import Message

logger = logging.getLogger(__name__)


class FileTransferMixin:
    """Chunked GW↔Agent file transfer helpers for MessageHandler."""

    _file_transfer_handler: Any

    def _ensure_file_transfer_handler(self) -> Any:
        if self._file_transfer_handler is None:
            from jiuwenswarm.gateway.file_transfer_handler import get_file_transfer_handler

            self._file_transfer_handler = get_file_transfer_handler()
        return self._file_transfer_handler

    async def _handle_file_transfer_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        session_id: str | None,
        channel_id: str,
        request_metadata: dict[str, Any] | None,
    ) -> None:
        """Handle AgentServer → Gateway file download events."""
        ft_handler = self._ensure_file_transfer_handler()
        if not ft_handler.enabled:
            logger.warning(
                "[MessageHandler] 收到文件下载事件但未启用分布式模式: event_type=%s",
                event_type,
            )
            return

        try:
            if event_type == FILE_DOWNLOAD_START:
                dl_params = FileTransferStartParams(
                    transfer_id=payload.get("transfer_id", ""),
                    filename=payload.get("filename", "unnamed"),
                    file_size=payload.get("file_size", 0),
                    sha256=payload.get("sha256", ""),
                    total_chunks=payload.get("total_chunks", 0),
                    chunk_size=payload.get("chunk_size", 65536),
                    mime_type=payload.get("mime_type", ""),
                    session_id=session_id or "",
                    channel_id=channel_id,
                )
                result = await ft_handler.handle_download_start(dl_params)
                logger.info(
                    "[MessageHandler] 文件下载开始: transfer_id=%s accepted=%s",
                    payload.get("transfer_id"),
                    result.get("accepted"),
                )

            elif event_type == FILE_DOWNLOAD_CHUNK:
                await ft_handler.handle_download_chunk(
                    transfer_id=payload.get("transfer_id", ""),
                    chunk_index=payload.get("chunk_index", 0),
                    base64_data=payload.get("base64_data", ""),
                )
                logger.debug(
                    "[MessageHandler] 文件下载分片: transfer_id=%s chunk=%d",
                    payload.get("transfer_id"),
                    payload.get("chunk_index"),
                )

            elif event_type == FILE_DOWNLOAD_COMPLETE:
                result = await ft_handler.handle_download_complete(
                    transfer_id=payload.get("transfer_id", ""),
                    sha256=payload.get("sha256", ""),
                )

                if result.get("success"):
                    file_path = result.get("file_path", "")
                    filename = Path(file_path).name
                    logger.info(
                        "[MessageHandler] 文件下载完成: transfer_id=%s path=%s channel_id=%s",
                        payload.get("transfer_id"),
                        file_path,
                        channel_id,
                    )

                    # Enterprise web: land on Gateway disk + token (no Web Pod).
                    # Personal / other channels: local build_file_download_info.
                    should_push_to_web = is_enterprise() and (channel_id == "web")

                    if should_push_to_web:
                        download_info = await self._push_file_to_web_and_get_token(
                            file_path, filename, session_id or ""
                        )
                        if not download_info:
                            logger.error(
                                "[MessageHandler] 企业版落地 Gateway 下载 Token 失败，"
                                "跳过 chat.file: %s",
                                filename,
                            )
                            return
                    else:
                        from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                            build_file_download_info,
                        )

                        download_info = build_file_download_info(
                            file_path=file_path,
                            file_name=filename,
                            session_id=session_id or "",
                        )

                    from jiuwenswarm.common.schema.message import EventType, Message

                    files_payload = [
                        {
                            "path": file_path,
                            "name": download_info["name"],
                            "size": download_info["size"],
                            "mime_type": download_info["mime_type"],
                            "download_url": download_info["download_url"],
                            "download_token": download_info["download_token"],
                            "expires_at": download_info.get("expires_at"),
                        }
                    ]

                    file_msg = Message(
                        id=f"file_{payload.get('transfer_id', '')}",
                        type="event",
                        channel_id=channel_id,
                        session_id=session_id,
                        params={},
                        timestamp=time.time(),
                        ok=True,
                        payload={
                            "event_type": EventType.CHAT_FILE.value,
                            "files": files_payload,
                        },
                        event_type=EventType.CHAT_FILE,
                        metadata=request_metadata,
                    )
                    await self.publish_robot_messages(file_msg)
                    logger.info(
                        "[MessageHandler] 已发送 chat.file 事件: channel_id=%s file=%s download_url=%s",
                        channel_id,
                        file_path,
                        download_info["download_url"],
                    )
                else:
                    logger.warning(
                        "[MessageHandler] 文件下载失败: transfer_id=%s error=%s",
                        payload.get("transfer_id"),
                        result.get("error"),
                    )

        except Exception as e:
            logger.exception(
                "[MessageHandler] 处理文件传输事件失败: event_type=%s error=%s",
                event_type,
                e,
            )

    def _should_transfer_files(self, env: "E2AEnvelope") -> bool:
        """Whether Gateway should chunk-upload local files to AgentServer.

        Enterprise attachments use MinIO URLs + materializer — never GW→Agent
        path transfer. Personal only when chunked transfer is explicitly
        enabled and local paths exist.
        """
        if is_enterprise():
            return False

        ft_handler = self._ensure_file_transfer_handler()
        if not ft_handler.enabled:
            return False

        params = env.params or {}
        files = params.get("files")
        if not files or not isinstance(files, list):
            return False

        for file_info in files:
            if isinstance(file_info, dict):
                path = file_info.get("path", "")
                if path and Path(path).exists():
                    return True
        return False

    async def _transfer_files_to_agent_server(
        self,
        env: "E2AEnvelope",
        msg: "Message",
    ) -> "E2AEnvelope":
        """Replace local ``params.files`` paths with AgentServer-side paths."""
        ft_handler = self._ensure_file_transfer_handler()
        params = dict(env.params or {})
        files = params.get("files", [])
        if not files:
            return env

        _ft_svc = str(env.service_id or "").strip()
        _ft_ag = str(env.agent_id or "").strip()

        async def send_callback(method: str, ft_params: dict[str, Any]) -> dict[str, Any]:
            if method == FILE_TRANSFER_START:
                start_params = FileTransferStartParams(
                    transfer_id=ft_params.get("transfer_id", ""),
                    filename=ft_params.get("filename", "unnamed"),
                    file_size=ft_params.get("file_size", 0),
                    sha256=ft_params.get("sha256", ""),
                    total_chunks=ft_params.get("total_chunks", 0),
                    chunk_size=ft_params.get("chunk_size", 65536),
                    mime_type=ft_params.get("mime_type", ""),
                    session_id=ft_params.get("session_id", "") or env.session_id or "",
                    channel_id=env.channel or "",
                    service_id=str(ft_params.get("service_id") or _ft_svc or ""),
                    agent_id=str(ft_params.get("agent_id") or _ft_ag or ""),
                )
                return await self.agent_client.file_transfer_start(start_params)
            if method == FILE_TRANSFER_CHUNK:
                return await self.agent_client.file_transfer_chunk(
                    transfer_id=ft_params.get("transfer_id", ""),
                    chunk_index=ft_params.get("chunk_index", 0),
                    base64_data=ft_params.get("base64_data", ""),
                    chunk_size=ft_params.get("chunk_size", 0),
                    channel_id=env.channel or "",
                    service_id=str(ft_params.get("service_id") or _ft_svc or ""),
                    agent_id=str(ft_params.get("agent_id") or _ft_ag or ""),
                    session_id=str(ft_params.get("session_id") or env.session_id or ""),
                )
            if method == FILE_TRANSFER_COMPLETE:
                return await self.agent_client.file_transfer_complete(
                    transfer_id=ft_params.get("transfer_id", ""),
                    sha256=ft_params.get("sha256", ""),
                    channel_id=env.channel or "",
                    service_id=str(ft_params.get("service_id") or _ft_svc or ""),
                    agent_id=str(ft_params.get("agent_id") or _ft_ag or ""),
                    session_id=str(ft_params.get("session_id") or env.session_id or ""),
                )
            return {"accepted": False, "error": f"unknown method: {method}"}

        semaphore = asyncio.Semaphore(ft_handler.config.max_concurrent_transfers)

        async def transfer_single_file(file_info: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                local_path = file_info.get("path", "")
                if not local_path or not Path(local_path).exists():
                    logger.warning(
                        "[MessageHandler] 文件不存在或路径无效: %s",
                        local_path,
                    )
                    return file_info

                try:
                    result = await ft_handler.send_file_to_agent_server(
                        file_path=local_path,
                        send_callback=send_callback,
                        session_id=env.session_id or "",
                        channel_id=env.channel or "",
                        request_id=env.request_id or "",
                        service_id=_ft_svc,
                        agent_id=_ft_ag,
                    )
                    if result.get("success"):
                        new_path = result.get("file_path", "")
                        logger.info(
                            "[MessageHandler] 文件传输成功: local=%s -> remote=%s",
                            local_path,
                            new_path,
                        )
                        updated_info = dict(file_info)
                        updated_info["path"] = new_path
                        updated_info["name"] = os.path.basename(new_path)
                        updated_info["size"] = result.get(
                            "file_size", file_info.get("size", 0)
                        )
                        updated_info["_transferred"] = True
                        updated_info["_original_path"] = local_path
                        updated_info["_original_name"] = file_info.get("name", "")
                        return updated_info

                    logger.warning(
                        "[MessageHandler] 文件传输失败: path=%s error=%s, 回退到本地模式",
                        local_path,
                        result.get("error", "unknown"),
                    )
                    return file_info
                except Exception as e:
                    logger.exception(
                        "[MessageHandler] 文件传输异常: path=%s error=%s",
                        local_path,
                        e,
                    )
                    return file_info

        transfer_tasks = [
            transfer_single_file(f) for f in files if isinstance(f, dict)
        ]
        if not transfer_tasks:
            return env

        logger.info(
            "[MessageHandler] 开始分布式文件传输: request_id=%s files=%d",
            env.request_id,
            len(transfer_tasks),
        )
        updated_files = await asyncio.gather(*transfer_tasks)
        params["files"] = (
            self._normalize_files_for_agent_dispatch(updated_files) or updated_files
        )
        updated_env = replace(env, params=params)
        transferred_count = sum(1 for f in updated_files if f.get("_transferred"))
        logger.info(
            "[MessageHandler] 文件传输完成: request_id=%s files=%d transferred=%d",
            env.request_id,
            len(files),
            transferred_count,
        )
        return updated_env

    async def _maybe_start_file_transfer_cleanup(self) -> None:
        ft_handler = self._ensure_file_transfer_handler()
        if ft_handler.enabled:
            await ft_handler.start_cleanup_task()

    async def _maybe_stop_file_transfer_cleanup(self) -> None:
        if self._file_transfer_handler is not None and self._file_transfer_handler.enabled:
            await self._file_transfer_handler.stop_cleanup_task()
