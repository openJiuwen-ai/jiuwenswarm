# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Send File Toolkit

提供发送文件到用户的工具。支持发送一个或多个文件。

使用方式：
1. 创建 SendFileToolkit 实例
2. 调用 get_tools() 获取工具列表
3. 工具会自动注册到 Runner 中
"""

from __future__ import annotations

import json
import os
import logging
from typing import Any, List, Union

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard


logger = logging.getLogger(__name__)

# Session-level dedup for send_file_to_user. Compression may drop prior tool
# results, so the agent can re-call the same path; IM request-level dedup alone
# cannot stop cross-turn duplicates.
_SENT_FILE_PATHS_BY_SESSION: dict[str, set[str]] = {}


def _normalize_sent_file_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/").lower()


def _partition_sent_files(
    session_id: str,
    paths: list[str],
) -> tuple[list[str], list[str]]:
    """Split *paths* into (new_to_send, already_sent). Does not mutate the registry."""
    sid = (session_id or "").strip() or "default"
    sent = _SENT_FILE_PATHS_BY_SESSION.get(sid) or set()
    new_paths: list[str] = []
    skipped: list[str] = []
    for path in paths:
        key = _normalize_sent_file_path(path)
        if key in sent:
            skipped.append(path)
        else:
            new_paths.append(path)
    return new_paths, skipped


def _mark_files_sent(session_id: str, paths: list[str]) -> None:
    sid = (session_id or "").strip() or "default"
    sent = _SENT_FILE_PATHS_BY_SESSION.setdefault(sid, set())
    for path in paths:
        sent.add(_normalize_sent_file_path(path))


def clear_sent_files_for_session(session_id: str | None) -> None:
    """Drop session dedup state when the session adapter is cleaned up."""
    sid = (session_id or "").strip() or "default"
    _SENT_FILE_PATHS_BY_SESSION.pop(sid, None)


def _tool_fail(message: str) -> str:
    """失败信封，供 stream_event_rail / 桌面步骤卡认 is_error。

    不要带 data=None：桌面把 success=False + data=None + 空 error 当成 HITL 占位
    （步骤卡停在「运行中」）。多行 error 还会让空 error 正则误判。
    """
    escaped = message.replace("\\", "\\\\").replace("'", "\\'")
    return f"success=False error='{escaped}'"


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
            "[SendFileToolkit] 初始化 request_id=%s session_id=%s channel_id=%s has_metadata=%s",
            request_id,
            session_id,
            channel_id,
            bool(self._request_metadata),
        )

    def update_runtime_context(
        self,
        *,
        request_id: str,
        session_id: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update per-request runtime context without recreating the toolkit/tool.
        """
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        logger.debug(
            "[SendFileToolkit] update_runtime_context request_id=%s session_id=%s channel_id=%s has_metadata=%s",
            request_id,
            session_id,
            channel_id,
            bool(self._request_metadata),
        )

    @staticmethod
    def _normalize_target_channels(target_channels: Any) -> list[str]:
        """Normalize target_channels into a list of non-empty strings.

        Accepts a single string, a JSON array string, or a native list.
        Returns [] when absent/empty.
        """
        if target_channels is None:
            return []
        if isinstance(target_channels, str):
            stripped = target_channels.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
                if isinstance(parsed, str):
                    return [parsed.strip()] if parsed.strip() else []
                return [stripped]
            except (TypeError, ValueError):
                return [stripped]
        if isinstance(target_channels, (list, tuple)):
            return [str(x).strip() for x in target_channels if str(x).strip()]
        return [str(target_channels).strip()]

    async def send_file(
        self,
        abs_file_path_list: Union[List[str], str],
        target_channels: Union[List[str], str, None] = None,
        **_ignored: Any,
    ) -> str:
        """Send files to user.

        Args:
            abs_file_path_list: List of absolute file paths to send.
            target_channels: Optional explicit delivery targets. Each item is
                a channel id (e.g. "feishu", "web") or a team human-agent
                seat name (the member_name used in /join). When omitted the
                Gateway auto-routes the file to all channels joined to the
                session (team mode). When provided, the file is delivered
                only to the specified targets.

        Returns:
            Success message or error description.
        """
        target_channel_list = SendFileToolkit._normalize_target_channels(target_channels)
        if target_channel_list:
            logger.info(
                "[SendFileToolkit] send_file target_channels=%s session_id=%s",
                target_channel_list, self.session_id,
            )
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
            return _tool_fail("\n".join(msg_parts))

        valid_files, skipped_files = _partition_sent_files(self.session_id, valid_files)
        if not valid_files:
            logger.info(
                "[SendFileToolkit] skip duplicate send session_id=%s skipped=%s missing=%s",
                self.session_id,
                skipped_files,
                missing_files,
            )
            msg_parts: list[str] = []
            if skipped_files:
                msg_parts.append("文件已在本次会话发送过，跳过重复投递：")
                for sf in skipped_files:
                    msg_parts.append(f"  - {sf}")
            if missing_files:
                msg_parts.append("以下文件不存在，未发送：")
                for mf in missing_files:
                    msg_parts.append(f"  - {mf}")
            if not msg_parts:
                msg_parts.append("没有可发送的文件")
            return "\n".join(msg_parts)

        logger.info(
            "[SendFileToolkit] send_file 开始 session_id=%s 有效文件=%d 缺失=%d 跳过重复=%d",
            self.session_id,
            len(valid_files),
            len(missing_files),
            len(skipped_files),
        )

        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

            server = AgentWebSocketServer.get_instance()

            files_payload = []
            try:
                from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                    build_file_download_info,
                )

                for file_path in valid_files:
                    base_name = os.path.basename(file_path)
                    download_info = build_file_download_info(
                        file_path, base_name, self.session_id
                    )
                    files_payload.append({
                        "path": file_path,
                        "name": base_name,
                        "size": download_info["size"],
                        "mime_type": download_info["mime_type"],
                        "download_url": download_info["download_url"],
                        "download_token": download_info["download_token"],
                    })
            except Exception as download_err:
                logger.warning(
                    "[SendFileToolkit] 生成下载信息失败，回退到基础模式: %s",
                    download_err,
                )
                files_payload = [
                    {
                        "path": file_path,
                        "name": os.path.basename(file_path),
                    }
                    for file_path in valid_files
                ]

            import time
            from jiuwenswarm.server.runtime.session.session_history import (
                append_history_record,
            )
            # 团队模式下成员经 send_file 直推（不经 team 流，吃不到 team_helpers 的
            # 成员归因）——从成员执行上下文读 member_name（agent_teams 在成员协程内
            # set_member_id），让推送帧与落盘记录都能归到成员子聊天框。
            # leader / 非 team 会话读不到（""），行为与之前完全一致。
            member_name = ""
            try:
                from openjiuwen.core.common.logging import get_member_id

                member_name = str(get_member_id() or "").strip()
            except Exception as exe:
                member_name = ""
                logger.warning(
                    "[SendFileToolkit] 获取 member_name 失败，不阻断发文件: %s",
                    exe,
                )
            history_extra: dict[str, Any] = {"files": files_payload}
            if member_name:
                history_extra["member_name"] = member_name
            append_history_record(
                session_id=self.session_id,
                request_id=self.request_id,
                channel_id=self.channel_id,
                role="assistant",
                event_type="chat.file",
                content="",
                timestamp=time.time(),
                extra=history_extra,
                mode="team" if member_name else None,
            )

            msg = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": {
                    "event_type": "chat.file",
                    "files": files_payload,
                    **({"member_name": member_name} if member_name else {}),
                },
                "is_complete": False,
            }
            # 合并 metadata：原始 request metadata + 文件投递目标提示。
            # send_file_targets 由 Gateway 的 dispatch 层解析为 fan_out_targets，
            # 使文件可跨 channel 投递到 team 会话已接入的 channel（如飞书）。
            merged_meta: dict[str, Any] = {}
            if self._request_metadata:
                merged_meta.update(self._request_metadata)
            if target_channel_list:
                merged_meta["send_file_targets"] = list(target_channel_list)
            if merged_meta:
                msg["metadata"] = merged_meta
            await server.send_push(msg)
            _mark_files_sent(self.session_id, valid_files)
            result_parts = [f"成功发送 {len(valid_files)} 个文件"]
            if skipped_files:
                result_parts.append("以下文件已在本次会话发送过，已跳过：")
                for sf in skipped_files:
                    result_parts.append(f"  - {sf}")
            if missing_files:
                result_parts.append("以下文件不存在，未发送：")
                for mf in missing_files:
                    result_parts.append(f"  - {mf}")
            return "\n".join(result_parts)
        except Exception as e:
            logger.exception(
                "[SendFileToolkit] send_file 失败 session_id=%s error=%s",
                self.session_id,
                str(e),
            )
            return _tool_fail(f"提交文件失败: {str(e)}")

    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner.

        Returns:
            List of tools for sending files.
        """

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
                    "【强制】任务过程中每新建、修改、下载或重命名一个产物（文件、文档、图片、视频、音乐等），都必须调用本工具发送给用户；不得跳过。"
                    "【产物发送工具】当需要将生成、下载、导出、新建、修改或重命名后的文档、图片、视频、音乐等产物发送给用户时使用此工具。"
                    "使用场景包括：用户请求导出/下载文件、任务完成后需要交付文件、生成报告/文档后发送给用户。"
                    "参数格式：abs_file_path_list 接受单个路径字符串或路径数组，路径必须是绝对路径。"
                    "示例：'/tmp/report.pdf' 或 ['/tmp/file1.csv', '/tmp/file2.xlsx']。"
                    "target_channels 可选：指定文件投递目标，每项可以是 channel id（如 'web'）"
                    "或 team 人类席位名（如 'human-player-1'）。"
                    "省略时默认投给最近发起请求的人类成员（按 session 记录的发起者）；web 发起或无人类成员时投 web。"
                    "多 app 场景定向到指定 feishu 用户时，传入该用户的 member_name（不会误投其它 app）；"
                    "跨端投递（如把文件发给飞书用户、或发给 web）时传入对应 member_name 或 'web'。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "abs_file_path_list": {
                            "type": "string",
                            "description": (
                                "要发送的文件绝对路径。"
                                "可以是单个路径字符串如 '/path/to/file.pdf'，"
                                "或 JSON 数组字符串如 '[\"/path/file1.csv\", \"/path/file2.xlsx\"]'。"
                                "支持任意文件类型（pdf、xlsx、docx、png、zip等）。"
                            ),
                        },
                        "target_channels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "可选：文件投递目标列表。每项可为 channel id（如 'web'）"
                                "或 team 人类席位名（如 'human-player-1'）。"
                                "省略时默认投给最近发起请求的人类成员；web 发起或无人类成员时投 web。"
                                "定向到指定 feishu 用户传其 member_name；跨端投递传对应 member_name 或 'web'。"
                            ),
                        },
                    },
                    "required": ["abs_file_path_list"],
                },
                func=self.send_file,
            ),
        ]
