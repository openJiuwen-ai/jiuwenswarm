# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recommendation panel RPCs. AgentServer runs them; gateway only forwards."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.schema.message import (
    TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
)

logger = logging.getLogger(__name__)


@dataclass
class RecommendationResult:
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None
    code: str | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _ok(
    payload: dict[str, Any],
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> RecommendationResult:
    return RecommendationResult(ok=True, payload=payload, events=list(events or []))


def _fail(error: str, code: str) -> RecommendationResult:
    return RecommendationResult(ok=False, error=error, code=code)


def _prompt_store(host: Any) -> Any:
    from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    path = prompt_suggestion.get_agent_workspace_dir() / "recommendation.db"
    store = host.prompt_store
    if store is None or store.path != path:
        store = TopicStarterStore(path)
        host.prompt_store = store
    return store


def _history_store(host: Any) -> Any:
    host.ensure_services()
    if host.store is not None:
        return host.store
    return _prompt_store(host)


async def dispatch_recommendation(
    host: Any,
    *,
    action: str,
    params: dict[str, Any],
    session_id: str,
    request_id: str,
) -> RecommendationResult:
    params.pop("action", None)
    auth_session = params.pop("auth_session", None)
    if action == "prompt_suggestion_event":
        return await _prompt_suggestion_event(host, params)
    if action == "status":
        host.ensure_services()
        return _ok(await _status_payload(host, {"auth_session": auth_session}))
    if action == "select_model":
        host.ensure_services()
        return await _select_model(host, params, auth_session)
    if action == "profile":
        host.ensure_services()
        return _profile(host)
    if action == "restore_direction":
        host.ensure_services()
        return _restore_direction(host, params)
    if action == "control":
        host.ensure_services()
        return await _control(host, params, auth_session)
    if action == "get":
        host.ensure_services()
        return await _get(host, params, session_id)
    if action == "foreground":
        host.ensure_services()
        return _foreground(host, params, session_id)
    if action == "event":
        host.ensure_services()
        return await _event(host, params, session_id, request_id)
    if action == "apply_saved_config":
        host.ensure_services()
        await _apply_saved_config(host, params)
        return _ok({"synced": True})
    return _fail("unknown recommendation action", "BAD_REQUEST")


async def _prompt_suggestion_event(host: Any, params: dict[str, Any]) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls,
    )

    if not load_recommendation_controls()["enabled"]:
        return _ok({"recorded": False, "reason": "disabled"})
    if params.get("event_type") == "input_changed":
        target_session = params.get("session_id")
        if not isinstance(target_session, str) or not target_session.strip():
            return _fail("session_id is required", "BAD_REQUEST")
        try:
            cancelled = prompt_suggestion.forks.cancel_suggestion(target_session.strip())
        except Exception:  # noqa: BLE001 - a failed cancel still answers the page
            logger.warning(
                "[PromptSuggestion] cancel failed: session_id=%s",
                target_session,
                exc_info=True,
            )
            cancelled = False
        return _ok({"cancelled": cancelled})
    recorded = prompt_suggestion.record_page_outcome(_prompt_store(host), params)
    if recorded is None:
        return _fail("unknown suggestion or outcome", "BAD_REQUEST")
    return _ok({"recorded": recorded})


async def _status_payload(host: Any, params: dict[str, Any]) -> dict[str, Any]:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls,
        recommendation_model_config,
        recommendation_model_options,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        background_configuration_status,
    )

    service = host.topic_service
    payload = (
        service.configuration_status()
        if service is not None
        else background_configuration_status(recommendation_model_config())
    )
    auth_session = params.get("auth_session") or None
    models = await asyncio.to_thread(recommendation_model_options, auth_session)
    return {**payload, **models, "enabled": load_recommendation_controls()["enabled"]}


async def _select_model(
    host: Any, params: dict[str, Any], auth_session: Any,
) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        save_recommendation_model,
    )

    model = params.get("model")
    if not isinstance(model, str) or not save_recommendation_model(model):
        return _fail("model cannot prepare topics", "BAD_REQUEST")
    if host.topic_service is not None:
        await host.topic_service.reload_config()
    return _ok(await _status_payload(host, {"auth_session": auth_session}))


