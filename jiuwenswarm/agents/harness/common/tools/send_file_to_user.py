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
import shutil
from pathlib import Path
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


class SendFileToolkit:
    """Toolkit for sending files to users."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        channel_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        project_dir: str | None = None,
        team_workspace_root: str | None = None,
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
        self._user_id = str(user_id or "").strip()
        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._team_workspace_root = (
            str(Path(team_workspace_root).resolve()) if team_workspace_root else None
        )
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
        user_id: str | None = None,
        project_dir: str | None = None,
        team_workspace_root: str | None = None,
    ) -> None:
        """Update per-request runtime context without recreating the toolkit/tool.
        """
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        self._user_id = str(user_id or "").strip()
        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._team_workspace_root = (
            str(Path(team_workspace_root).resolve()) if team_workspace_root else None
        )
        logger.debug(
            "[SendFileToolkit] update_runtime_context request_id=%s session_id=%s channel_id=%s has_metadata=%s",
            request_id,
            session_id,
            channel_id,
            bool(self._request_metadata),
        )

    def _resolve_project_dir(self) -> str | None:
        """Resolve the project root, including persistent session fallback."""
        if self._project_dir:
            return self._project_dir
        if not self.session_id:
            return None
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            metadata = get_session_metadata(
                self.session_id,
                cache_bust=True,
                enable_writeback=False,
            )
            project_dir = str((metadata or {}).get("project_dir") or "").strip()
            if project_dir:
                self._project_dir = str(Path(project_dir).resolve())
        except Exception as exc:
            logger.warning(
                "[SendFileToolkit] failed to resolve project_dir from session metadata: %s",
                exc,
            )
        return self._project_dir

    @staticmethod
    def _infer_team_workspace_root(source: Path) -> Path | None:
        """Recognize an OpenJiuwen ``.agent_teams/*/team-workspace`` path."""
        for candidate in (source, *source.parents):
            if (
                candidate.name == "team-workspace"
                and candidate.parent.parent.name == ".agent_teams"
            ):
                return candidate
        return None

    def _materialize_team_deliverable(self, file_path: str) -> str:
        """Copy a team-workspace deliverable into the active user project.

        The team workspace is an internal collaboration area.  Files selected
        for user delivery are project artifacts, so preserve their path
        relative to the team workspace under the current project before
        building download metadata.  Files outside the team workspace keep
        their original path.
        """
        project_dir = self._resolve_project_dir()
        if not project_dir:
            return file_path

        source = Path(file_path).resolve()
        configured_team_root = (
            Path(self._team_workspace_root) if self._team_workspace_root else None
        )
        team_root = configured_team_root or self._infer_team_workspace_root(source)
        if team_root is None:
            return file_path
        project_root = Path(project_dir)
        try:
            relative_path = source.relative_to(team_root)
        except ValueError:
            return file_path

        destination = (project_root / relative_path).resolve()
        try:
            destination.relative_to(project_root)
        except ValueError as exc:
            raise OSError(
                f"project delivery path escapes the project root: {destination}"
            ) from exc
        if destination == source:
            return str(source)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_file() and source.read_bytes() == destination.read_bytes():
                return str(destination)
            raise FileExistsError(
                f"refusing to overwrite an existing project file: {destination}"
            )
        shutil.copy2(source, destination)
        logger.info(
            "[SendFileToolkit] materialized team deliverable source=%s destination=%s",
            source,
            destination,
        )
        return str(destination)

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
        # skills.rebuild / 知识转 Skill 静默 Agent：禁止 push / chat.file，避免保存卡片污染 UI
        if isinstance(self._request_metadata, dict) and (
            self._request_metadata.get("skills_rebuild_silent")
            or self._request_metadata.get("skills_create_from_knowledge_silent")
        ):
            logger.info(
                "[SendFileToolkit] 静默模式跳过 send_file session_id=%s",
                self.session_id,
            )
            return (
                "静默模式禁止 send_file_to_user；"
                "请直接用文件写入工具生成 Skill 目录，不要投递文件给用户。"
            )

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

        source_files = list(valid_files)
        materialized_files: list[str] = []
        for fp in valid_files:
            try:
                materialized_files.append(self._materialize_team_deliverable(fp))
            except OSError as exc:
                logger.error(
                    "[SendFileToolkit] 团队交付文件复制到项目目录失败: %s: %s",
                    fp,
                    exc,
                )
                return f"发送文件失败：无法将团队交付文件写入当前项目目录\n  - {fp}: {exc}"
        valid_files = materialized_files
        copied_to_project = any(
            Path(source).resolve() != Path(delivered).resolve()
            for source, delivered in zip(source_files, valid_files)
        )

        if not valid_files:
            msg_parts = ["发送文件失败：所有文件均不存在"]
            for mf in missing_files:
                msg_parts.append(f"  - {mf}")
            return "\n".join(msg_parts)

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
                        file_path, base_name, self.session_id, user_id=self._user_id
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
            append_history_record(
                session_id=self.session_id,
                request_id=self.request_id,
                channel_id=self.channel_id,
                role="assistant",
                event_type="chat.file",
                content="",
                timestamp=time.time(),
                extra={"files": files_payload},
            )

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
            if copied_to_project:
                result_parts.append("最终交付文件已位于当前项目目录：")
                for delivered_path in valid_files:
                    result_parts.append(f"  - {delivered_path}")
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
            return f"提交文件失败: {str(e)}"

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
                    "【文件发送工具】当需要将生成的文件、导出的数据、创建的文档等发送给用户时使用此工具。"
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
