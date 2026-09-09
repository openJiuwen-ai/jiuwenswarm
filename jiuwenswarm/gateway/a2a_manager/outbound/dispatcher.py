"""Registered-Agent-only A2A outbound dispatching for the personal edition."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from a2a.client import ClientCallContext, ClientConfig
from a2a.client.card_resolver import parse_agent_card
from a2a.client.client_factory import ClientFactory
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)

from .credentials import A2AOutboundCredentialStore
from .discovery import A2AOutboundDiscoveryService, create_pinned_transport
from .errors import A2AOutboundError, A2AOutboundErrorCode, safe_error_summary
from .locks import KeyedLockPool
from .models import (
    A2AOutboundAgent,
    A2AOutboundAvailability,
    A2AOutboundDispatch,
    A2AOutboundDispatchMode,
    A2AOutboundDispatchStatus,
)
from .repository import A2AOutboundRepository, utc_now_text

logger = logging.getLogger(__name__)

MAX_TASK_TEXT_LENGTH = 64 * 1024
MAX_RESULT_TEXT_LENGTH = 64 * 1024
MAX_RESULT_ARTIFACTS = 32
DEFAULT_GLOBAL_CONCURRENCY = 16
DEFAULT_AGENT_CONCURRENCY = 4
DEFAULT_QUERY_INTERVAL_SECONDS = 1.0
DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS = 3600.0


class _ClientLike(Protocol):
    async def send_message(self, request, *, context=None):
        raise NotImplementedError

    async def get_task(self, request, *, context=None):
        raise NotImplementedError

    async def cancel_task(self, request, *, context=None):
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def _is_selected_interface(item: Any, selected: Any) -> bool:
    """Match a parsed Agent Card interface against the agent's pinned selection."""
    same_url = item.url == selected.url
    same_binding = item.protocol_binding.upper() == selected.protocol_binding.upper()
    return same_url and same_binding


def _skill_search_tokens(skills: Any) -> set[str]:
    """Collect lowercase id/name/tag tokens for one agent's public skills."""
    tokens: set[str] = set()
    for skill in skills:
        tokens.add(str(skill.get("id") or "").lower())
        tokens.add(str(skill.get("name") or "").lower())
        for tag in skill.get("tags") or ():
            tokens.add(str(tag).lower())
    tokens.discard("")
    return tokens


ClientBuilder = Callable[[A2AOutboundAgent, str], Awaitable[_ClientLike]]


@dataclass(frozen=True)
class _NormalizedRemote:
    status: A2AOutboundDispatchStatus
    remote_task_id: str | None = None
    remote_context_id: str | None = None
    result: dict[str, Any] | None = None


class _CapacityLease:
    def __init__(self, dispatcher: "A2AOutboundDispatcher", agent_id: str) -> None:
        self._dispatcher = dispatcher
        self._agent_id = agent_id

    async def __aenter__(self) -> None:
        async with self._dispatcher._capacity_lock:
            agent_active = self._dispatcher._agent_active.get(self._agent_id, 0)
            if (
                self._dispatcher._global_active >= self._dispatcher._global_limit
                or agent_active >= self._dispatcher._agent_limit
            ):
                raise A2AOutboundError(A2AOutboundErrorCode.OUTBOUND_BUSY)
            self._dispatcher._global_active += 1
            self._dispatcher._agent_active[self._agent_id] = agent_active + 1

    async def __aexit__(self, *_args: object) -> None:
        async with self._dispatcher._capacity_lock:
            self._dispatcher._global_active = max(
                0, self._dispatcher._global_active - 1
            )
            remaining = self._dispatcher._agent_active.get(self._agent_id, 1) - 1
            if remaining > 0:
                self._dispatcher._agent_active[self._agent_id] = remaining
            else:
                self._dispatcher._agent_active.pop(self._agent_id, None)


