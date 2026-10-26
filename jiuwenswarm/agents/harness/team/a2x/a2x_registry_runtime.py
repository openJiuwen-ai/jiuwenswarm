# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Small runtime helpers for A2X registry integration.

This module intentionally stays independent from DeepAgent and TeamAgent so
startup paths can register a blank teammate without importing agent runtime
internals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_REGISTERED_BLANK_ENDPOINTS: set[tuple[str, str, str]] = set()
_REGISTERED_BLANK_REGISTRATIONS: dict[tuple[str, str, str], dict[str, str]] = {}

_TEAMMATE_CARD_DESCRIPTION = "Task Planner(team-1)"
_TEAMMATE_CARD_STATUS = "busy"
_TEAMMATE_CARD_SKILLS = [{"name": "plan", "description": "子任务拆解"}]


def _normalize_connect_addr(raw: Any) -> str:
    """Turn a local bind address into a peer-connectable address."""
    value = str(raw or "").strip()
    if not value:
        return ""
    return re.sub(r"^tcp://0\.0\.0\.0(?=[:/]|$)", "tcp://127.0.0.1", value)


def _derive_teammate_endpoint(config_base: dict[str, Any], explicit_endpoint: Any) -> str | None:
    endpoint = str(explicit_endpoint or "").strip()
    if endpoint:
        return endpoint

    team_cfg = config_base.get("team", {}) if isinstance(config_base, dict) else {}
    if not isinstance(team_cfg, dict):
        return None
    transport_cfg = team_cfg.get("transport", {}) if isinstance(team_cfg.get("transport"), dict) else {}
    params = transport_cfg.get("params", {}) if isinstance(transport_cfg.get("params"), dict) else {}
    derived = _normalize_connect_addr(params.get("bootstrap_direct_addr"))
    return derived or None


@dataclass
class ReservedBlankAgent:
    """Leader-side reservation handle for a blank teammate endpoint."""

    client: Any
    reservation: Any
    dataset: str
    service_id: str
    endpoint: str

    async def release(self) -> None:
        """Release this reservation if it has not been released yet."""
        try:
            await self.client.release_reservation(self.reservation)
        except Exception as exc:
            logger.warning(
                "[A2XRegistryRuntime] blank agent reservation release failed dataset=%s service_id=%s: %s",
                self.dataset,
                self.service_id,
                exc,
            )

    async def close(self) -> None:
        """Close the registry client backing this reservation."""
        try:
            await asyncio.wait_for(self.client.aclose(), timeout=2.0)
        except Exception as exc:
            logger.debug(
                "[A2XRegistryRuntime] reservation client close failed dataset=%s service_id=%s: %s",
                self.dataset,
                self.service_id,
                exc,
            )


def build_teammate_agent_card(member_name: str) -> dict[str, Any]:
    """Build the AgentCard used after teammate bootstrap."""
    name = str(member_name or "").strip()
    if not name:
        raise ValueError("member_name is required for teammate agent card replacement")
    return {
        "name": name,
        "description": _TEAMMATE_CARD_DESCRIPTION,
        "status": _TEAMMATE_CARD_STATUS,
        "skills": [dict(skill) for skill in _TEAMMATE_CARD_SKILLS],
    }


