# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevService — 无状态请求处理器.

设计要点：
- 无状态：不持有 Pipeline 对象，不做 Pipeline 生命周期管理
- 每次请求：StateStore 加载状态 → 创建 Pipeline → 执行 → checkpoint → 释放
- 路由层（Gateway）保证同一 task_id 的请求路由到同一实例，Service 无需关心

对外只暴露一个入口：handle(request) → AsyncIterator[AgentResponseChunk]

前端只需以下 method：
- skilldev.start     → 发起新任务
- skilldev.respond   → 统一确认（后端根据 task_id 当前阶段自动路由）
- skilldev.status    → 查状态 / 列任务
- skilldev.parse_skill → 导入本地 skill 压缩包并解压到工作区 skill/
- skilldev.download  → 下载产物
- skilldev.cancel    → 取消任务
- skilldev.file.list → 获取文件树
- skilldev.file.read → 读取文件内容
- skilldev.file.write → 修改工作区文件内容
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from jiuwenclaw.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod
from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.pipeline import SkillDevPipeline
from jiuwenclaw.agentserver.skilldev.schema import (
    SUSPENSION_POINTS,
    SkillDevEvent,
    SkillDevState,
    SkillDevStage,
)
from jiuwenclaw.agentserver.skilldev.common_utils import safe_extract_zip, repack_skill_dir
from jiuwenclaw.agentserver.skilldev.error_codes import (
    ERR_FW_MISSING_TASK_OR_PATH,
    ERR_FW_MISSING_CONTENT,
    ERR_FW_CONTENT_TOO_LARGE,
    ERR_FW_PATH_ESCAPE,
    ERR_FW_FILE_NOT_FOUND,
    ERR_FW_BINARY_FILE,
)
from jiuwenclaw.agentserver.skilldev.stages.validate_stage import parse_skill_frontmatter
from jiuwenclaw.agentserver.skilldev.session_history.restore_chunks import (
    RESTORE_RESPONSE_TOO_LARGE_CODE,
    RESTORE_UNARY_SAFE_BYTES,
    encode_restore_payload_chunks,
    restore_payload_to_json_bytes,
)
from jiuwenclaw.agentserver.skilldev.file_read_chunks import (
    FILE_READ_RESPONSE_TOO_LARGE_CODE,
    FILE_READ_UNARY_SAFE_BYTES,
    encode_file_read_payload_chunks,
    file_read_payload_to_json_bytes,
)
from jiuwenclaw.agentserver.skilldev.file_write_chunks import (
    FileWriteChunkError,
    FileWriteStagingStore,
)
from jiuwenclaw.agentserver.skilldev.utils import create_upload_file_obs
from jiuwenclaw.agentserver.skilldev.utils.download_file_from_url import download_file
from jiuwenclaw.agentserver.skilldev.utils.skill_md_validation import (
    validate_skill_md_content,
)
from jiuwenclaw.agentserver.skilldev.utils.static_security import (
    validate_scripts_file_content,
)

logger = logging.getLogger(__name__)

# method → handler 映射，避免 if/elif 链
_METHOD_DISPATCH = {
    ReqMethod.SKILLDEV_START: "_handle_start",
    ReqMethod.SKILLDEV_RESPOND: "_handle_respond",
    ReqMethod.SKILLDEV_STATUS: "_handle_status",
    ReqMethod.SKILLDEV_SESSION_LIST: "_handle_session_list",
    ReqMethod.SKILLDEV_RESTORE: "_handle_restore",
    ReqMethod.SKILLDEV_PARSE_SKILL: "_handle_parse_skill",
    ReqMethod.SKILLDEV_DOWNLOAD: "_handle_download",
    ReqMethod.SKILLDEV_CANCEL: "_handle_cancel",
    ReqMethod.SKILLDEV_FILE_LIST: "_handle_file_list",
    ReqMethod.SKILLDEV_FILE_READ: "_handle_file_read",
    ReqMethod.SKILLDEV_FILE_WRITE: "_handle_file_write",
    ReqMethod.SKILLDEV_BATCH_UPLOAD: "_handle_batch_upload",
    ReqMethod.SKILLDEV_BATCH_DOWNLOAD: "_handle_batch_download",
    ReqMethod.SKILLDEV_RESOURCE_DELETE: "_handle_resource_delete",
}

# 依次尝试的文本编码顺序。不包含 latin-1，以确保真正的二进制文件能被正确检测出来。
_TEXT_ENCODINGS = ("utf-8-sig", "gbk", "gb2312", "big5", "shift_jis", "euc-kr")


def _read_text_with_fallback(path: Path) -> tuple[str, str] | None:
    """依次尝试常用编码读取文本文件。

    返回 (content, encoding)；所有编码均失败则返回 None，表示该文件为二进制文件。
    """
    raw = path.read_bytes()
    for enc in _TEXT_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None


