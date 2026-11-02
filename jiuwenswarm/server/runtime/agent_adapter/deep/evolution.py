"""注册并刷新自演进 rail，并执行自动重建与演进事件推送。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from openjiuwen.harness.rails import unconfigure_skill_evolution

from jiuwenswarm.common.config import (
    get_evolution_enabled,
    get_passive_skill_evolution_triggers,
    get_skill_evolution_enabled,
)
from jiuwenswarm.server.runtime.agent_adapter import (
    evolution_version as evolution_version_ctl,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _generated_metric_id_key,
    _push_auto_rebuild_published_metric,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.stream_parse import (
    _parse_stream_chunk,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    AUTO_REBUILD_SOURCE_MANUAL,
    AUTO_REBUILD_SOURCE_WATCHER,
    EVOLUTION_ACCEPT_LABELS,
    TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES,
    TEAM_EVOLUTION_NOOP_STAGES,
    AutoRebuildJob,
    EvolutionPushContext,
    approve_evolution_records,
    approved_record_ids_from_answers,
    auto_rebuild_job,
    build_evolution_status_update,
    event_payload_dict,
    evolution_meta_from_payload,
    evolution_outcome_from_event,
    is_evolution_approval_event,
    is_evolution_outcome_event,
    merge_evolution_disabled_skills,
    push_evolution_event,
    push_evolution_generated,
    push_evolution_progress,
    push_evolution_status,
    record_ids_from_pending_approval,
    reject_evolution_records,
    snapshot_auto_evolution_metric_items,
    sync_evolution_disabled_skills,
    team_evolution_terminal_progress,
    terminal_stage,
    visible_evolution_progress_from_events,
    visible_regular_evolution_start_progress,
)

logger = logging.getLogger(__name__)


def _build_skill_evolution_rail(adapter, config: dict[str, Any]) -> SkillEvolutionRail | None:
    """Build SkillEvolutionRail.

    Product contract: while the rail is mounted, generated experiences always
    persist (``rail.auto_save=True``). Automatic version merge after
    persistence is gated per-skill by ``resolve_skill_evolution_action``
    (``selfEvolution=auto`` only).
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        EvolutionReviewRuntime,
        SkillEvolutionRail,
    )
    if not get_skill_evolution_enabled(config):
        return None
    from openjiuwen.extensions.observability.demand import (
        get_trajectory_span_processor,
    )

    try:
        evolution_triggers = get_passive_skill_evolution_triggers(config)
        model_name = adapter._default_model_name or config.get("model_name", "gpt-4")
        skill_evolution_rail = SkillEvolutionRail(
            skills_dir=adapter._resolve_skill_dirs(),
            llm=adapter._model,
            model=model_name,
            review_runtime=EvolutionReviewRuntime(),
            review_trigger=evolution_triggers["review_trigger"],
            signal_trigger=evolution_triggers["signal_trigger"],
            auto_save=True,
            disabled_skills=merge_evolution_disabled_skills(
                adapter._skill_manager.list_execution_disabled_skills()
            ),
            trajectory_span_processor=get_trajectory_span_processor(),
        )
        adapter._skill_evolution_rail = skill_evolution_rail
        logger.info("[JiuWenSwarmDeepAdapter] SkillEvolutionRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] SkillEvolutionRail create failed: %s", exc)
        skill_evolution_rail = None
    return skill_evolution_rail