def resolve_a2x_config(config_base: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``react.a2x_registry`` with safe defaults."""
    team_cfg = config_base.get("team", {}) if isinstance(config_base, dict) else {}
    runtime_cfg = team_cfg.get("runtime", {}) if isinstance(team_cfg.get("runtime"), dict) else {}
    runtime_mode = str(runtime_cfg.get("mode", "")).strip().lower()
    react_cfg = config_base.get("react", {}) if isinstance(config_base, dict) else {}
    a2x_cfg = react_cfg.get("a2x_registry", {}) if isinstance(react_cfg, dict) else {}

    base_url = str(a2x_cfg.get("base_url") or "http://127.0.0.1:8000").strip()
    if not base_url:
        base_url = "http://127.0.0.1:8000"

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid react.a2x_registry.base_url: {base_url!r}. Expected http(s) URL")

    timeout_raw = a2x_cfg.get("timeout", 30.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = 30.0

    api_key = str(a2x_cfg.get("api_key") or "").strip() or None
    ownership_file = a2x_cfg.get("ownership_file", False)
    dataset = str(a2x_cfg.get("dataset") or "").strip() or None
    role = str(a2x_cfg.get("role") or "teammate").strip().lower()
    if role not in {"teammate", "teamleader"}:
        logger.warning(
            "[A2XRegistryRuntime] invalid a2x_registry.role=%r, fallback to teammate",
            role,
        )
        role = "teammate"
    if role == "teammate":
        endpoint = _derive_teammate_endpoint(config_base, a2x_cfg.get("endpoint"))
    else:
        endpoint = str(a2x_cfg.get("endpoint") or "").strip() or None
    reservation_ttl_raw = a2x_cfg.get("reservation_ttl_seconds", 30)
    try:
        reservation_ttl_seconds = int(reservation_ttl_raw)
    except (TypeError, ValueError):
        reservation_ttl_seconds = 30
    if reservation_ttl_seconds < 1:
        reservation_ttl_seconds = 30

    return {
        "base_url": base_url,
        "timeout": timeout,
        "api_key": api_key,
        "ownership_file": ownership_file,
        "dataset": dataset,
        "endpoint": endpoint,
        "reservation_ttl_seconds": reservation_ttl_seconds,
        "role": role,
        "distributed_mode": runtime_mode == "distributed",
    }


async def init_a2x_client(config_base: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Create an AsyncA2XRegistryClient from runtime config."""
    from jiuwenswarm.agents.harness.team.a2x.client import AsyncA2XRegistryClient

    config = resolve_a2x_config(config_base)
    client = AsyncA2XRegistryClient(
        base_url=config["base_url"],
        timeout=config["timeout"],
        api_key=config["api_key"],
        ownership_file=config["ownership_file"],
    )
    return client, config


def _remember_blank_registration(
    client: Any,
    cache_key: tuple[str, str, str],
    *,
    dataset: Any,
    service_id: Any,
    endpoint: Any,
) -> None:
    """Expose blank registration details on a client and cache them for sibling clients."""
    registration = {
        "dataset": str(dataset or "").strip(),
        "service_id": str(service_id or "").strip(),
        "endpoint": str(endpoint or "").strip(),
    }
    _REGISTERED_BLANK_REGISTRATIONS[cache_key] = registration
    setattr(client, "_jiuwen_blank_agent_registration", registration)
    setattr(client, "_jiuwen_blank_agent_dataset", registration["dataset"])
    setattr(client, "_jiuwen_blank_agent_service_id", registration["service_id"])
    setattr(client, "_jiuwen_blank_agent_endpoint", registration["endpoint"])


async def register_blank_agent_if_teammate(
    client: Any,
    config: dict[str, Any],
    *,
    source: str,
) -> bool:
    """Register the configured teammate endpoint as a blank A2X agent."""
    if not config.get("distributed_mode"):
        logger.debug("[A2XRegistryRuntime] blank agent registration skipped source=%s: non-distributed mode", source)
        return False
    if config.get("role") != "teammate":
        return False

    dataset = config.get("dataset")
    endpoint = config.get("endpoint")
    if not dataset or not endpoint:
        logger.info(
            "[A2XRegistryRuntime] blank agent registration skipped source=%s: "
            "missing dataset or endpoint in react.a2x_registry config",
            source,
        )
        return False

    cache_key = (str(config.get("base_url") or ""), str(dataset), str(endpoint))
    if cache_key in _REGISTERED_BLANK_ENDPOINTS:
        cached_registration = _REGISTERED_BLANK_REGISTRATIONS.get(cache_key)
        if cached_registration:
            _remember_blank_registration(
                client,
                cache_key,
                dataset=cached_registration.get("dataset"),
                service_id=cached_registration.get("service_id"),
                endpoint=cached_registration.get("endpoint"),
            )
        logger.info(
            "[A2XRegistryRuntime] blank agent already registered source=%s dataset=%s "
            "service_id=%s endpoint=%s",
            source,
            dataset,
            cached_registration.get("service_id", "") if cached_registration else "",
            endpoint,
        )
        return True

    result = await client.register_blank_agent(dataset=dataset, endpoint=endpoint)
    _REGISTERED_BLANK_ENDPOINTS.add(cache_key)
    _remember_blank_registration(
        client,
        cache_key,
        dataset=dataset,
        service_id=getattr(result, "service_id", ""),
        endpoint=endpoint,
    )
    logger.info(
        "[A2XRegistryRuntime] blank agent registered source=%s dataset=%s service_id=%s endpoint=%s",
        source,
        dataset,
        getattr(result, "service_id", ""),
        endpoint,
    )
    return True


async def register_teammate_blank_agent_at_startup(
    config_base: dict[str, Any],
    *,
    source: str = "startup",
    timeout: float = 5.0,
) -> bool:
    """Best-effort startup registration for a blank teammate endpoint."""
    client = None
    try:
        client, config = await init_a2x_client(config_base)
        return await asyncio.wait_for(
            register_blank_agent_if_teammate(client, config, source=source),
            timeout=max(timeout, 0.1),
        )
    except asyncio.TimeoutError:
        logger.warning("[A2XRegistryRuntime] blank agent registration timed out source=%s", source)
    except Exception as exc:
        logger.warning(
            "[A2XRegistryRuntime] blank agent registration failed source=%s: %s",
            source,
            exc,
            exc_info=True,
        )
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=2.0)
            except Exception as exc:
                logger.debug("[A2XRegistryRuntime] A2X client close failed source=%s: %s", source, exc)
    return False