class SkillDevService:
    """SkillDev 模式的服务入口（无状态）."""

    def __init__(self, deps: SkillDevDeps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    async def handle(self, request: AgentRequest) -> AsyncIterator[AgentResponseChunk]:
        """根据 ReqMethod 分发到具体处理函数."""
        handler_name = _METHOD_DISPATCH.get(request.req_method)
        if handler_name is None:
            yield self._error_chunk(
                request.request_id,
                request.channel_id,
                f"未知 method: {request.req_method}",
            )
            return

        handler = getattr(self, handler_name)
        if handler_name in {"_handle_restore", "_handle_file_read"}:
            result = handler(
                request.params,
                request.request_id,
                request.channel_id,
                request.session_id,
                request.is_stream,
            )
        else:
            result = handler(request.params, request.request_id, request.channel_id, request.session_id)

        if inspect.isasyncgen(result):
            async for chunk in result:
                yield chunk
        elif inspect.isawaitable(result):
            yield await result
        else:
            yield result

    # ------------------------------------------------------------------
    # skilldev.start — 发起新任务
    # ------------------------------------------------------------------

    async def _handle_start(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AsyncIterator[AgentResponseChunk]:
        task_id = str(params.get("task_id") or session_id or "").strip()
        if not task_id:
            yield self._error_chunk(request_id, channel_id, "缺少 task_id 或 session_id 参数")
            return
        # 注入session id
        self._deps.model_client_config.setdefault("session", {"sessionId": task_id})

        loaded = await self._deps.state_store.load_state(task_id)
        # 新建任务：无状态或已完成 → 创建新 State；否则续跑已有任务（恢复后继续执行）
        if loaded is None or loaded.stage == SkillDevStage.COMPLETED:
            files_list = params.get("files", [])
            skill_packages_list = params.get("skill_packages", [])
            tool_spec_files_list = params.get("tool_spec_files", [])
            state = SkillDevState(
                task_id=task_id,
                input={
                    "query": params.get("query", ""),
                    "files": files_list,
                    "skill_packages": skill_packages_list,
                    "tool_spec_files": tool_spec_files_list,
                },
            )
            record_user_start = True
        else:
            state = loaded
            record_user_start = False

        pipeline = SkillDevPipeline(task_id=task_id, state=state, deps=self._deps)
        if self._deps.session_history is not None and record_user_start:
            self._deps.session_history.append_user_start(
                task_id=task_id,
                payload={
                    "task_id": task_id,
                    "session_id": session_id,
                    "query": params.get("query", ""),
                    "files": files_list,
                    "skill_packages": skill_packages_list,
                    "tool_spec_files": tool_spec_files_list,
                },
            )

        # 注册取消事件，供 _handle_cancel 在运行期间设置
        cancel_event = asyncio.Event()
        self._deps.cancel_events[task_id] = cancel_event

        try:
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"event_type": "skilldev.started", "task_id": task_id},
                is_complete=False,
            )
            if self._deps.session_history is not None:
                self._deps.session_history.append_agent_event(
                    task_id=task_id,
                    event_type="skilldev.started",
                    payload={"task_id": task_id},
                )

            async for event in pipeline.run():
                if state.stage == SkillDevStage.ERROR:
                    yield self._error_chunk(request_id, channel_id, state.error or "未知错误")
                    return
                if self._deps.session_history is not None:
                    self._deps.session_history.append_agent_event(
                        task_id=task_id,
                        event_type=event.event_type.value,
                        payload=dict(event.payload),
                    )
                yield self._event_to_chunk(event, request_id, channel_id)

            if state.stage == SkillDevStage.ERROR:
                event_type = "skilldev.error"
                yield self._error_chunk(
                    request_id, channel_id, state.error or "未知错误"
                )
            else:
                is_done = state.stage == SkillDevStage.COMPLETED
                event_type = "skilldev.completed" if is_done else "skilldev.suspended"
                yield AgentResponseChunk(
                    request_id=request_id,
                    channel_id=channel_id,
                    payload={
                        "event_type": event_type,
                        "task_id": task_id,
                        "stage": state.stage.value,
                    },
                    is_complete=True,
                )
            if self._deps.session_history is not None:
                self._deps.session_history.append_agent_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload={"task_id": task_id, "stage": state.stage.value},
                )
        finally:
            self._deps.cancel_events.pop(task_id, None)
            if self._deps.session_history is not None:
                self._deps.session_history.save_state_snapshot(task_id=task_id, state=state)

    # ------------------------------------------------------------------
    # skilldev.parse_skill — 导入并解压本地 skill 包到工作区
    # 仅允许在任务开始前调用（存在 state.json 时拒绝）
    # ------------------------------------------------------------------
    async def _handle_parse_skill(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AsyncIterator[AgentResponseChunk]:
        task_id = str(
            params.get("task_id") or params.get("session_id") or session_id or ""
        ).strip()
        logger.info(
            "[session=%s] [SkillDevService] _handle_parse_skill called: request_id=%s channel_id=%s",
            task_id,
            request_id,
            channel_id,
        )
        if not task_id:
            yield self._error_chunk(request_id, channel_id, "缺少 task_id 或 session_id 参数")
            return

        if self._deps.state_store.load_state_sync(task_id) is not None:
            yield self._error_chunk(
                request_id,
                channel_id,
                f"任务 {task_id} 已开始，禁止导入新的 skill 包",
            )
            return

        file_obj = params.get("skill_package")
        if not isinstance(file_obj, dict):
            yield self._error_chunk(request_id, channel_id, "缺少 skill_package 参数")
            return

        # 小艺适配
        if file_obj.get("url", ""):
            filename = file_obj.get("filename")
            download_url = file_obj.get("url")
            suffix = Path(filename).suffix.lower()
            if suffix not in (".zip", ".skill"):
                yield self._error_chunk(request_id, channel_id, "仅支持 .zip 或 .skill 格式")
                return

            workspace = await self._deps.workspace_provider.ensure_local(task_id)

            skill_dir = workspace / "skill"
            download_path = workspace / f"imported{suffix}"
            try:
                _ = await download_file(download_url, str(download_path))
            except Exception as exc:
                logger.info(
                    "[session=%s] [SkillDevService] skill 包下载失败: err=%s",
                    task_id,
                    exc,
                )
                yield self._error_chunk(request_id, channel_id, f"skill 包下载失败: {exc}")
                return

        else:
            filename = str(file_obj.get("filename") or "").strip()
            content_b64 = str(file_obj.get("base64Data") or "").strip()
            if not filename or not content_b64:
                yield self._error_chunk(
                    request_id,
                    channel_id,
                    "skill_package 参数缺少 filename 或 base64Data",
                )
                return
            suffix = Path(filename).suffix.lower()
            if suffix not in (".zip", ".skill"):
                yield self._error_chunk(request_id, channel_id, "仅支持 .zip 或 .skill 格式")
                return

            workspace = await self._deps.workspace_provider.ensure_local(task_id)

            skill_dir = workspace / "skill"
            download_path = workspace / f"imported{suffix}"

            raw = base64.b64decode(content_b64)
            download_path.write_bytes(raw)

        try:
            safe_extract_zip(download_path, skill_dir, extract_to_stem_dir=False)
            skill_md = skill_dir / "SKILL.md"
            skill_name, _, _ = parse_skill_frontmatter(skill_md)
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"event_type": "skilldev.skill_name_ready", "skill_name": skill_name},
                is_complete=False,
            )
        except Exception as exc:
            logger.info(
                "[session=%s] [SkillDevService] skill 包解压缩失败: err=%s",
                task_id,
                exc,
            )
            yield self._error_chunk(request_id, channel_id, f"skill 包解压缩失败: {exc}")
            return
        finally:
            if download_path.exists():
                download_path.unlink(missing_ok=True)

        yield AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={
                "ok": True,
                "task_id": task_id,
                "message": f"Skill 包导入成功: {filename}",
            },
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.respond — 统一确认入口
    # 前端只管发 {task_id, action, ...}，后端根据当前阶段自动路由
    # ------------------------------------------------------------------

    async def _handle_respond(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AsyncIterator[AgentResponseChunk]:
        task_id = params.get("task_id")
        if not task_id:
            yield self._error_chunk(request_id, channel_id, "缺少 task_id 参数")
            return

        state = await self._deps.state_store.load_state(task_id)
        if state is None:
            yield self._error_chunk(request_id, channel_id, f"任务 {task_id} 不存在")
            return

        if state.stage not in SUSPENSION_POINTS:
            yield self._error_chunk(
                request_id,
                channel_id,
                f"任务 {task_id} 当前阶段 {state.stage.value} 不是挂起点，无法 respond",
            )
            return

        pipeline = SkillDevPipeline(task_id=task_id, state=state, deps=self._deps)
        suspension = SUSPENSION_POINTS.get(state.stage)
        if self._deps.session_history is not None:
            self._deps.session_history.append_user_respond(
                task_id=task_id,
                payload={
                    "task_id": task_id,
                    "session_id": session_id,
                    "action": params.get("action"),
                    "feedback": params.get("feedback"),
                    "answers": params.get("answers"),
                },
            )
            if suspension is not None:
                self._deps.session_history.append_agent_event(
                    task_id=task_id,
                    event_type="skilldev.confirm_resolved",
                    payload={
                        "task_id": task_id,
                        "confirm_type": suspension.confirm_type,
                        "action": params.get("action"),
                        "feedback": params.get("feedback"),
                        "answers": params.get("answers"),
                    },
                )

        # 注册取消事件，供 _handle_cancel 在运行期间设置
        cancel_event = asyncio.Event()
        self._deps.cancel_events[task_id] = cancel_event

        try:
            async for event in pipeline.resume(data=params):
                if state.stage == SkillDevStage.ERROR:
                    yield self._error_chunk(request_id, channel_id, state.error or "未知错误")
                    return
                if self._deps.session_history is not None:
                    self._deps.session_history.append_agent_event(
                        task_id=task_id,
                        event_type=event.event_type.value,
                        payload=dict(event.payload),
                    )
                yield self._event_to_chunk(event, request_id, channel_id)

            if state.stage == SkillDevStage.ERROR:
                event_type = "skilldev.error"
                yield self._error_chunk(
                    request_id, channel_id, state.error or "未知错误"
                )
            else:
                is_done = state.stage == SkillDevStage.COMPLETED
                event_type = "skilldev.completed" if is_done else "skilldev.suspended"
                yield AgentResponseChunk(
                    request_id=request_id,
                    channel_id=channel_id,
                    payload={
                        "event_type": event_type,
                        "task_id": task_id,
                        "stage": state.stage.value,
                    },
                    is_complete=True,
                )
            if self._deps.session_history is not None:
                self._deps.session_history.append_agent_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload={"task_id": task_id, "stage": state.stage.value},
                )
        finally:
            self._deps.cancel_events.pop(task_id, None)
            if self._deps.session_history is not None:
                self._deps.session_history.save_state_snapshot(task_id=task_id, state=state)

    # ------------------------------------------------------------------
    # skilldev.status — 查状态 / 列任务
    # 传 task_id → 返回单个任务状态；不传 → 返回任务列表
    # ------------------------------------------------------------------

    def _handle_status(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        task_id = params.get("task_id")
        if not task_id:
            task_ids = self._deps.state_store.list_tasks()
            return AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"ok": True, "tasks": task_ids},
                is_complete=True,
            )

        state = self._deps.state_store.load_state_sync(task_id)
        payload = (
            state.to_status_dict() if state else {"error": f"任务 {task_id} 不存在"}
        )
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"ok": state is not None, **payload},
            is_complete=True,
        )

    def _handle_session_list(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        if self._deps.session_history is None:
            return self._error_chunk(request_id, channel_id, "session history service is unavailable")
        sessions = self._deps.session_history.list_sessions()
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"ok": True, "sessions": sessions},
            is_complete=True,
        )

    async def _handle_restore(
            self,
            params: dict,
            request_id: str,
            channel_id: str,
            session_id: str,
            is_stream: bool = False,
    ) -> AsyncIterator[AgentResponseChunk]:
        if self._deps.session_history is None:
            yield self._error_chunk(request_id, channel_id, "session history service is unavailable")
            return
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            yield self._error_chunk(request_id, channel_id, "缺少 task_id 参数")
            return
        restored = self._deps.session_history.restore_session(task_id)
        if restored is None:
            yield self._error_chunk(request_id, channel_id, f"任务 {task_id} 不存在")
            return
        payload = {"ok": True, **restored}
        if is_stream:
            for chunk in encode_restore_payload_chunks(
                    payload,
                    request_id=request_id,
                    channel_id=channel_id,
                    task_id=task_id,
            ):
                yield chunk
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"is_complete": True},
                is_complete=True,
            )
            return

        payload_bytes = restore_payload_to_json_bytes(payload)
        if len(payload_bytes) > RESTORE_UNARY_SAFE_BYTES:
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={
                    "event_type": "skilldev.error",
                    "code": RESTORE_RESPONSE_TOO_LARGE_CODE,
                    "error": (
                        "skilldev.restore response is too large for a unary frame; "
                        "retry with is_stream=true"
                    ),
                    "size_bytes": len(payload_bytes),
                    "max_unary_bytes": RESTORE_UNARY_SAFE_BYTES,
                },
                is_complete=True,
            )
            return
        yield AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.download — 下载产物
    # ------------------------------------------------------------------

    async def _handle_download(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        task_id = params.get("task_id")
        if not task_id:
            return self._error_chunk(request_id, channel_id, "缺少 task_id 参数")

        # state = self._deps.state_store.load_state_sync(task_id)
        # if state is None or not state.zip_path:
        #     return self._error_chunk(
        #         request_id, channel_id, f"任务 {task_id} 尚未完成打包"
        #     )

        # zip_path = Path(state.zip_path)
        workspace = self._deps.workspace_provider.get_local_path(task_id)
        skill_dir = workspace / "output"

        zip_path = next((f for f in skill_dir.iterdir() if f.suffix in (".skill", ".zip")), None)

        if not zip_path:
            return self._error_chunk(request_id, channel_id, "产物文件不存在")
        try:
            upload_file_obs = create_upload_file_obs(session_id=task_id)
            download_url = await upload_file_obs.upload_file(str(zip_path))
        except Exception as exc:
            logger.info(
                "[session=%s] [SkillDevService] skill 包上传到OBS服务器失败: err=%s",
                task_id,
                exc,
            )
            return self._error_chunk(request_id, channel_id, f"skill 包上传到OBS服务器失败: {exc}")

        export_id = f"exp_{secrets.token_hex(3)}"
        export_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={
                "filename": zip_path.name,
                "url": download_url,
                "mimeType": "application/zip",
                "exportId": export_id,
                "exportedAt": export_time,
            },
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.cancel — 取消任务
    # ------------------------------------------------------------------
    def _handle_cancel(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        task_id = params.get("task_id", "")
        event = self._deps.cancel_events.get(task_id)
        if event:
            event.set()
            msg = "取消信号已发送，pipeline 将在下一阶段边界终止"
            logger.info("[session=%s] [SkillDevService] 取消信号已发送", task_id)
        else:
            msg = "任务未在运行中，无需取消"
            logger.info("[session=%s] [SkillDevService] 取消请求：任务未在运行", task_id)
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"ok": True, "message": msg},
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.resource.delete — 按身份删除 message.send 上传的附件
    # ------------------------------------------------------------------

    def _handle_resource_delete(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        from jiuwenclaw.agentserver.skilldev_agent.utils.resource_sync import (
            delete_uploaded_resources,
        )

        task_id = params.get("task_id")
        if not task_id:
            return self._error_chunk(request_id, channel_id, "缺少 task_id 参数")

        items = params.get("items")
        if not isinstance(items, list) or not items:
            return self._error_chunk(
                request_id, channel_id, "items must be a non-empty array"
            )

        workspace = self._deps.workspace_provider.get_local_path(task_id)
        result = delete_uploaded_resources(workspace, items)
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=result,
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.file.list — 获取工作区文件树（供产物弹窗浏览）
    # ------------------------------------------------------------------

    def _handle_file_list(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        task_id = params.get("task_id")
        if not task_id:
            return self._error_chunk(request_id, channel_id, "缺少 task_id 参数")

        workspace = self._deps.workspace_provider.get_local_path(task_id)
        skill_dir = workspace / "skill"
        if not skill_dir.exists():
            return AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"ok": True, "tree": []},
                is_complete=True,
            )

        tree = self._build_file_tree(skill_dir, skill_dir)
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"ok": True, "tree": tree},
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.file.read — 读取工作区文件内容
    # ------------------------------------------------------------------
    async def _handle_file_read(
            self,
            params: dict,
            request_id: str,
            channel_id: str,
            session_id: str,
            is_stream: bool = False,
    ) -> AsyncIterator[AgentResponseChunk]:
        task_id = params.get("task_id")
        file_path = params.get("path", "")
        if not task_id or not file_path:
            yield self._error_chunk(
                request_id, channel_id, "缺少 task_id 或 path 参数"
            )
            return

        workspace = self._deps.workspace_provider.get_local_path(task_id)
        skill_dir = workspace / "skill"
        full_path = (skill_dir / file_path).resolve()

        if not str(full_path).startswith(str(skill_dir.resolve())):
            yield self._error_chunk(
                request_id, channel_id, "路径非法：不能访问工作区外的文件"
            )
            return

        if not full_path.exists() or not full_path.is_file():
            yield self._error_chunk(request_id, channel_id, f"文件不存在: {file_path}")
            return

        editable = True
        result = _read_text_with_fallback(full_path)
        if result is None:
            content = f"[二进制文件，大小 {full_path.stat().st_size} bytes]"
            editable = False
        else:
            content, _ = result

        payload = {"ok": True, "path": file_path, "content": content, "editable": editable}
        if is_stream:
            for chunk in encode_file_read_payload_chunks(
                    payload,
                    request_id=request_id,
                    channel_id=channel_id,
                    task_id=str(task_id),
                    path=str(file_path),
            ):
                yield chunk
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={"is_complete": True},
                is_complete=True,
            )
            return

        payload_bytes = file_read_payload_to_json_bytes(payload)
        if len(payload_bytes) > FILE_READ_UNARY_SAFE_BYTES:
            yield AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={
                    "event_type": "skilldev.error",
                    "code": FILE_READ_RESPONSE_TOO_LARGE_CODE,
                    "error": (
                        "skilldev.file.read response is too large for a unary frame; "
                        "retry with is_stream=true"
                    ),
                    "size_bytes": len(payload_bytes),
                    "max_unary_bytes": FILE_READ_UNARY_SAFE_BYTES,
                },
                is_complete=True,
            )
            return

        yield AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.file.write — 修改工作区文件内容
    # ------------------------------------------------------------------

    _MAX_FILE_CONTENT_SIZE = 1_048_576  # 1MB

    def _handle_file_write(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        task_id = params.get("task_id")
        file_path = params.get("path", "")
        if not task_id or not file_path:
            logger.info(
                "[session=%s] [SkillDevService] file_write 缺少参数: task_id=%s, path=%s",
                session_id, task_id, file_path,
            )
            return self._rpc_error_chunk(
                request_id, channel_id, ERR_FW_MISSING_TASK_OR_PATH
            )

        workspace = self._deps.workspace_provider.get_local_path(task_id)
        phase = str(params.get("phase") or "").strip().lower()
        if phase:
            try:
                store = FileWriteStagingStore(workspace)
                if phase == "status":
                    payload = store.status(params)
                elif phase == "abort":
                    payload = store.abort(params)
                elif phase == "chunk":
                    target = self._resolve_file_write_target(workspace, file_path)
                    if isinstance(target, str):
                        raise FileWriteChunkError(target)
                    payload = store.accept_chunk(params)
                elif phase == "commit":
                    raw, cached = store.assemble(params)
                    if cached is not None:
                        payload = cached
                    else:
                        assert raw is not None
                        target = self._resolve_file_write_target(workspace, file_path)
                        if isinstance(target, str):
                            raise FileWriteChunkError(target)
                        skill_dir, full_path = target
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise FileWriteChunkError(
                                "file write content is not valid UTF-8"
                            ) from exc
                        result = self._commit_file_write_content(
                            task_id=str(task_id),
                            file_path=str(file_path),
                            content=content,
                            workspace=workspace,
                            skill_dir=skill_dir,
                            full_path=full_path,
                            session_id=session_id,
                        )
                        if isinstance(result, str):
                            return self._rpc_error_chunk(request_id, channel_id, result)
                        payload = store.mark_committed(params, result)
                else:
                    raise FileWriteChunkError(f"unsupported file write phase: {phase}")
            except FileWriteChunkError as exc:
                logger.warning(
                    "[session=%s] [SkillDevService] file_write 分块请求失败: path=%s phase=%s error=%s",
                    session_id,
                    file_path,
                    phase,
                    exc,
                )
                return self._rpc_error_chunk(request_id, channel_id, str(exc))
            return AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload=payload,
                is_complete=True,
            )

        target = self._resolve_file_write_target(workspace, file_path)
        if isinstance(target, str):
            logger.warning(
                "[session=%s] [SkillDevService] file_write 目标校验失败: path=%s error=%s",
                session_id,
                file_path,
                target,
            )
            return self._rpc_error_chunk(request_id, channel_id, target)
        skill_dir, full_path = target

        content = params.get("content")
        if content is None:
            logger.info(
                "[session=%s] [SkillDevService] file_write 缺少 content 参数, path=%s",
                session_id, file_path,
            )
            return self._rpc_error_chunk(
                request_id, channel_id, ERR_FW_MISSING_CONTENT
            )
        if not isinstance(content, str):
            return self._rpc_error_chunk(request_id, channel_id, "content 必须是字符串")

        result = self._commit_file_write_content(
            task_id=str(task_id),
            file_path=str(file_path),
            content=content,
            workspace=workspace,
            skill_dir=skill_dir,
            full_path=full_path,
            session_id=session_id,
        )
        if isinstance(result, str):
            return self._rpc_error_chunk(request_id, channel_id, result)
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=result,
            is_complete=True,
        )

    @staticmethod
    def _resolve_file_write_target(
            workspace: Path, file_path: str
    ) -> tuple[Path, Path] | str:
        skill_dir = workspace / "skill"
        full_path = (skill_dir / file_path).resolve()
        try:
            full_path.relative_to(skill_dir.resolve())
        except ValueError:
            return ERR_FW_PATH_ESCAPE
        if not full_path.exists() or not full_path.is_file():
            return ERR_FW_FILE_NOT_FOUND
        if _read_text_with_fallback(full_path) is None:
            return ERR_FW_BINARY_FILE
        return skill_dir, full_path

    def _commit_file_write_content(
            self,
            *,
            task_id: str,
            file_path: str,
            content: str,
            workspace: Path,
            skill_dir: Path,
            full_path: Path,
            session_id: str,
    ) -> dict | str:
        try:
            content.encode("utf-8")
        except UnicodeEncodeError:
            return "文件内容不是有效的 UTF-8 文本"
        if len(content) > self._MAX_FILE_CONTENT_SIZE:
            logger.info(
                "[session=%s] [SkillDevService] file_write 内容超限: path=%s, size=%d, limit=%d",
                session_id, file_path, len(content), self._MAX_FILE_CONTENT_SIZE,
            )
            return ERR_FW_CONTENT_TOO_LARGE

        # 如果修改的是 SKILL.md，写入前校验内容格式
        if full_path.name == "SKILL.md":
            validation_error = validate_skill_md_content(content)
            if validation_error:
                logger.info(
                    "[session=%s] [SkillDevService] file_write SKILL.md 校验失败: %s",
                    session_id, validation_error,
                )
                return validation_error

        # 如果修改的是 skill/<skill_name>/scripts/**，写入前做静态安全校验（规则同 skill-verifier）
        try:
            rel = full_path.relative_to(skill_dir)
        except ValueError:
            rel = None

        if rel is not None and len(rel.parts) >= 3 and rel.parts[1] == "scripts":
            rel_path = rel.as_posix()
            validation_error = validate_scripts_file_content(content, rel_path=rel_path)
            if validation_error:
                logger.info(
                    "[session=%s] [SkillDevService] file_write 脚本安全校验失败: path=%s, error=%s",
                    session_id, rel_path, validation_error,
                )
                return validation_error

        temp_path = full_path.with_name(
            f".{full_path.name}.write-{secrets.token_hex(8)}.tmp"
        )
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.chmod(temp_path, full_path.stat().st_mode)
            os.replace(temp_path, full_path)
        finally:
            temp_path.unlink(missing_ok=True)

        repackaged = False
        output_dir = workspace / "output"
        if output_dir.exists():
            try:
                _, renamed_to = repack_skill_dir(skill_dir, output_dir, session_id=task_id)
                repackaged = True
                if renamed_to and "/" in file_path:
                    parts = file_path.split("/", 1)
                    file_path = f"{renamed_to}/{parts[1]}"
            except Exception as exc:
                logger.info(
                    "[session=%s] [SkillDevService] 重新打包失败: %s", task_id, exc
                )
        logger.info(
            "[session=%s] [SkillDevService] 文件修改成功: path=%s",
            session_id, rel
        )

        return {
            "ok": True,
            "path": file_path,
            "size": len(content),
            "repackaged": repackaged,
        }

    @staticmethod
    def _rpc_error_chunk(
            request_id: str, channel_id: str, error_code: str
    ) -> AgentResponseChunk:
        """用于非流式 RPC 请求的错误响应（不含 event_type，确保 gateway 返回 type=res）."""
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"ok": False, "error": error_code},
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.batch_upload — 批量打包 workspace 并上传 OBS
    # ------------------------------------------------------------------

    async def _handle_batch_upload(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        session_ids = params.get("session_ids")
        if not isinstance(session_ids, list) or not session_ids:
            return self._error_chunk(request_id, channel_id, "缺少 session_ids 参数或为空")

        import shutil
        import tempfile
        from jiuwenclaw.utils import get_user_workspace_dir

        logger.info(
            "[SkillDevService] batch_upload 开始: request_id=%s, session_ids=%s",
            request_id, session_ids,
        )

        results: list[dict] = []
        upload_file_obs = create_upload_file_obs()

        for sid in session_ids:
            sid = str(sid or "").strip()
            if not sid:
                results.append({"sessionID": sid, "url": "", "name": "", "status": "error", "error": "session_id 为空"})
                continue

            service_dir = get_user_workspace_dir() / f"service_{sid}"
            if not service_dir.is_dir():
                logger.info(
                    "[SkillDevService] batch_upload 目录不存在: session_id=%s, path=%s",
                    sid, service_dir,
                )
                results.append({"sessionID": sid, "url": "", "name": "", "status": "error", "error": "目录不存在"})
                continue

            try:
                tmp_dir = Path(tempfile.mkdtemp())
                zip_name = f"service_{sid}"
                zip_path = shutil.make_archive(str(tmp_dir / zip_name), "zip", str(service_dir))
                logger.info(
                    "[SkillDevService] batch_upload 打包完成: session_id=%s, zip_path=%s",
                    sid, zip_path,
                )
                upload_file_obs.session_id = sid
                download_url = await upload_file_obs.upload_file(zip_path)
                logger.info(
                    "[SkillDevService] batch_upload 上传成功: session_id=%s, url=%s",
                    sid, download_url,
                )
                results.append({
                    "sessionID": sid,
                    "url": download_url,
                    "name": f"{zip_name}.zip",
                    "status": "success",
                })
            except Exception as exc:
                logger.info(
                    "[SkillDevService] batch_upload failed for session_id=%s: %s", sid, exc,
                )
                results.append({"sessionID": sid, "url": "", "name": "", "status": "error", "error": str(exc)})
            finally:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

        success_count = sum(1 for r in results if r.get("status") == "success")
        logger.info(
            "[SkillDevService] batch_upload 完成: request_id=%s, total=%d, success=%d, failed=%d",
            request_id, len(results), success_count, len(results) - success_count,
        )

        log_result = None
        if os.getenv("ENVIRONMENT") == "dev":
            log_dir = Path("/opt/huawei/logs/run")
            if log_dir.is_dir():
                tmp_log_dir = None
                try:
                    tmp_log_dir = Path(tempfile.mkdtemp())
                    log_zip_path = shutil.make_archive(str(tmp_log_dir / "run_logs"), "zip", str(log_dir))
                    log_url = await upload_file_obs.upload_file(log_zip_path)
                    log_result = {"url": log_url, "name": "run_logs.zip", "status": "success"}
                    logger.info("[SkillDevService] batch_upload dev日志上传成功: url=%s", log_url)
                except Exception as exc:
                    logger.info("[SkillDevService] batch_upload dev日志上传失败: %s", exc)
                    log_result = {"url": "", "name": "run_logs.zip", "status": "error", "error": str(exc)}
                finally:
                    if tmp_log_dir:
                        shutil.rmtree(tmp_log_dir, ignore_errors=True)
            else:
                logger.info("[SkillDevService] batch_upload dev日志目录不存在: %s", log_dir)

        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"results": results, "log_result": log_result},
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # skilldev.batch_download — 批量下载并解压到 workspace
    # ------------------------------------------------------------------

    async def _handle_batch_download(
            self, params: dict, request_id: str, channel_id: str, session_id: str
    ) -> AgentResponseChunk:
        items = params.get("items")
        if not isinstance(items, list) or not items:
            return self._error_chunk(request_id, channel_id, "缺少 items 参数或为空")

        import shutil
        import tempfile
        from jiuwenclaw.utils import get_user_workspace_dir

        logger.info(
            "[SkillDevService] batch_download 开始: request_id=%s, items_count=%d",
            request_id, len(items),
        )

        results: list[dict] = []

        for item in items:
            if not isinstance(item, dict):
                results.append({"sessionID": "", "status": "error", "error": "item 格式无效"})
                continue

            sid = str(item.get("sessionID") or item.get("session_id") or "").strip()
            url = str(item.get("url") or "").strip()

            if not sid:
                results.append({"sessionID": sid, "status": "error", "error": "session_id 为空"})
                continue
            if not url:
                results.append({"sessionID": sid, "status": "error", "error": "url 为空"})
                continue

            try:
                target_dir = get_user_workspace_dir() / f"service_{sid}"
                tmp_dir = Path(tempfile.mkdtemp())
                tmp_zip = tmp_dir / f"service_{sid}.zip"

                logger.info(
                    "[SkillDevService] batch_download 下载中: session_id=%s, url=%s",
                    sid, url,
                )
                await download_file(url, str(tmp_zip))

                if target_dir.exists():
                    shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)

                safe_extract_zip(tmp_zip, target_dir, extract_to_stem_dir=False)
                logger.info(
                    "[SkillDevService] batch_download 解压成功: session_id=%s, target=%s",
                    sid, target_dir,
                )
                results.append({"sessionID": sid, "status": "success"})
            except Exception as exc:
                logger.info(
                    "[SkillDevService] batch_download failed for session_id=%s: %s", sid, exc,
                )
                results.append({"sessionID": sid, "status": "error", "error": str(exc)})
            finally:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

        success_count = sum(1 for r in results if r.get("status") == "success")
        logger.info(
            "[SkillDevService] batch_download 完成: request_id=%s, total=%d, success=%d, failed=%d",
            request_id, len(results), success_count, len(results) - success_count,
        )

        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"results": results},
            is_complete=True,
        )

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_chunk(
            event: SkillDevEvent, request_id: str, channel_id: str
    ) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"event_type": event.event_type.value, **event.payload},
            is_complete=False,
        )

    @staticmethod
    def _error_chunk(
            request_id: str, channel_id: str, message: str
    ) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={"event_type": "skilldev.error", "error": message},
            is_complete=True,
        )

    @staticmethod
    def _build_file_tree(directory: Path, root: Path) -> list[dict]:
        """递归构建文件树."""
        result: list[dict] = []
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (not p.is_dir(), p.name)
            )
        except PermissionError:
            return result

        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = str(entry.relative_to(root)).replace("\\", "/")
            if entry.is_dir():
                children = SkillDevService._build_file_tree(entry, root)
                result.append({"path": rel + "/", "type": "dir", "children": children})
            else:
                result.append(
                    {"path": rel, "type": "file", "size": entry.stat().st_size}
                )
        return result