async def _ensure_active_evolution_rails_registered(adapter) -> None:
    """Configure, register, and cache single-agent skill evolution rails."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _set_skill_evolution_triggers,
        configure_skill_evolution_runtime,
        get_passive_skill_evolution_triggers,
    )
    if adapter._instance is None:
        return

    resolved_language = adapter._resolve_runtime_language()
    evolution_triggers = get_passive_skill_evolution_triggers(adapter._config_cache)
    if (
        adapter._skill_evolution_rail is not None
        and getattr(adapter._skill_evolution_rail, "_language", None) != resolved_language
    ):
        await _unconfigure_active_evolution_rails(adapter)

    disabled_skills = merge_evolution_disabled_skills(
        adapter._skill_manager.list_execution_disabled_skills()
        if adapter._skill_manager is not None
        else []
    )
    from openjiuwen.extensions.observability.demand import (
        get_trajectory_span_processor,
    )

    await configure_skill_evolution_runtime(
        adapter._instance,
        skills_dir=adapter._resolve_skill_dirs(),
        llm=adapter._model,
        model=adapter._default_model_name
        or adapter._config_cache.get("model_name", "gpt-4"),
        review_trigger=evolution_triggers["review_trigger"],
        signal_trigger=evolution_triggers["signal_trigger"],
        auto_save=True,
        disabled_skills=disabled_skills,
        language=resolved_language,
        trajectory_span_processor=get_trajectory_span_processor(),
    )
    _refresh_active_evolution_rail_refs(adapter)
    if adapter._skill_evolution_rail is not None:
        sync_evolution_disabled_skills(adapter._skill_evolution_rail, disabled_skills)
        _set_skill_evolution_triggers(
            adapter._skill_evolution_rail,
            review_trigger=evolution_triggers["review_trigger"],
            signal_trigger=evolution_triggers["signal_trigger"],
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillEvolutionRail configured "
            "(review_trigger=%s, signal_trigger=%s)",
            evolution_triggers["review_trigger"],
            evolution_triggers["signal_trigger"],
        )


async def _unconfigure_active_evolution_rails(adapter) -> None:
    """Remove cached single-agent evolution rails before rebuilding them."""
    if adapter._instance is None:
        return

    rails = [
        rail
        for rail in (adapter._skill_evolution_rail, adapter._evolution_interrupt_rail)
        if rail is not None
    ]
    try:
        unconfigure_skill_evolution(adapter._instance, team=False)
    except AttributeError:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] evolution bulk unconfigure unavailable"
        )
    unregister = getattr(adapter._instance, "unregister_rail", None)
    if callable(unregister):
        for rail in rails:
            await unregister(rail)
    stale_rails = getattr(adapter._instance, "_stale_rails", None)
    if isinstance(stale_rails, list):
        removed_ids = {id(rail) for rail in rails}
        adapter._instance._stale_rails = [  # pylint: disable=protected-access
            rail for rail in stale_rails if id(rail) not in removed_ids
        ]
    adapter._skill_evolution_rail = None
    adapter._evolution_interrupt_rail = None


def _refresh_active_evolution_rail_refs(adapter) -> None:
    """Refresh cached rail references after agent-core runtime configure."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        EvolutionInterruptRail,
        SkillEvolutionRail,
        SubagentRail,
    )
    if adapter._instance is None:
        return
    find_rails = getattr(adapter._instance, "find_rails_by_type", None)
    if not callable(find_rails):
        return

    regular_rails = find_rails((SkillEvolutionRail,))
    adapter._skill_evolution_rail = None
    for rail in regular_rails:
        if isinstance(rail, SkillEvolutionRail) and not isinstance(
            rail, EvolutionInterruptRail
        ):
            adapter._skill_evolution_rail = rail
            break
    interrupt_rails = find_rails((EvolutionInterruptRail,))
    adapter._evolution_interrupt_rail = next(iter(interrupt_rails), None)
    subagent_rails = find_rails((SubagentRail,))
    adapter._subagent_rail = next(iter(subagent_rails), None)


def _sync_active_evolution_review_agent_after_reload(adapter) -> None:
    """Restore SkillEvolutionRail-owned review subagent after DeepAgent hot reload."""
    if adapter._instance is None or adapter._skill_evolution_rail is None:
        return
    if not get_evolution_enabled(adapter._config_cache):
        return

    register_review_agent = getattr(
        adapter._skill_evolution_rail,
        "_register_evolution_review_agent",
        None,
    )
    if callable(register_review_agent):
        register_review_agent(adapter._instance)

    _refresh_active_evolution_rail_refs(adapter)


