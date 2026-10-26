# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""GitDiffWebSocketHandler: /ws/git 路由的消息分发与推送(设计文档 §2.6 / §4.2)。

职责:
  - 处理 /ws/git 连接的消息循环
  - 分发 ``diff_watch`` / ``diff_files_watch`` / ``diff_detail_watch`` /
    ``diff_unwatch`` 请求
  - 首次响应通过 ``channel.send_response`` 返回快照
  - 后续变化由 ``GitDiffWatcherRegistry`` 通过 ``channel.send_event`` 推送

事件推送复用 ``WebChannel.send_event(ws, event, payload)``,
``seq``/``stream_id`` 传 ``None``(设计文档 §5.3.11)。

四种事件:
  - ``project.git.diff_changed``: summary fingerprint 变化时推送
  - ``project.git.diff_files_changed``: 文件列表 fingerprint 变化时推送
  - ``project.git.diff_detail_changed``: 已订阅文件 hunk 变化时推送
  - ``project.git.error``: 监控过程中 Git 命令失败时推送
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: /ws/git 支持的 source 取值
_VALID_SOURCES: frozenset[str] = frozenset({"current", "last_turn"})

#: /ws/git 支持的 unwatch scope 取值
_VALID_UNWATCH_SCOPES: frozenset[str] = frozenset({"all", "files", "detail"})


