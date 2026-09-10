# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Artifact detection rail for SkillTurbo tool calls.

复用 TaskExecutionRail 的共享产物检测逻辑（detect_artifact_paths_safe /
fire_artifact_hook 等），在 SkillTurbo 工具调用后检测产物文件，同时
触发 IMAGE_ARTIFACT_POST_PROCESS 和 ARTIFACT_POST_PROCESS 扩展 hook
供扩展做原地后处理（如加水印），并发射 artifact.generated 事件到
session stream。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

# 复用 jiuwenswarm TaskExecutionRail 的共享产物检测逻辑
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    ARTIFACT_DETECTION_TOOL_NAMES,
    WorkspaceBaselineState,
    detect_artifact_paths_safe,
    filter_unhooked,
    fire_artifact_hook,
    mark_hooked,
    pop_tool_start_time,
    resolve_workspace_base,
    update_baseline_after_hook,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[SkillTurboArtifact]"


class SkillTurboArtifactRail:
    """在 SkillTurbo 工具调用后检测产物文件并发射 artifact.generated 事件。

    使用共享的 detect_artifact_paths 提取产物路径（含 invoke_tool 解包、
    按工具类型分流、mtime/工作区/去重过滤），同时触发
    IMAGE_ARTIFACT_POST_PROCESS 和 ARTIFACT_POST_PROCESS 扩展 hook
    （供原地后处理如加水印），再发射 artifact.generated 事件到
    session stream。
    """

    priority = 90

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        # 已触发过产物 hook 的文件身份（路径+mtime_ns+size），防止重复后处理
        self._hooked_artifacts: set[tuple[str, int, int]] = set()
        # 工具调用开始时间（按 tool_call_id 记录，用于 mtime 校验）
        self._tool_start_times: dict[str, float] = {}
        # 工作区基线懒建状态：bash 类工具执行前的文件快照，供增量 diff 检测产物
        self._baseline = WorkspaceBaselineState()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = ctx.inputs.tool_name
        if tool_name not in ARTIFACT_DETECTION_TOOL_NAMES:
            return
        tc = getattr(ctx.inputs, "tool_call", None)
        tool_call_id = str(getattr(tc, "id", "") or "")
        if tool_call_id:
            self._tool_start_times[tool_call_id] = time.time()
        # bash 类工具懒建基线（含 invoke_tool 间接调用的内部工具名
        # 判定、超时禁用与并行去重，详见 WorkspaceBaselineState.ensure）
        await self._baseline.ensure(
            tool_name,
            getattr(ctx.inputs, "tool_args", None),
            log_prefix=_LOG_PREFIX,
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return
        if ctx.inputs.tool_name not in ARTIFACT_DETECTION_TOOL_NAMES:
            return

        session_id = self._get_session_id(ctx.session)
        task_id = self._executor.current_task_id()
        detect_start = time.perf_counter()

        # 线程 + 超时 + 异常兜底执行产物检测，避免 stat() 阻塞 event loop
        # （共享 TaskExecutionRail 的 detect_artifact_paths_safe；
        #  基线禁用后传 None：跳过基线 diff，走文本提取）
        detection = await detect_artifact_paths_safe(
            ctx,
            session_id,
            pop_tool_start_time(self._tool_start_times, ctx),
            log_prefix=_LOG_PREFIX,
            baseline=self._baseline.effective,
        )
        if detection is None:
            return
        # 基线 diff 快照失败/超限：禁用本会话基线路径，避免反复无效扫描
        if detection.baseline_scan_failed:
            self._baseline.disabled = True
            logger.warning(
                "%s baseline scan failed, disable baseline diff for this "
                "session",
                _LOG_PREFIX,
            )
        snapshot = detection.baseline_snapshot

        # 去重：跳过已 hook 过且内容未变化的文件
        # （存在性过滤已在 detect_artifact_paths 统一出口处理）
        paths = filter_unhooked(detection.paths, self._hooked_artifacts)

        fired = False
        if not paths:
            # 无新产物：仅在有本次快照时刷新基线（记录当前状态）后返回
            # （snapshot 为 None 表示非基线路径/降级，保持原基线不变）
            if snapshot is not None:
                self._baseline.snapshot = await asyncio.to_thread(
                    update_baseline_after_hook,
                    snapshot, fired, paths, resolve_workspace_base(),
                )
            return

        logger.info(
            "%s Detect start: tool=%s session_id=%s count=%d",
            _LOG_PREFIX,
            detection.tool_name,
            session_id,
            len(paths),
        )

        # 触发扩展 hook（发射事件前，供扩展原地后处理如加水印）
        fired = await fire_artifact_hook(
            session_id=session_id,
            tool_name=detection.tool_name,
            task_id=task_id,
            artifact_paths=paths,
            log_prefix=_LOG_PREFIX,
        )
        if fired:
            mark_hooked(paths, self._hooked_artifacts)

        # 更新基线：仅在有本次快照时更新（snapshot 为 None 表示非基线路径/
        # 降级，保持原基线不变）；hook 可能原地改写文件（水印），局部刷新
        # 候选条目（含 sha256 读文件，放线程防阻塞 event loop）
        if snapshot is not None:
            self._baseline.snapshot = await asyncio.to_thread(
                update_baseline_after_hook,
                snapshot, fired, paths, resolve_workspace_base(),
            )

        # 发射 artifact.generated 事件到 session stream
        emitted = await self._emit_artifact_generated(
            ctx.session, paths, session_id, detection.tool_name, task_id
        )

        logger.info(
            "%s Detect done: tool=%s session_id=%s emitted=%s elapsed_ms=%d",
            _LOG_PREFIX,
            detection.tool_name,
            session_id,
            emitted,
            int((time.perf_counter() - detect_start) * 1000),
        )

    @staticmethod
    def _get_session_id(session: Any) -> str:
        try:
            return str(session.get_session_id() or "?")
        except Exception:
            logger.debug(
                "%s failed to get session_id", _LOG_PREFIX, exc_info=True
            )
            return "?"

    async def _emit_artifact_generated(
        self,
        session: Any,
        paths: list[str],
        session_id: str,
        tool_name: str,
        task_id: str | None,
    ) -> bool:
        """构建 artifact.generated payload 并写入 session stream。"""
        artifacts_payload = []
        for path_str in paths:
            p = Path(path_str)
            try:
                stat = p.stat()
                size, exists = stat.st_size, True
            except OSError:
                size, exists = 0, False
            artifacts_payload.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "extension": p.suffix.lower(),
                    "size": size,
                    "exists": exists,
                }
            )

        payload = {
            "artifacts": artifacts_payload,
            "tool_name": tool_name,
            "task_id": task_id,
            "timestamp": time.time(),
            "count": len(artifacts_payload),
        }

        try:
            await session.write_stream(
                OutputSchema(
                    type="artifact.generated",
                    index=0,
                    payload=payload,
                )
            )
            return True
        except Exception as exc:
            logger.warning(
                "%s emit failed: %s", _LOG_PREFIX, exc, exc_info=True
            )
            return False