async def _handle_evolution_approval(adapter, request_id: str, answers: list) -> bool:
    """Handle evolution approval via SkillEvolutionRail.on_approve/on_reject.

    Uses the optimizer path: calls rail.on_approve() for accepted records
    which will flush to store and solidify, or rail.on_reject() to discard.
    """
    rail = adapter._skill_evolution_rail
    if rail is None:
        logger.warning("[JiuWenSwarmDeepAdapter] evolution approval failed: no SkillEvolutionRail")
        return False

    record_ids_by_index = record_ids_from_pending_approval(rail, request_id)
    accepted, approved_record_ids = approved_record_ids_from_answers(
        answers,
        EVOLUTION_ACCEPT_LABELS,
        record_ids_by_index,
    )

    if accepted:
        pending_snapshots = getattr(rail, "_pending_approval_snapshots", None)
        pending = (
            pending_snapshots.get(request_id)
            if isinstance(pending_snapshots, dict)
            else None
        )
        skill_for_rebuild = str(getattr(pending, "skill_name", "") or "").strip()
        await approve_evolution_records(
            rail,
            request_id,
            approved_record_ids,
            legacy_fallback=True,
        )
        logger.info("[JiuWenSwarmDeepAdapter] evolution approval accepted: request_id=%s", request_id)
        if skill_for_rebuild:
            _queue_auto_rebuild_skill(
                adapter,
                skill_for_rebuild,
                source=AUTO_REBUILD_SOURCE_MANUAL,
                request_id=request_id,
                push_metrics=False,
            )
            _schedule_pending_auto_rebuild(adapter, request_id)
    else:
        await reject_evolution_records(
            rail,
            request_id,
            legacy_fallback=True,
        )
        logger.info("[JiuWenSwarmDeepAdapter] evolution approval rejected: request_id=%s", request_id)

    return True


async def _handle_governance_approval(
    adapter, request_id: str, answers: list, kind: str
) -> bool:
    """Unified handler for simplify governance approvals."""
    rail = adapter._skill_evolution_rail
    if rail is None:
        logger.warning("[JiuWenSwarmDeepAdapter] governance approval failed: no SkillEvolutionRail")
        return False

    accept_labels = {"执行"} if kind == "simplify" else set()
    accepted = any(
        isinstance(ans, dict)
        and bool(accept_labels & set(ans.get("selected_options", [])))
        for ans in answers
    )

    if kind == "simplify":
        if accepted:
            await rail.on_approve_simplify(request_id)
        else:
            await rail.on_reject_simplify(request_id)

    logger.info(
        "[JiuWenSwarmDeepAdapter] governance %s %s: request_id=%s",
        kind, "accepted" if accepted else "rejected", request_id,
    )
    return True


def _get_disk_evolution_store(adapter):
    return evolution_version_ctl.get_disk_evolution_store(adapter._resolve_skill_dirs())


def _queue_auto_rebuild_skill(
    adapter,
    skill_name: str,
    *,
    source: str = AUTO_REBUILD_SOURCE_MANUAL,
    session_id: str = "",
    channel_id: str | None = None,
    request_id: str = "",
    push_metrics: bool | None = None,
) -> None:
    name = str(skill_name or "").strip()
    if not name:
        return
    if not adapter._should_auto_merge_evolved_skill(name):
        return
    # Keep the first queued delivery context; later sessions must not overwrite.
    if name in adapter._pending_auto_rebuild_jobs:
        return
    adapter._pending_auto_rebuild_jobs[name] = auto_rebuild_job(
        name,
        source=source,
        session_id=session_id,
        channel_id=channel_id,
        request_id=request_id,
        push_metrics=push_metrics,
    )


def _take_pending_auto_rebuild_jobs(adapter) -> list[AutoRebuildJob]:
    pending = list(adapter._pending_auto_rebuild_jobs.values())
    adapter._pending_auto_rebuild_jobs.clear()
    return pending


def _take_pending_auto_rebuild_skills(adapter) -> list[str]:
    """Compatibility drain used by tests; prefer ``_take_pending_auto_rebuild_jobs``."""
    return [job.skill_name for job in _take_pending_auto_rebuild_jobs(adapter)]


