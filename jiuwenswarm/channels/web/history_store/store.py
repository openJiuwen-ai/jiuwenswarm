# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""会话历史存储：个人版纯内存；企业版 mysql/pg 经 foundation DB-actor。

写路径可来自：
- ``app_web`` WS 反代（``HistoryFrameRunner``）
- Gateway ``WebChannel`` Listen

存储后端：
- ``memory``（个人版）：进程内内存，不落库、不引 foundation。
- ``mysql``/``postgresql``（企业版）：经 foundation 高层 CRUD
  (``list_records``/``get``/``create``/``update``)，统一在 ``HistoryDbActor`` 专用 loop
  内执行（asyncpg/aiomysql engine 绑 event loop，而本库被多 loop/线程消费——写经
  ``HistoryFrameRunner`` 独立 loop、读经同步 HTTP 线程）。

sqlite 已废弃（两端均不支持）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Literal

from .settings import WebHistoryDbSettings, resolve_history_db_type

logger = logging.getLogger("jiuwenswarm.web.history")

_TITLE_LEN = 30
_PREVIEW_LEN = 100
# list 的单页上限：project.sessions 一次拉 500 条做项目内过滤，需与之对齐
_MAX_LIST_LIMIT = 500

HistoryBackend = Literal["memory", "mysql", "postgresql"]