async def restore_teammate_blank_agent_on_destroy(
    config_base: dict[str, Any],
    *,
    dataset: str | None = None,
    service_id: str | None = None,
    endpoint: str | None = None,
    source: str = "teammate-team-destroy",
    timeout: float = 5.0,
) -> bool:
    """Reset this teammate's registry agent card to blank/online after team teardown."""
    client = None
    try:
        client, config = await init_a2x_client(config_base)
        if not config.get("distributed_mode") or config.get("role") != "teammate":
            return False
        resolved_dataset = str(dataset or config.get("dataset") or "").strip()
        resolved_endpoint = _normalize_connect_addr(endpoint or config.get("endpoint"))
        resolved_service_id = str(service_id or "").strip() or None
        if not resolved_dataset or not resolved_endpoint:
            logger.info(
                "[A2XRegistryRuntime] blank agent restore skipped source=%s: missing dataset or endpoint",
                source,
            )
            return False

        async def _restore() -> bool:
            if not resolved_service_id:
                result = await client.register_blank_agent(
                    dataset=resolved_dataset,
                    endpoint=resolved_endpoint,
                    persistent=True,
                )
                restored_service_id = result.service_id
            else:
                from jiuwenswarm.agents.harness.team.a2x.client.errors import NotOwnedError
                from jiuwenswarm.agents.harness.team.a2x.client import _internal as _a2x_internal

                blank_card = _a2x_internal.build_blank_agent_card(resolved_endpoint)
                try:
                    result = await client.replace_agent_card(
                        resolved_dataset,
                        resolved_service_id,
                        blank_card,
                        release_lease=True,
                    )
                except NotOwnedError:
                    logger.info(
                        "[A2XRegistryRuntime] blank agent restore ownership missing; "
                        "re-registering before replace source=%s dataset=%s service_id=%s endpoint=%s",
                        source,
                        resolved_dataset,
                        resolved_service_id,
                        resolved_endpoint,
                    )
                    registered = await client.register_blank_agent(
                        dataset=resolved_dataset,
                        endpoint=resolved_endpoint,
                        service_id=resolved_service_id,
                        persistent=True,
                    )
                    result = await client.replace_agent_card(
                        resolved_dataset,
                        registered.service_id,
                        blank_card,
                        release_lease=True,
                    )
                restored_service_id = result.service_id
            _REGISTERED_BLANK_ENDPOINTS.add(
                (str(config.get("base_url") or ""), resolved_dataset, resolved_endpoint)
            )
            _REGISTERED_BLANK_REGISTRATIONS[
                (str(config.get("base_url") or ""), resolved_dataset, resolved_endpoint)
            ] = {
                "dataset": resolved_dataset,
                "service_id": str(restored_service_id or "").strip(),
                "endpoint": resolved_endpoint,
            }
            logger.info(
                "[A2XRegistryRuntime] blank agent restored after team destroy "
                "source=%s dataset=%s service_id=%s endpoint=%s",
                source,
                resolved_dataset,
                restored_service_id,
                resolved_endpoint,
            )
            return True

        return await asyncio.wait_for(_restore(), timeout=max(timeout, 0.1))
    except asyncio.TimeoutError:
        logger.warning("[A2XRegistryRuntime] blank agent restore timed out source=%s", source)
    except Exception as exc:
        logger.warning(
            "[A2XRegistryRuntime] blank agent restore failed source=%s: %s",
            source,
            exc,
            exc_info=True,
        )
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=2.0)
            except Exception as exc:
                logger.debug("[A2XRegistryRuntime] A2X client close failed source=%s: %s", source, exc)
    return False


def _agent_service_id(agent: dict[str, Any]) -> str:
    for key in ("id", "service_id", "sid"):
        value = str(agent.get(key) or "").strip()
        if value:
            return value
    return ""


