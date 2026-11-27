# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Send File Toolkit

提供发送文件到用户的工具。支持发送一个或多个文件。

使用方式：
1. 创建 SendFileToolkit 实例
2. 调用 get_tools() 获取工具列表
3. 工具会自动注册到 Runner 中

分布式模式：
- 当 file_transfer.enabled=true 时，使用分片传输将文件发送到 Gateway
- Gateway 接收后调用 Channel API 发送给用户
"""

from __future__ import annotations

import json
import os
import logging
from typing import Any, List, Union

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.config import get_file_transfer_config
from jiuwenclaw.agentserver.file_transfer_manager import get_file_transfer_manager
from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


class SendFileToolkit:
    """Toolkit for sending files to users."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        channel_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Initialize SendFileToolkit.

        Args:
            request_id: Request identifier for message routing.
            session_id: Session identifier for message routing.
            channel_id: Channel identifier for message routing.
            metadata: 与 AgentRequest.metadata 一致（E2A channel_context 映射结果），用于 send_push。
        """
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        logger.debug(
            format_session_log(
                session_id,
                f"[SendFileToolkit] 初始化 request_id={request_id} "
                f"channel_id={channel_id} has_metadata={bool(self._request_metadata)}",
            )
        )

    async def send_file(self, abs_file_path_list: Union[List[str], str]) -> str:
        """Send files to user.

        Args:
            abs_file_path_list: List of absolute file paths to send.

        Returns:
            Success message or error description.
        """
        if isinstance(abs_file_path_list, str):
            try:
                parsed = json.loads(abs_file_path_list)
                if isinstance(parsed, list):
                    abs_file_path_list = parsed
                elif isinstance(parsed, str):
                    abs_file_path_list = [parsed]
                else:
                    abs_file_path_list = [abs_file_path_list]
            except (TypeError, ValueError):
                abs_file_path_list = [abs_file_path_list]

        if not isinstance(abs_file_path_list, list):
            abs_file_path_list = [str(abs_file_path_list)]

        valid_files = []
        missing_files = []
        for fp in abs_file_path_list:
            fp = str(fp).strip()
            if not fp:
                continue
            if os.path.isfile(fp):
                valid_files.append(fp)
            else:
                missing_files.append(fp)
                logger.warning("[SendFileToolkit] 文件不存在: %s", fp)

        if not valid_files:
            msg_parts = ["发送文件失败：所有文件均不存在"]
            for mf in missing_files:
                msg_parts.append(f"  - {mf}")
            return "\n".join(msg_parts)

        logger.info(
            format_session_log(
                self.session_id,
                f"[SendFileToolkit] send_file 开始 有效文件={len(valid_files)} 缺失={len(missing_files)}",
            )
        )

        # 检查是否启用分布式文件传输
        ft_config = get_file_transfer_config()
        if ft_config.enabled:
            return await self._send_file_distributed(valid_files, missing_files)
        else:
            return await self._send_file_local(valid_files, missing_files)

    async def _send_file_local(
        self,
        valid_files: List[str],
        missing_files: List[str],
    ) -> str:
        """本地模式：直接传递文件路径（原有逻辑）.

        Args:
            valid_files: 有效文件路径列表
            missing_files: 缺失文件路径列表

        Returns:
            结果消息
        """
        try:
            # Lazy import to avoid circular import
            from jiuwenclaw.agentserver.agent_ws_server import AgentWebSocketServer
            server = AgentWebSocketServer.get_instance()
            files_payload = [
                {
                    "path": file_path,
                    "name": os.path.basename(file_path),
                }
                for file_path in valid_files
            ]
            msg = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": {
                    "event_type": "chat.file",
                    "files": files_payload,
                },
                "is_complete": False,
            }
            if self._request_metadata:
                msg["metadata"] = dict(self._request_metadata)
            await server.send_push(msg)
            result_parts = [f"成功发送 {len(valid_files)} 个文件"]
            if missing_files:
                result_parts.append("以下文件不存在，未发送：")
                for mf in missing_files:
                    result_parts.append(f"  - {mf}")
            return "\n".join(result_parts)
        except Exception as e:
            logger.exception(
                format_session_log(self.session_id, f"[SendFileToolkit] _send_file_local 失败 error={str(e)}")
            )
            return f"提交文件失败: {str(e)}"

    async def _send_file_distributed(
        self,
        valid_files: List[str],
        missing_files: List[str],
    ) -> str:
        """分布式模式：通过分片传输发送文件到 Gateway.

        Args:
            valid_files: 有效文件路径列表
            missing_files: 缺失文件路径列表

        Returns:
            结果消息
        """
        ft_manager = get_file_transfer_manager()
        results = []
        success_count = 0
        failed_files = []

        for file_path in valid_files:
            try:
                # 定义发送回调函数
                async def send_callback(event_type: str, params: dict) -> None:
                    """发送文件传输事件到 Gateway."""
                    # Lazy import to avoid circular import
                    from jiuwenclaw.agentserver.agent_ws_server import AgentWebSocketServer
                    server = AgentWebSocketServer.get_instance()
                    msg = {
                        "request_id": self.request_id,
                        "channel_id": self.channel_id,
                        "session_id": self.session_id,
                        "payload": {
                            "event_type": event_type,
                            **params,
                        },
                        "is_complete": False,
                    }
                    await server.send_push(msg)

                # 使用 FileTransferManager 发送文件
                result = await ft_manager.send_file(
                    file_path=file_path,
                    send_callback=send_callback,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    request_id=self.request_id,
                )

                if result.get("success"):
                    success_count += 1
                    logger.info(
                        "[SendFileToolkit] 分布式发送成功: file=%s transfer_id=%s",
                        file_path,
                        result.get("transfer_id"),
                    )
                else:
                    failed_files.append({
                        "file": file_path,
                        "error": result.get("error", "unknown error"),
                    })
                    logger.warning(
                        "[SendFileToolkit] 分布式发送失败: file=%s error=%s",
                        file_path,
                        result.get("error"),
                    )

            except Exception as e:
                failed_files.append({
                    "file": file_path,
                    "error": str(e),
                })
                logger.exception(
                    "[SendFileToolkit] 分布式发送异常: file=%s",
                    file_path,
                )

        # 构建结果消息
        result_parts = []
        if success_count > 0:
            result_parts.append(f"成功发送 {success_count} 个文件")
        if failed_files:
            result_parts.append(f"发送失败 {len(failed_files)} 个文件：")
            for ff in failed_files:
                result_parts.append(f"  - {ff['file']}: {ff['error']}")
        if missing_files:
            result_parts.append("以下文件不存在，未发送：")
            for mf in missing_files:
                result_parts.append(f"  - {mf}")

        return "\n".join(result_parts) if result_parts else "发送完成"

    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner.

        Returns:
            List of tools for sending files.
        """
        session_id = self.session_id

        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="send_file_to_user",
                description=(
                    "【文件发送工具】当需要将生成的文件、导出的数据、创建的文档等发送给用户时使用此工具。"
                    "使用场景包括：用户请求导出/下载文件、任务完成后需要交付文件、生成报告/文档后发送给用户。"
                    "参数格式：接受路径数组，路径必须是绝对路径。"
                    "示例：['/tmp/file1.csv', '/tmp/file2.xlsx']"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "abs_file_path_list": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "要发送的文件绝对路径。"
                                "必须是数组格式，例如 ['/path/file1.csv', '/path/file2.xlsx']。"
                                "支持任意文件类型（pdf、xlsx、docx、png、zip等）。"
                            ),
                        }
                    },
                    "required": ["abs_file_path_list"],
                },
                func=self.send_file,
            ),
        ]
