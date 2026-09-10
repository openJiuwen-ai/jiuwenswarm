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
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, List, Optional, Union

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.edition import is_enterprise


logger = logging.getLogger(__name__)

# Per-request send_file routing context (session_id / request_id / channel_id / metadata).
# send_file_to_user 工具按全局名注册成单例时，并发请求会互相覆盖实例字段。
# 此 ContextVar 按 async 上下文隔离；工具执行时优先据此解析当前请求路由，
# 避免「最后一次注册的 session/request」串扰（对齐 test/jiuwenclaw）。
_send_file_request_context: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "send_file_request_context", default=None
)

# Session-level dedup for send_file_to_user. Compression may drop prior tool
# results, so the agent can re-call the same path; IM request-level dedup alone
# cannot stop cross-turn duplicates.
_SENT_FILE_PATHS_BY_SESSION: dict[str, set[str]] = {}


def set_send_file_request_context(
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    channel_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Token:
    """Bind send_file routing context for the current async request.

    返回 Token 供调用方在请求结束时 ``reset_send_file_request_context`` 恢复。
    仅记录非空字段；调用方应在请求入口设置、finally 中重置。
    """
    ctx: dict[str, Any] = {}
    if request_id is not None:
        ctx["request_id"] = request_id
    if session_id is not None:
        ctx["session_id"] = session_id
    if channel_id is not None:
        ctx["channel_id"] = channel_id
    if metadata is not None:
        ctx["metadata"] = dict(metadata)
    return _send_file_request_context.set(ctx)


def get_send_file_request_context() -> dict[str, Any] | None:
    """Return send_file routing context for the current request, or None if unset."""
    return _send_file_request_context.get()


def reset_send_file_request_context(token: Token) -> None:
    """Restore the previous send_file routing context binding."""
    _send_file_request_context.reset(token)


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


@dataclass
class SendFileRoute:
    """Per-request routing context for send_file (resolved from contextvars)."""

    request_id: str
    session_id: str
    channel_id: str
    metadata: dict[str, Any] | None


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

    def _resolve_route(self) -> SendFileRoute:
        """执行时解析路由信息。

        优先从请求级 ``send_file_request_context`` ContextVar 取（按 async 上下文
        隔离，并发安全），缺失时回退到实例字段。

        不再读取 ``_CRON_TOOL_CHANNEL_ID``：该 ContextVar 默认值是 ``web``，
        请求结束后 reset 仍会返回默认 web，导致过期 request 的 chat.file 被推到
        错误 channel（对齐 test/jiuwenclaw 的隔离语义）。
        """
        ctx = get_send_file_request_context() or {}
        request_id = ctx.get("request_id") or self.request_id
        session_id = ctx.get("session_id") or self.session_id
        channel_id = ctx.get("channel_id") or self.channel_id
        # ctx 已绑定时以其为权威：缺失即视为本请求无 metadata，避免回退到被并发
        # 请求覆盖的脏实例字段；仅在无 ContextVar 时回退实例 metadata。
        if ctx:
            metadata = ctx.get("metadata")
            if (
                ctx.get("session_id")
                and ctx.get("session_id") != self.session_id
            ):
                logger.info(
                    "[SendFileToolkit] route 由 ContextVar 修正 "
                    "instance_session=%s ctx_session=%s",
                    self.session_id,
                    ctx.get("session_id"),
                )
        else:
            metadata = self._request_metadata
        return SendFileRoute(
            request_id=request_id,
            session_id=session_id,
            channel_id=channel_id,
            metadata=dict(metadata) if metadata else None,
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
        route = self._resolve_route()
        target_channel_list = SendFileToolkit._normalize_target_channels(target_channels)
        if target_channel_list:
            logger.info(
                "[SendFileToolkit] send_file target_channels=%s session_id=%s",
                target_channel_list, route.session_id,
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
            return "\n".join(msg_parts)

        valid_files, skipped_files = _partition_sent_files(route.session_id, valid_files)
        if not valid_files:
            logger.info(
                "[SendFileToolkit] skip duplicate send session_id=%s skipped=%s missing=%s",
                route.session_id,
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
            route.session_id,
            len(valid_files),
            len(missing_files),
            len(skipped_files),
        )

        # 企业默认：OBS URL 经当前 chat SSE（不依赖 PushRegistry）。
        # 个人版不进此分支，保持本机 path + send_push / 显式 file_transfer。
        if self._should_use_obs_download():
            return await self._send_file_via_obs(
                valid_files,
                missing_files,
                skipped_files,
                route,
                target_channel_list,
            )

        from jiuwenswarm.common.file_transfer_config import get_file_transfer_config

        if get_file_transfer_config().enabled:
            return await self._send_file_distributed(
                valid_files,
                missing_files,
                skipped_files,
                route,
                target_channel_list,
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
                        file_path, base_name, route.session_id
                    )
                    files_payload.append({
                        "path": file_path,
                        "name": base_name,
                        "size": download_info["size"],
                        "mime_type": download_info["mime_type"],
                        "download_url": download_info["download_url"],
                        "download_token": download_info["download_token"],
                        "expires_at": download_info.get("expires_at"),
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

            msg = {
                "request_id": route.request_id,
                "channel_id": route.channel_id,
                "session_id": route.session_id,
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
            if route.metadata:
                merged_meta.update(route.metadata)
            if target_channel_list:
                merged_meta["send_file_targets"] = list(target_channel_list)
            if merged_meta:
                msg["metadata"] = merged_meta

            delivered = await server.send_push(msg)
            # send_push 在无订阅者时只打 warning 并返回 0；不得再伪装成「成功发送」。
            if not isinstance(delivered, int) or delivered <= 0:
                logger.warning(
                    "[SendFileToolkit] send_push 未送达 session_id=%s request_id=%s delivered=%s",
                    route.session_id,
                    route.request_id,
                    delivered,
                )
                return (
                    "发送文件失败：推送通道无活跃订阅者或投递失败"
                    f"（delivered={delivered!r}）。请检查 Gateway/Relay WebSocket 连接后重试。"
                )

            # 文件已通过 send_push 送达：去重标记必须先于历史记录写入，
            # 历史 DB/IO 失败不得伪装成「提交文件失败」导致 agent 重试重复投递。
            _mark_files_sent(route.session_id, valid_files)
            try:
                append_history_record(
                    session_id=route.session_id,
                    request_id=route.request_id,
                    channel_id=route.channel_id,
                    role="assistant",
                    event_type="chat.file",
                    content="",
                    timestamp=time.time(),
                    extra={"files": files_payload},
                )
            except Exception:
                logger.warning(
                    "[SendFileToolkit] append_history_record 失败 session_id=%s",
                    route.session_id,
                    exc_info=True,
                )
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
                route.session_id,
                str(e),
            )
            return f"提交文件失败: {str(e)}"

    @staticmethod
    def _escape_file_download_via_push() -> bool:
        raw = os.getenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @classmethod
    def _should_use_obs_download(cls) -> bool:
        """Enterprise default: MinIO URL on current chat SSE (not file.download.*)."""
        return bool(is_enterprise()) and not cls._escape_file_download_via_push()

    async def _send_file_via_obs(
        self,
        valid_files: List[str],
        missing_files: List[str],
        skipped_files: List[str],
        route: SendFileRoute,
        target_channel_list: List[str],
    ) -> str:
        """Enterprise: put files to MinIO and emit chat.file(url) on request SSE."""
        from openjiuwen.core.session.stream import OutputSchema

        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_subagent_parent_session,
        )
        from jiuwenswarm.channels.web.minio_upload import (
            load_minio_upload_config,
            upload_local_file_to_minio,
        )

        session = get_subagent_parent_session()
        if session is None or not hasattr(session, "write_stream"):
            return (
                "发送文件失败：当前请求无可用的流式通道，无法投递企业下载链接。"
                "请确认工具在对话流内执行（session.write_stream）。"
            )

        try:
            minio_cfg = load_minio_upload_config()
        except Exception as exc:
            logger.warning("[SendFileToolkit] OBS 配置不可用: %s", exc)
            return f"发送文件失败：对象存储未配置或不可用（{exc}）"

        files_payload: list[dict[str, Any]] = []
        failed_files: list[dict[str, str]] = []
        sent_ok: list[str] = []

        for file_path in valid_files:
            try:
                uploaded = upload_local_file_to_minio(
                    minio_cfg,
                    file_path,
                    filename=os.path.basename(file_path),
                    object_prefix="downloads",
                )
                files_payload.append(
                    {
                        "url": str(uploaded["url"]),
                        "name": str(uploaded["name"]),
                        "size": int(uploaded["size"]),
                    }
                )
                sent_ok.append(file_path)
                logger.info(
                    "[SendFileToolkit] OBS 上传成功 file=%s url=%s",
                    file_path,
                    uploaded.get("url"),
                )
            except Exception as exc:
                failed_files.append({"file": file_path, "error": str(exc)})
                logger.exception(
                    "[SendFileToolkit] OBS 上传失败 file=%s",
                    file_path,
                )

        if not files_payload:
            parts = ["发送文件失败：全部文件上传对象存储失败"]
            for ff in failed_files:
                parts.append(f"  - {ff['file']}: {ff['error']}")
            return "\n".join(parts)

        stream_payload: dict[str, Any] = {
            "event_type": "chat.file",
            "files": files_payload,
        }
        # Gateway materialize 识别 outbound url；target 提示与 push 路径对齐
        if target_channel_list:
            stream_payload["send_file_targets"] = list(target_channel_list)

        try:
            await session.write_stream(
                OutputSchema(
                    type="chat.file",
                    index=0,
                    payload=stream_payload,
                )
            )
        except Exception as exc:
            logger.exception(
                "[SendFileToolkit] write_stream chat.file 失败 session_id=%s",
                route.session_id,
            )
            return f"发送文件失败：写入对话流失败（{exc}）"

        _mark_files_sent(route.session_id, sent_ok)
        result_parts = [
            f"已上传 {len(files_payload)} 个文件到对象存储，下载由 Gateway 代理对象存储"
        ]
        if failed_files:
            result_parts.append(f"上传失败 {len(failed_files)} 个文件：")
            for ff in failed_files:
                result_parts.append(f"  - {ff['file']}: {ff['error']}")
        if skipped_files:
            result_parts.append("以下文件已在本次会话发送过，已跳过：")
            for sf in skipped_files:
                result_parts.append(f"  - {sf}")
        if missing_files:
            result_parts.append("以下文件不存在，未发送：")
            for mf in missing_files:
                result_parts.append(f"  - {mf}")
        return "\n".join(result_parts)

    async def _send_file_distributed(
        self,
        valid_files: List[str],
        missing_files: List[str],
        skipped_files: List[str],
        route: SendFileRoute,
        target_channel_list: List[str],
    ) -> str:
        """分布式模式：分片推送到 Gateway，由 Gateway 拼包后发 chat.file。"""
        from jiuwenswarm.server.file_transfer_manager import get_file_transfer_manager

        ft_manager = get_file_transfer_manager()
        success_count = 0
        failed_files: list[dict[str, str]] = []
        sent_ok: list[str] = []

        for file_path in valid_files:
            try:
                async def send_callback(
                    event_type: str,
                    params: dict,
                    *,
                    _route: SendFileRoute = route,
                    _targets: List[str] = target_channel_list,
                ) -> None:
                    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

                    server = AgentWebSocketServer.get_instance()
                    msg: dict[str, Any] = {
                        "request_id": _route.request_id,
                        "channel_id": _route.channel_id,
                        "session_id": _route.session_id,
                        "payload": {
                            "event_type": event_type,
                            **params,
                        },
                        "is_complete": False,
                    }
                    merged_meta: dict[str, Any] = {}
                    if _route.metadata:
                        merged_meta.update(_route.metadata)
                    if _targets:
                        merged_meta["send_file_targets"] = list(_targets)
                    if merged_meta:
                        msg["metadata"] = merged_meta
                    delivered = await server.send_push(msg)
                    if not isinstance(delivered, int) or delivered <= 0:
                        raise RuntimeError(
                            "推送通道无活跃订阅者或投递失败"
                            f"（delivered={delivered!r}）。"
                            "企业环境请走 OBS 下载主路径；逃生阀需有 Gateway PushRegistry 订户。"
                        )

                result = await ft_manager.send_file(
                    file_path=file_path,
                    send_callback=send_callback,
                    session_id=route.session_id,
                    channel_id=route.channel_id,
                    request_id=route.request_id,
                )
                if result.get("success"):
                    success_count += 1
                    sent_ok.append(file_path)
                    logger.info(
                        "[SendFileToolkit] 分布式发送成功 file=%s transfer_id=%s",
                        file_path,
                        result.get("transfer_id"),
                    )
                else:
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": str(result.get("error", "unknown error")),
                        }
                    )
                    logger.warning(
                        "[SendFileToolkit] 分布式发送失败 file=%s error=%s",
                        file_path,
                        result.get("error"),
                    )
            except Exception as e:
                failed_files.append({"file": file_path, "error": str(e)})
                logger.exception(
                    "[SendFileToolkit] 分布式发送异常 file=%s",
                    file_path,
                )

        if sent_ok:
            _mark_files_sent(route.session_id, sent_ok)

        result_parts: list[str] = []
        if success_count > 0:
            result_parts.append(f"成功发送 {success_count} 个文件")
        if failed_files:
            result_parts.append(f"发送失败 {len(failed_files)} 个文件：")
            for ff in failed_files:
                result_parts.append(f"  - {ff['file']}: {ff['error']}")
        if skipped_files:
            result_parts.append("以下文件已在本次会话发送过，已跳过：")
            for sf in skipped_files:
                result_parts.append(f"  - {sf}")
        if missing_files:
            result_parts.append("以下文件不存在，未发送：")
            for mf in missing_files:
                result_parts.append(f"  - {mf}")
        return "\n".join(result_parts) if result_parts else "发送完成"

    def get_tools(self, *, tool_id: str | None = None) -> List[Tool]:
        """Return tools for registration in Runner.

        Args:
            tool_id: Optional stable id for the tool card before owner qualification.
        """
        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card_kwargs: dict[str, Any] = {
                "name": name,
                "description": description,
                "input_params": input_params,
            }
            if tool_id:
                card_kwargs["id"] = tool_id
            card = ToolCard(**card_kwargs)
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="send_file_to_user",
                description=(
                    "【文件发送工具】当需要将生成的文件、导出的数据、创建的文档等发送给用户时使用此工具。"
                    "使用场景包括：用户请求导出/下载文件、任务完成后需要交付文件、生成报告/文档后发送给用户。"
                    "参数格式：abs_file_path_list 接受路径数组，路径必须是绝对路径。"
                    "示例：['/tmp/file1.csv', '/tmp/file2.xlsx']。"
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
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "要发送的文件绝对路径。\n"
                                "必须是数组格式，例如 ['/path/file1.csv', '/path/file2.xlsx']。\n"
                                "建议使用 get_effective_request_output_dir() 获取的 output_dir 作为文件保存位置。\n"
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
