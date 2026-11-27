from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenclaw.channel.vibeskill_session_dcs_store import VibeSkillSessionDcsStore

logger = logging.getLogger(__name__)

VIBESKILL_CHANNEL_ID = "vibeskill"
_VIBESKILL_ORIGINAL_SESSION_ID_KEY = "vibeskill_original_session_id"


class VibeSkillSessionState(str, Enum):
    """VibeSkill Session 状态。"""

    IDLE = "idle"
    BUSY = "busy"
    COMPLETED = "completed"
    RETRY = "retry"


@dataclass
class VibeSkillSession:
    """VibeSkill Session 状态和元数据。"""

    session_id: str = ""
    state: VibeSkillSessionState = VibeSkillSessionState.IDLE
    exportable: bool = False
    last_export_obs_url: str = ""
    file_ready_obs_urls: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    mode: str = "SkillCreate"  # "SkillCreate" or "Standard"


class VibeSkillSessionStore:
    """VibeSkill Session 状态管理器。

    由 VibeSkillChannel 持有实例，不使用单例模式。

    支持可选的 DCS 持久化后端（``VibeSkillSessionDcsStore``）：
      - 读路径：先查内存表，miss 则查 DCS，命中后回填本地内存。
      - 写路径：先写 DCS，成功后再改本地内存；DCS 失败抛错，本地内存不污染。
      - 未注入 DCS（``dcs_store=None``）时退化为纯内存模式。

    Session 状态转换：
        idle ──(message.send)──► busy
        busy ──(skilldev.agent_completed)──► idle
        busy ──(skilldev.completed)──► completed
        completed ──(message.send)──► busy
        busy ──(chat.final/cancel)──► idle
        busy ──(skilldev.error)──► idle
        retry ──(message.send)──► busy
    """

    def __init__(self, dcs_store: "VibeSkillSessionDcsStore | None" = None) -> None:
        self._sessions: dict[str, VibeSkillSession] = {}
        self._lock = asyncio.Lock()
        self._dcs = dcs_store

    def _index_locally(self, session: VibeSkillSession) -> None:
        self._sessions[session.session_id] = session

    async def _load_from_dcs(self, session_id: str) -> VibeSkillSession | None:
        if self._dcs is None:
            return None
        logger.info(
            "[VibeSkillSessionStore] Querying session metadata from DCS: session_id=%s",
            session_id,
        )
        session = await self._dcs.load_session(session_id)
        if session is None:
            logger.info(
                "[VibeSkillSessionStore] Session metadata not found in DCS: session_id=%s",
                session_id,
            )
            return None
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            self._index_locally(session)
        state_value = (
            session.state.value
            if isinstance(session.state, VibeSkillSessionState)
            else session.state
        )
        logger.info(
            "[VibeSkillSessionStore] Loaded session metadata from DCS: session_id=%s "
            "mode=%s state=%s exportable=%s user_id=%s",
            session_id,
            session.mode,
            state_value,
            session.exportable,
            str((session.metadata or {}).get("user_id") or "").strip() or "n/a",
        )
        return session

    async def get_or_create(
        self,
        session_id: str | None = None,
        mode: str = "SkillCreate",
    ) -> VibeSkillSession:
        """获取或创建 session。

        优先按 session_id 查找（内存 → DCS），都找不到则新建。
        """
        sid = str(session_id or "").strip() or None

        if sid and sid in self._sessions:
            return self._sessions[sid]

        if sid and self._dcs is not None:
            loaded = await self._load_from_dcs(sid)
            if loaded is not None:
                return loaded

        async with self._lock:
            if sid and sid in self._sessions:
                return self._sessions[sid]

            new_sid = sid or f"vibeskill_{secrets.token_hex(6)}"
            session = VibeSkillSession(session_id=new_sid, mode=mode)
            if self._dcs is not None:
                await self._dcs.save_session(session)
            self._index_locally(session)
            return session

    async def set_state(self, session_id: str, state: VibeSkillSessionState) -> None:
        """更新 session 状态。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
            if session is None:
                return
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return
            current.state = state
            current.updated_at = time.time()
            if self._dcs is not None:
                try:
                    await self._dcs.save_session(current)
                except Exception:
                    logger.exception(
                        "[VibeSkillSessionStore] DCS save_session failed in set_state, session_id=%s",
                        session_id,
                    )
                    raise

    async def set_last_export_obs_url(self, session_id: str, url: str) -> str:
        """更新最近一次 skill 打包的 OBS URL，返回替换前的 URL（可能为空）。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
            if session is None:
                return ""
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return ""
            old_url = str(current.last_export_obs_url or "").strip()
            current.last_export_obs_url = str(url or "").strip()
            current.updated_at = time.time()
            if self._dcs is not None:
                try:
                    await self._dcs.save_session(current)
                except Exception:
                    logger.exception(
                        "[VibeSkillSessionStore] DCS save_session failed in "
                        "set_last_export_obs_url, session_id=%s",
                        session_id,
                    )
                    raise
            return old_url

    async def append_file_ready_obs_url(self, session_id: str, url: str) -> None:
        """追加 file.ready 事件返回的文件 OBS URL。"""
        obs_url = str(url or "").strip()
        if not obs_url:
            return
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
            if session is None:
                return
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return
            current.file_ready_obs_urls.append(obs_url)
            current.updated_at = time.time()
            if self._dcs is not None:
                try:
                    await self._dcs.save_session(current)
                except Exception:
                    logger.exception(
                        "[VibeSkillSessionStore] DCS save_session failed in "
                        "append_file_ready_obs_url, session_id=%s",
                        session_id,
                    )
                    raise

    async def set_exportable(self, session_id: str, exportable: bool) -> None:
        """更新 session 是否可导出。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
            if session is None:
                return
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return
            current.exportable = exportable
            current.updated_at = time.time()
            if self._dcs is not None:
                try:
                    await self._dcs.save_session(current)
                except Exception:
                    logger.exception(
                        "[VibeSkillSessionStore] DCS save_session failed in set_exportable, session_id=%s",
                        session_id,
                    )
                    raise

    async def set_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        """更新 session 元数据。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
            if session is None:
                return
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return
            current.metadata.update(metadata)
            current.updated_at = time.time()
            if self._dcs is not None:
                try:
                    await self._dcs.save_session(current)
                except Exception:
                    logger.exception(
                        "[VibeSkillSessionStore] DCS save_session failed in set_metadata, session_id=%s",
                        session_id,
                    )
                    raise

    async def get_state(self, session_id: str) -> VibeSkillSessionState:
        """获取 session 状态。"""
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._load_from_dcs(session_id)
        return session.state if session else VibeSkillSessionState.IDLE

    async def resolve_session(self, session_id: str) -> VibeSkillSession | None:
        """通过 session ID 解析已有 session；不存在则返回 None。"""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        existing = self._sessions.get(sid)
        if existing is not None:
            return existing
        return await self._load_from_dcs(sid)

    async def get_session(self, session_id: str) -> VibeSkillSession | None:
        """获取 session 对象。"""
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        return await self._load_from_dcs(session_id)

    def get_user_id(self, session_id: str) -> str | None:
        """Return the routing user_id for a VibeSkill session.

        仅查本地内存——跨 gateway 场景下，调用方应先经异步方法（``get_session`` /
        ``resolve_session``）触发一次 DCS read-through 回填，再同步调用本方法。
        """
        session = self._sessions.get(session_id)
        if not session:
            return None
        user_id = str((session.metadata or {}).get("user_id") or "").strip()
        return user_id or None

    async def list_sessions(self) -> list[VibeSkillSession]:
        """列出**本机内存**中的 session。

        DCS 持久化模式下不会扫描远端 keys，故跨 gateway 时不具备"全量"含义。
        """
        return list(self._sessions.values())

    async def delete_session(self, session_id: str) -> bool:
        """删除 session。"""
        if self._dcs is not None:
            try:
                await self._dcs.delete_session(session_id)
            except Exception:
                logger.exception(
                    "[VibeSkillSessionStore] DCS delete_session failed, session_id=%s",
                    session_id,
                )
                raise

        async with self._lock:
            if session_id not in self._sessions:
                return False
            self._sessions.pop(session_id)
            return True