def _profile(host: Any) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        inspect_recommendation_profile,
    )

    return _ok(inspect_recommendation_profile(_history_store(host), now=time.time()))


def _restore_direction(host: Any, params: dict[str, Any]) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        inspect_recommendation_profile,
        restore_dismissed_direction,
    )

    direction_id = params.get("direction_id")
    store = _history_store(host)
    if not isinstance(direction_id, str) or not restore_dismissed_direction(
        store, direction_id, now=time.time(),
    ):
        return _fail("no such permanent direction", "BAD_REQUEST")
    return _ok(inspect_recommendation_profile(store, now=time.time()))


async def _control(
    host: Any, params: dict[str, Any], auth_session: Any,
) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.switches import (
        save_recommendation_switches,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        save_recommendation_controls,
    )

    control_action = params.get("control_action")
    if (
        control_action not in {"enable", "disable", "delete"}
        or not isinstance(params.get("include_history", False), bool)
    ):
        return _fail("invalid recommendation control", "BAD_REQUEST")
    action = control_action
    save_recommendation_switches(topic_enabled=action == "enable", now=time.time())
    if action != "enable":
        tasks = tuple(host.legacy_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if action != "enable" and host.topic_prepare_service is not None:
        await host.topic_prepare_service.shutdown()
    include_history = params.get("include_history", False)
    if host.topic_service is not None:
        if action == "delete":
            await host.topic_service.delete_data()
        else:
            await host.topic_service.set_enabled(
                action == "enable", include_history=include_history,
            )
    else:
        save_recommendation_controls(
            enabled=action == "enable",
            include_history=include_history,
            deleted=action == "delete",
            now=time.time(),
        )
        if action == "delete":
            _history_store(host).delete_recommendation_data()
    return _ok(
        await _status_payload(host, {"auth_session": auth_session}),
        [(TOPIC_STARTERS_CONTROL_CHANGED_EVENT, {"enabled": action == "enable"})],
    )


async def _get(host: Any, params: dict[str, Any], session_id: str) -> RecommendationResult:
    received_at = time.time()
    requested_session_id = str(params.get("session_id") or session_id or "").strip()
    if not requested_session_id:
        return _fail("session_id is required", "BAD_REQUEST")
    from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
        POLICY_ID as PREPARE_POLICY_ID,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        project_id_for_scope,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        POLICY_ID as TOPIC_POLICY_ID,
    )

    policy_id = str(params.get("policy_id") or "").strip()
    if policy_id in {"", "topic-starter-mvp-v4"}:
        task = asyncio.current_task()
        host.legacy_tasks.add(task)
        try:
            from jiuwenswarm.agents.harness.common.recommendation import topic_history
            from jiuwenswarm.common.config import get_config, get_default_models
            from jiuwenswarm.server.runtime.opencode_zen import get_zen_free_model_entries

            config = get_config()
            payload = await topic_history.run_history_topic_get(
                params,
                store=_history_store(host),
                received_at=received_at,
                config_entries=get_default_models(config),
                zen_entries=get_zen_free_model_entries(),
                context_engine_config=(
                    (config.get("react", {}) or {}).get("context_engine_config", {}) or {}
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] get failed: %s", exc)
            payload = {"available": False, "reason": "topic_starter_unavailable"}
        finally:
            host.legacy_tasks.discard(task)
        return _ok(payload)
    if policy_id not in {PREPARE_POLICY_ID, TOPIC_POLICY_ID}:
        return _fail("unsupported policy_id", "BAD_REQUEST")
    context_scope = str(params.get("context_scope") or "")
    project_id = params.get("project_id")
    if policy_id == PREPARE_POLICY_ID:
        failed = (
            ("context_scope" in params and context_scope != GLOBAL_NEW_CHAT_SCOPE)
            or ("project_id" in params and project_id is not None)
        )
        if failed:
            return _fail(
                "prepare lane only supports global_new_chat without project_id",
                "BAD_REQUEST",
            )
        try:
            if host.topic_prepare_service is None:
                payload = {
                    "available": False,
                    "reason": "prepare_service_unavailable",
                    "session_id": requested_session_id,
                    "policy_id": PREPARE_POLICY_ID,
                }
            else:
                payload = dict(host.topic_prepare_service.serve(requested_session_id))
            payload.update({
                "session_id": requested_session_id,
                "policy_id": PREPARE_POLICY_ID,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicPrepare] pool read failed: %s", exc)
            payload = {
                "available": False,
                "reason": "topic_starter_unavailable",
                "session_id": requested_session_id,
                "policy_id": PREPARE_POLICY_ID,
            }
        return _ok(payload)
    try:
        if project_id_for_scope(context_scope) != project_id:
            raise ValueError("project mismatch")
    except ValueError:
        return _fail("invalid context_scope or project_id", "BAD_REQUEST")
    try:
        if host.topic_service is None:
            payload = {
                "available": False,
                "reason": "topic_service_unavailable",
                "session_id": requested_session_id,
                "policy_id": TOPIC_POLICY_ID,
                "context_scope": context_scope,
                "project_id": project_id,
            }
        else:
            if host.topic_service.ark_config is None:
                try:
                    await host.topic_service.reload_config()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Topic] config reload on read failed: %s", exc)
            from jiuwenswarm.agents.harness.common.recommendation.switches import (
                is_topic_recommendation_enabled,
            )

            wanted = is_topic_recommendation_enabled()
            if bool(host.topic_service.configuration_status()["enabled"]) != wanted:
                await host.topic_service.set_enabled(wanted, persist=False)
            if params.get("manual_retry") is True:
                host.topic_service.retry(
                    context_scope=context_scope,
                    project_id=project_id,
                    visit_id=requested_session_id,
                )
            payload = host.topic_service.serve(
                requested_session_id,
                context_scope=context_scope,
                project_id=project_id,
                **({"slate_id": params["slate_id"]} if params.get("slate_id") else {}),
            )
            if context_scope == GLOBAL_NEW_CHAT_SCOPE:
                host.topic_service.schedule_news_check(requested_session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Topic] pool read failed: %s", exc)
        payload = {
            "available": False,
            "reason": "topic_starter_unavailable",
            "session_id": requested_session_id,
            "policy_id": TOPIC_POLICY_ID,
            "context_scope": context_scope,
            "project_id": project_id,
        }
    return _ok(payload)


def _foreground(host: Any, params: dict[str, Any], session_id: str) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls,
    )

    scheduled = False
    if (
        load_recommendation_controls()["enabled"]
        and host.topic_service is not None
        and params.get("context_scope") == "global_new_chat"
    ):
        visit_id = str(params.get("visit_id") or session_id or "").strip() or "foreground"
        scheduled = host.topic_service.schedule_news_check(visit_id) is not None
    return _ok({"scheduled": scheduled})


