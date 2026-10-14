# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared Skill state; reuse the SDK's grant transitions under Redis CAS.

The SDK store API is synchronous. Network calls are therefore deliberately
bounded (no retries on transport failure); no process-local grant cache is used.
All keys share a deployment-specific Redis Cluster hash tag.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from functools import wraps
from typing import Any

from openjiuwen.harness.security.skill_authorization import (
    GrantDecision,
    GrantStatus,
    SkillGrant,
    SkillGrantStore,
)

logger = logging.getLogger(__name__)
_SCHEMA = 1
_READ = """
local retired = 0
for i=1,#ARGV do
  if redis.call('GETBIT', KEYS[3], ARGV[i]) == 0 then retired = -1; break end
end
return {redis.call('GET', KEYS[1]) or '0', redis.call('GET', KEYS[2]) or '', retired}
"""
_CAS = """
if (redis.call('GET', KEYS[1]) or '0') ~= ARGV[1] then return -1 end
local retired = true
for i=5,#ARGV do
  if redis.call('GETBIT', KEYS[3], ARGV[i]) == 0 then retired = false; break end
end
if retired then return -2 end
if (redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
return 1
"""
_RETIRE = """
for i=1,#ARGV do redis.call('SETBIT', KEYS[2], ARGV[i], 1) end
redis.call('DEL', KEYS[1])
return 1
"""


def _shared(method: Callable) -> Callable:
    """Keep the SDK signatures/semantics without duplicating its state machine."""

    @wraps(method)
    def call(self, session_id, *args, **kwargs):
        return self.operate(
            session_id, lambda store, doc: method(store, session_id, *args, **kwargs)
        )

    return call


class _TransactionStore(SkillGrantStore):
    """Keep SDK protected state access inside its derived storage adapter."""

    @property
    def authorization_generation(self) -> int:
        return self.epoch

    def has_runtime_state(self) -> bool:
        """Inspect a native or derived SDK store before startup installation."""
        with self._lock:
            return bool(self._grants or self._skill_executions)

    def restore_session_state(self, sid: str, doc: dict) -> None:
        """Load only data into the isolated transaction store."""
        grants = {}
        for scope, records in doc.get("grants", {}).items():
            decoded = {}
            for fields in records:
                grant = SkillGrant(
                    **{
                        **fields,
                        "decision": GrantDecision(fields["decision"]),
                        "status": GrantStatus(fields["status"]),
                    }
                )
                decoded[grant.skill_name] = grant
            grants[(sid, scope)] = decoded
        self._grants = grants
        self._skill_executions = {
            (sid, scope): name for scope, name in doc.get("executions", {}).items()
        }
        self._invalidated_scopes = {
            (sid, scope) for scope in doc.get("invalidated", [])
        }
        # Retirement is checked against Redis before loading a transaction.
        self._retired_sessions = set()

    def export_session_state(self, sid: str) -> dict:
        """Return a data snapshot without exposing SDK dictionaries to clients."""
        return {
            "grants": {
                scope: [asdict(grant) for grant in grants.values()]
                for (stored_sid, scope), grants in self._grants.items()
                if stored_sid == sid
            },
            "executions": {
                scope: name
                for (stored_sid, scope), name in self._skill_executions.items()
                if stored_sid == sid
            },
            "invalidated": sorted(
                scope
                for stored_sid, scope in self._invalidated_scopes
                if stored_sid == sid
            ),
        }


