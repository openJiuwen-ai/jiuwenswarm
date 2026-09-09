"""Discovery cache and registered-Agent management for A2A outbound."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from .credentials import A2AOutboundCredentialStore
from .discovery import A2AOutboundDiscoveryService, DiscoveredCard
from .errors import A2AOutboundError, A2AOutboundErrorCode
from .locks import KeyedLockPool
from .models import A2AOutboundAgent, A2AOutboundAvailability, A2AOutboundDiscovery
from .repository import A2AOutboundRepository, utc_now_text

DISCOVERY_TTL_SECONDS = 600
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_SYNC_WAIT_SECONDS = 300.0

logger = logging.getLogger(__name__)


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _pending_payload(card: DiscoveredCard) -> dict[str, Any]:
    return {
        "source_url": card.source_url,
        "card_path": card.card_path,
        "card_fingerprint": card.card_fingerprint,
        "agent_card": card.agent_card,
        "selected_interface": card.agent.compatible_interfaces[0].__dict__,
        "security_requirements": list(card.security_requirements),
    }


@dataclass(frozen=True)
class _CriticalIdentity:
    """The subset of an Agent Card that must not silently change underfoot."""

    url: str
    protocol_binding: str
    security_requirements: tuple[Any, ...]
    security_schemes: tuple[str, ...]
    provider_url: str
    signature_protected: tuple[str, ...]


def _critical_identity(agent: A2AOutboundAgent) -> _CriticalIdentity:
    card = agent.agent_card
    return _CriticalIdentity(
        url=agent.selected_interface.url,
        protocol_binding=agent.selected_interface.protocol_binding,
        security_requirements=tuple(card.get("securityRequirements") or ()),
        security_schemes=tuple(sorted((card.get("securitySchemes") or {}).keys())),
        provider_url=str((card.get("provider") or {}).get("url") or ""),
        signature_protected=tuple(
            str((signature or {}).get("protected") or "")
            for signature in card.get("signatures") or ()
        ),
    )


def _critical_discovered_identity(card: DiscoveredCard) -> _CriticalIdentity:
    selected = card.agent.compatible_interfaces[0]
    payload = card.agent_card
    return _CriticalIdentity(
        url=selected.url,
        protocol_binding=selected.protocol_binding,
        security_requirements=tuple(payload.get("securityRequirements") or ()),
        security_schemes=tuple(sorted((payload.get("securitySchemes") or {}).keys())),
        provider_url=str((payload.get("provider") or {}).get("url") or ""),
        signature_protected=tuple(
            str((signature or {}).get("protected") or "")
            for signature in payload.get("signatures") or ()
        ),
    )


class A2AOutboundRegistry:
    def __init__(
        self,
        repository: A2AOutboundRepository,
        *,
        discovery_service: A2AOutboundDiscoveryService | None = None,
        credential_store: A2AOutboundCredentialStore | None = None,
        discovery_ttl_seconds: int = DISCOVERY_TTL_SECONDS,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._discovery = discovery_service or A2AOutboundDiscoveryService()
        self._credentials = credential_store or A2AOutboundCredentialStore()
        self._ttl = max(1, int(discovery_ttl_seconds))
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._discoveries: dict[str, A2AOutboundDiscovery] = {}
        self._cache_lock = asyncio.Lock()
        self._registration_lock = asyncio.Lock()
        self._agent_operation_locks = KeyedLockPool()

    def _require_mutable_catalog(self) -> None:
        if getattr(self._repository, "manager_owned", False):
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)

    def set_allow_loopback_http(self, enabled: bool) -> None:
        self._discovery.set_allow_loopback_http(enabled)

    async def discover(self, url: str, card_path: str | None = None) -> dict[str, Any]:
        self._require_mutable_catalog()
        card = await self._discovery.discover(url, card_path)
        now = self._now()
        item = A2AOutboundDiscovery(
            discovery_id=f"disc_{uuid4().hex}",
            expires_at=(now + timedelta(seconds=self._ttl))
            .isoformat()
            .replace("+00:00", "Z"),
            source_url=card.source_url,
            card_path=card.card_path,
            card_fingerprint=card.card_fingerprint,
            agent=card.agent,
            agent_card=card.agent_card,
            security_requirements=card.security_requirements,
            warnings=card.warnings,
        )
        async with self._cache_lock:
            self._purge_expired_locked(now)
            self._discoveries[item.discovery_id] = item
        return item.to_dict()

    async def register(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_mutable_catalog()
        discovery_id = str(params.get("discovery_id") or "").strip()
        if not discovery_id:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_NOT_FOUND)
        async with self._repository.hold_discovery(discovery_id):
            discovery = await self._get_discovery(discovery_id)
            async with self._registration_lock:
                for existing in await self._repository.list_agents():
                    if (
                        existing.source_url == discovery.source_url
                        and existing.card_path == discovery.card_path
                    ) or existing.selected_interface.url in {
                        item.url for item in discovery.agent.compatible_interfaces
                    }:
                        raise A2AOutboundError(
                            A2AOutboundErrorCode.AGENT_ALREADY_REGISTERED
                        )
                if not discovery.agent.compatible_interfaces:
                    raise A2AOutboundError(A2AOutboundErrorCode.CARD_INVALID)
                agent_id = f"agent_{uuid4().hex}"
                timeouts = (
                    params.get("timeouts")
                    if isinstance(params.get("timeouts"), Mapping)
                    else {}
                )
                credential = str(params.get("credential") or "")
                credential_ref = (
                    self._credentials.reference_for(agent_id) if credential else None
                )
                stamp = utc_now_text()
                agent = A2AOutboundAgent(
                    agent_id=agent_id,
                    display_name=str(
                        params.get("display_name") or discovery.agent.name
                    ).strip(),
                    source_url=discovery.source_url,
                    card_path=discovery.card_path,
                    card_fingerprint=discovery.card_fingerprint,
                    card_revision=1,
                    agent_card=discovery.agent_card,
                    selected_interface=discovery.agent.compatible_interfaces[0],
                    enabled=params.get("enabled") is not False,
                    availability=A2AOutboundAvailability.AVAILABLE,
                    credential_ref=credential_ref,
                    connect_timeout_seconds=float(
                        timeouts.get("connect_seconds", DEFAULT_CONNECT_TIMEOUT_SECONDS)
                    ),
                    sync_wait_seconds=float(
                        timeouts.get("sync_wait_seconds", DEFAULT_SYNC_WAIT_SECONDS)
                    ),
                    last_checked_at=stamp,
                    last_success_at=stamp,
                    created_at=stamp,
                    updated_at=stamp,
                ).validate()
                try:
                    if credential:
                        self._credentials.set_for_agent(agent_id, credential)
                    created = await self._repository.create_agent(agent)
                except Exception:
                    if credential_ref:
                        self._credentials.delete(credential_ref)
                    raise
            async with self._cache_lock:
                self._discoveries.pop(discovery_id, None)
            logger.info(
                "a2a.outbound audit action=register agent_id=%s enabled=%s",
                created.agent_id,
                created.enabled,
            )
            return created.public_dict()

    async def list_agents(self) -> dict[str, Any]:
        list_projected = getattr(self._repository, "list_projected_agents", None)
        if callable(list_projected):
            projected = await list_projected()
            items = [item.public_dict() for item in projected]
            return {"items": items, "total": len(items)}
        items = [item.public_dict() for item in await self._repository.list_agents()]
        return {"items": items, "total": len(items)}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        get_projected = getattr(self._repository, "get_projected_agent", None)
        if callable(get_projected):
            projected = await get_projected(agent_id)
            if projected is None:
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
            return projected.public_dict()
        return (await self._require_agent(agent_id)).public_dict()

    async def set_user_enabled(
        self, agent_id: str, user_enabled: bool
    ) -> dict[str, Any]:
        setter = getattr(self._repository, "set_user_enabled", None)
        if not callable(setter):
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        projected = await setter(agent_id, user_enabled)
        return projected.public_dict()

    async def edit_agent(self, agent_id: str) -> dict[str, Any]:
        """Read the credential only for an explicit management editing request."""
        self._require_mutable_catalog()
        async with self._agent_operation_locks.hold(agent_id):
            agent = await self._require_agent(agent_id)
            result = agent.public_dict()
            result["credential"] = self._credentials.get(agent.credential_ref)
            return result

    async def update_agent(
        self, agent_id: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require_mutable_catalog()
        allowed = {
            "display_name",
            "enabled",
            "connect_timeout_seconds",
            "sync_wait_seconds",
            "credential",
            "clear_credential",
        }
        if set(params) - allowed:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        async with self._agent_operation_locks.hold(agent_id):
            current = await self._require_agent(agent_id)
            credential_present = "credential" in params
            credential = str(params.get("credential") or "")
            clear_credential = params.get("clear_credential") is True
            credential_ref = self._credentials.reference_for(agent_id)
            secret_changed = (
                credential_present and bool(credential)
            ) or clear_credential
            old_secret = (
                self._credentials.get(current.credential_ref) if secret_changed else ""
            )

            if credential_present and credential:
                self._credentials.set_for_agent(agent_id, credential)
            elif clear_credential:
                self._credentials.delete(credential_ref)

            def updater(item: A2AOutboundAgent) -> A2AOutboundAgent:
                changes: dict[str, Any] = {"updated_at": utc_now_text()}
                if "display_name" in params:
                    changes["display_name"] = str(params["display_name"] or "").strip()
                if "enabled" in params:
                    changes["enabled"] = params["enabled"] is True
                for field in ("connect_timeout_seconds", "sync_wait_seconds"):
                    if field in params:
                        changes[field] = float(params[field])
                if credential_present and credential:
                    changes["credential_ref"] = credential_ref
                elif clear_credential:
                    changes["credential_ref"] = None
                return replace(item, **changes)

            try:
                updated = await self._repository.update_agent(agent_id, updater)
                if updated is None:
                    raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
            except Exception:
                if secret_changed:
                    try:
                        if current.credential_ref and old_secret:
                            self._credentials.set_for_agent(agent_id, old_secret)
                        else:
                            self._credentials.delete(credential_ref)
                    except Exception:
                        logger.exception(
                            "a2a.outbound credential rollback failed agent_id=%s",
                            agent_id,
                        )
                raise
        logger.info(
            "a2a.outbound audit action=update agent_id=%s fields=%s",
            agent_id,
            ",".join(sorted(set(params) - {"credential"})),
        )
        return updated.public_dict()

    async def refresh_agent(self, agent_id: str) -> dict[str, Any]:
        self._require_mutable_catalog()
        async with self._agent_operation_locks.hold(agent_id):
            current = await self._require_agent(agent_id)
            try:
                card = await self._discovery.discover(
                    current.source_url, current.card_path
                )
            except A2AOutboundError as exc:
                error_code = exc.code.value
                error_summary = exc.summary
                failed_availability = (
                    A2AOutboundAvailability.INCOMPATIBLE
                    if exc.code is A2AOutboundErrorCode.CARD_INVALID
                    else A2AOutboundAvailability.UNREACHABLE
                )
                updated = await self._repository.update_agent(
                    agent_id,
                    lambda item: replace(
                        item,
                        availability=(
                            A2AOutboundAvailability.REVIEW_REQUIRED
                            if item.pending_revision
                            else failed_availability
                        ),
                        last_checked_at=utc_now_text(),
                        last_error_code=error_code,
                        last_error_summary=error_summary,
                        updated_at=utc_now_text(),
                    ),
                )
                if updated is None:
                    raise A2AOutboundError(
                        A2AOutboundErrorCode.AGENT_NOT_REGISTERED
                    ) from exc
                return updated.public_dict()

            def updater(item: A2AOutboundAgent) -> A2AOutboundAgent:
                stamp = utc_now_text()
                common = {
                    "last_checked_at": stamp,
                    "last_success_at": stamp,
                    "last_error_code": None,
                    "last_error_summary": None,
                    "updated_at": stamp,
                }
                if (
                    item.availability is A2AOutboundAvailability.REVIEW_REQUIRED
                    and item.pending_revision
                ):
                    return replace(
                        item,
                        pending_revision=_pending_payload(card),
                        **common,
                    )
                critical = _critical_identity(item) != _critical_discovered_identity(
                    card
                )
                if critical:
                    return replace(
                        item,
                        availability=A2AOutboundAvailability.REVIEW_REQUIRED,
                        pending_revision=_pending_payload(card),
                        **common,
                    )
                return replace(
                    item,
                    card_fingerprint=card.card_fingerprint,
                    card_revision=item.card_revision
                    + (card.card_fingerprint != item.card_fingerprint),
                    agent_card=card.agent_card,
                    selected_interface=card.agent.compatible_interfaces[0],
                    availability=A2AOutboundAvailability.AVAILABLE,
                    pending_revision=None,
                    **common,
                )

            updated = await self._repository.update_agent(agent_id, updater)
            if updated is None:
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        logger.info(
            "a2a.outbound audit action=refresh agent_id=%s review_required=%s",
            agent_id,
            updated.availability is A2AOutboundAvailability.REVIEW_REQUIRED,
        )
        return updated.public_dict()

    async def confirm_revision(self, agent_id: str, *, accept: bool) -> dict[str, Any]:
        self._require_mutable_catalog()
        if not isinstance(accept, bool):
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)

        def updater(item: A2AOutboundAgent) -> A2AOutboundAgent:
            pending = item.pending_revision
            if (
                item.availability is not A2AOutboundAvailability.REVIEW_REQUIRED
                or not pending
            ):
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_REVIEW_REQUIRED)
            stamp = utc_now_text()
            if not accept:
                return replace(
                    item,
                    availability=A2AOutboundAvailability.AVAILABLE,
                    pending_revision=None,
                    updated_at=stamp,
                )
            return replace(
                item,
                source_url=str(pending["source_url"]),
                card_path=str(pending["card_path"]),
                card_fingerprint=str(pending["card_fingerprint"]),
                card_revision=item.card_revision + 1,
                agent_card=dict(pending["agent_card"]),
                selected_interface=item.selected_interface.from_dict(
                    pending["selected_interface"]
                ),
                availability=A2AOutboundAvailability.AVAILABLE,
                pending_revision=None,
                last_success_at=stamp,
                updated_at=stamp,
            )

        async with self._agent_operation_locks.hold(agent_id):
            updated = await self._repository.update_agent(agent_id, updater)
            if updated is None:
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        logger.info(
            "a2a.outbound audit action=confirm_revision agent_id=%s accepted=%s",
            agent_id,
            accept,
        )
        return updated.public_dict()

    async def delete_agent(self, agent_id: str) -> dict[str, Any]:
        self._require_mutable_catalog()
        async with self._agent_operation_locks.hold(agent_id):
            if not await self._repository.delete_agent(agent_id):
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        logger.info("a2a.outbound audit action=delete agent_id=%s", agent_id)
        return {"agent_id": agent_id, "deleted": True}

    async def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        item = await self._repository.get_dispatch(str(dispatch_id or "").strip())
        if item is None:
            raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_NOT_FOUND)
        return item.to_record()

    async def list_dispatches(self, *, limit: int = 200) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit), 200))
        records = await self._repository.list_dispatches(limit=normalized_limit)
        total = await self._repository.count_dispatches()
        items = []
        for item in records:
            items.append(
                {
                    "dispatch_id": item.dispatch_id,
                    "agent_id": item.agent_id,
                    "agent_name": item.agent_name,
                    "mode": item.mode.value,
                    "status": item.status.value,
                    "remote_task_id": item.remote_task_id,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "accepted_at": item.accepted_at,
                    "finished_at": item.finished_at,
                    "error_code": item.error_code,
                    "error_summary": item.error_summary,
                    "source_resource_id": item.source_resource_id,
                }
            )
        return {"items": items, "total": total}

    async def _require_agent(self, agent_id: str) -> A2AOutboundAgent:
        item = await self._repository.get_agent(str(agent_id or "").strip())
        if item is None:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        return item

    async def _get_discovery(self, discovery_id: str) -> A2AOutboundDiscovery:
        now = self._now()
        async with self._cache_lock:
            item = self._discoveries.get(discovery_id)
            if item is None:
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_NOT_FOUND)
            if _parse_time(item.expires_at) <= now:
                self._discoveries.pop(discovery_id, None)
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_EXPIRED)
            return item

    def _purge_expired_locked(self, now: datetime) -> None:
        expired = [
            key
            for key, item in self._discoveries.items()
            if _parse_time(item.expires_at) <= now
        ]
        for key in expired:
            self._discoveries.pop(key, None)


__all__ = ["A2AOutboundRegistry", "DISCOVERY_TTL_SECONDS"]