def _agent_endpoint(agent: dict[str, Any]) -> str:
    value = str(agent.get("endpoint") or "").strip()
    if value:
        return value
    metadata = agent.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("endpoint") or "").strip()
    return ""


async def reserve_blank_teammate_agent(
    config_base: dict[str, Any],
    *,
    source: str = "leader-bootstrap",
) -> ReservedBlankAgent | None:
    """Reserve one blank teammate from A2X registry for leader bootstrap."""
    client = None
    try:
        client, config = await init_a2x_client(config_base)
        dataset = config.get("dataset")
        if not config.get("distributed_mode") or config.get("role") != "teamleader":
            logger.debug(
                "[A2XRegistryRuntime] blank teammate reservation skipped source=%s: "
                "non-distributed leader runtime",
                source,
            )
            await client.aclose()
            return None
        if not dataset:
            logger.debug(
                "[A2XRegistryRuntime] blank teammate reservation skipped source=%s: missing dataset",
                source,
            )
            await client.aclose()
            return None
        reservation = await client.reserve_blank_agents(
            dataset=dataset,
            n=1,
            ttl_seconds=int(config.get("reservation_ttl_seconds") or 30),
        )
        for agent in reservation.agents:
            if not isinstance(agent, dict):
                continue
            service_id = _agent_service_id(agent)
            endpoint = _agent_endpoint(agent)
            if service_id and endpoint:
                logger.info(
                    "[A2XRegistryRuntime] reserved blank teammate source=%s dataset=%s "
                    "service_id=%s endpoint=%s holder_id=%s",
                    source,
                    dataset,
                    service_id,
                    endpoint,
                    getattr(reservation, "holder_id", ""),
                )
                return ReservedBlankAgent(
                    client=client,
                    reservation=reservation,
                    dataset=dataset,
                    service_id=service_id,
                    endpoint=endpoint,
                )
        logger.info(
            "[A2XRegistryRuntime] no usable blank teammate reservation source=%s dataset=%s",
            source,
            dataset,
        )
        await client.release_reservation(reservation)
        await client.aclose()
        return None
    except Exception as exc:
        logger.warning(
            "[A2XRegistryRuntime] blank teammate reservation failed source=%s: %s",
            source,
            exc,
            exc_info=True,
        )
        if client is not None:
            try:
                await client.aclose()
            except Exception as close_exc:
                logger.debug("[A2XRegistryRuntime] reservation client close failed: %s", close_exc)
        return None


async def replace_teammate_agent_card_after_bootstrap(
    client: Any,
    *,
    dataset: str,
    service_id: str,
    member_name: str,
    source: str = "teammate-bootstrap",
    description: str | None = None,
    status: str | None = None,
    skills: list[dict[str, Any]] | None = None,
) -> bool:
    """Replace the teammate's blank card with its runtime planner card."""
    dataset = str(dataset or "").strip()
    service_id = str(service_id or "").strip()
    member_name = str(member_name or "").strip()
    if not dataset or not service_id or not member_name:
        raise ValueError(
            "[A2XRegistryRuntime] replace teammate card failed: "
            "missing required dataset/service_id/member_name "
            f"(source={source}, dataset={dataset!r}, service_id={service_id!r}, member_name={member_name!r})"
        )
    if client is None:
        raise RuntimeError(
            "[A2XRegistryRuntime] replace teammate card failed: missing A2X client "
            f"(source={source}, dataset={dataset}, service_id={service_id}, member_name={member_name})"
        )

    agent_card = build_teammate_agent_card(member_name)
    if description is not None and str(description).strip():
        agent_card["description"] = str(description).strip()
    if status is not None and str(status).strip():
        agent_card["status"] = str(status).strip()
    if skills is not None:
        agent_card["skills"] = [dict(skill) for skill in skills if isinstance(skill, dict)]

    try:
        await client.replace_agent_card(dataset, service_id, agent_card)
        logger.info(
            "[A2XRegistryRuntime] teammate agent card replaced source=%s dataset=%s service_id=%s member_name=%s",
            source,
            dataset,
            service_id,
            member_name,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[A2XRegistryRuntime] teammate agent card replace failed source=%s "
            "dataset=%s service_id=%s member_name=%s: %s",
            source,
            dataset,
            service_id,
            member_name,
            exc,
            exc_info=True,
        )
        return False



def clear_blank_registration_cache_for_tests() -> None:
    """Clear in-process registration cache for isolated tests."""
    _REGISTERED_BLANK_ENDPOINTS.clear()
    _REGISTERED_BLANK_REGISTRATIONS.clear()
