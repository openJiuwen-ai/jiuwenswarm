# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-owned topic and prompt recommendation runtime.

Gateway forwards recommendation.job and broadcasts the events this host pushes.
The pipeline, recommendation database, session history and model resolution stay
in this process, which is allowed to import agents.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

_ACTIVE_HOST: "RecommendationHost | None" = None


def active_recommendation_host() -> "RecommendationHost | None":
    return _ACTIVE_HOST


def activate_recommendation_host(host: "RecommendationHost") -> None:
    global _ACTIVE_HOST
    _ACTIVE_HOST = host


def deactivate_recommendation_host(host: "RecommendationHost") -> None:
    global _ACTIVE_HOST
    if _ACTIVE_HOST is host:
        _ACTIVE_HOST = None


def _no_active_request_ids() -> Iterable[str]:
    """No request stream is bound until the AgentServer binds one."""
    return ()


def _build_topic_starter_services(
    topic_starter_store: Any,
    *,
    active_request_ids_loader: Callable[[], Iterable[str]] | None = None,
) -> tuple[Any, Any | None, Any]:
    """Always build the prepare lane; leave a misconfigured product lane disabled."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_collector import (
        TopicStarterOutcomeCollector,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls,
        permitted_recommendation_history,
        recommendation_model_config,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
        load_all_session_records,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        TopicStarterStoreOutcomeSink,
        migrate_dismissal_records,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        TopicPipeline,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_prepare import (
        TopicPreparePipeline,
        TopicPrepareService,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_rid import (
        build_ark_specific_interest_matcher,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        OpenJiuwenNativeSearchContractError,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_service import (
        TopicService,
    )
    from jiuwenswarm.server.runtime.session.session_history import load_history_records

    def permitted_records():
        # One consent read per batch rather than one per session.
        control = load_recommendation_controls()
        return {sid: permitted_recommendation_history(rows, control)
                for sid, rows in load_all_session_records().items()}

    topic_prepare = TopicPreparePipeline(topic_starter_store, records_loader=permitted_records)
    topic_prepare_service = TopicPrepareService(
        topic_starter_store,
        generate=topic_prepare.generate_prepared,
        prepare_state=topic_prepare.refresh_state,
    )
    topic_outcome_queue = NewPageOutcomeQueue()
    topic_outcome_queue.attach_sink(
        TopicStarterStoreOutcomeSink(topic_starter_store)
    )

    try:
        topic_pipeline = TopicPipeline(
            topic_starter_store,
            records_loader=permitted_records,
            active_request_ids_loader=active_request_ids_loader,
        )
        search_provider_id = topic_pipeline.search_provider_id
    except OpenJiuwenNativeSearchContractError as exc:
        logger.warning(
            "[Topic] disabled at startup: %s",
            exc,
        )
        return (
            topic_prepare_service,
            None,
            topic_outcome_queue,
        )

    # Bounds the cross-thread wait for one RID pair, so a stalled loop cannot wedge collection.
    summary_pair_match_timeout_seconds = 60.0
    outcome_collection_lock = asyncio.Lock()

    async def prepare_topic_outcomes() -> str:
        async with outcome_collection_lock:
            # Pairs on the service's model; a switch cancels this round and the next one pairs anew.
            ark_config = topic_service.ark_config
            stop_collection = threading.Event()
            matcher_tasks: set[asyncio.Task] = set()
            matcher = None
            if ark_config is not None:
                try:
                    matcher = build_ark_specific_interest_matcher(
                        topic_starter_store,
                        ark_config,
                    )
                except Exception as exc:  # noqa: BLE001 - fail-closed label exclusion
                    logger.warning(
                        "[Topic] RID summary-pair matcher unavailable: %s",
                        exc,
                    )
            collector_kwargs: dict[str, Any] = {}
            if matcher is not None:
                loop = asyncio.get_running_loop()

                async def match_if_running(earlier: Any, later: Any) -> bool:
                    if stop_collection.is_set():
                        raise asyncio.CancelledError()
                    task = asyncio.current_task()
                    matcher_tasks.add(task)
                    try:
                        return await matcher.match(earlier, later)
                    finally:
                        matcher_tasks.discard(task)

                def match_summary_pair(earlier: Any, later: Any) -> bool:
                    if stop_collection.is_set():
                        raise asyncio.CancelledError()
                    future = asyncio.run_coroutine_threadsafe(
                        match_if_running(earlier, later),
                        loop,
                    )
                    # This thread is not the loop: a loop that stops scheduling (shutdown,
                    # saturated executor) would otherwise block it forever and keep
                    # `asyncio.shield(worker)` from ever completing.
                    try:
                        result = future.result(
                            timeout=summary_pair_match_timeout_seconds
                        )
                    except TimeoutError:
                        future.cancel()
                        raise
                    if not isinstance(result, bool):
                        raise ValueError("RID summary-pair matcher returned invalid output")
                    return result

                collector_kwargs = {
                    "summary_pair_matcher": match_summary_pair,
                    "summary_pair_matcher_id": matcher.matcher_id,
                    "summary_pair_matcher_version": matcher.matcher_version,
                }
            topic_outcome_collector = TopicStarterOutcomeCollector(
                topic_starter_store,
                history_loader=lambda sid: permitted_recommendation_history(load_history_records(sid)),
                cancel_event=stop_collection,
                **collector_kwargs,
            )
            logger.info("[Topic] outcome collection started")
            worker = asyncio.create_task(asyncio.to_thread(topic_outcome_collector.collect))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop_collection.set()
                tasks = list(matcher_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(worker, return_exceptions=True)
                logger.info("[Topic] outcome collection stopped")
                raise
            finally:
                if matcher is not None:
                    await matcher.aclose()
            # A failed round propagates; the service logs it as failed.
            logger.info(
                "[Topic] outcome collection %s",
                "stopped" if stop_collection.is_set() else "finished",
            )
        return ""

    def history_project_ids() -> list[str]:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        records = topic_pipeline.completed_records_loader()
        project_ids: set[str] = set()
        # Only completed sessions contribute, so read their metadata one by one: the batch
        # loader materialises every session on the machine just to read one field.
        for session_id, completed in records.items():
            if not completed:
                continue
            project_id = get_session_metadata(
                str(session_id), enable_writeback=False,
            ).get("project_id")
            if project_id not in (None, "", "default", "default_code"):
                project_ids.add(str(project_id))
        return sorted(project_ids)

    # Dismissals written before strikes were counted take their time from the ledger, once.
    migrate_dismissal_records(topic_starter_store, now=time.time())
    topic_service = TopicService(
        topic_starter_store,
        generate=topic_pipeline.generate,
        refresh_news=topic_pipeline.refresh_news,
        history_project_ids_loader=history_project_ids,
        search_provider_id=search_provider_id,
        prepare_state=topic_pipeline.refresh_state,
        prepare_short_intent=topic_pipeline.refresh_short_intent,
        prepare_outcomes=prepare_topic_outcomes,
        input_revision_loader=topic_pipeline.scope_input_revision,
        pending_request_loader=topic_pipeline.scope_has_pending_request,
        config_loader=recommendation_model_config,
        enabled=bool(load_recommendation_controls()["enabled"]),
    )
    topic_pipeline.on_candidates_ready = topic_service.candidate_generation_ready
    return (
        topic_prepare_service,
        topic_service,
        topic_outcome_queue,
    )


def _seed_topic_starter_services(
    topic_prepare_service: Any,
    topic_service: Any | None,
    run_id: str,
) -> None:
    if topic_service is not None:
        topic_service.schedule_seed(run_id)


async def _topic_starter_seed_loop(seed: Callable[[str], None]) -> None:
    """Refresh topic pools on this process, without entering the cron job queue.

    The first wake is shortly after AgentServer starts. Later wakes are the next
    local midnight. A disabled recommendation switch returns immediately inside
    ``seed``.
    """

    from datetime import datetime, timedelta, timezone

    started = False
    while True:
        if not started:
            await asyncio.sleep(5.0)
        else:
            now = datetime.now(timezone.utc).astimezone()
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            await asyncio.sleep(max(0.0, (next_midnight - now).total_seconds()))
        started = True
        try:
            seed(f"topic-starter-v6:{int(time.time())}")
        except Exception as exc:  # noqa: BLE001 - isolated scheduled wake
            logger.warning("[Topic] starter seed wake failed: %s", exc)


async def _shutdown_topic_starter_services(
    topic_prepare_service: Any,
    topic_service: Any | None,
) -> None:
    await topic_prepare_service.shutdown()
    if topic_service is not None:
        await topic_service.shutdown()


class RecommendationHost:
    """Owns the topic pipeline and answers recommendation.job for one AgentServer."""

    def __init__(self) -> None:
        self.store: Any = None
        self.prompt_store: Any = None
        self.topic_prepare_service: Any = None
        self.topic_service: Any = None
        self.topic_outcome_queue: Any = None
        self.legacy_topic_service: Any = None
        self.legacy_tasks: set[asyncio.Task] = set()
        self._services_built = False
        self._seed_task: asyncio.Task[None] | None = None
        self._active_request_ids: Callable[[], Iterable[str]] = _no_active_request_ids
        self._push: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def bind_active_requests(self, loader: Callable[[], Iterable[str]]) -> None:
        self._active_request_ids = loader

    def bind_push(self, push: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._push = push

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        push = self._push
        if push is not None:
            await push(event_type, payload)

    def ensure_services(self) -> None:
        if self._services_built:
            return
        from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
            TopicStarterStore,
        )
        from jiuwenswarm.common.schema.message import TOPIC_STARTERS_READY_EVENT
        from jiuwenswarm.common.utils import get_agent_workspace_dir

        self.store = TopicStarterStore(get_agent_workspace_dir() / "recommendation.db")
        prepare, service, queue = _build_topic_starter_services(
            self.store,
            active_request_ids_loader=lambda: self._active_request_ids(),
        )
        self.topic_prepare_service = prepare
        self.topic_service = service
        self.topic_outcome_queue = queue
        if service is not None:
            def _on_prepared(context_scope: str, state: str, pool_written: bool) -> None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(self.emit(
                    TOPIC_STARTERS_READY_EVENT,
                    {
                        "context_scope": context_scope,
                        "state": state,
                        "pool_written": pool_written,
                    },
                ))

            service.on_prepared = _on_prepared
        self._services_built = True

    async def start(self) -> None:
        self.ensure_services()
        if self._seed_task is None:
            self._seed_task = asyncio.create_task(
                _topic_starter_seed_loop(
                    lambda run_id: _seed_topic_starter_services(
                        self.topic_prepare_service,
                        self.topic_service,
                        run_id,
                    ),
                )
            )

    async def shutdown(self) -> None:
        task = self._seed_task
        self._seed_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        pending = tuple(self.legacy_tasks)
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.topic_prepare_service is not None:
            await _shutdown_topic_starter_services(
                self.topic_prepare_service,
                self.topic_service,
            )
        self._services_built = False

    def on_chat_completed(self, session_id: str, request_id: str) -> None:
        self.ensure_services()
        service = self.topic_service
        if service is None:
            return
        from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
            scope_for_project,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False,
        ) or {}
        project_id = metadata.get("project_id")
        service.schedule_successor(
            context_scope="global_new_chat",
            project_id=None,
            visit_id=request_id,
        )
        if project_id not in (None, "", "default", "default_code"):
            service.schedule_successor(
                context_scope=scope_for_project(str(project_id)),
                project_id=project_id,
                visit_id=request_id,
            )

    async def session_left_project(self, project_id: str, session_id: str) -> None:
        if not project_id or project_id in {"default", "default_code"}:
            return
        self.ensure_services()
        service = self.topic_service
        if service is None or not service.configuration_status()["enabled"]:
            return
        from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
            scope_for_project,
        )
        from jiuwenswarm.common.schema.message import TOPIC_STARTERS_READY_EVENT

        context_scope = scope_for_project(project_id)
        await self.emit(
            TOPIC_STARTERS_READY_EVENT,
            {"context_scope": context_scope, "state": "ready"},
        )
        service.schedule_successor(
            context_scope=context_scope,
            project_id=project_id,
            visit_id=f"session-left-project:{session_id}",
        )

    async def handle(
        self,
        *,
        action: str,
        params: dict[str, Any],
        session_id: str,
        request_id: str,
    ) -> Any:
        from jiuwenswarm.server.runtime.recommendation_rpc import dispatch_recommendation

        return await dispatch_recommendation(
            self,
            action=action,
            params=params,
            session_id=session_id,
            request_id=request_id,
        )