async def _event(
    host: Any,
    params: dict[str, Any],
    session_id: str,
    request_id: str,
) -> RecommendationResult:
    event = dict(params)
    event["session_id"] = str(event.get("session_id") or session_id or "").strip()
    if (
        not event["session_id"]
        or not str(event.get("slate_id") or "").strip()
        or not str(event.get("event_type") or "").strip()
    ):
        return _fail("session_id, slate_id and event_type are required", "BAD_REQUEST")
    policy_id = str(event.get("policy_id") or "").strip()
    selection_mode = str(event.get("selection_mode") or "").strip()
    from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
        POLICY_ID as PREPARE_POLICY_ID,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        project_id_for_scope,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        POLICY_ID as TOPIC_POLICY_ID,
    )

    if policy_id in {"topic-starter-mvp-v3", "topic-starter-mvp-v4"}:
        if "profile_version" not in event or selection_mode not in {
            "static_fallback",
            "personalized_dynamic",
            "generic_dynamic",
        }:
            return _fail(
                "profile_version, policy_id and selection_mode are required",
                "BAD_REQUEST",
            )
        event["policy_id"] = policy_id
        event["selection_mode"] = selection_mode
        event.setdefault("propensity", None)
        event.setdefault("propensity_reason", "not_estimated")
        event.setdefault("created_at", time.time())
        try:
            from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
                build_default_topic_starter_service,
            )

            if host.legacy_topic_service is None:
                host.legacy_topic_service = build_default_topic_starter_service()
            recorded = host.legacy_topic_service.record_event(event)
            return _ok({"recorded": bool(recorded)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicStarter] event failed: %s", exc)
            return _fail("failed to record event", "INTERNAL_ERROR")
    if policy_id not in {PREPARE_POLICY_ID, TOPIC_POLICY_ID}:
        return _fail("unsupported policy_id", "BAD_REQUEST")
    if policy_id == PREPARE_POLICY_ID:
        return _prepare_event(host, event, selection_mode, request_id)
    return _product_event(host, event, selection_mode)


def _prepare_event(
    host: Any,
    event: dict[str, Any],
    selection_mode: str,
    request_id: str,
) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
    )

    if selection_mode not in {"personalized_dynamic", "mixed_dynamic", "generic_dynamic"}:
        return _fail("selection_mode is required", "BAD_REQUEST")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return _fail("payload must be object", "BAD_REQUEST")
    context_scope = str(payload.get("context_scope") or "")
    project_id = payload.get("project_id")
    failed = (
        ("context_scope" in payload and context_scope != GLOBAL_NEW_CHAT_SCOPE)
        or ("project_id" in payload and project_id is not None)
    )
    if failed:
        return _fail(
            "prepare lane only supports global_new_chat without project_id",
            "BAD_REQUEST",
        )
    manifest_id = str(payload.get("manifest_id") or "").strip()
    generation_batch_id = str(payload.get("generation_batch_id") or "").strip()
    if not manifest_id or not generation_batch_id:
        return _fail("manifest_id and generation_batch_id are required", "BAD_REQUEST")
    recorded = False
    event_type = str(event.get("event_type") or "").strip()
    store = getattr(host.topic_prepare_service, "store", None)
    if store is not None:
        from jiuwenswarm.agents.harness.common.recommendation.topic_feedback import (
            record_v5_exposure,
            record_v5_interaction,
            record_v5_slate_event,
        )

        if event_type == "dynamic_exposure":
            raw_topics = payload.get("topics")
            cards = None
            if isinstance(raw_topics, list) and all(isinstance(item, dict) for item in raw_topics):
                cards = [
                    {"card_revision_id": str(item.get("card_revision_id") or "").strip()}
                    for item in raw_topics
                ]
            recorded = record_v5_exposure(
                store,
                session_id=event["session_id"],
                slate_id=str(event["slate_id"]),
                served={
                    "manifest_id": manifest_id,
                    "generation_batch_id": generation_batch_id,
                    "cards": cards,
                },
                created_at=time.time(),
            )
        elif event_type in {"topic_click", "not_interested"}:
            card_revision_id = str(payload.get("card_revision_id") or "").strip()
            if card_revision_id and card_revision_id == str(event.get("topic_id") or "").strip():
                recorded = record_v5_interaction(
                    store,
                    event_type=event_type,
                    session_id=event["session_id"],
                    slate_id=str(event["slate_id"]),
                    manifest_id=manifest_id,
                    card_revision_id=card_revision_id,
                    generation_batch_id=generation_batch_id,
                    request_id=str(request_id),
                    created_at=time.time(),
                )
                if recorded and event_type == "not_interested" and host.topic_prepare_service is not None:
                    host.topic_prepare_service.schedule_successor()
        elif event_type in {"post_exposure_direct", "page_left", "session_bound"}:
            recorded = record_v5_slate_event(
                store,
                event_type=event_type,
                session_id=event["session_id"],
                slate_id=str(event["slate_id"]),
                manifest_id=manifest_id,
                generation_batch_id=generation_batch_id,
                created_at=time.time(),
            )
    return _ok({"recorded": bool(recorded)})


