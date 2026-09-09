# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Send File Toolkit

提供发送文件到用户的工具。支持发送一个或多个文件。

使用方式：
1. 创建 SendFileToolkit 实例
2. 调用 get_tools() 获取工具列表
3. 工具会自动注册到 Runner 中
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.runtime.host_services import send_runtime_push

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
        SendFileAuthorizationItem,
    )
    from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
        VerifiedDownloadAsset,
        VerifiedDownloadAssetOwner,
    )

logger = logging.getLogger(__name__)

_VERIFIED_ASSET_TTL_SECONDS = 600


@dataclass(frozen=True)
class _SendFileRuntimeEnvelope:
    """Immutable per-call copy of mutable toolkit host context."""

    routing_request_id: str
    session_id: str
    channel_id: str
    user_id: str
    metadata: Mapping[str, Any] | None
    project_dir: str | None
    team_workspace_root: str | None
    require_execution_authorization: bool
    asset_owner: VerifiedDownloadAssetOwner | None


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
    seen = set(sent)
    new_paths: list[str] = []
    skipped: list[str] = []
    for path in paths:
        key = _normalize_sent_file_path(path)
        if key in seen:
            skipped.append(path)
        else:
            new_paths.append(path)
            seen.add(key)
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
        require_execution_authorization: bool = False,
        asset_owner: VerifiedDownloadAssetOwner | None = None,
    ) -> None:
        """Initialize SendFileToolkit.

        Args:
            request_id: Request identifier for message routing.
            session_id: Session identifier for message routing.
            channel_id: Channel identifier for message routing.
            metadata: 与 AgentRequest.metadata 一致（E2A channel_context 映射结果），用于 send_push。
            user_id: Optional AgentOS user route for Web download URLs.
            project_dir: Active user project directory for team deliverables.
            team_workspace_root: Optional exact team workspace root.
            require_execution_authorization: Require an exact host capability.
            asset_owner: Optional injected durable verified-asset owner.
        """
        self.routing_request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        self._user_id = str(user_id or "").strip()
        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._team_workspace_root = (
            str(Path(team_workspace_root).resolve()) if team_workspace_root else None
        )
        self._require_execution_authorization = bool(require_execution_authorization)
        self._asset_owner = asset_owner
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
        require_execution_authorization: bool | None = None,
    ) -> None:
        """Update per-request runtime context without recreating the toolkit/tool."""
        self.routing_request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        self._user_id = str(user_id or "").strip()
        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._team_workspace_root = (
            str(Path(team_workspace_root).resolve()) if team_workspace_root else None
        )
        if require_execution_authorization is not None:
            self._require_execution_authorization = bool(
                require_execution_authorization
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
        """Materialize using the toolkit's current synchronous runtime context."""
        return self._materialize_team_deliverable_from_roots(
            file_path,
            project_dir=self._resolve_project_dir(),
            team_workspace_root=self._team_workspace_root,
        )

    @classmethod
    def _materialize_team_deliverable_from_roots(
        cls,
        file_path: str,
        *,
        project_dir: str | None,
        team_workspace_root: str | None,
    ) -> str:
        """Copy a team-workspace deliverable into the active user project.

        A projectless team member's deliverables already live in the shared team
        ``outputs/`` directory (``team-workspace/artifacts/<date>/chat-<n>/
        outputs/``); with no project bound, the file is delivered in place and
        nothing is copied. When the member is bound to a project, files written
        under the team workspace are project artifacts, so preserve their path
        relative to the team workspace under the current project before building
        download metadata. Files outside the team workspace keep their original
        path.
        """
        if not project_dir:
            return file_path

        source = Path(file_path).resolve()
        configured_team_root = (
            Path(team_workspace_root) if team_workspace_root else None
        )
        team_root = configured_team_root or cls._infer_team_workspace_root(source)
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
            if (
                destination.is_file()
                and source.read_bytes() == destination.read_bytes()
            ):
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
        """Normalize target channels with the exact grant contract."""

        from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
            normalize_send_file_target_channels,
        )

        return list(normalize_send_file_target_channels(target_channels))

    async def send_file(
        self,
        abs_file_path_list: list[str] | str,
        target_channels: list[str] | str | None = None,
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

        owns_execution_grant = self._require_execution_authorization
        envelope: _SendFileRuntimeEnvelope | None = None
        try:
            envelope = self._snapshot_runtime_envelope()
            return await self._send_file_with_envelope(
                envelope,
                abs_file_path_list=abs_file_path_list,
                target_channels=target_channels,
            )
        except Exception as error:
            session_id = (
                envelope.session_id
                if envelope is not None
                else str(self.session_id or "")
            )
            logger.exception(
                "[SendFileToolkit] send_file 失败 session_id=%s error=%s",
                session_id,
                str(error),
            )
            return f"提交文件失败: {error!s}"
        finally:
            if owns_execution_grant:
                from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
                    clear_send_file_execution_grant,
                )

                clear_send_file_execution_grant()

    def _snapshot_runtime_envelope(self) -> _SendFileRuntimeEnvelope:
        """Freeze every mutable host field before the first await."""

        asset_owner = self._asset_owner
        if self._require_execution_authorization:
            if asset_owner is None:
                from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
                    get_verified_download_asset_owner,
                )

                asset_owner = get_verified_download_asset_owner()
        metadata = (
            MappingProxyType(copy.deepcopy(self._request_metadata))
            if self._request_metadata
            else None
        )
        return _SendFileRuntimeEnvelope(
            routing_request_id=str(self.routing_request_id or ""),
            session_id=str(self.session_id or ""),
            channel_id=str(self.channel_id or ""),
            user_id=self._user_id,
            metadata=metadata,
            project_dir=self._resolve_project_dir(),
            team_workspace_root=self._team_workspace_root,
            require_execution_authorization=(self._require_execution_authorization),
            asset_owner=asset_owner,
        )

    async def _send_file_with_envelope(
        self,
        envelope: _SendFileRuntimeEnvelope,
        *,
        abs_file_path_list: Any,
        target_channels: Any,
    ) -> str:
        target_channel_list = SendFileToolkit._normalize_target_channels(
            target_channels
        )
        if target_channel_list:
            logger.info(
                "[SendFileToolkit] send_file target_channels=%s session_id=%s",
                target_channel_list,
                envelope.session_id,
            )
        requested_paths = self._normalize_requested_paths(abs_file_path_list)
        authorization_items: tuple[SendFileAuthorizationItem, ...] = ()
        if envelope.require_execution_authorization:
            authorization_items = self._consume_execution_authorization(
                requested_paths=requested_paths,
                target_channels=target_channels,
            )
            valid_files = [
                item.resolved_path.as_posix() for item in authorization_items
            ]
            missing_files: list[str] = []
        else:
            valid_files = []
            missing_files = []
            for file_path in requested_paths:
                if os.path.isfile(file_path):
                    valid_files.append(file_path)
                else:
                    missing_files.append(file_path)
                    logger.warning(
                        "[SendFileToolkit] 文件不存在: %s",
                        file_path,
                    )

        source_files = list(valid_files)
        materialized_files: list[str] = []
        for fp in valid_files:
            try:
                materialized_files.append(
                    self._materialize_team_deliverable_from_roots(
                        fp,
                        project_dir=envelope.project_dir,
                        team_workspace_root=envelope.team_workspace_root,
                    )
                )
            except OSError as exc:
                logger.error(
                    "[SendFileToolkit] 团队交付文件复制到项目目录失败: %s: %s",
                    fp,
                    exc,
                )
                return (
                    f"发送文件失败：无法将团队交付文件写入当前项目目录\n  - {fp}: {exc}"
                )
        valid_files = materialized_files
        copied_to_project = any(
            Path(source).resolve() != Path(delivered).resolve()
            for source, delivered in zip(source_files, valid_files)
        )
        authorized_delivered_paths: set[str] = set()
        if envelope.require_execution_authorization:
            for delivered_path in valid_files:
                normalized_path = _normalize_sent_file_path(delivered_path)
                if normalized_path in authorized_delivered_paths:
                    raise ValueError("send_file_materialized_path_collision")
                authorized_delivered_paths.add(normalized_path)

        if not valid_files:
            msg_parts = ["发送文件失败：所有文件均不存在"]
            for mf in missing_files:
                msg_parts.append(f"  - {mf}")
            return "\n".join(msg_parts)

        valid_files, skipped_files = _partition_sent_files(
            envelope.session_id,
            valid_files,
        )
        if not valid_files:
            logger.info(
                "[SendFileToolkit] skip duplicate send session_id=%s skipped=%s missing=%s",
                envelope.session_id,
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
            envelope.session_id,
            len(valid_files),
            len(missing_files),
            len(skipped_files),
        )

        owned_assets: list[VerifiedDownloadAsset] = []
        assets_by_path: dict[str, VerifiedDownloadAsset] = {}
        exposure_started = False
        try:
            from jiuwenswarm.server.runtime.session.session_history import (
                append_history_record,
            )

            if envelope.require_execution_authorization:
                if envelope.asset_owner is None:
                    raise RuntimeError("send_file_asset_owner_missing")
                expires_at = float(int(time.time()) + _VERIFIED_ASSET_TTL_SECONDS)
                for file_path in valid_files:
                    asset = await asyncio.to_thread(
                        envelope.asset_owner.stage,
                        Path(file_path),
                        file_name=Path(file_path).name,
                        expires_at=expires_at,
                    )
                    owned_assets.append(asset)
                    assets_by_path[_normalize_sent_file_path(file_path)] = asset
            files_payload = self._build_files_payload(
                envelope,
                valid_files=valid_files,
                assets_by_path=assets_by_path,
            )
            msg = self._build_push_message(
                envelope,
                files_payload=files_payload,
                target_channels=target_channel_list,
            )

            # The Runtime push is the externally visible commit point. Entering
            # it makes delivery uncertain on exceptions, so staged assets must
            # remain valid until TTL instead of being revoked prematurely.
            exposure_started = True
            if not await send_runtime_push(msg):
                exposure_started = False
                raise RuntimeError(
                    "send_file_to_user requires an active Runtime push host"
                )
            if envelope.asset_owner is not None:
                for asset in owned_assets:
                    try:
                        envelope.asset_owner.commit(asset)
                    except Exception:
                        logger.exception(
                            "[SendFileToolkit] asset commit failed; "
                            "staged TTL ownership retained asset_id=%s",
                            asset.asset_id,
                        )
            _mark_files_sent(envelope.session_id, valid_files)
            try:
                append_history_record(
                    session_id=envelope.session_id,
                    request_id=envelope.routing_request_id,
                    channel_id=envelope.channel_id,
                    role="assistant",
                    event_type="chat.file",
                    content="",
                    timestamp=time.time(),
                    extra={"files": files_payload},
                )
            except Exception as history_error:  # noqa: BLE001
                logger.warning(
                    "[SendFileToolkit] file delivered but history persistence failed: "
                    "session_id=%s error=%s",
                    envelope.session_id,
                    history_error,
                    exc_info=True,
                )
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
        finally:
            if (
                owned_assets
                and not exposure_started
                and envelope.asset_owner is not None
            ):
                for asset in owned_assets:
                    envelope.asset_owner.revoke(asset)

    @staticmethod
    def _normalize_requested_paths(value: Any) -> tuple[str, ...]:
        from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
            normalize_send_file_paths,
        )

        return normalize_send_file_paths(value)

    @staticmethod
    def _consume_execution_authorization(
        *,
        requested_paths: tuple[str, ...],
        target_channels: Any,
    ) -> tuple[SendFileAuthorizationItem, ...]:
        from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
            consume_send_file_execution_grant,
        )

        return consume_send_file_execution_grant(
            requested_paths=requested_paths,
            target_channels=target_channels,
        )

    @staticmethod
    def _build_files_payload(
        envelope: _SendFileRuntimeEnvelope,
        *,
        valid_files: list[str],
        assets_by_path: dict[str, VerifiedDownloadAsset],
    ) -> list[dict[str, Any]]:
        files_payload: list[dict[str, Any]] = []
        if envelope.require_execution_authorization:
            from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                build_verified_asset_download_info,
            )

            for file_path in valid_files:
                asset = assets_by_path[_normalize_sent_file_path(file_path)]
                base_name = os.path.basename(file_path)
                download_info = build_verified_asset_download_info(
                    asset,
                    base_name,
                    envelope.session_id,
                    envelope.user_id,
                )
                files_payload.append(
                    {
                        "path": asset.sealed_path.as_posix(),
                        "name": base_name,
                        "size": download_info["size"],
                        "mime_type": download_info["mime_type"],
                        "download_url": download_info["download_url"],
                        "download_token": download_info["download_token"],
                    }
                )
            return files_payload

        try:
            from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                build_file_download_info,
            )

            for file_path in valid_files:
                base_name = os.path.basename(file_path)
                download_info = build_file_download_info(
                    file_path,
                    base_name,
                    envelope.session_id,
                    user_id=envelope.user_id,
                )
                files_payload.append(
                    {
                        "path": file_path,
                        "name": base_name,
                        "size": download_info["size"],
                        "mime_type": download_info["mime_type"],
                        "download_url": download_info["download_url"],
                        "download_token": download_info["download_token"],
                    }
                )
        except Exception as download_err:
            logger.warning(
                "[SendFileToolkit] 生成下载信息失败，回退到基础模式: %s",
                download_err,
            )
            return [
                {
                    "path": file_path,
                    "name": os.path.basename(file_path),
                }
                for file_path in valid_files
            ]
        return files_payload

    @staticmethod
    def _build_push_message(
        envelope: _SendFileRuntimeEnvelope,
        *,
        files_payload: list[dict[str, Any]],
        target_channels: list[str],
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "request_id": envelope.routing_request_id,
            "channel_id": envelope.channel_id,
            "session_id": envelope.session_id,
            "payload": {
                "event_type": "chat.file",
                "files": files_payload,
            },
            "is_complete": False,
        }
        merged_meta = dict(envelope.metadata or {})
        if target_channels:
            merged_meta["send_file_targets"] = list(target_channels)
        if merged_meta:
            msg["metadata"] = merged_meta
        return msg

    def get_tools(self) -> list[Tool]:
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
                                '或 JSON 数组字符串如 \'["/path/file1.csv", "/path/file2.xlsx"]\'。'
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