def _filter_unreported_generated_items(
    adapter,
    skill_name: str,
    items: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Drop entry ids already pushed from this adapter (full-snapshot guard)."""
    fresh: list[dict[str, str]] = []
    for item in items:
        entry_id = str(item.get("id") or "").strip()
        if not entry_id:
            continue
        key = _generated_metric_id_key(skill_name, entry_id)
        if key in adapter._auto_evolution_generated_ids:
            continue
        adapter._auto_evolution_generated_ids.add(key)
        fresh.append(item)
    return fresh


def _schedule_pending_auto_rebuild(adapter, request_id: str | None = None) -> None:
    """Fire-and-forget version merge for skills queued after experience persist."""
    if not adapter._pending_auto_rebuild_jobs:
        return
    existing = adapter._auto_rebuild_task
    if existing is not None and not existing.done():
        # In-flight runner drains pending under the lock (and re-checks after).
        return
    rid = str(request_id or "auto-rebuild").strip() or "auto-rebuild"
    adapter._auto_rebuild_task = asyncio.create_task(
        _run_auto_rebuild_skills_detached(adapter, request_id=rid),
        name=f"auto-rebuild-{rid}",
    )


async def _skill_has_live_evolution_records(adapter, skill_name: str) -> bool:
    """Return True when disk evolutions.json has at least one live entry."""
    try:
        store = _get_disk_evolution_store(adapter)
        subject = evolution_version_ctl.resolve_subject(store, skill_name)
        evo_log = await store.load_full_evolution_log(
            skill_name,
            subject_kind=str(subject.get("kind") or "skill") or None,
        )
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] could not inspect live evolutions for "
            "skill=%s: %s",
            skill_name,
            exc,
        )
        # Fail open: let generate_evolution_merge_version decide.
        return True
    entries = getattr(evo_log, "entries", None) or []
    return bool(entries)


async def _run_auto_rebuild_skills_detached(adapter, *, request_id: str | None = None) -> None:
    """Background auto version merge gated by per-skill selfEvolution=auto."""
    try:
        async with adapter._auto_rebuild_lock:
            while True:
                jobs = _take_pending_auto_rebuild_jobs(adapter)
                if not jobs:
                    break
                for job in jobs:
                    skill_name = job.skill_name
                    job_request_id = job.request_id or request_id
                    if not adapter._should_auto_merge_evolved_skill(skill_name):
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] skip auto rebuild: request_id=%s "
                            "skill=%s reason=selfEvolution_not_auto",
                            job_request_id,
                            skill_name,
                        )
                        continue
                    if not await _skill_has_live_evolution_records(adapter, skill_name):
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] skip auto rebuild: request_id=%s "
                            "skill=%s reason=no_evolution_records",
                            job_request_id,
                            skill_name,
                        )
                        continue
                    try:
                        result = await adapter.generate_evolution_merge_version(
                            skill_name=skill_name
                        )
                        new_version = ""
                        success = False
                        if isinstance(result, dict):
                            success = bool(result.get("success"))
                            new_version = str(result.get("new_version") or "").strip()
                        if success and new_version:
                            await _push_auto_rebuild_published_metric(
                                job,
                                version=new_version,
                                request_id=job_request_id,
                            )
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] auto rebuild failed: "
                            "request_id=%s skill=%s error=%s",
                            job_request_id,
                            skill_name,
                            exc,
                        )
    finally:
        # Close race: skills queued after last empty take while this task was
        # still marked running (coalesced schedule). Re-arm if needed.
        if adapter._pending_auto_rebuild_jobs:
            _schedule_pending_auto_rebuild(adapter, request_id)


async def _ensure_evolution_rail_for_slash(adapter, mode: str) -> str | None:
    """Check evolution availability for slash commands; lazily init rail if needed.

    Returns None when the rail is (or becomes) available, or an error message string.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_skill_create_enabled,
    )
    # 合并后演进能力在 agent 模式可用（兼容历史 agent.plan token）。
    if mode not in ("agent", "agent.plan"):
        display_mode = str(mode or "当前").strip() or "当前"
        return f"{display_mode} 模式下演进功能不可用。"
    if not get_evolution_enabled(adapter._config_cache):
        return "演进功能未启用。"
    await _ensure_active_evolution_rails_registered(adapter)
    if adapter._skill_evolution_rail is None:
        return "演进功能初始化失败。"

    # SkillCreateRail requires skill_create config
    if get_skill_create_enabled(adapter._config_cache):
        if adapter._skill_create_rail is None:
            adapter._skill_create_rail = adapter._build_skill_create_rail(adapter._config_cache)
    return None