def _product_event(host: Any, event: dict[str, Any], selection_mode: str) -> RecommendationResult:
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        project_id_for_scope,
    )

    if selection_mode not in {"personalized_dynamic", "mixed_dynamic", "generic_dynamic"}:
        return _fail("selection_mode is required", "BAD_REQUEST")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return _fail("payload must be object", "BAD_REQUEST")
    context_scope = str(payload.get("context_scope") or "")
    project_id = payload.get("project_id")
    try:
        if project_id_for_scope(context_scope) != project_id:
            raise ValueError("project mismatch")
    except ValueError:
        return _fail("invalid context_scope or project_id", "BAD_REQUEST")
    manifest_id = str(payload.get("manifest_id") or "").strip()
    generation_batch_id = str(payload.get("generation_batch_id") or "").strip()
    if not manifest_id or not generation_batch_id:
        return _fail("manifest_id and generation_batch_id are required", "BAD_REQUEST")
    recorded = False
    event_type = str(event.get("event_type") or "").strip()
    service = host.topic_service
    if event_type == "dynamic_exposure" and service is not None:
        recorded = service.record_exposure(
            {
                "session_id": event["session_id"],
                "slate_id": str(event["slate_id"]),
                "manifest_id": manifest_id,
                "generation_batch_id": generation_batch_id,
                "context_scope": context_scope,
                "project_id": project_id,
                "exposure_slice_id": str(payload.get("exposure_slice_id") or ""),
                "topics": payload.get("topics"),
            },
            created_at=time.time(),
        )
    elif event_type in {
        "topic_click",
        "not_interested",
        "interest_paused",
        "not_interested_undone",
        "interest_pause_undone",
    } and service is not None and host.topic_outcome_queue is not None:
        return _ok(service.record_interaction(
            event,
            outcome_queue=host.topic_outcome_queue,
            created_at=time.time(),
        ))
    elif event_type in {"post_exposure_direct", "page_left"} and service is not None:
        from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
            record_terminal_slate_outcome,
        )

        recorded = record_terminal_slate_outcome(
            service.store,
            event_type=event_type,
            manifest_id=manifest_id,
            generation_batch_id=generation_batch_id,
            exposure_id=str(event["slate_id"]),
            context_scope=context_scope,
            project_id=project_id,
            created_at=time.time(),
        )
    elif event_type == "session_bound" and service is not None:
        card_revision_id = str(payload.get("card_revision_id") or "").strip()
        if card_revision_id and card_revision_id == str(event.get("topic_id") or "").strip():
            from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
                record_terminal_session_start,
            )

            recorded = record_terminal_session_start(
                service.store,
                manifest_id=manifest_id,
                generation_batch_id=generation_batch_id,
                exposure_id=str(event["slate_id"]),
                card_revision_id=card_revision_id,
                context_scope=context_scope,
                project_id=project_id,
                started_session_id=str(event["session_id"]),
                created_at=time.time(),
            )
    return _ok({"recorded": bool(recorded)})


async def _apply_saved_config(host: Any, params: dict[str, Any]) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.switches import (
        is_topic_recommendation_enabled,
    )

    service = host.topic_service
    if service is None:
        return
    updated = set(params.get("updated_env_keys") or [])
    scopes = set(params.get("reload_scopes") or [])
    if "topic_recommendation_enabled" not in updated and "model" not in scopes:
        return
    enabled_now = None
    if "topic_recommendation_enabled" in updated:
        enabled_now = is_topic_recommendation_enabled(params.get("config"))
        if enabled_now is False:
            try:
                await service.set_enabled(False, persist=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Topic] enable sync on config save failed: %s", exc)
    try:
        await service.reload_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Topic] model reload on config save failed: %s", exc)
    if enabled_now is True:
        try:
            await service.set_enabled(True, persist=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Topic] enable sync on config save failed: %s", exc)
    if enabled_now is not None:
        await host.emit(
            TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
            {"enabled": enabled_now},
        )