class GitDiffWebSocketHandler:
    """/ws/git socket 的消息分发与推送,作为 ``WebChannel`` 内部组件。

    由 ``WebChannel._connection_handler`` 在 path 分发中创建并调用
    ``handle_connection``。
    """

    def __init__(self, channel: Any, registry: Any) -> None:
        self._channel = channel
        self._registry = registry

    async def handle_connection(self, ws: Any, parsed_query: dict[str, str]) -> None:
        """处理 /ws/git 连接的消息循环。

        ``parsed_query`` 为已扁平化的 query dict(query 参数 → str)。
        断连清理由 ``WebChannel._connection_handler`` 的 ``finally`` 块负责
        (``unregister_ws`` + ``cleanup_ws``)。
        """
        try:
            async for raw in ws:
                await self._handle_message(ws, raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GitWS] connection loop ended: %s", exc)

    async def _handle_message(self, ws: Any, raw: str) -> None:
        """解析并分发单条消息。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._channel.send_response(
                ws, "", ok=False, error="invalid json", code="BAD_REQUEST",
            )
            return
        if not isinstance(data, dict):
            await self._channel.send_response(
                ws, "", ok=False, error="invalid request", code="BAD_REQUEST",
            )
            return

        req_type = data.get("type")
        req_id = data.get("id")
        method = data.get("method")
        params = data.get("params")

        if req_type != "req" or not isinstance(req_id, str) or not isinstance(method, str):
            await self._channel.send_response(
                ws,
                req_id if isinstance(req_id, str) else "",
                ok=False,
                error="invalid request",
                code="BAD_REQUEST",
            )
            return
        if not isinstance(params, dict):
            params = {}

        if method == "project.git.diff_watch":
            await self._handle_diff_watch(ws, req_id, params)
        elif method == "project.git.diff_files_watch":
            await self._handle_diff_files_watch(ws, req_id, params)
        elif method == "project.git.diff_detail_watch":
            await self._handle_diff_detail_watch(ws, req_id, params)
        elif method == "project.git.diff_unwatch":
            await self._handle_diff_unwatch(ws, req_id, params)
        elif method == "project.git.discard_turn_changes":
            await self._handle_discard_turn_changes(ws, req_id, params)
        elif method == "project.git.redo_turn_changes":
            await self._handle_redo_turn_changes(ws, req_id, params)
        else:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"unknown method: {method}", code="BAD_REQUEST",
            )
            return

    @staticmethod
    def _resolve_git_project(project_id: str, *, cache_bust: bool = False):
        """校验并加载可用于 Git 操作的 code 项目。

        委托给共享 helper ``project_git.resolve_git_project``,
        与 ``app_web_handlers.py`` 的 Git RPC handler 共用同一校验逻辑。

        Args:
            project_id: 项目 ID
            cache_bust: 是否绕过项目缓存。**写操作必须传 True**——项目可能刚被
                隐藏/删除/修改 work_mode,使用旧缓存会导致对已失效项目执行写操作。
                只读操作可保持 False 以复用缓存。

        Returns:
            ``(project, error_message, error_code)``: 成功时后两项为 None。
        """
        from jiuwenswarm.server.runtime.session.project_git import resolve_git_project
        return resolve_git_project(project_id, cache_bust=cache_bust)

    async def _send_git_error_response(
        self, ws: Any, req_id: str, exc: Exception,
    ) -> None:
        """发送 Git 结构化错误响应(设计文档 §1.4)。

        委托给共享 helper ``project_git.send_git_error_response``。
        """
        from jiuwenswarm.server.runtime.session.project_git import send_git_error_response
        await send_git_error_response(self._channel, ws, req_id, exc)

    async def _handle_diff_watch(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """订阅 diff summary 监控(设计文档 §4.2.1)。

        首次响应只返回统计快照(``files`` 固定 ``{}``);后续变化由
        ``GitDiffWatcherRegistry`` 推送 ``project.git.diff_changed`` 事件。
        ``include_last_turn``(默认 true)控制是否监控 last_turn 变化。
        """
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = self._resolve_git_project(project_id)
        if proj is None:
            await self._channel.send_response(ws, req_id, ok=False, error=err, code=code)
            return

        session_id = str(params.get("session_id") or "").strip()
        scope = str(params.get("scope") or "summary").strip() or "summary"
        if scope != "summary":
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"invalid scope: {scope}, only 'summary' is supported",
                code="BAD_REQUEST",
            )
            return

        include_last_turn = self._parse_bool_param(
            params, "include_last_turn", default=True,
        )

        async def _on_initial(watch: Any) -> dict[str, Any]:
            """计算首次 summary 快照并发送响应;抛错由 registry 触发 remove_watch。"""
            from jiuwenswarm.server.runtime.session.git_diff_status import (
                get_diff_status_service,
            )
            service = get_diff_status_service()
            # 与轮询路径(_compute_and_push)对齐:只在 include_last_turn 时
            # 传 session_id,避免 current-only 订阅因 file_ops 历史读取异常
            # 而首次订阅失败。get_project_diff_status 内只要有 session_id
            # 就会调 get_turn_diff_summaries 且异常向上抛,破坏订阅。
            session_id_for_status = session_id if include_last_turn else None
            status = await asyncio.to_thread(
                service.get_project_diff_status,
                project=proj,
                session_id=session_id_for_status or None,
                include_files=False,
                include_hunks=False,
            )
            status_dict = status.to_dict(include_hunks=False)
            snapshot = self._build_summary_snapshot(
                watch.watch_id, status_dict, include_last_turn=include_last_turn,
            )
            await self._channel.send_response(ws, req_id, ok=True, payload=snapshot)
            return status_dict

        try:
            await self._registry.add_watch(
                ws, project_id, session_id, scope="summary",
                include_last_turn=include_last_turn,
                on_initial=_on_initial,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_git_error_response(ws, req_id, exc)
            return

    @staticmethod
    def _parse_bool_param(
        params: dict[str, Any], key: str, *, default: bool,
    ) -> bool:
        """解析布尔参数:接受 bool 或字符串 'true'/'false'。"""
        raw = params.get(key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return default

    @staticmethod
    def _build_summary_snapshot(
        watch_id: str, status_dict: dict[str, Any],
        *, include_last_turn: bool = True,
    ) -> dict[str, Any]:
        """构造 diff_watch 首次响应 payload(设计文档 §4.2.1)。

        ``include_last_turn=False`` 时 ``last_turn`` 固定为 ``None``。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            build_summary_entry,
            build_turn_summary_entry,
        )
        repo = status_dict.get("repo") or {}
        current = status_dict.get("current")
        last_turn = status_dict.get("last_turn") if include_last_turn else None
        return {
            "watch_id": watch_id,
            "scope": "summary",
            "snapshot": {
                "project_id": status_dict.get("project_id", ""),
                "session_id": status_dict.get("session_id"),
                "repo": {
                    "is_git": repo.get("is_git", False),
                    "repo_root": repo.get("repo_root"),
                    "branch": repo.get("branch"),
                    "head": repo.get("head"),
                    "transient": repo.get("transient", False),
                },
                "current": build_summary_entry(current),
                "last_turn": build_turn_summary_entry(last_turn),
                "revision": f"gitdiff:{int(time.time())}:init",
            },
        }

    async def _handle_diff_files_watch(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """订阅变更文件列表(设计文档 §4.2.2)。

        在已有 ``watch_id`` 上开启或切换文件列表监控,立即返回当前文件列表快照
        (不含 hunk);后续文件列表 fingerprint 变化时推送
        ``project.git.diff_files_changed``。
        """
        project_id = str(params.get("project_id") or "").strip()
        watch_id = str(params.get("watch_id") or "").strip()
        source = str(params.get("source") or "").strip()

        if not watch_id:
            await self._channel.send_response(
                ws, req_id, ok=False, error="watch_id is required", code="BAD_REQUEST",
            )
            return
        if source not in _VALID_SOURCES:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"invalid source: {source}, must be 'current' or 'last_turn'",
                code="BAD_REQUEST",
            )
            return

        proj, err, code = self._resolve_git_project(project_id)
        if proj is None:
            await self._channel.send_response(ws, req_id, ok=False, error=err, code=code)
            return

        async def _on_snapshot(watch: Any) -> None:
            """计算首次 files 快照并发送响应;抛错由 registry 触发回滚。"""
            session_id = str(params.get("session_id") or "").strip() or watch.session_id
            from jiuwenswarm.server.runtime.session.git_diff_status import (
                get_diff_status_service,
            )
            service = get_diff_status_service()
            # 与轮询路径(_compute_and_push)对齐:只在 source="last_turn"
            # 时传 session_id;source="current" 不需要 last_turn,传 session_id
            # 会因 file_ops 历史读取异常导致首次订阅失败。
            session_id_for_status = session_id if source == "last_turn" else None
            status = await asyncio.to_thread(
                service.get_project_diff_status,
                project=proj,
                session_id=session_id_for_status or None,
                include_files=True,
                include_hunks=False,
            )
            status_dict = status.to_dict(include_hunks=False)
            files_dict = self._extract_files(status_dict, source) or {}
            files_no_hunks = self._strip_hunks(files_dict)
            payload = {
                "watch_id": watch_id,
                "files_scope": {"source": source},
                "revision": f"gitdiff:{int(time.time())}:init",
                "files": files_no_hunks,
            }
            await self._channel.send_response(ws, req_id, ok=True, payload=payload)
            # seed files fingerprint + mark_dirty(Registry 内部完成)
            self._registry.commit_initial_files(watch_id, status_dict, source)

        try:
            watch = await self._registry.update_files_with_restore(
                watch_id, source,
                expected_ws=ws,
                expected_project_id=project_id,
                on_snapshot=_on_snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_git_error_response(ws, req_id, exc)
            return
        if watch is None:
            await self._channel.send_response(
                ws, req_id, ok=False, error="watch not found", code="NOT_FOUND",
            )
            return

    async def _handle_diff_detail_watch(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """订阅具体文件的 diff 内容(设计文档 §4.2.3)。

        在已有 ``watch_id`` 上切换详情监控对象,后端替换 source 并立即返回新快照
        (含 hunk)。只有 ``detail_files`` 中显式订阅的文件 hunk 内容变化时,
        才推送 ``project.git.diff_detail_changed``。
        """
        project_id = str(params.get("project_id") or "").strip()
        watch_id = str(params.get("watch_id") or "").strip()
        source = str(params.get("source") or "").strip()
        files_param = params.get("files")

        if not watch_id:
            await self._channel.send_response(
                ws, req_id, ok=False, error="watch_id is required", code="BAD_REQUEST",
            )
            return
        if source not in _VALID_SOURCES:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"invalid source: {source}, must be 'current' or 'last_turn'",
                code="BAD_REQUEST",
            )
            return

        if not isinstance(files_param, list) or not files_param:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="files must be a non-empty array of strings",
                code="BAD_REQUEST",
            )
            return
        detail_files: list[str] = []
        for f in files_param:
            if not isinstance(f, str) or not f.strip():
                await self._channel.send_response(
                    ws, req_id, ok=False,
                    error="files must contain only non-empty strings",
                    code="BAD_REQUEST",
                )
                return
            detail_files.append(f.strip())

        proj, err, code = self._resolve_git_project(project_id)
        if proj is None:
            await self._channel.send_response(ws, req_id, ok=False, error=err, code=code)
            return

        async def _on_snapshot(watch: Any) -> None:
            """计算首次 detail 快照并发送响应;抛错由 registry 触发回滚。"""
            session_id = str(params.get("session_id") or "").strip() or watch.session_id
            from jiuwenswarm.server.runtime.session.git_diff_status import (
                get_diff_status_service,
            )
            service = get_diff_status_service()
            # 与轮询路径(_compute_and_push)对齐:只在 source="last_turn"
            # 时传 session_id;source="current" 不需要 last_turn,传 session_id
            # 会因 file_ops 历史读取异常导致首次订阅失败。
            session_id_for_status = session_id if source == "last_turn" else None
            status = await asyncio.to_thread(
                service.get_project_diff_status,
                project=proj,
                session_id=session_id_for_status or None,
                include_files=True,
                include_hunks=True,
                hunk_paths=detail_files if source == "current" else None,
            )
            status_dict = status.to_dict(include_hunks=True)
            files_dict = self._extract_files(status_dict, source) or {}
            detail_files_map: dict[str, Any] = {}
            for path in detail_files:
                entry = files_dict.get(path)
                if isinstance(entry, dict):
                    detail_files_map[path] = entry
                else:
                    detail_files_map[path] = None
            payload = {
                "watch_id": watch_id,
                "detail_scope": {"source": source, "files": detail_files},
                "revision": f"gitdiff:{int(time.time())}:init",
                "files": detail_files_map,
            }
            await self._channel.send_response(ws, req_id, ok=True, payload=payload)
            # seed detail fingerprint + mark_dirty(Registry 内部完成)
            self._registry.commit_initial_detail(
                watch_id, status_dict, source, detail_files,
            )

        try:
            watch = await self._registry.update_detail_with_restore(
                watch_id, source, detail_files,
                expected_ws=ws,
                expected_project_id=project_id,
                on_snapshot=_on_snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_git_error_response(ws, req_id, exc)
            return
        if watch is None:
            await self._channel.send_response(
                ws, req_id, ok=False, error="watch not found", code="NOT_FOUND",
            )
            return

    async def _handle_diff_unwatch(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """取消监控并释放 watcher 资源(设计文档 §4.2.4)。

        ``scope="all"`` 移除整个 watcher;``scope="files"`` 仅取消文件列表;
        ``scope="detail"`` 仅取消文件内容。后两者保留 summary 订阅。
        watch_id 不存在时幂等成功。
        """
        watch_id = str(params.get("watch_id") or "").strip()
        scope = str(params.get("scope") or "all").strip() or "all"

        if not watch_id:
            await self._channel.send_response(
                ws, req_id, ok=False, error="watch_id is required", code="BAD_REQUEST",
            )
            return
        if scope not in _VALID_UNWATCH_SCOPES:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"invalid scope: {scope}, must be 'all', 'files', or 'detail'",
                code="BAD_REQUEST",
            )
            return

        await self._registry.remove_watch(watch_id, scope=scope, expected_ws=ws)
        await self._channel.send_response(
            ws, req_id, ok=True,
            payload={"watch_id": watch_id, "cancelled": True, "scope": scope},
        )

    async def _handle_discard_turn_changes(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """撤销本轮代码修改(设计文档 §4.2.5)。

        将当前会话最后一轮 agent 通过工具调用产生的文件变更全部回滚到
        该轮开始前的状态,并清理本轮的 file_ops 日志,使 git 监控的
        current/last_turn diff 与实际工作区一致。

        前置条件:
          - project_id 指向 code 模式的 Git 项目
          - session_id 非空
          - 会话非忙碌(agent 未在执行)

        后置效果:
          - 本轮修改的文件被恢复到该轮开始前的内容(或删除 agent 新建的文件)
          - **仅在所有文件恢复成功时**清理本轮 file_ops 日志;有失败项时
            保留日志以便重试(详见下方"file_ops 截断条件")
          - 触发 git watcher 重算,前端立即收到 diff_changed 事件

        file_ops 截断条件(P1 修复):
          ``restore_session_files`` 把单文件失败收集到 ``errors`` 不抛异常。
          若不管 ``errors`` 一律截断 file_ops,失败文件将失去重试所需的日志。
          故仅在 ``errors`` 为空时截断;有错误时返回 ``ok=False, partial=True``
          并保留日志,调用方可重试。
        """
        project_id = str(params.get("project_id") or "").strip()
        # 写操作:使用 cache_bust=True 避免对已隐藏/删除/变更 work_mode 的项目执行撤销
        proj, err, code = self._resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await self._channel.send_response(ws, req_id, ok=False, error=err, code=code)
            return

        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session_id is required", code="BAD_REQUEST",
            )
            return

        # 校验 session 与 project 的绑定关系:避免跨项目误撤销。
        # 读取 session metadata 的 project_id,与请求传入的 project_id 比对。
        # 用 get_session_metadata(enable_writeback=False):保留推断能力(存量会话
        # 缺 project_id 时可从 project_dir 反查补全,避免误拒),但跳过异步写盘
        # 避免读路径副作用。cache_bust=True 跳过缓存直接读盘,确保绑定校验基于
        # 最新数据(跨进程同步场景下缓存可能 stale)。
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        try:
            session_meta = await asyncio.to_thread(
                get_session_metadata, session_id,
                cache_bust=True, enable_writeback=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[GitWS] discard_turn_changes: failed to read session metadata "
                "(session=%s): %s", session_id, exc,
            )
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"failed to read session metadata: {exc}",
                code="INTERNAL_ERROR",
            )
            return

        session_project_id = str(session_meta.get("project_id") or "").strip()
        if not session_project_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session has no project_id binding; cannot verify project ownership",
                code="SESSION_NOT_BOUND",
            )
            return
        if session_project_id != project_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=(
                    f"session_id does not belong to project_id: "
                    f"expected {project_id}, got {session_project_id}"
                ),
                code="PROJECT_SESSION_MISMATCH",
            )
            return

        # 校验会话非忙碌:避免与正在执行的 agent 文件写入冲突
        if self._channel.is_session_busy(session_id):
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session is busy; stop the current run before discarding changes",
                code="SESSION_BUSY",
            )
            return

        # 获取最后一轮 turn_index 和 timestamp
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            get_last_turn_info,
        )
        last_turn = await asyncio.to_thread(
            get_last_turn_info, session_id=session_id,
        )
        turn_index = last_turn["turn_index"]
        cut_timestamp = last_turn["timestamp"]

        if turn_index <= 0:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="no turn to discard: session has no user messages",
                code="NO_TURN_TO_DISCARD",
            )
            return

        # 恢复本轮修改的文件到该轮开始前的状态
        # 显式传入 proj.project_dir:Web/code 模式新会话的 channel_metadata 不含 cwd,
        # 底层 _get_project_dir_from_metadata 已支持读顶层 project_dir 兜底,
        # 但显式传入更可靠(避免依赖 metadata 读取顺序),也避免重复读盘。
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            restore_session_files,
        )
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )
        extra_history_roots = get_session_extra_history_roots(session_id)
        try:
            restore_result = await asyncio.to_thread(
                restore_session_files,
                session_id=session_id,
                turn_index=turn_index,
                project_dir=proj.project_dir,
                extra_history_roots=extra_history_roots,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[GitWS] discard_turn_changes: restore failed "
                "(session=%s project=%s): %s",
                session_id, project_id, exc,
            )
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"failed to restore session files: {exc}",
                code="INTERNAL_ERROR",
            )
            return

        # 清理本轮的 file_ops 日志,使 git 监控的 last_turn diff 与实际工作区一致。
        # 注意 1:仅清理 session-specific file_ops;全局 file_ops 缺少 session 归属字段,
        #   多 session 同文件场景下按路径清理会误伤其他 session 的修改,故不清理。
        # 注意 2(P1 修复):仅在 ``restore_result.errors`` 为空时截断。
        #   restore_session_files 把单文件失败收集到 errors 不抛异常,若一律截断
        #   会让失败文件失去重试所需的日志。有错误时返回 partial 并保留日志。
        # 注意 3(P1 修复):显式传入 proj.project_dir,与 restore_session_files 一致,
        #   确保扫描到项目目录下的 session-specific file_ops。
        # 注意 4:使用 soft=True 保留 file_ops 条目(打 discarded_out 标记)而非物理
        #   删除,使 redo_turn_changes 可通过去标记恢复条目可见性。显示层
        #   (_read_agent_history include_rewound=False)自动隐藏标记条目,
        #   last_turn diff 行为与 hard 截断一致。
        #   使用 discarded=True 打 ``discarded_out`` 而非 ``rewound_out``:与
        #   conversation rewind 的标记区分后,redo 只恢复 discard 标记,
        #   不会误暴露 rewind 软隐藏的"未来"条目。
        restore_errors = restore_result.get("errors", []) or []
        file_ops_truncated = False
        discarded_change_set_id: str | None = None
        if cut_timestamp > 0 and not restore_errors:
            from jiuwenswarm.server.utils.diff_service import get_diff_service
            diff_service = get_diff_service()
            discarded_change_set_id = await asyncio.to_thread(
                diff_service.mark_turn_discarded,
                session_id, turn_index,
                project_dir=proj.project_dir,
                extra_history_roots=extra_history_roots,
            )
            await asyncio.to_thread(
                diff_service.truncate_file_ops_by_timestamp,
                session_id, cut_timestamp,
                soft=True,
                discarded=True,
                project_dir=proj.project_dir,
                extra_history_roots=extra_history_roots,
            )
            file_ops_truncated = True

        # 唤醒 git watcher 重算,前端立即收到 diff_changed 事件
        try:
            self._registry.mark_dirty(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GitWS] mark_dirty failed after discard_turn_changes "
                "(project=%s): %s", project_id, exc,
            )

        # 有文件恢复失败时返回 ok=False + partial=True,调用方可重试
        # (file_ops 未截断,失败文件日志仍在,可再次撤销)
        is_partial = bool(restore_errors)
        await self._channel.send_response(
            ws, req_id, ok=not is_partial,
            payload={
                "session_id": session_id,
                "turn_index": turn_index,
                "change_set_id": discarded_change_set_id,
                "restored_files": restore_result.get("restored_files", []),
                "deleted_files": restore_result.get("deleted_files", []),
                "errors": restore_errors,
                "file_ops_truncated": file_ops_truncated,
                # P2: 全局 file_ops 始终不清理(缺 session_id 字段,误伤其他 session)。
                # 显式返回 false 让调用方知晓:last_turn diff 可能残留历史全局记录,
                # 工作区已恢复但 last_turn 与工作区可能不一致。
                "global_file_ops_truncated": False,
                "partial": is_partial,
            },
            error=(
                f"partial failure: {len(restore_errors)} file(s) failed to restore; "
                "file_ops not truncated, retryable"
                if is_partial else None
            ),
            code="PARTIAL_RESTORE_FAILED" if is_partial else None,
        )

    async def _handle_redo_turn_changes(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """重新应用本轮被撤销的代码修改(与 ``discard_turn_changes`` 对称).

        将当前会话最后一轮被 ``discard_turn_changes`` 撤销的文件变更重新
        应用到工作区,恢复 file_ops 日志条目的可见性,并清除该轮的
        discarded 状态。

        前置条件:
          - project_id 指向 code 模式的 Git 项目
          - session_id 非空
          - 会话非忙碌(agent 未在执行)
          - 最后一轮已被 discard(status == "discarded")
        """
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = self._resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await self._channel.send_response(ws, req_id, ok=False, error=err, code=code)
            return

        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session_id is required", code="BAD_REQUEST",
            )
            return

        # 校验 session 与 project 的绑定关系(与 discard 对称)
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        try:
            session_meta = await asyncio.to_thread(
                get_session_metadata, session_id,
                cache_bust=True, enable_writeback=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[GitWS] redo_turn_changes: failed to read session metadata "
                "(session=%s): %s", session_id, exc,
            )
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"failed to read session metadata: {exc}",
                code="INTERNAL_ERROR",
            )
            return

        session_project_id = str(session_meta.get("project_id") or "").strip()
        if not session_project_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session has no project_id binding; cannot verify project ownership",
                code="SESSION_NOT_BOUND",
            )
            return
        if session_project_id != project_id:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=(
                    f"session_id does not belong to project_id: "
                    f"expected {project_id}, got {session_project_id}"
                ),
                code="PROJECT_SESSION_MISMATCH",
            )
            return

        # 校验会话非忙碌
        if self._channel.is_session_busy(session_id):
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="session is busy; stop the current run before redoing changes",
                code="SESSION_BUSY",
            )
            return

        # 获取最后一轮 turn_index 和 timestamp
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            get_last_turn_info,
        )
        last_turn = await asyncio.to_thread(
            get_last_turn_info, session_id=session_id,
        )
        turn_index = last_turn["turn_index"]

        if turn_index <= 0:
            await self._channel.send_response(
                ws, req_id, ok=False,
                error="no turn to redo: session has no user messages",
                code="NO_TURN_TO_REDO",
            )
            return

        # 校验最后一轮已被 discard:只有已撤销的轮次才能 redo
        from jiuwenswarm.server.utils.diff_service import get_diff_service
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )
        extra_history_roots = get_session_extra_history_roots(session_id)
        diff_service = get_diff_service()
        target_turn = await asyncio.to_thread(
            diff_service.get_turn_diff,
            session_id, turn_index=turn_index,
            project_dir=proj.project_dir,
            extra_history_roots=extra_history_roots,
        )
        turn_status = str((target_turn or {}).get("status") or "")
        if turn_status != "discarded":
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=(
                    f"last turn (index={turn_index}) is not discarded "
                    f"(status={turn_status or 'unknown'}); nothing to redo"
                ),
                code="NOTHING_TO_REDO",
            )
            return

        # 重新应用被撤销的文件修改(写回 new_content)
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            redo_session_files,
        )
        try:
            redo_result = await asyncio.to_thread(
                redo_session_files,
                session_id=session_id,
                turn_index=turn_index,
                project_dir=proj.project_dir,
                extra_history_roots=extra_history_roots,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[GitWS] redo_turn_changes: redo failed "
                "(session=%s project=%s): %s",
                session_id, project_id, exc,
            )
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=f"failed to redo session files: {exc}",
                code="INTERNAL_ERROR",
            )
            return

        # 清除 discarded 状态 + 恢复 file_ops 条目可见性(去 discarded_out 标记)
        # 与 discard 对称:后者 mark_turn_discarded + truncate(soft=True, discarded=True),
        # 这里 unmark_turn_discarded(含 restore_rewound_entries(discarded=True))
        redo_errors = redo_result.get("errors", []) or []
        redone_files = redo_result.get("redone_files", []) or []
        deleted_files = redo_result.get("deleted_files", []) or []

        # 空 redo 防护:turn 状态是 discarded 但没有找到任何可恢复的文件条目
        # (file_ops 缺失/损坏/没被打 discarded_out 标记)。若继续 unmark 会把
        # 状态改回 completed,用户看到"成功"但实际没有恢复任何文件,且 discarded
        # 状态丢失无法重试。这里直接返回失败,保留 discarded 状态供排查/重试。
        if not redo_errors and not redone_files and not deleted_files:
            logger.warning(
                "[GitWS] redo_turn_changes: no redoable files found "
                "(session=%s turn=%s project=%s); discarded_out entries missing",
                session_id, turn_index, project_id,
            )
            await self._channel.send_response(
                ws, req_id, ok=False,
                error=(
                    "no redoable files found: file_ops for this discarded turn "
                    "is missing or has no discarded_out entries; "
                    "discarded status preserved"
                ),
                code="REDO_HISTORY_MISSING",
                payload={
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "redone_files": [],
                    "deleted_files": [],
                    "errors": [],
                },
            )
            return

        restored_change_set_id: str | None = None
        if not redo_errors:
            restored_change_set_id = await asyncio.to_thread(
                diff_service.unmark_turn_discarded,
                session_id, turn_index,
                project_dir=proj.project_dir,
                extra_history_roots=extra_history_roots,
            )

        # 唤醒 git watcher 重算
        try:
            self._registry.mark_dirty(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GitWS] mark_dirty failed after redo_turn_changes "
                "(project=%s): %s", project_id, exc,
            )

        is_partial = bool(redo_errors)
        await self._channel.send_response(
            ws, req_id, ok=not is_partial,
            payload={
                "session_id": session_id,
                "turn_index": turn_index,
                "change_set_id": restored_change_set_id,
                "redone_files": redone_files,
                "deleted_files": deleted_files,
                "errors": redo_errors,
                "partial": is_partial,
            },
            error=(
                f"partial failure: {len(redo_errors)} file(s) failed to redo; "
                "discarded status not cleared, retryable"
                if is_partial else None
            ),
            code="PARTIAL_REDO_FAILED" if is_partial else None,
        )

    @staticmethod
    def _extract_files(
        status_dict: dict[str, Any], source: str,
    ) -> dict[str, Any] | None:
        """从 status_dict 中提取指定 source 的 files 映射。

        委托给 ``git_diff_status.extract_files_from_status`` 统一实现,
        与 watcher 共用同一 schema 访问逻辑。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            extract_files_from_status,
        )
        return extract_files_from_status(status_dict, source)

    @staticmethod
    def _strip_hunks(files_dict: dict[str, Any]) -> dict[str, Any]:
        """去除文件条目中的 hunk(文件列表事件不推送 hunk)。

        委托给 ``git_diff_status.file_map_to_dict_no_hunks`` 统一实现,
        确保与 watcher 推送事件及 ``DiffFileEntry.to_dict(include_hunks=False)``
        输出一致(设计文档 §3.6)。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            file_map_to_dict_no_hunks,
        )
        return file_map_to_dict_no_hunks(files_dict)