async def _watch_evolution_and_push(adapter, rid: str, cid: str, session_id: str) -> None:
    """Poll passive evolution events and push progress, approval, and terminal status."""
    from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
        TEAM_EVOLUTION_IDLE_SLEEP_SEC,
        build_server_push_message,
        resolve_evolution_event_timeout_sec,
    )

    push_context = EvolutionPushContext(
        transport=WebSocketGatewayPushTransport(),
        channel_id=cid,
        session_id=session_id,
    )

    async def _push_status(status: str, stage: str, message: str = "") -> None:
        await push_evolution_status(
            push_context,
            build_evolution_status_update(rid, status, stage, message),
            build_server_push_message,
            include_payload_request_id=False,
        )

    async def _push_approval(evt) -> None:
        await push_evolution_event(
            push_context,
            rid,
            evt,
            build_server_push_message,
        )

    async def _cleanup_evolution_rail(*, cancel: bool = False) -> None:
        if adapter._skill_evolution_rail is None:
            return
        try:
            rail = adapter._skill_evolution_rail
            if cancel:
                cancel_fn = getattr(rail, "cancel_pending_evolution", None)
                if cancel_fn is not None:
                    await cancel_fn()
                    return
            await rail.cleanup_background_tasks()
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] evolution cleanup failed: request_id=%s "
                "session_id=%s error=%s",
                rid,
                session_id,
                exc,
            )

    try:
        if adapter._skill_evolution_rail is None:
            return

        active = False
        last_event_at = time.monotonic()
        event_timeout_sec = resolve_evolution_event_timeout_sec(
            adapter._skill_evolution_rail,
            fallback_sec=TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
        )

        while True:
            if adapter._skill_evolution_rail is None:
                return

            events = await adapter._skill_evolution_rail.drain_pending_approval_events(wait=False) or []
            if not events:
                idle_for = time.monotonic() - last_event_at
                if idle_for >= event_timeout_sec:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] evolution watcher timed out: "
                        "request_id=%s session_id=%s idle_for=%.1fs",
                        rid,
                        session_id,
                        idle_for,
                    )
                    if active:
                        message = (
                            f"Evolution analysis timed out after "
                            f"{event_timeout_sec:.0f}s without host events"
                        )
                        await _push_status("end", "hidden", message)
                    # Drain any queued auto rebuilds before abandoning the watcher.
                    _schedule_pending_auto_rebuild(adapter, rid)
                    await _cleanup_evolution_rail(cancel=True)
                    return
                await asyncio.sleep(TEAM_EVOLUTION_IDLE_SLEEP_SEC)
                continue
            last_event_at = time.monotonic()

            # Queue + schedule on persist (``auto_approved``) immediately.
            # Direct-LLM rebuild does not share DeepAgent, so do not wait for
            # watcher terminal. Do not treat ``completed`` as persist: SDK may
            # emit stage=completed with skill_name for ``no_evolution_no_records``,
            # and team rail emits completed when a request is merely ready.
            # Per-skill gate: only selfEvolution=auto is queued inside
            # ``_queue_auto_rebuild_skill``. Terminal paths still schedule as a
            # no-op-safe safety net when pending is empty.
            for evt in events:
                payload = event_payload_dict(evt)
                meta = evolution_meta_from_payload(payload) or {}
                skill_name = str(
                    payload.get("skill_name")
                    or meta.get("skill_name")
                    or ""
                ).strip()
                raw_stage = str(
                    payload.get("stage") or meta.get("stage") or ""
                ).strip().lower()
                if skill_name and raw_stage == "auto_approved":
                    try:
                        store = _get_disk_evolution_store(adapter)
                        subject = evolution_version_ctl.resolve_subject(store, skill_name)
                        items = await snapshot_auto_evolution_metric_items(
                            store,
                            skill_name,
                            subject_kind=str(subject.get("kind") or "skill") or None,
                            request_id=rid,
                        )
                        new_items = _filter_unreported_generated_items(
                            adapter,
                            skill_name,
                            items,
                        )
                        if not new_items:
                            logger.warning(
                                "[JiuWenSwarmDeepAdapter] skip generated metrics: "
                                "request_id=%s skill=%s reason=no_new_items "
                                "snapshot_count=%s",
                                rid,
                                skill_name,
                                len(items),
                            )
                        else:
                            await push_evolution_generated(
                                push_context,
                                request_id=rid,
                                skill_name=skill_name,
                                items=new_items,
                                build_push_message=build_server_push_message,
                                source="auto",
                            )
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] auto generated metrics "
                            "push failed: request_id=%s skill=%s error=%s",
                            rid,
                            skill_name,
                            exc,
                        )
                    _queue_auto_rebuild_skill(
                        adapter,
                        skill_name,
                        source=AUTO_REBUILD_SOURCE_WATCHER,
                        session_id=session_id,
                        channel_id=cid,
                        request_id=rid,
                        push_metrics=True,
                    )
                    _schedule_pending_auto_rebuild(adapter, rid)

            visible_progress_statuses = visible_evolution_progress_from_events(events)
            just_started_with_progress = None
            if not active:
                start_progress_statuses = visible_regular_evolution_start_progress(
                    visible_progress_statuses
                )
                if start_progress_statuses:
                    just_started_with_progress = start_progress_statuses[0]

            if just_started_with_progress is not None:
                start_stage = just_started_with_progress.stage
                start_message = just_started_with_progress.message
                await _push_status("start", start_stage, start_message)
                active = True

            await push_evolution_progress(
                push_context,
                rid,
                events,
                parse_stream_chunk=_parse_stream_chunk,
                build_push_message=build_server_push_message,
            )

            progress_statuses_to_push = visible_progress_statuses
            if just_started_with_progress is not None:
                progress_statuses_to_push = [
                    progress_status
                    for progress_status in visible_progress_statuses
                    if progress_status is not just_started_with_progress
                ]
            for progress_status in progress_statuses_to_push:
                if progress_status.terminal:
                    continue
                await _push_status("progress", progress_status.stage, progress_status.message)

            approval_events = [evt for evt in events if is_evolution_approval_event(evt)]
            if approval_events:
                if not active:
                    await _push_status("start", "approval_required", "")
                    active = True
                for evt in approval_events:
                    await _push_approval(evt)
                await _push_status("end", "approval_required", "")
                await _cleanup_evolution_rail()
                return

            outcomes = [
                evolution_outcome_from_event(evt)
                for evt in events
                if is_evolution_outcome_event(evt)
            ]
            if outcomes:
                outcome = outcomes[-1]
                stage = str(outcome.get("status") or "completed").strip().lower()
                message = str(outcome.get("message") or "")
                if stage in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES:
                    end_stage = "hidden"
                else:
                    end_stage = stage or "completed"
                if (
                    not active
                    and (
                        end_stage == "hidden"
                        or end_stage in TEAM_EVOLUTION_NOOP_STAGES
                    )
                ):
                    _schedule_pending_auto_rebuild(adapter, rid)
                    await _cleanup_evolution_rail()
                    return
                if not active:
                    await _push_status("start", end_stage, message)
                    active = True
                await _push_status(
                    "end",
                    end_stage,
                    message or "Evolution analysis completed",
                )
                await _cleanup_evolution_rail()
                # Safety net: primary schedule is on auto_approved (direct LLM).
                _schedule_pending_auto_rebuild(adapter, rid)
                return

            terminal_progress = [
                terminal
                for terminal in (team_evolution_terminal_progress(evt) for evt in events)
                if terminal is not None
            ]
            if terminal_progress:
                terminal = terminal_progress[-1]
                end_stage = terminal_stage(terminal) or "no_evolution_generated"
                if end_stage in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES:
                    end_stage = "hidden"
                if (
                    not active
                    and (
                        end_stage == "hidden"
                        or end_stage in TEAM_EVOLUTION_NOOP_STAGES
                    )
                ):
                    await _cleanup_evolution_rail()
                    _schedule_pending_auto_rebuild(adapter, rid)
                    return
                if not active:
                    await _push_status(
                        "start",
                        end_stage,
                        str(terminal.get("message") or ""),
                    )
                    active = True
                await _push_status(
                    "end",
                    end_stage,
                    str(terminal.get("message") or ""),
                )
                await _cleanup_evolution_rail()
                # Safety net: primary schedule is on auto_approved (direct LLM).
                _schedule_pending_auto_rebuild(adapter, rid)
                return
    except asyncio.CancelledError:
        try:
            await _cleanup_evolution_rail(cancel=True)
        finally:
            raise
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] evolution watcher failed: %s", exc)
        try:
            await _push_status("end", "hidden", "")
        except Exception:
            pass


def _on_evolution_watcher_done(adapter, task: asyncio.Task) -> None:
    """Callback when an evolution watcher task completes.

    Discards the task from the tracking set and logs any exception.
    """
    adapter._evolution_watcher_tasks.discard(task)
    try:
        task.result()
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] evolution watcher task exception: %s", exc)
