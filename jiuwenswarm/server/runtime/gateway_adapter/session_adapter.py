"""SessionAdapter：会话域用户业务适配器。

复用 ``server/runtime/session`` 中立门面，覆盖以下 E2A method（决策 D2：
独立 method，一一对应原 Web/TUI handler）：

- ``session.list`` → ``get_all_sessions_metadata``，输出经 ``to_session_info`` 投影
  （web 通道）或原始 metadata（TUI 通道，按 channel_id 过滤）；
- ``session.get_metadata`` → ``session_metadata.get_session_metadata``（只读 O(1)）；
- ``session.pin`` → ``session_metadata.set_session_pinned``（置顶/取消 + 紧凑重编号）；
- ``session.color_set`` → ``session_metadata``（accent_color 读写，TUI 语义；
  color=None 为查询模式，合法值白名单与 TUI 一致）；
- ``session.preview`` → ``session_history.load_history_records`` + 对话白名单过滤
  （chat.final / team.message，与 TUI 预览行为一致）；
- ``session.delete`` → 目录删除（team session 拒绝、evict KV cache）；
- ``session.rename`` → 会话重命名；
- ``session.restore_files`` → 会话文件恢复；
- ``history.list_turns`` → 会话历史轮次列表。

注：``session.delete`` / ``session.rename`` 在 AgentServer 在线 dispatch 时
由 ``_GATEWAY_ADAPTER_LEGACY_METHODS`` 跳过适配器、走原 handler（保留
KV cache evict 等在线状态清理语义）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
    parse_int_param,
)
from jiuwenswarm.server.runtime.session.session_history import (
    load_history_records,
)
from jiuwenswarm.server.runtime.session.session_info import to_session_info
from jiuwenswarm.server.runtime.session.session_metadata import (
    _read_metadata,
    _write_metadata_sync,
    get_all_sessions_metadata,
    get_session_metadata,
    set_session_pinned,
)
from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename

logger = logging.getLogger(__name__)

_SESSION_LIST_LIMIT_DEFAULT: Final[int] = 20
_SESSION_LIST_LIMIT_MAX: Final[int] = 200

# TUI session.color_set 合法色值白名单（与 tui_connect._session_color_set 一致）
_VALID_ACCENT_COLORS: Final[frozenset[str]] = frozenset(
    {"default", "blue", "green", "pink", "purple", "red", "yellow"}
)

# session.preview 对话类型白名单（与 tui_connect._session_preview 一致）：
# chat.final（assistant 完整最终回复）与 team.message（团队消息）；刻意排除
# chat.delta / reasoning / tool_* / error / usage_* 等碎片化或非对话记录。
_PREVIEW_CHAT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"chat.final", "team.message"}
)

_PREVIEW_COUNT_DEFAULT: Final[int] = 30
_PREVIEW_COUNT_MAX: Final[int] = 100


def _is_previewable(item: object) -> bool:
    """session.preview 白名单判定（与 TUI 预览一致）。"""
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    content = item.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    if role == "user":
        return has_content
    # 非 user 记录只放行白名单内的对话类型（兼容团队成员回复的 teammate 等 role）
    return item.get("event_type") in _PREVIEW_CHAT_EVENT_TYPES and has_content


def _build_preview_messages(raw: object, count: int) -> list[dict[str, object]]:
    """从历史记录提取最新 ``count`` 条可预览对话（保持时间顺序）。"""
    messages: list[dict[str, object]] = []
    if not isinstance(raw, list):
        return messages
    previewable = [item for item in raw if _is_previewable(item)]
    for msg in previewable[-count:]:
        messages.append(
            {
                "role": msg.get("role", "unknown"),
                "content": msg.get("content", "") if isinstance(msg.get("content"), str) else "",
                "event_type": msg.get("event_type", ""),
            }
        )
    return messages


class SessionAdapter(GatewayAdapter):
    """会话域适配器：session.list/get_metadata/pin/color_set/preview/delete/rename。"""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.SESSION_LIST.value,
            ReqMethod.SESSION_GET_METADATA.value,
            ReqMethod.SESSION_PIN.value,
            ReqMethod.SESSION_COLOR_SET.value,
            ReqMethod.SESSION_PREVIEW.value,
            ReqMethod.HISTORY_LIST_TURNS.value,
            ReqMethod.SESSION_RESTORE_FILES.value,
            # 以下两个 method 保留给 e2a_proxy 的单用户共享目录离线 fallback
            # 使用；AgentWebSocketServer 在线 dispatch 会显式跳过适配器、走
            # 原 handler：SESSION_DELETE 需执行 Team runtime 清理、
            # Runner.release 和 session binding 解绑；SESSION_RENAME 保持迁移前
            # 的失败 code 语义。
            ReqMethod.SESSION_DELETE.value,
            ReqMethod.SESSION_RENAME.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method
        if method == ReqMethod.SESSION_GET_METADATA:
            return await self._handle_get_metadata(request)
        if method == ReqMethod.SESSION_PIN:
            return await self._handle_pin(request)
        if method == ReqMethod.SESSION_COLOR_SET:
            return await self._handle_color_set(request)
        if method == ReqMethod.SESSION_PREVIEW:
            return await self._handle_preview(request)
        if method == ReqMethod.HISTORY_LIST_TURNS:
            return await self._handle_history_list_turns(request)
        if method == ReqMethod.SESSION_RESTORE_FILES:
            return await self._handle_restore_files(request)
        if method == ReqMethod.SESSION_DELETE:
            return await self._handle_delete(request)
        if method == ReqMethod.SESSION_RENAME:
            return await self._handle_rename(request)
        return await self._handle_list(request)

    async def _handle_history_list_turns(self, request: AgentRequest) -> AgentResponse:
        """Return session turns without entering the chat request path."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            list_session_turns,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        if not session_id:
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        try:
            payload = await asyncio.to_thread(list_session_turns, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] history.list_turns failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_restore_files(self, request: AgentRequest) -> AgentResponse:
        """Restore files for a historical turn without creating a chat turn."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            restore_session_files,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        if not session_id:
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        turn_index = params.get("turn_index")
        if turn_index is None:
            return build_error_response(
                request, "turn_index is required", code="BAD_REQUEST"
            )
        try:
            turn_index = int(turn_index)
        except (TypeError, ValueError):
            return build_error_response(
                request, "turn_index must be an integer", code="BAD_REQUEST"
            )
        try:
            payload = await asyncio.to_thread(
                restore_session_files, session_id=session_id, turn_index=turn_index
            )
        except ValueError as exc:
            return build_error_response(request, str(exc), code="BAD_REQUEST")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.restore_files failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_list(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        # Keep the requested page intact even when metadata loading fails.  The
        # TUI fallback contract is an empty page, not a reset to page one.
        limit = _SESSION_LIST_LIMIT_DEFAULT
        offset = 0
        try:
            limit = parse_int_param(
                params,
                "limit",
                _SESSION_LIST_LIMIT_DEFAULT,
                minimum=1,
                maximum=_SESSION_LIST_LIMIT_MAX,
            )
            offset = parse_int_param(
                params, "offset", 0, minimum=0, maximum=10**9
            )
            sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.list failed: %s", exc)
            # 按通道保持迁移前的失败契约：
            # - TUI：迁移前由 AgentServer handler 处理，元数据读失败时降级为
            #   可渲染的空列表而非 RPC 失败；
            # - Web：迁移前由 Gateway 本地 handler 处理，异常上抛返回
            #   ok=False（INTERNAL_ERROR），不静默吞成"无会话"。
            channel_id = str(request.channel_id or "").strip().lower()
            if channel_id == "tui":
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "sessions": [],
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                    },
                    metadata=request.metadata,
                )
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")

        # 按通道分流（决策 Q3 核对结论）：
        # - TUI 通道已走 E2A 且消费原始 metadata（channel_id 过滤、channel_metadata.git_branch 等），
        #   保持原始格式，避免破坏 TUI session.list 行为；
        # - 其余通道（web 等）输出投影 SessionInfo，与 Web fallback 迁移前 payload 完全一致。
        channel_id = str(request.channel_id or "").strip().lower()
        if channel_id == "tui":
            session_infos = sessions
        else:
            session_infos = [to_session_info(s) for s in sessions]
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "sessions": session_infos,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            metadata=request.metadata,
        )

    async def _handle_get_metadata(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        sid = params.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        sid = sid.strip()
        try:
            # cache_bust=True 强制读盘，跨进程（Gateway 读 / AgentServer 写）拿最新
            meta = get_session_metadata(sid, cache_bust=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.get_metadata failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        if not meta:
            return build_error_response(
                request, "session not found", code="NOT_FOUND"
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=meta,
            metadata=request.metadata,
        )

    async def _handle_pin(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        sid = params.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        sid = sid.strip()
        raw_pinned = params.get("pinned")
        if not isinstance(raw_pinned, bool):
            return build_error_response(
                request, "pinned must be boolean", code="BAD_REQUEST"
            )
        try:
            result = set_session_pinned(sid, raw_pinned)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.pin failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        if result is None:
            return build_error_response(
                request, "session not found", code="NOT_FOUND"
            )
        new_pinned, new_order = result
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"pinned": new_pinned, "pin_order": new_order},
            metadata=request.metadata,
        )

    async def _handle_color_set(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or request.session_id or "").strip()
        if not target:
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        color = params.get("color")
        if color is None:
            # 查询模式：cache_bust=True 强制读盘，跨进程拿最新（与 _handle_get_metadata 一致）
            try:
                metadata = get_session_metadata(target, cache_bust=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[SessionAdapter] session.color_set query failed: %s", exc)
                return build_error_response(request, str(exc), code="INTERNAL_ERROR")
            accent_color = metadata.get("accent_color", "default") if metadata else "default"
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"session_id": target, "accent_color": accent_color},
                metadata=request.metadata,
            )

        if str(color) not in _VALID_ACCENT_COLORS:
            return build_error_response(
                request, f"invalid color: {color}", code="BAD_REQUEST"
            )
        try:
            # 设置模式 - 同步写入确保跨进程可见（与 TUI 原实现一致）
            metadata = _read_metadata(target)
            metadata["accent_color"] = str(color)
            _write_metadata_sync(target, metadata)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.color_set failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"session_id": target, "accent_color": str(color)},
            metadata=request.metadata,
        )

    async def _handle_preview(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or request.session_id or "").strip()
        if not target:
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        preview_count = parse_int_param(
            params,
            "count",
            _PREVIEW_COUNT_DEFAULT,
            minimum=1,
            maximum=_PREVIEW_COUNT_MAX,
        )
        try:
            raw = load_history_records(target)
            preview_messages = _build_preview_messages(raw, preview_count)
        except Exception as exc:  # noqa: BLE001
            # 与迁移前 TUI 行为一致：历史读取异常时优雅降级为空预览，
            # 而非整条失败（前端据 ok=True + 空列表渲染空预览）。
            logger.warning("[SessionAdapter] session.preview read failed: %s", exc)
            preview_messages = []
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"session_id": target, "preview_messages": preview_messages},
            metadata=request.metadata,
        )

    async def _handle_delete(self, request: AgentRequest) -> AgentResponse:
        """session.delete 文件级删除（单用户共享目录 legacy fallback 语义）。

        Web/TUI 手写 ``_delete_from_shared_dir`` 收敛到本适配器，使单用户模式
        AgentServer 不可达时由薄代理跑同一中立门面。与迁移前手写行为一致：
        team session 拒绝（需 AgentServer runtime）、目录不存在返回
        NOT_FOUND、删除前 evict KV cache、``shutil.rmtree`` 目录。
        """
        import shutil

        # 函数级 import：与既有 handler 惯例一致，保证测试可 monkeypatch 源模块
        # 门面（monkeypatch 不改变已绑定到适配器模块的引用）。用别名避免
        # 与模块级同名导入产生 redefined-outer-name 告警。
        from jiuwenswarm.common.utils import (
            get_agent_sessions_dir as _get_agent_sessions_dir,
        )
        from jiuwenswarm.server.runtime.session.session_history import (
            resolve_session_dir as _resolve_session_dir,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata as _get_session_metadata,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or request.session_id or "").strip()
        if not target:
            return build_error_response(
                request, "session_id is required", code="BAD_REQUEST"
            )
        try:
            metadata = _get_session_metadata(target)
            if str(metadata.get("mode") or "").strip().lower() == "team":
                return build_error_response(
                    request,
                    "team session delete requires agent server",
                    code="AGENT_UNAVAILABLE",
                )
            session_dir, invalid_reason = _resolve_session_dir(
                target, sessions_root=_get_agent_sessions_dir()
            )
            if session_dir is None:
                return build_error_response(
                    request,
                    invalid_reason or "invalid session_id",
                    code="BAD_REQUEST",
                )
            if not session_dir.exists():
                return build_error_response(
                    request, "session not found", code="NOT_FOUND"
                )
            if not session_dir.is_dir():
                return build_error_response(
                    request, "session is not a directory", code="BAD_REQUEST"
                )
            from openjiuwen.core.session.agent import create_agent_session
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
                get_kv_cache_runtime,
            )

            try:
                session = create_agent_session(
                    session_id=target,
                    kv_cache_runtime=get_kv_cache_runtime(),
                )
                await session.release_kvc()
            except Exception as exc:  # noqa: BLE001 - preserve deletion behavior
                logger.warning(
                    "[SessionAdapter] session.delete KV cache evict failed: "
                    "session_id=%s error=%s",
                    target,
                    exc,
                )
            await asyncio.to_thread(shutil.rmtree, session_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.delete failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"session_id": target},
            metadata=request.metadata,
        )

    async def _handle_rename(self, request: AgentRequest) -> AgentResponse:
        """session.rename（查询/清除/设置）经中立门面 ``apply_session_rename`` 执行。

        与 AgentServer ``_handle_session_rename`` / 迁移前 TUI 手写 fallback 共用
        同一门面；``init_channel_id`` 取请求通道（tui/web），保持通道语义。
        """
        params = request.params if isinstance(request.params, dict) else {}
        channel = str(request.channel_id or "").strip() or "tui"
        try:
            ok, payload, err, code = apply_session_rename(
                params,
                request.session_id or "",
                init_channel_id=channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionAdapter] session.rename failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        if ok:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload or {},
                metadata=request.metadata,
            )
        return build_error_response(request, err or "session.rename failed", code=code or "BAD_REQUEST")
