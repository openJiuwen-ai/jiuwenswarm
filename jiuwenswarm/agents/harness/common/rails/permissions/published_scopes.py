# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入侧发布会话/Skill 配置；读侧只选择已经发布的 key。"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import weakref
from collections import OrderedDict
from uuid import uuid4

from agent_shield.tool_permission import get_permission_manager
from openjiuwen.harness.security.skill_authorization import (
    is_skill_authorization_enabled,
)

_publishers = weakref.WeakValueDictionary()
_publishers_lock = threading.RLock()
# Only unleased compiled policies are cacheable/evictable. Active requests and
# detached children own leases; the SDK remains the final global key budget.
MAX_IDLE_PERMISSION_SESSIONS = 128
_idle_sessions = OrderedDict()


def _release_orphaned_keys(manager, keys):
    # No reference to the publisher/rail: GC must be able to collect their
    # host callback cycle even when normal Agent uninit was missed.
    for key in tuple(keys):
        manager.remove_config(key)
    keys.clear()


def _publisher_snapshot():
    with _publishers_lock:
        return list(_publishers.values())


def find_published_scopes(base_key, session_id):
    with _publishers_lock:
        publisher = _publishers.get(base_key)
    if publisher is None or not publisher.has_session(session_id):
        raise KeyError(
            "Permission session configuration is not published by this owner"
        )
    return publisher


def _evict_idle_session():
    while _idle_sessions:
        (_, session_id), reference = _idle_sessions.popitem(last=False)
        publisher = reference()
        if publisher is not None and publisher.is_idle_session(session_id):
            publisher.remove_session(session_id)
            return True
    return False


def publish_with_key_budget(publish):
    """Retry SDK capacity failures after evicting only an unleased session."""
    with _publishers_lock:
        while True:
            try:
                return publish()
            except RuntimeError as exc:
                # Other compiler/configuration errors must preserve the table.
                if (
                    not str(exc).startswith("permission config limit reached (")
                    or not _evict_idle_session()
                ):
                    raise


def _cache_idle_session(publisher, session_id):
    if publisher.is_idle_session(session_id):
        identity = publisher.base_key, session_id
        _idle_sessions[identity] = weakref.ref(publisher)
        _idle_sessions.move_to_end(identity)
        while len(_idle_sessions) > MAX_IDLE_PERMISSION_SESSIONS:
            if not _evict_idle_session():
                break


def refresh_session_overlays(session_id, *, remove=False):
    """配置写入入口更新已有会话；删除会话时清理配置。"""
    for publisher in _publisher_snapshot():
        for sid in publisher.session_ids():
            if session_id is not None and sid != session_id:
                continue
            publisher.refresh_session_overlay(sid, remove=remove)


def _grant_version(grant):
    if grant is None:
        return None
    value = [
        grant.skill_name,
        grant.permissions_hash,
        grant.skill_md_hash,
        grant.overlay_snapshot,
    ]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _projection_metadata(config):
    metadata = {
        name: copy.deepcopy(config.get(name, default))
        for name, default in (("tools", {}), ("rules", []), ("owner_scopes", {}))
    }
    metadata["net_guard"] = {
        "enabled": bool(
            config.get("enabled") and config.get("net_guard", {}).get("enabled")
        )
    }
    metadata["skill_enabled"] = is_skill_authorization_enabled(config)
    return metadata