class ChatHistoryStore:
    """会话历史存储：memory（个人版）/ mysql·pg（企业版）。"""

    def __init__(
        self,
        settings: WebHistoryDbSettings | None = None,
        *,
        memory: bool = False,
        db_type: str | None = None,
    ) -> None:
        self._settings = settings
        self._memory = memory
        self._db_type = (db_type or "").strip().lower() or None
        self._mem_lock = threading.Lock()
        self._mem_sessions: dict[str, dict[str, Any]] = {}
        self._mem_messages: list[dict[str, Any]] = []
        self._actor: Any = None  # HistoryDbActor（惰性，仅企业版远程路径）

    @classmethod
    def for_db_type(cls, db_type: str) -> "ChatHistoryStore":
        normalized = str(db_type or "").strip().lower() or "memory"
        if normalized == "memory":
            return cls(memory=True)
        if normalized in ("postgresql", "postgres", "pg"):
            return cls(settings=WebHistoryDbSettings.from_env(), db_type="postgresql")
        if normalized == "mysql":
            return cls(settings=WebHistoryDbSettings.from_env(), db_type="mysql")
        logger.warning(
            "[history] 不支持的历史库类型 %r，回退内存（不支持 sqlite）",
            normalized,
        )
        return cls(memory=True)

    @classmethod
    def from_env(cls) -> "ChatHistoryStore":
        return cls.for_db_type(resolve_history_db_type())

    @classmethod
    def memory(cls) -> "ChatHistoryStore":
        return cls(memory=True)

    @property
    def backend(self) -> HistoryBackend:
        if self._memory:
            return "memory"
        if self._db_type == "postgresql":
            return "postgresql"
        if self._db_type == "mysql":
            return "mysql"
        return "memory"

    @property
    def mysql_settings(self) -> WebHistoryDbSettings | None:
        return self._settings

    @property
    def available(self) -> bool:
        if self._memory:
            return True
        return self._settings is not None

    def _get_actor(self) -> Any:
        if self._actor is None:
            from .db_actor import HistoryDbActor

            self._actor = HistoryDbActor(self._settings, self._db_type)
        return self._actor

    def _insert_message_and_upsert_session(
        self,
        _conn: Any,
        *,
        session_id: str,
        request_id: str,
        role: str,
        content: str,
        event_type: str | None,
        ts: float,
        user: str | None,
        title: str | None,
        preview: str,
        group_id: str | None = None,
        bot_id: str | None = None,
        project_id: str | None = None,
        cron_id: str | None = None,
        work_mode: str | None = None,
    ) -> bool:
        """memory 写入（企业版远程走 actor，不经此方法）。"""
        from .identity import normalize_identity_value

        key = (session_id, request_id, role)
        with self._mem_lock:
            if any(
                (m["session_id"], m["request_id"], m["role"]) == key
                for m in self._mem_messages
            ):
                return False
            self._mem_messages.append({
                "session_id": session_id,
                "request_id": request_id,
                "role": role,
                "content": content,
                "event_type": event_type,
                "timestamp": ts,
            })
            existing = self._mem_sessions.get(session_id)
            if existing is None:
                self._mem_sessions[session_id] = {
                    "session_id": session_id,
                    "user": user or "guest",
                    "title": title,
                    "message_count": 1,
                    "last_preview": preview,
                    "created_at": ts,
                    "updated_at": ts,
                    "last_user_message_at": ts if role == "user" else None,
                    "group_id": normalize_identity_value(group_id),
                    "bot_id": normalize_identity_value(bot_id),
                    "project_id": str(project_id).strip() if str(project_id or "").strip() else None,
                    "cron_id": str(cron_id).strip() if str(cron_id or "").strip() else None,
                    "work_mode": str(work_mode).strip() if str(work_mode or "").strip() else None,
                }
            else:
                existing["message_count"] = int(existing.get("message_count") or 0) + 1
                existing["last_preview"] = preview
                existing["updated_at"] = ts
                if role == "user":
                    existing["last_user_message_at"] = ts
                if not existing.get("title") and title:
                    existing["title"] = title
        return True

    async def record_user(
        self,
        *,
        request_id: str,
        session_id: str,
        query: str,
        ts: float,
        user: str | None = None,
        group_id: str | None = None,
        bot_id: str | None = None,
        project_id: str | None = None,
        cron_id: str | None = None,
        work_mode: str | None = None,
    ) -> bool:
        if not user:
            user = "guest"
        title = query[:_TITLE_LEN]
        preview = query[:_PREVIEW_LEN]
        if self._memory:
            inserted = await asyncio.to_thread(
                self._insert_message_and_upsert_session,
                None,
                session_id=session_id,
                request_id=request_id,
                role="user",
                content=query,
                event_type=None,
                ts=ts,
                user=user,
                title=title,
                preview=preview,
                group_id=group_id,
                bot_id=bot_id,
                project_id=project_id,
                cron_id=cron_id,
                work_mode=work_mode,
            )
        else:
            inserted = await self._get_actor().record_message(
                session_id=session_id,
                request_id=request_id,
                role="user",
                content=query,
                event_type=None,
                ts=ts,
                user=user,
                title=title,
                preview=preview,
                group_id=group_id,
                bot_id=bot_id,
                project_id=project_id,
                cron_id=cron_id,
                work_mode=work_mode,
            )
        if inserted:
            logger.info(
                "[history] 落盘 user: rid=%s sid=%s user=%s len=%d",
                request_id, session_id, user, len(query),
            )
        return inserted

    async def record_assistant(
        self,
        *,
        request_id: str,
        session_id: str,
        content: str,
        event_type: str,
        ts: float,
    ) -> bool:
        preview = content[:_PREVIEW_LEN]
        if self._memory:
            inserted = await asyncio.to_thread(
                self._insert_message_and_upsert_session,
                None,
                session_id=session_id,
                request_id=request_id,
                role="assistant",
                content=content,
                event_type=event_type,
                ts=ts,
                user="guest",
                title=None,
                preview=preview,
            )
        else:
            inserted = await self._get_actor().record_message(
                session_id=session_id,
                request_id=request_id,
                role="assistant",
                content=content,
                event_type=event_type,
                ts=ts,
                user="guest",
                title=None,
                preview=preview,
            )
        if inserted:
            logger.info(
                "[history] 落盘 assistant: rid=%s sid=%s event=%s len=%d",
                request_id, session_id, event_type, len(content),
            )
        return inserted

    def list_sessions_blocking(
        self, *, limit: int, offset: int, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(0, offset)
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                rows = [
                    dict(s) for s in self._mem_sessions.values()
                    if s.get("user") == user
                ]
            rows = [r for r in rows if scope_matches(r, group_id, bot_id)]
            rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
            return rows[offset:offset + limit]
        try:
            return self._get_actor().list_sessions_sync(
                limit=limit, offset=offset, user=user,
                group_id=group_id, bot_id=bot_id,
            )
        except Exception:
            logger.exception("[history] %s 读取会话列表失败", self.backend.upper())
            return []

    def count_sessions_blocking(
        self, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> int:
        """全量会话计数。

        与 list 不同：store/DB 不可用时抛异常，供调用方区分
        确实为空与库故障（list 失败返回 [] 的语义保持不变）。
        """
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                return sum(
                    1 for s in self._mem_sessions.values()
                    if s.get("user") == user and scope_matches(s, group_id, bot_id)
                )
        return self._get_actor().count_sessions_sync(
            user=user, group_id=group_id, bot_id=bot_id,
        )

    def get_session_detail_strict_blocking(
        self, session_id: str, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> dict[str, Any] | None:
        """读会话详情，严格版：DB 故障抛异常；None 仅表示会话不存在。

        网关 history.get 回退需要区分库挂了（透传原始错误，不合成空页）
        与会话确实没有历史（合成空页）。memory 后端无库故障态，直接复用。
        """
        if not user:
            user = "guest"
        if self._memory:
            return self.get_session_detail_blocking(
                session_id, user=user, group_id=group_id, bot_id=bot_id,
            )
        return self._get_actor().get_session_detail_sync(
            session_id=session_id, user=user, group_id=group_id, bot_id=bot_id,
        )

    def get_session_detail_blocking(
        self, session_id: str, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                s = self._mem_sessions.get(session_id)
                if s is None or s.get("user") != user:
                    return None
                if not scope_matches(s, group_id, bot_id):
                    return None
                msgs = [
                    {
                        "role": m["role"],
                        "content": m["content"],
                        "event_type": m["event_type"],
                        "timestamp": m["timestamp"],
                        "request_id": m["request_id"],
                    }
                    for m in self._mem_messages
                    if m["session_id"] == session_id
                ]
            msgs.sort(key=lambda m: float(m.get("timestamp") or 0))
            return {**s, "messages": msgs}
        try:
            return self._get_actor().get_session_detail_sync(
                session_id=session_id, user=user, group_id=group_id, bot_id=bot_id,
            )
        except Exception:
            logger.exception("[history] %s 读取会话详情失败", self.backend.upper())
            return None

    async def list_sessions(
        self, *, limit: int = 20, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if self._memory:
            return await asyncio.to_thread(
                self.list_sessions_blocking, limit=limit, offset=offset, user=None,
            )
        return await self._get_actor().list_sessions(limit=limit, offset=offset, user=None)

    async def get_session_detail(self, session_id: str) -> dict[str, Any] | None:
        if self._memory:
            return await asyncio.to_thread(
                self.get_session_detail_blocking, session_id, user=None,
            )
        return await self._get_actor().get_session_detail(session_id=session_id, user=None)

    def delete_session_blocking(
        self, session_id: str, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> bool:
        """删除会话（sessions 行 + messages 行）。库不可用返回 False。

        行身份列非空时还须与查询身份 scope 匹配（跨组织越权拒绝）。
        """
        if not session_id:
            return False
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                s = self._mem_sessions.get(session_id)
                if s is None:
                    return False
                if user and s.get("user") != user:
                    return False
                if not scope_matches(s, group_id, bot_id):
                    return False
                del self._mem_sessions[session_id]
                self._mem_messages = [
                    m for m in self._mem_messages if m.get("session_id") != session_id
                ]
            return True
        try:
            return self._get_actor().delete_session_sync(
                str(session_id), user=user, group_id=group_id, bot_id=bot_id,
            )
        except Exception:
            logger.exception("[history] %s 删除会话失败", self.backend.upper())
            return False

    def set_session_pinned_blocking(
        self, session_id: str, pinned: bool, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> tuple[bool, int] | None:
        """置顶/取消置顶并紧凑重编号；目标会话不存在返回 None。

        重编号范围限定当前身份 scope（与 session.list 可见口径一致）。
        """
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                s = self._mem_sessions.get(session_id)
                if s is None or s.get("user") != user:
                    return None
                s["pinned"] = bool(pinned)
                s["pin_order"] = int(s.get("pin_order") or 0) if pinned else 0
                pinned_list = sorted(
                    (
                        x for x in self._mem_sessions.values()
                        if x.get("pinned") and scope_matches(x, group_id, bot_id)
                    ),
                    key=lambda x: int(x.get("pin_order") or 0),
                )
                orders: dict[str, int] = {}
                for idx, x in enumerate(pinned_list, start=1):
                    x["pinned"] = True
                    x["pin_order"] = idx
                    orders[str(x.get("session_id"))] = idx
                return pinned, orders.get(session_id, 0)
        return self._get_actor().set_session_pinned_sync(
            session_id, pinned, user=user, group_id=group_id, bot_id=bot_id,
        )

    def rename_session_blocking(
        self, session_id: str, title: str | None, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> dict[str, Any] | None:
        """改标题/查询（``title=None`` 仅查询）；会话不存在返回 None。

        行身份列非空且与查询身份 scope 不匹配（跨组织越权）同样返回 None。
        严格版：DB 故障异常向上抛（与 set_session_pinned_blocking 一致），
        由调用方区分库故障与会话不存在。
        """
        if not session_id:
            return None
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                s = self._mem_sessions.get(session_id)
                if s is None or s.get("user") != user:
                    return None
                if not scope_matches(s, group_id, bot_id):
                    return None
                previous_title = str(s.get("title") or "")
                if title is not None:
                    s["title"] = title or None
                return {
                    "title": previous_title if title is None else title,
                    "previous_title": previous_title,
                }
        return self._get_actor().rename_session_sync(
            str(session_id), title, user=user, group_id=group_id, bot_id=bot_id,
        )

    def list_pinned_sessions_blocking(
        self, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """当前身份 scope 内全部置顶会话，按 pin_order 升序。库不可用返回空。"""
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                rows = [
                    dict(s) for s in self._mem_sessions.values()
                    if s.get("user") == user and s.get("pinned")
                ]
            rows = [r for r in rows if scope_matches(r, group_id, bot_id)]
            rows.sort(key=lambda r: int(r.get("pin_order") or 0))
            return rows
        try:
            return self._get_actor().list_pinned_sessions_sync(
                user=user, group_id=group_id, bot_id=bot_id,
            )
        except Exception:
            logger.exception("[history] %s 读取置顶会话失败", self.backend.upper())
            return []

    def list_all_sessions_blocking(
        self, *, user: str | None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """当前身份 scope 内全部会话行（project 视图切 PG 用）。

        库不可用返回 ``None``（与 list_sessions_blocking 返回 [] 区分：
        调用方可据此回退 pod 拉取路径）。
        """
        if not user:
            user = "guest"
        if self._memory:
            from .identity import scope_matches

            with self._mem_lock:
                rows = [
                    dict(s) for s in self._mem_sessions.values()
                    if s.get("user") == user
                ]
            rows = [r for r in rows if scope_matches(r, group_id, bot_id)]
            rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
            return rows
        try:
            return self._get_actor().list_all_sessions_sync(
                user=user, group_id=group_id, bot_id=bot_id,
            )
        except Exception:
            logger.exception("[history] %s 读取全量会话失败", self.backend.upper())
            return None

    def ensure_session_row_blocking(
        self,
        session_id: str,
        *,
        user: str | None,
        group_id: str | None = None,
        bot_id: str | None = None,
        project_id: str | None = None,
        cron_id: str | None = None,
        work_mode: str | None = None,
        title: str | None = None,
        ts: float,
    ) -> bool:
        """session.create 落行（web/cron 共用）；库不可用抛异常由调用方忽略。"""
        if self._memory:
            from .identity import normalize_identity_value

            with self._mem_lock:
                existing = self._mem_sessions.get(session_id)
                if existing is None:
                    self._mem_sessions[session_id] = {
                        "session_id": session_id,
                        "user": user or "guest",
                        "title": (str(title).strip() or None) if title is not None else None,
                        "message_count": 0,
                        "last_preview": None,
                        "created_at": ts,
                        "updated_at": ts,
                        "last_user_message_at": None,
                        "group_id": normalize_identity_value(group_id),
                        "bot_id": normalize_identity_value(bot_id),
                        "project_id": str(project_id).strip() if str(project_id or "").strip() else None,
                        "cron_id": str(cron_id).strip() if str(cron_id or "").strip() else None,
                        "work_mode": str(work_mode).strip() if str(work_mode or "").strip() else None,
                    }
                    return True

            def _blank(v: Any) -> bool:
                if v is None:
                    return True
                if isinstance(v, str):
                    return not v.strip()
                return False

            with self._mem_lock:
                existing = self._mem_sessions.get(session_id)
                if existing is not None:
                    for col, value in (
                        ("group_id", normalize_identity_value(group_id)),
                        ("bot_id", normalize_identity_value(bot_id)),
                        ("project_id", str(project_id or "").strip() or None),
                        ("cron_id", str(cron_id or "").strip() or None),
                        ("work_mode", str(work_mode or "").strip() or None),
                    ):
                        if value and _blank(existing.get(col)):
                            existing[col] = value
                return True
        return self._get_actor().ensure_session_row_sync(
            session_id,
            user=user,
            group_id=group_id,
            bot_id=bot_id,
            project_id=project_id,
            cron_id=cron_id,
            work_mode=work_mode,
            title=title,
            ts=ts,
        )

    def touch_session_blocking(self, session_id: str, *, ts: float) -> bool:
        """仅刷新活动时间（cron run 后调用）；memory 分支与 DB 语义一致。"""
        if self._memory:
            with self._mem_lock:
                s = self._mem_sessions.get(session_id)
                if s is None:
                    return False
                s["updated_at"] = ts
                s["last_user_message_at"] = ts
                return True
        try:
            return self._get_actor().touch_session_sync(session_id, ts=ts)
        except Exception:
            logger.exception("[history] %s 刷新会话活动时间失败", self.backend.upper())
            return False

    async def delete_session(
        self, session_id: str, *, user: str | None = None,
        group_id: str | None = None, bot_id: str | None = None,
    ) -> bool:
        if self._memory:
            return await asyncio.to_thread(
                self.delete_session_blocking, session_id, user=user,
                group_id=group_id, bot_id=bot_id,
            )
        return await self._get_actor().delete_session(
            str(session_id), user=user, group_id=group_id, bot_id=bot_id,
        )

    async def close(self) -> None:
        if self._actor is not None:
            self._actor.stop()
        logger.info("[history] store 已关闭 backend=%s", self.backend)