class RedisSkillGrantStore(SkillGrantStore):
    """Redis is authoritative for grants, execution windows and active skills."""

    def __init__(self, client: Any, *, namespace: str, ttl: int = 604800) -> None:
        super().__init__()
        if not namespace.strip() or ttl <= 0:
            raise ValueError("Skill state namespace and positive TTL are required")
        tag = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self._prefix = f"{namespace}:skill-authorization:{{{tag}}}:"
        self._client = client
        self._ttl = ttl
        self._memory = _TransactionStore()
        self._operation_lock = threading.RLock()
        self._generation_key = self._prefix + "generation"
        self._retired_key = self._prefix + "retired"

    @property
    def authorization_generation(self) -> int:
        # Transport errors propagate: a stale approval must never be accepted.
        return int(self._client.get(self._generation_key) or 0)

    def _session_key(self, session_id: str) -> str:
        return (
            self._prefix + "session:" + hashlib.sha256(session_id.encode()).hexdigest()
        )

    @staticmethod
    def _retired_bits(session_id: str) -> list[int]:
        digest = hashlib.sha256(session_id.encode()).digest()
        return [
            int.from_bytes(digest[start:stop], "big") % (1024 * 1024 * 8)
            for start, stop in zip(range(0, 28, 4), range(4, 32, 4))
        ]

    def _restore(self, sid: str, doc: dict) -> None:
        """Deserialize only data, never Python objects or executable payloads."""
        self._memory.restore_session_state(sid, doc)

    def _snapshot(self, sid: str, doc: dict) -> None:
        state = self._memory.export_session_state(sid)
        doc.update(state)
        if "main" in state["invalidated"]:
            doc.pop("active_skill", None)

    def operate(self, session_id: str, action: Callable) -> Any:
        """Apply an SDK operation to one session under bounded Redis CAS."""
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        key = self._session_key(sid)
        with self._operation_lock:
            initial_generation = None
            deadline = time.monotonic() + 1.0
            for _ in range(32):
                if time.monotonic() >= deadline:
                    break
                generation, raw, retired = self._client.eval(
                    _READ,
                    3,
                    self._generation_key,
                    key,
                    self._retired_key,
                    *self._retired_bits(sid),
                )
                generation = int(generation)
                if initial_generation is not None and generation != initial_generation:
                    raise RuntimeError(
                        "Skill authorization changed during the operation"
                    )
                initial_generation = generation
                doc = json.loads(raw) if raw else {}
                if doc and doc.get("schema") != _SCHEMA:
                    raise ValueError("Unsupported Skill authorization state schema")
                if doc.get("generation") != generation:
                    doc = {"schema": _SCHEMA, "generation": generation}
                is_retired = retired == 0
                if is_retired:
                    # Reject late callbacks as well as new approval candidates.
                    raise RuntimeError("Skill authorization session has been deleted")
                self._restore(sid, doc)
                self._memory.epoch = generation
                result = action(self._memory, doc)
                self._snapshot(sid, doc)
                new_raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
                outcome = self._client.eval(
                    _CAS,
                    3,
                    self._generation_key,
                    key,
                    self._retired_key,
                    generation,
                    raw,
                    new_raw,
                    self._ttl,
                    *self._retired_bits(sid),
                )
                if outcome == 1:
                    return result
                if outcome == -2:
                    raise RuntimeError("Skill authorization session has been deleted")
                if outcome == -1:
                    raise RuntimeError(
                        "Skill authorization changed during the operation"
                    )
            raise RuntimeError(
                "Concurrent Skill authorization update; retry the request"
            )

    create_pending_grant = _shared(SkillGrantStore.create_pending_grant)
    activate_pending = _shared(SkillGrantStore.activate_pending)
    get_grant = _shared(SkillGrantStore.get_grant)
    list_grants = _shared(SkillGrantStore.list_grants)
    drop_pending = _shared(SkillGrantStore.drop_pending)
    enter_skill_execution = _shared(SkillGrantStore.enter_skill_execution)
    exit_skill_execution = _shared(SkillGrantStore.exit_skill_execution)

    def get_active(self, session_id: str, agent_scope_id: str):
        try:
            return self.operate(
                session_id,
                lambda store, doc: store.get_active(session_id, agent_scope_id),
            )
        except Exception:
            logger.warning(
                "[skill_authorization] shared grant unavailable; no overlay",
                exc_info=True,
            )
            return None

    def get_active_skill_execution(self, session_id: str, agent_scope_id: str):
        try:
            return self.operate(
                session_id,
                lambda store, doc: store.get_active_skill_execution(
                    session_id, agent_scope_id
                ),
            )
        except Exception:
            logger.warning(
                "[skill_authorization] shared execution unavailable; no window",
                exc_info=True,
            )
            return None

    def invalidate_scope(
        self, session_id: str, agent_scope_id: str, *, reason: str
    ) -> None:
        def action(store, doc):
            store.invalidate_scope(session_id, agent_scope_id, reason=reason)
            if (agent_scope_id or "").strip() == "main":
                doc.pop("active_skill", None)

        self.operate(session_id, action)

    def revoke_grants(
        self,
        session_id: str,
        agent_scope_id: str,
        *,
        skill_name=None,
        reason="skill_complete",
    ) -> None:
        def action(store, doc):
            store.revoke_grants(
                session_id, agent_scope_id, skill_name=skill_name, reason=reason
            )
            if (agent_scope_id or "").strip() == "main":
                doc.pop("active_skill", None)

        self.operate(session_id, action)

    def clear_scope(self, session_id: str, agent_scope_id: str) -> None:
        def action(store, doc):
            store.clear_scope(session_id, agent_scope_id)
            if (agent_scope_id or "").strip() == "main":
                doc.pop("active_skill", None)

        self.operate(session_id, action)

    def clear_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        self._client.eval(
            _RETIRE,
            2,
            self._session_key(sid),
            self._retired_key,
            *self._retired_bits(sid),
        )

    def clear_all(self) -> None:
        # No SCAN/flush: one durable epoch invalidates all existing documents.
        self._client.incr(self._generation_key)

    def get_active_skill_state(self, session_id: str) -> dict | None:
        try:
            return self.operate(session_id, lambda store, doc: doc.get("active_skill"))
        except Exception:
            logger.warning(
                "[skill_authorization] shared active skill unavailable; no credentials",
                exc_info=True,
            )
            return None

    def update_active_skill_state(
        self, session_id: str, update: Callable[[dict], dict | None]
    ) -> dict | None:
        def action(store, doc):
            updated = update(dict(doc.get("active_skill") or {}))
            if updated is None:
                doc.pop("active_skill", None)
            else:
                doc["active_skill"] = updated
            return updated

        return self.operate(session_id, action)