class PublishedPermissionScopes:
    """复用 Manager.update_config，生命周期写入处调用；不创建第二个 Manager。"""

    def __init__(self, base_key, *, base_config=None, store=None):
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_grant_store,
        )

        self.base_key = base_key
        self.base_config = copy.deepcopy(base_config or {})
        self._store = store or get_skill_grant_store()
        self._lock = threading.RLock()
        self._sessions = {}
        self._session_keys = {}
        self._generations = {}
        self._metadata = {}
        self._skills = {}
        self._skill_metadata = {}
        self._leases = {}
        self._closed = False
        with _publishers_lock:
            if self.base_key in _publishers:
                raise ValueError("Permission publisher owner key is already registered")
            manager = get_permission_manager()
            self._owned_keys = (
                {base_key}
                if base_key != "default" and manager.has_config(base_key)
                else set()
            )
            self._finalizer = weakref.finalize(
                self, _release_orphaned_keys, manager, self._owned_keys
            )
            self._store.add_permission_listener(self)
            _publishers[self.base_key] = self

    def has_session(self, session_id):
        with self._lock:
            return session_id in self._sessions

    def ensure_open(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Permission scopes have been released")

    def is_idle_session(self, session_id):
        with self._lock:
            return (
                session_id in self._sessions
                and not self._leases.get(session_id)
                and not self._closed
            )

    def session_ids(self):
        with self._lock:
            return list(self._sessions)

    def _key(self, session_id, scope="", version=""):
        # key 对 SDK 是不透明字符串；业务沿用已存在的 Agent/会话/Skill 身份。
        suffix = json.dumps(
            [session_id, self._generations[session_id], scope, version],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return f"{self.base_key}:session:{suffix}"

    @staticmethod
    def publish_session(session_id, config):
        """会话级 Manager key 已停用；保留空实现兼容旧调用方。"""
        _ = session_id, config
        return

    @staticmethod
    def _lease(session_id):
        """会话租约已停用；返回空释放回调。"""
        _ = session_id
        return lambda: None

    @staticmethod
    def acquire_session(session_id, config):
        """会话级 Manager key 已停用；不发布、不租约。"""
        _ = session_id, config
        return lambda: None

    @staticmethod
    def retain_session(session_id):
        """会话租约已停用；子 Agent 与主 Agent 共用用户级 key。"""
        _ = session_id
        return lambda: None

    def refresh_session_overlay(self, session_id, *, remove=False):
        """会话不再发布到 Manager；remove 时仅清本地会话缓存（若有）。"""
        with _publishers_lock, self._lock:
            if remove or self._closed:
                self.remove_session(session_id)

    @staticmethod
    def _refresh(session_id):
        """Skill 临时 config 改由 SkillAuthorizationPermissionRail._check_tool_permission
        判定时合成；此处不再登记 Skill Manager key。
        """
        _ = session_id
        return

    def _active(self, session_id, scope):
        read = getattr(self._store, "get_active_for_permission", self._store.get_active)
        return read(session_id, scope)

    def get_active(self, session_id, scope):
        return self._active(session_id, scope)

    def has_active_grant(self, session_id, scope):
        return self._active(session_id, scope) is not None

    def skill_permissions_changed(self, session_id, agent_scope_id, operation):
        """Grant Store 在写入完成后通知；撤销/删除立即移除派生配置。"""
        del agent_scope_id
        with _publishers_lock, self._lock:
            if self._closed:
                # No new grants after owner shutdown, but revocation must still
                # invalidate keys retained by an already running child.
                for route, (_, key) in list(self._skills.items()):
                    if session_id is None or route[0] == session_id:
                        get_permission_manager().remove_config(key)
                        self._owned_keys.discard(key)
                        self._skills.pop(route, None)
                        self._skill_metadata.pop(key, None)
                return
            if operation == "clear_session":
                self.remove_session(session_id)
                return
            sessions = list(self._sessions) if session_id is None else [session_id]
            for sid in sessions:
                if sid in self._sessions:
                    _idle_sessions.pop((self.base_key, sid), None)
                    try:
                        self._refresh(sid)
                    finally:
                        _cache_idle_session(self, sid)

    def key_for(self, session_id, scope="main"):
        """只读路由；未知会话/跨进程尚未发布的新授权抛 KeyError，不补配置。"""
        with self._lock:
            key = self._session_keys[session_id]
            if not scope or not self._metadata[session_id]["skill_enabled"]:
                return key
            active = self._active(session_id, scope)
            if active is None or not active.overlay_snapshot:
                return key
            version, skill_key = self._skills[session_id, scope]
            if version != _grant_version(active):
                raise KeyError("Active Skill configuration has not been published")
            return skill_key

    def baseline_key_for(self, session_id):
        with self._lock:
            return self._session_keys[session_id]

    def projection_config(self, session_id, key=None):
        """写入侧保留的工具投影元数据；判定不读取 Manager 配置。"""
        with self._lock:
            if key is not None and key != self._session_keys[session_id]:
                return copy.deepcopy(self._skill_metadata[key])
            return copy.deepcopy(self._metadata[session_id])

    def remove_session(self, session_id):
        with _publishers_lock, self._lock:
            _idle_sessions.pop((self.base_key, session_id), None)
            manager = get_permission_manager()
            for route, (_, key) in list(self._skills.items()):
                if route[0] == session_id:
                    manager.remove_config(key)
                    self._owned_keys.discard(key)
                    self._skill_metadata.pop(key, None)
                    del self._skills[route]
            key = self._session_keys.pop(session_id, None)
            if key is not None:
                manager.remove_config(key)
                self._owned_keys.discard(key)
            self._sessions.pop(session_id, None)
            self._metadata.pop(session_id, None)
            self._generations.pop(session_id, None)
            self._leases.pop(session_id, None)
            self._unregister_closed_owner()

    def _unregister_closed_owner(self):
        if (
            self._closed
            and not self._sessions
            and _publishers.get(self.base_key) is self
        ):
            _publishers.pop(self.base_key, None)
            self._finalizer.detach()
            self._owned_keys.clear()

    def close(self):
        with _publishers_lock, self._lock:
            self._closed = True
            for session_id in list(self._sessions):
                if not self._leases.get(session_id):
                    self.remove_session(session_id)
            self._unregister_closed_owner()
