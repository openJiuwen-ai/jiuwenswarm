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

    async def _send_git_error_response(
        self, ws: Any, req_id: str, exc: Exception,
    ) -> None:
        """发送 Git 结构化错误响应(设计文档 §1.4)。

        委托给共享 helper ``project_git.send_git_error_response``。
        """
        from jiuwenswarm.server.runtime.session.project_git import send_git_error_response
        await send_git_error_response(self._channel, ws, req_id, exc)

    @staticmethod
    def _raise_diff_status_error(status_dict: dict[str, Any]) -> None:
        """首次快照的 diff 状态获取失败时抛携带结构化 code 的异常。

        AgentServer 响应携带 ``{error, code}``(如 ``PROJECT_NOT_FOUND`` /
        ``FORBIDDEN`` / ``NOT_GIT_REPOSITORY``);若退化为普通 ``RuntimeError``,
        ``send_git_error_response`` 会走非 GitError 兜底分支,code 丢失并
        退化为 ``INTERNAL_ERROR``,破坏前端按 code 分支的行为。
        """
        from jiuwenswarm.server.runtime.session.project_git import (
            GitError,
            GitOperationError,
        )

        message = str(status_dict.get("error") or "diff status failed")
        code = str(status_dict.get("code") or "").strip()
        if not code:
            raise RuntimeError(message)
        raise GitOperationError(GitError(code=code, message=message))

    async def _fetch_diff_status_via_e2a(
        self,
        ws: Any,
        project_id: str,
        session_id: str | None,
        *,
        include_files: bool,
        include_hunks: bool,
        hunk_paths: list[str] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """经 E2A 从目标 AgentServer 获取项目 diff 状态（项目解析 + diff 计算都在注入目录）。

        请求构造与 GitDiffWatcherRegistry 轮询 fetcher 共用
        ``e2a_proxy.fetch_git_diff_status``。
        """
        from jiuwenswarm.gateway.routing.e2a_proxy import fetch_git_diff_status

        return await fetch_git_diff_status(
            agent_client=getattr(self._channel, "agent_client", None),
            project_id=project_id,
            session_id=session_id or None,
            include_files=include_files,
            include_hunks=include_hunks,
            hunk_paths=hunk_paths,
            user_id=self._channel.connection_user_id(ws),
            channel_id="web",
        )

    async def _proxy_turn_mutation(
        self,
        ws: Any,
        req_id: str,
        params: dict[str, Any],
        *,
        req_method: Any,
        busy_error: str,
    ) -> None:
        """Keep socket coordination here while AgentServer changes user files."""
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        session_id = str(params.get("session_id") or "").strip()
        if session_id and self._channel.is_session_busy(session_id):
            await self._channel.send_response(
                ws, req_id, ok=False, error=busy_error, code="SESSION_BUSY"
            )
            return
        project_id = str(params.get("project_id") or "").strip()
        connection_user_id = self._channel.connection_user_id(ws)

        def _on_done(ok: bool, payload: dict[str, Any]) -> None:
            # 成功或"已发生局部变更的失败"（discard/redo 部分恢复）都唤醒 watcher，
            # 让前端立即重算 diff；完全失败的请求无需唤醒。
            if ok or bool(payload.get("partial")):
                self._registry.mark_dirty(project_id)

        await proxy_unary_request(
            channel=self._channel,
            agent_client=getattr(self._channel, "agent_client", None),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id or None,
            user_id=connection_user_id,
            req_method=req_method,
            label=req_method.value,
            preserve_error_payload=True,
            on_done=_on_done,
        )

    async def _handle_diff_watch(
        self, ws: Any, req_id: str, params: dict[str, Any],
    ) -> None:
        """订阅 diff summary 监控(设计文档 §4.2.1)。

        首次响应只返回统计快照(``files`` 固定 ``{}``);后续变化由
        ``GitDiffWatcherRegistry`` 推送 ``project.git.diff_changed`` 事件。
        ``include_last_turn``(默认 true)控制是否监控 last_turn 变化。
        """
        project_id = str(params.get("project_id") or "").strip()

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
            """计算首次 summary 快照并发送响应;抛错由 registry 触发 remove_watch。

            项目解析与 diff 计算经 E2A 在目标 AgentServer 注入目录执行。
            """
            # 与轮询路径对齐:只在 include_last_turn 时传 session_id。
            session_id_for_status = session_id if include_last_turn else None
            ok, status_dict = await self._fetch_diff_status_via_e2a(
                ws,
                project_id,
                session_id_for_status,
                include_files=False,
                include_hunks=False,
            )
            if not ok:
                self._raise_diff_status_error(status_dict)
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
                    "repo_is_parent_of_project": repo.get("repo_is_parent_of_project", False),
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

        async def _on_snapshot(watch: Any) -> None:
            """计算首次 files 快照并发送响应;抛错由 registry 触发回滚。"""
            session_id = str(params.get("session_id") or "").strip() or watch.session_id
            # 与轮询路径对齐:只在 source="last_turn" 时传 session_id。
            session_id_for_status = session_id if source == "last_turn" else None
            ok, status_dict = await self._fetch_diff_status_via_e2a(
                ws, project_id, session_id_for_status,
                include_files=True, include_hunks=False,
            )
            if not ok:
                self._raise_diff_status_error(status_dict)
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

        async def _on_snapshot(watch: Any) -> None:
            """计算首次 detail 快照并发送响应;抛错由 registry 触发回滚。"""
            session_id = str(params.get("session_id") or "").strip() or watch.session_id
            # 与轮询路径对齐:只在 source="last_turn" 时传 session_id。
            session_id_for_status = session_id if source == "last_turn" else None
            ok, status_dict = await self._fetch_diff_status_via_e2a(
                ws, project_id, session_id_for_status,
                include_files=True, include_hunks=True,
                hunk_paths=detail_files if source == "current" else None,
            )
            if not ok:
                self._raise_diff_status_error(status_dict)
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
        from jiuwenswarm.common.schema.message import ReqMethod

        await self._proxy_turn_mutation(
            ws,
            req_id,
            params,
            req_method=ReqMethod.PROJECT_GIT_DISCARD_TURN_CHANGES,
            busy_error="session is busy; stop the current run before discarding changes",
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
        from jiuwenswarm.common.schema.message import ReqMethod

        await self._proxy_turn_mutation(
            ws,
            req_id,
            params,
            req_method=ReqMethod.PROJECT_GIT_REDO_TURN_CHANGES,
            busy_error="session is busy; stop the current run before redoing changes",
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