class A2AOutboundDispatcher:
    """Dispatch through persisted registrations and normalize remote A2A state."""

    def __init__(
        self,
        repository: A2AOutboundRepository,
        *,
        credential_store: A2AOutboundCredentialStore | None = None,
        discovery_service: A2AOutboundDiscoveryService | None = None,
        client_builder: ClientBuilder | None = None,
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
        agent_concurrency: int = DEFAULT_AGENT_CONCURRENCY,
        query_interval_seconds: float = DEFAULT_QUERY_INTERVAL_SECONDS,
        retention_check_interval_seconds: float = DEFAULT_RETENTION_CHECK_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._credentials = credential_store or A2AOutboundCredentialStore()
        self._discovery = discovery_service or A2AOutboundDiscoveryService()
        self._client_builder = client_builder
        self._global_limit = max(1, int(global_concurrency))
        self._agent_limit = max(1, int(agent_concurrency))
        self._query_interval = max(0.0, float(query_interval_seconds))
        self._retention_check_interval = max(
            0.0, float(retention_check_interval_seconds)
        )
        self._monotonic = monotonic
        self._capacity_lock = asyncio.Lock()
        self._global_active = 0
        self._agent_active: dict[str, int] = {}
        self._query_locks = KeyedLockPool()
        self._last_query_monotonic: dict[str, float] = {}
        self._last_retention_monotonic: float | None = None
        self._retention_task: asyncio.Task[None] | None = None

    def set_allow_loopback_http(self, enabled: bool) -> None:
        self._discovery.set_allow_loopback_http(enabled)

    async def _update_runtime_state(
        self,
        agent_id: str,
        availability: A2AOutboundAvailability,
        *,
        error_code: A2AOutboundErrorCode | None = None,
    ) -> None:
        try:
            await self._repository.update_runtime_state(
                agent_id,
                availability,
                error_code=error_code.value if error_code is not None else None,
            )
        except Exception:
            logger.exception(
                "a2a.outbound runtime state update failed agent_id=%s",
                agent_id,
            )

    async def find_agents(
        self,
        query: str = "",
        required_skills: list[str] | tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        normalized_query = str(query or "").strip().lower()
        query_terms = self._search_terms(normalized_query)
        required = {
            str(value).strip().lower()
            for value in (required_skills or ())
            if str(value).strip()
        }
        normalized_limit = max(1, min(int(limit), 20))
        matches: list[tuple[int, dict[str, Any]]] = []
        for agent in await self._repository.list_agents():
            if not self._callable(agent):
                continue
            card = agent.agent_card
            skills = self._public_skills(card)
            skill_tokens = _skill_search_tokens(skills)
            if required and not required.issubset(skill_tokens):
                continue
            searchable = " ".join(
                [agent.display_name, str(card.get("description") or "")]
                + [
                    " ".join(
                        [
                            *(
                                str(skill.get(key) or "")
                                for key in ("id", "name", "description")
                            ),
                            *(str(tag) for tag in skill.get("tags") or []),
                        ]
                    )
                    for skill in skills
                ]
            ).lower()
            score = sum(1 for term in query_terms if term in searchable)
            if normalized_query and normalized_query in searchable:
                score += 2
            matches.append(
                (
                    score,
                    {
                        "agent_id": agent.agent_id,
                        "name": agent.display_name,
                        "description": str(card.get("description") or ""),
                        "skills": skills,
                        "input_modes": list(card.get("defaultInputModes") or []),
                        "output_modes": list(card.get("defaultOutputModes") or []),
                        "availability": agent.availability.value,
                    },
                )
            )
        matches.sort(key=lambda item: (-item[0], item[1]["name"].lower()))
        items = [item for _, item in matches[:normalized_limit]]
        return {
            "items": items,
            # Back-compat: "total" keeps its pre-existing meaning of "count of
            # returned items". The full (unsliced) candidate count that used to
            # live here is now reported separately as "total_matches" so
            # existing consumers relying on total == len(items) don't break.
            "total": len(items),
            "total_matches": len(matches),
            "matched_total": sum(1 for score, _ in matches if score > 0),
        }

    @staticmethod
    def _search_terms(query: str) -> set[str]:
        """Tokenize English identifiers and Chinese text without external NLP."""
        terms: set[str] = set()
        for token in re.findall(
            r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+", query.lower()
        ):
            terms.add(token)
            if all("\u3400" <= char <= "\u9fff" for char in token) and len(token) > 2:
                terms.update(
                    token[index:index + 2] for index in range(len(token) - 1)
                )
        return terms

    async def dispatch(
        self,
        *,
        agent_id: str,
        task: str,
        mode: A2AOutboundDispatchMode | str,
        source_session_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        reason_present = bool(str(reason or "").strip())
        task_text = str(task or "")
        if not task_text.strip() or len(task_text) > MAX_TASK_TEXT_LENGTH:
            raise A2AOutboundError(A2AOutboundErrorCode.TASK_INVALID)
        try:
            normalized_mode = A2AOutboundDispatchMode(str(mode))
        except ValueError as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.MODE_INVALID) from exc
        session_id = str(source_session_id or "").strip()
        if not session_id:
            raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_REJECTED)
        agent = await self._require_callable_agent(agent_id)
        self._schedule_cleanup_dispatches()

        dispatch_id = f"disp_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        stamp = utc_now_text()
        dispatch = A2AOutboundDispatch(
            dispatch_id=dispatch_id,
            agent_id=agent.agent_id,
            agent_revision=agent.card_revision,
            mode=normalized_mode,
            status=A2AOutboundDispatchStatus.CREATED,
            request_message_id=message_id,
            source_session_id=session_id,
            created_at=stamp,
            updated_at=stamp,
            agent_name=agent.display_name,
            input_length=len(task_text),
            input_content_type="text/plain",
            input_digest=f"sha256:{hashlib.sha256(task_text.encode()).hexdigest()}",
        )
        create_task: asyncio.Task[A2AOutboundDispatch] | None = None
        try:
            create_task = asyncio.create_task(
                self._repository.create_dispatch(dispatch),
                name=f"a2a-outbound-create-{dispatch.dispatch_id}",
            )
            await asyncio.shield(create_task)
            try:
                async with _CapacityLease(self, agent.agent_id):
                    result = await self._submit(agent, dispatch, task_text)
            except A2AOutboundError as exc:
                if exc.code is not A2AOutboundErrorCode.OUTBOUND_BUSY:
                    raise
                updated = await self._repository.transition_dispatch(
                    dispatch.dispatch_id,
                    A2AOutboundDispatchStatus.DISPATCH_FAILED,
                    error_code=exc.code.value,
                )
                result = self._public_dispatch(updated or dispatch)
        except asyncio.CancelledError:
            if create_task is not None:
                try:
                    await asyncio.shield(create_task)
                except Exception:
                    logger.debug(
                        "a2a.outbound create_dispatch failed during cancellation",
                        exc_info=True,
                    )
            await asyncio.shield(
                self._repository.transition_dispatch(
                    dispatch.dispatch_id,
                    A2AOutboundDispatchStatus.UNKNOWN,
                    error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
                )
            )
            raise
        logger.info(
            "a2a.outbound audit action=dispatch dispatch_id=%s agent_id=%s "
            "agent_revision=%s mode=%s status=%s source_session_id=%s reason_present=%s",
            dispatch.dispatch_id,
            dispatch.agent_id,
            dispatch.agent_revision,
            dispatch.mode.value,
            result.get("status"),
            dispatch.source_session_id,
            reason_present,
        )
        return result

    async def query_dispatch(
        self, dispatch_id: str, *, source_session_id: str
    ) -> dict[str, Any]:
        normalized_id = str(dispatch_id or "").strip()
        async with self._query_locks.hold(normalized_id):
            current = await self._require_owned_dispatch(
                normalized_id, source_session_id
            )
            if current.is_terminal or not current.remote_task_id:
                return self._public_dispatch(current)
            now = self._monotonic()
            last = self._last_query_monotonic.get(normalized_id)
            if last is not None and now - last < self._query_interval:
                return self._public_dispatch(
                    current,
                    retry_after_seconds=round(self._query_interval - (now - last), 3),
                )
            self._last_query_monotonic[normalized_id] = now
            agent = await self._repository.get_agent(current.agent_id)
            if agent is None:
                return self._public_dispatch(current)
            try:
                client = await self._build_client(agent)
                try:
                    task = await client.get_task(
                        GetTaskRequest(id=current.remote_task_id, history_length=1),
                        context=ClientCallContext(
                            timeout=agent.connect_timeout_seconds
                        ),
                    )
                finally:
                    await client.close()
                normalized = self._normalize_task(task)
                updated = await self._apply_remote(
                    current.dispatch_id, normalized, polled=True
                )
                return self._public_dispatch(updated or current)
            except Exception:
                logger.debug(
                    "a2a.outbound query_dispatch failed: dispatch_id=%s",
                    normalized_id,
                    exc_info=True,
                )
                updated = await self._repository.transition_dispatch(
                    current.dispatch_id,
                    A2AOutboundDispatchStatus.UNKNOWN,
                    error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
                    last_polled_at=utc_now_text(),
                )
                return self._public_dispatch(updated or current)

    async def cancel_dispatch(
        self, dispatch_id: str, *, source_session_id: str
    ) -> dict[str, Any]:
        async with self._query_locks.hold(str(dispatch_id)):
            current = await self._require_owned_dispatch(dispatch_id, source_session_id)
            if current.is_terminal or not current.remote_task_id:
                return self._public_dispatch(current)
            agent = await self._repository.get_agent(current.agent_id)
            if agent is None:
                return self._public_dispatch(current)
            try:
                client = await self._build_client(agent)
                try:
                    task = await client.cancel_task(
                        CancelTaskRequest(id=current.remote_task_id),
                        context=ClientCallContext(
                            timeout=agent.connect_timeout_seconds
                        ),
                    )
                finally:
                    await client.close()
                normalized = self._normalize_task(task)
                updated = await self._apply_remote(current.dispatch_id, normalized)
                return self._public_dispatch(updated or current)
            except Exception:
                logger.debug(
                    "a2a.outbound cancel_dispatch failed: dispatch_id=%s",
                    dispatch_id,
                    exc_info=True,
                )
                updated = await self._repository.transition_dispatch(
                    current.dispatch_id,
                    A2AOutboundDispatchStatus.UNKNOWN,
                    error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
                )
                return self._public_dispatch(updated or current)

    async def _submit(
        self,
        agent: A2AOutboundAgent,
        dispatch: A2AOutboundDispatch,
        task_text: str,
    ) -> dict[str, Any]:
        current = await self._repository.transition_dispatch(
            dispatch.dispatch_id, A2AOutboundDispatchStatus.SUBMITTING
        )
        client: _ClientLike | None = None
        last_remote = _NormalizedRemote(A2AOutboundDispatchStatus.SUBMITTING)
        observed_response = False
        try:
            client = await self._build_client(agent)
            request = SendMessageRequest(
                message=Message(
                    message_id=dispatch.request_message_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text=task_text)],
                ),
                configuration=SendMessageConfiguration(
                    # Both modes need the remote acceptance/rejection result promptly.
                    # Sync mode waits locally by polling the accepted task below instead
                    # of asking the remote HTTP request to stay open for the whole task.
                    return_immediately=True
                ),
            )

            async def consume() -> _NormalizedRemote:
                nonlocal last_remote, observed_response
                # The outer sync_wait timeout owns the total synchronous task budget.
                # Keeping the SDK call context unset prevents it from replacing the
                # client's granular connect/write/pool limits with a short read limit.
                call_context = (
                    None
                    if dispatch.mode is A2AOutboundDispatchMode.SYNC
                    else ClientCallContext(timeout=agent.connect_timeout_seconds)
                )
                async for event in client.send_message(
                    request,
                    context=call_context,
                ):
                    last_remote = self._normalize_stream_event(event, last_remote)
                    if not observed_response:
                        observed_response = True
                        await self._update_runtime_state(
                            agent.agent_id,
                            A2AOutboundAvailability.AVAILABLE,
                        )
                    applied = await self._apply_remote(
                        dispatch.dispatch_id, last_remote
                    )
                    if applied is not None:
                        current_status = applied.status
                        if applied.is_terminal or current_status in {
                            A2AOutboundDispatchStatus.INPUT_REQUIRED,
                            A2AOutboundDispatchStatus.AUTH_REQUIRED,
                        }:
                            break
                    if (
                        dispatch.mode is A2AOutboundDispatchMode.ASYNC
                        and last_remote.remote_task_id
                    ):
                        break
                return last_remote

            if dispatch.mode is A2AOutboundDispatchMode.SYNC:
                try:
                    async with asyncio.timeout(agent.sync_wait_seconds):
                        await consume()
                        while True:
                            latest = await self._repository.get_dispatch(
                                dispatch.dispatch_id
                            )
                            if latest is None:
                                raise A2AOutboundError(
                                    A2AOutboundErrorCode.DISPATCH_NOT_FOUND
                                )
                            if latest.is_terminal or latest.status in {
                                A2AOutboundDispatchStatus.INPUT_REQUIRED,
                                A2AOutboundDispatchStatus.AUTH_REQUIRED,
                            }:
                                break
                            if not latest.remote_task_id:
                                break
                            await asyncio.sleep(min(self._query_interval, 1.0))
                            remote_task = await client.get_task(
                                GetTaskRequest(
                                    id=latest.remote_task_id, history_length=1
                                ),
                                context=ClientCallContext(
                                    timeout=agent.connect_timeout_seconds
                                ),
                            )
                            last_remote = self._normalize_task(remote_task)
                            await self._apply_remote(
                                dispatch.dispatch_id, last_remote, polled=True
                            )
                except TimeoutError:
                    if not observed_response:
                        await self._update_runtime_state(
                            agent.agent_id,
                            A2AOutboundAvailability.UNREACHABLE,
                            error_code=A2AOutboundErrorCode.DISPATCH_TIMEOUT,
                        )
                    updated = await self._repository.transition_dispatch(
                        dispatch.dispatch_id,
                        A2AOutboundDispatchStatus.TIMED_OUT,
                        remote_task_id=last_remote.remote_task_id,
                        remote_context_id=last_remote.remote_context_id,
                        error_code=A2AOutboundErrorCode.DISPATCH_TIMEOUT.value,
                    )
                    return self._public_dispatch(updated or current)
            else:
                await consume()

            updated = await self._repository.get_dispatch(dispatch.dispatch_id)
            if updated is None:
                raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_NOT_FOUND)
            if (
                dispatch.mode is A2AOutboundDispatchMode.ASYNC
                and not updated.is_terminal
                and not updated.remote_task_id
            ):
                updated = (
                    await self._repository.transition_dispatch(
                        dispatch.dispatch_id,
                        A2AOutboundDispatchStatus.DISPATCH_FAILED,
                        error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
                    )
                    or updated
                )
            return self._public_dispatch(updated)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._settle_cancelled(
                    client,
                    agent,
                    dispatch.dispatch_id,
                    last_remote,
                )
            )
            raise
        except A2AOutboundError as exc:
            if exc.code is A2AOutboundErrorCode.AGENT_UNAVAILABLE:
                await self._update_runtime_state(
                    agent.agent_id,
                    A2AOutboundAvailability.INCOMPATIBLE,
                    error_code=exc.code,
                )
            status = (
                A2AOutboundDispatchStatus.AUTH_REQUIRED
                if exc.code is A2AOutboundErrorCode.AUTH_REQUIRED
                else A2AOutboundDispatchStatus.DISPATCH_FAILED
            )
            updated = await self._repository.transition_dispatch(
                dispatch.dispatch_id,
                status,
                remote_task_id=last_remote.remote_task_id,
                remote_context_id=last_remote.remote_context_id,
                error_code=exc.code.value,
            )
            return self._public_dispatch(updated or current)
        except Exception:
            if not observed_response:
                await self._update_runtime_state(
                    agent.agent_id,
                    A2AOutboundAvailability.UNREACHABLE,
                    error_code=A2AOutboundErrorCode.AGENT_UNAVAILABLE,
                )
            target = (
                A2AOutboundDispatchStatus.UNKNOWN
                if last_remote.remote_task_id
                else A2AOutboundDispatchStatus.DISPATCH_FAILED
            )
            code = (
                A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN
                if last_remote.remote_task_id
                else A2AOutboundErrorCode.DISPATCH_REJECTED
            )
            updated = await self._repository.transition_dispatch(
                dispatch.dispatch_id,
                target,
                remote_task_id=last_remote.remote_task_id,
                remote_context_id=last_remote.remote_context_id,
                error_code=code.value,
            )
            return self._public_dispatch(updated or current)
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.debug("a2a.outbound client close failed", exc_info=True)

    async def _build_client(self, agent: A2AOutboundAgent) -> _ClientLike:
        credential = self._credentials.get(agent.credential_ref)
        if self._credential_required(agent.agent_card) and not credential:
            raise A2AOutboundError(A2AOutboundErrorCode.AUTH_REQUIRED)
        headers, params, cookies = self._credential_transport_options(
            agent.agent_card, credential
        )
        if self._client_builder is not None:
            return await self._client_builder(agent, credential)
        target = await self._discovery.validate_network_target(
            agent.selected_interface.url
        )
        http_client = httpx.AsyncClient(
            transport=create_pinned_transport({target.host: target.pinned_address}),
            headers=headers,
            params=params,
            cookies=cookies,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=agent.connect_timeout_seconds,
                write=agent.connect_timeout_seconds,
                pool=agent.connect_timeout_seconds,
                read=None,
            ),
        )
        try:
            parsed = parse_agent_card(copy.deepcopy(agent.agent_card))
            selected = agent.selected_interface
            matching = [
                item
                for item in parsed.supported_interfaces
                if _is_selected_interface(item, selected)
            ]
            if not matching:
                raise A2AOutboundError(A2AOutboundErrorCode.AGENT_UNAVAILABLE)
            del parsed.supported_interfaces[:]
            parsed.supported_interfaces.extend(matching)
            return ClientFactory(
                ClientConfig(
                    streaming=True,
                    polling=False,
                    httpx_client=http_client,
                    supported_protocol_bindings=[selected.protocol_binding],
                )
            ).create(parsed)
        except Exception:
            await http_client.aclose()
            raise

    def _schedule_cleanup_dispatches(self) -> None:
        now = self._monotonic()
        last = self._last_retention_monotonic
        if last is not None and now - last < self._retention_check_interval:
            return
        if self._retention_task is not None and not self._retention_task.done():
            return
        self._last_retention_monotonic = now
        task = asyncio.create_task(
            self._run_retention_cleanup(), name="a2a-outbound-retention"
        )
        self._retention_task = task
        task.add_done_callback(self._clear_retention_task)

    def _clear_retention_task(self, task: asyncio.Task[None]) -> None:
        if self._retention_task is task:
            self._retention_task = None

    async def _run_retention_cleanup(self) -> None:
        try:
            deleted = await self._repository.cleanup_dispatches()
            if deleted:
                logger.info(
                    "a2a.outbound retention cleanup deleted=%s",
                    deleted,
                )
        except Exception:  # noqa: BLE001
            logger.exception("a2a.outbound retention cleanup failed")

    @staticmethod
    def _credential_required(card: dict[str, Any]) -> bool:
        requirements = card.get("securityRequirements") or []
        return bool(requirements) and not any(
            isinstance(requirement, dict)
            and (not requirement or requirement == {"schemes": {}})
            for requirement in requirements
        )

    @staticmethod
    def _credential_transport_options(
        card: dict[str, Any], credential: str
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Build credential placement from the Agent Card security contract."""
        if not credential:
            return {}, {}, {}
        requirements = card.get("securityRequirements") or []
        if not requirements:
            raise A2AOutboundError(A2AOutboundErrorCode.AUTH_SCHEME_MISSING)
        schemes = card.get("securitySchemes") or {}
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            if not requirement:
                return {}, {}, {}
            required_schemes = requirement.get("schemes")
            if not isinstance(required_schemes, dict):
                continue
            if not required_schemes:
                return {}, {}, {}
            headers: dict[str, str] = {}
            params: dict[str, str] = {}
            cookies: dict[str, str] = {}
            supported = True
            for scheme_name in required_schemes:
                scheme = schemes.get(scheme_name)
                if not isinstance(scheme, dict):
                    supported = False
                    break
                api_key = scheme.get("apiKeySecurityScheme")
                http_auth = scheme.get("httpAuthSecurityScheme")
                if isinstance(api_key, dict):
                    name = str(api_key.get("name") or "").strip()
                    location = str(api_key.get("location") or "").strip().lower()
                    if not name or location not in {"header", "query", "cookie"}:
                        supported = False
                        break
                    {"header": headers, "query": params, "cookie": cookies}[location][
                        name
                    ] = credential
                elif isinstance(http_auth, dict):
                    auth_scheme = str(http_auth.get("scheme") or "").strip().lower()
                    if auth_scheme == "bearer":
                        headers["Authorization"] = (
                            credential
                            if credential.lower().startswith("bearer ")
                            else f"Bearer {credential}"
                        )
                    elif auth_scheme == "basic":
                        encoded = (
                            credential[6:].strip()
                            if credential.lower().startswith("basic ")
                            else base64.b64encode(credential.encode()).decode()
                        )
                        headers["Authorization"] = f"Basic {encoded}"
                    else:
                        supported = False
                        break
                elif (
                    "oauth2SecurityScheme" in scheme
                    or "openIdConnectSecurityScheme" in scheme
                ):
                    headers["Authorization"] = (
                        credential
                        if credential.lower().startswith("bearer ")
                        else f"Bearer {credential}"
                    )
                else:
                    supported = False
                    break
            if supported:
                return headers, params, cookies
        raise A2AOutboundError(A2AOutboundErrorCode.AUTH_REQUIRED)

    async def _require_callable_agent(self, agent_id: str) -> A2AOutboundAgent:
        agent = await self._repository.get_agent(str(agent_id or "").strip())
        if agent is None:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        if not agent.enabled:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_DISABLED)
        if agent.availability is A2AOutboundAvailability.REVIEW_REQUIRED:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_REVIEW_REQUIRED)
        if agent.availability is not A2AOutboundAvailability.AVAILABLE:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_UNAVAILABLE)
        return agent

    @staticmethod
    def _callable(agent: A2AOutboundAgent) -> bool:
        return bool(
            agent.enabled and agent.availability is A2AOutboundAvailability.AVAILABLE
        )

    async def _require_owned_dispatch(
        self, dispatch_id: str, source_session_id: str
    ) -> A2AOutboundDispatch:
        dispatch = await self._repository.get_dispatch(str(dispatch_id or "").strip())
        if dispatch is None:
            raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_NOT_FOUND)
        if dispatch.source_session_id != str(source_session_id or "").strip():
            # Deliberately indistinguishable from a missing ID.
            raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_NOT_FOUND)
        return dispatch

    async def _apply_remote(
        self, dispatch_id: str, remote: _NormalizedRemote, *, polled: bool = False
    ) -> A2AOutboundDispatch | None:
        stamp = utc_now_text()
        changes: dict[str, Any] = {
            "remote_task_id": remote.remote_task_id,
            "remote_context_id": remote.remote_context_id,
            "result": remote.result,
            "error_code": None,
            "error_summary": None,
        }
        if remote.remote_task_id:
            changes["accepted_at"] = stamp
        if polled:
            changes["last_polled_at"] = stamp
        if remote.status is A2AOutboundDispatchStatus.AUTH_REQUIRED:
            changes["error_code"] = A2AOutboundErrorCode.AUTH_REQUIRED.value
        elif remote.status is A2AOutboundDispatchStatus.REJECTED:
            changes["error_code"] = A2AOutboundErrorCode.DISPATCH_REJECTED.value
        elif remote.status is A2AOutboundDispatchStatus.UNKNOWN:
            changes["error_code"] = A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value
        return await self._repository.transition_dispatch(
            dispatch_id, remote.status, **changes
        )

    async def _settle_cancelled(
        self,
        client: _ClientLike | None,
        agent: A2AOutboundAgent,
        dispatch_id: str,
        remote: _NormalizedRemote,
    ) -> None:
        if client is not None and remote.remote_task_id:
            try:
                task = await client.cancel_task(
                    CancelTaskRequest(id=remote.remote_task_id),
                    context=ClientCallContext(timeout=agent.connect_timeout_seconds),
                )
                await self._apply_remote(dispatch_id, self._normalize_task(task))
                return
            except Exception:
                logger.debug(
                    "a2a.outbound remote cancel_task failed dispatch_id=%s",
                    dispatch_id,
                    exc_info=True,
                )
        await self._repository.transition_dispatch(
            dispatch_id,
            A2AOutboundDispatchStatus.UNKNOWN,
            remote_task_id=remote.remote_task_id,
            remote_context_id=remote.remote_context_id,
            error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
        )

    def _normalize_stream_event(
        self, event: Any, previous: _NormalizedRemote
    ) -> _NormalizedRemote:
        if event.HasField("message"):
            message = event.message
            return _NormalizedRemote(
                A2AOutboundDispatchStatus.COMPLETED,
                remote_task_id=message.task_id or previous.remote_task_id,
                remote_context_id=message.context_id or previous.remote_context_id,
                result=self._result_from_message(message),
            )
        if event.HasField("task"):
            return self._normalize_task(event.task)
        if event.HasField("status_update"):
            update = event.status_update
            status = self._map_task_state(update.status.state)
            result = (
                self._result_from_message(update.status.message)
                if update.status.HasField("message")
                else previous.result
            )
            if status is A2AOutboundDispatchStatus.COMPLETED:
                result = self._promote_artifact_text(result)
            return _NormalizedRemote(
                status,
                remote_task_id=update.task_id or previous.remote_task_id,
                remote_context_id=update.context_id or previous.remote_context_id,
                result=result,
            )
        if event.HasField("artifact_update"):
            update = event.artifact_update
            result = dict(previous.result or {"text": "", "artifacts": []})
            artifacts = list(result.get("artifacts") or [])
            if len(artifacts) < MAX_RESULT_ARTIFACTS:
                artifacts.append(self._artifact_summary(update.artifact))
            result["artifacts"] = artifacts
            return _NormalizedRemote(
                previous.status,
                remote_task_id=update.task_id or previous.remote_task_id,
                remote_context_id=update.context_id or previous.remote_context_id,
                result=result,
            )
        return previous

    def _normalize_task(self, task: Task) -> _NormalizedRemote:
        result = None
        if task.status.HasField("message") or task.artifacts:
            result = (
                self._result_from_message(task.status.message)
                if task.status.HasField("message")
                else {"text": "", "artifacts": []}
            )
            result["artifacts"] = [
                self._artifact_summary(item)
                for item in list(task.artifacts)[:MAX_RESULT_ARTIFACTS]
            ]
            if (
                self._map_task_state(task.status.state)
                is A2AOutboundDispatchStatus.COMPLETED
            ):
                result = self._promote_artifact_text(result)
        return _NormalizedRemote(
            self._map_task_state(task.status.state),
            remote_task_id=task.id or None,
            remote_context_id=task.context_id or None,
            result=result,
        )

    @staticmethod
    def _map_task_state(state: int) -> A2AOutboundDispatchStatus:
        return {
            TaskState.TASK_STATE_SUBMITTED: A2AOutboundDispatchStatus.ACCEPTED,
            TaskState.TASK_STATE_WORKING: A2AOutboundDispatchStatus.WORKING,
            TaskState.TASK_STATE_COMPLETED: A2AOutboundDispatchStatus.COMPLETED,
            TaskState.TASK_STATE_FAILED: A2AOutboundDispatchStatus.FAILED,
            TaskState.TASK_STATE_CANCELED: A2AOutboundDispatchStatus.CANCELED,
            TaskState.TASK_STATE_INPUT_REQUIRED: A2AOutboundDispatchStatus.INPUT_REQUIRED,
            TaskState.TASK_STATE_REJECTED: A2AOutboundDispatchStatus.REJECTED,
            TaskState.TASK_STATE_AUTH_REQUIRED: A2AOutboundDispatchStatus.AUTH_REQUIRED,
        }.get(state, A2AOutboundDispatchStatus.UNKNOWN)

    @staticmethod
    def _result_from_message(message: Message) -> dict[str, Any]:
        text = "\n".join(part.text for part in message.parts if part.HasField("text"))
        truncated = len(text) > MAX_RESULT_TEXT_LENGTH
        return {
            "text": text[:MAX_RESULT_TEXT_LENGTH],
            "artifacts": [],
            **({"truncated": True} if truncated else {}),
        }

    @staticmethod
    def _artifact_summary(artifact: Any) -> dict[str, Any]:
        text = "\n".join(part.text for part in artifact.parts if part.HasField("text"))
        truncated = len(text) > MAX_RESULT_TEXT_LENGTH
        return {
            "artifact_id": artifact.artifact_id or None,
            "name": artifact.name or None,
            "description": artifact.description or None,
            "text": text[:MAX_RESULT_TEXT_LENGTH],
            **({"truncated": True} if truncated else {}),
        }

    @staticmethod
    def _promote_artifact_text(
        result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Use completed artifact content as the primary tool result text."""
        if not result:
            return result
        artifact_texts = [
            str(item.get("text") or "").strip()
            for item in result.get("artifacts") or []
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if not artifact_texts:
            return result
        promoted = dict(result)
        text = "\n".join(artifact_texts)
        promoted["text"] = text[:MAX_RESULT_TEXT_LENGTH]
        if len(text) > MAX_RESULT_TEXT_LENGTH:
            promoted["truncated"] = True
        return promoted

    @staticmethod
    def _public_skills(card: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "tags": [str(tag) for tag in item.get("tags") or []],
            }
            for item in card.get("skills") or []
            if isinstance(item, dict)
        ]

    @staticmethod
    def _public_dispatch(
        dispatch: A2AOutboundDispatch,
        *,
        retry_after_seconds: float | None = None,
    ) -> dict[str, Any]:
        ok = dispatch.status in {
            A2AOutboundDispatchStatus.ACCEPTED,
            A2AOutboundDispatchStatus.WORKING,
            A2AOutboundDispatchStatus.COMPLETED,
        }
        # Breaking change vs. earlier batches: remote_task_id/remote_context_id are
        # intentionally NOT exposed here (still retained on the internal dataclass and
        # persisted record). Callers must use the local dispatch_id for all follow-up
        # a2a_get_dispatch/a2a_cancel_dispatch calls, so leaking the remote agent's own
        # task/context identifiers would only invite an LLM caller to misuse them.
        payload: dict[str, Any] = {
            "ok": ok,
            "dispatch_id": dispatch.dispatch_id,
            "agent_id": dispatch.agent_id,
            "agent_name": dispatch.agent_name,
            "mode": dispatch.mode.value,
            "status": dispatch.status.value,
            "created_at": dispatch.created_at,
            "accepted_at": dispatch.accepted_at,
            "finished_at": dispatch.finished_at,
            "result": dispatch.result,
            "error_code": dispatch.error_code,
            "error_summary": (
                safe_error_summary(dispatch.error_code) if dispatch.error_code else None
            ),
        }
        if dispatch.remote_task_id and dispatch.status in {
            A2AOutboundDispatchStatus.ACCEPTED,
            A2AOutboundDispatchStatus.WORKING,
            A2AOutboundDispatchStatus.TIMED_OUT,
            A2AOutboundDispatchStatus.UNKNOWN,
        }:
            payload["next_action"] = (
                "Call a2a_get_dispatch with dispatch_id to retrieve status and result."
            )
        if retry_after_seconds is not None:
            payload["retry_after_seconds"] = retry_after_seconds
        return payload


__all__ = [
    "A2AOutboundDispatcher",
    "DEFAULT_AGENT_CONCURRENCY",
    "DEFAULT_GLOBAL_CONCURRENCY",
    "MAX_TASK_TEXT_LENGTH",
]