def configure_shared_skill_authorization(config: dict) -> RedisSkillGrantStore | None:
    """Install once, before AgentServer accepts requests; opt-in only."""
    from openjiuwen.harness.security.skill_authorization import (
        get_skill_grant_store,
        set_skill_grant_store,
    )

    from jiuwenswarm.extensions.redis.redis_client import RedisConfig

    settings = config.get("skill_authorization_store") or {}
    backend = str(settings.get("backend") or "memory").strip().lower()
    if backend == "memory":
        return None
    if backend != "redis":
        raise ValueError("skill_authorization_store.backend must be memory or redis")
    current = get_skill_grant_store()
    if isinstance(current, RedisSkillGrantStore):
        return current
    if not hasattr(SkillGrantStore, "authorization_generation"):
        raise RuntimeError(
            "Redis Skill state requires an SDK supporting shared authorization_generation"
        )
    if _TransactionStore.has_runtime_state(current):
        raise RuntimeError("Install Redis Skill state before accepting requests")
    import redis

    cfg = RedisConfig.from_mapping(config.get("redis"))
    timeout = float(settings.get("operation_timeout", 0.3))
    ttl = int(settings.get("ttl", 604800))
    namespace = str(settings.get("namespace") or cfg.key_prefix).strip()
    if not (0 < timeout <= 5):
        raise ValueError("Skill Redis operation_timeout must be in (0, 5] seconds")
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    kwargs = {
        "retry": Retry(NoBackoff(), 0),
        "username": cfg.username,
        "password": cfg.password,
        "decode_responses": True,
        "socket_timeout": timeout,
        "socket_connect_timeout": timeout,
        "retry_on_timeout": False,
        "max_connections": cfg.pool_size,
    }
    client = None
    try:
        if cfg.mode == "cluster":
            from redis.cluster import ClusterNode, RedisCluster

            nodes = [
                ClusterNode(node["host"], node["port"]) for node in cfg.startup_nodes
            ] or [ClusterNode(cfg.host, cfg.port)]
            client = RedisCluster(startup_nodes=nodes, **kwargs)
        else:
            client = redis.Redis(host=cfg.host, port=cfg.port, db=cfg.db, **kwargs)
        client.ping()
    except redis.exceptions.RedisError as exc:
        if client is not None:
            client.close()
        # No requests have been accepted and no Redis state was installed.
        # Keep the original memory store for this process; never copy old grants
        # or switch backends after requests have started.
        logger.warning(
            "[skill_authorization] Redis unavailable at startup (%s); "
            "using memory for this process; shared authorization disabled",
            type(exc).__name__,
        )
        return None
    store = RedisSkillGrantStore(client, namespace=namespace, ttl=ttl)
    set_skill_grant_store(store)
    logger.info(
        "[skill_authorization] shared Redis state enabled ttl=%s timeout=%s",
        ttl,
        timeout,
    )
    return store
