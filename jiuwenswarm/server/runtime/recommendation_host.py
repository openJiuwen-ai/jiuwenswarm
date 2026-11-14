# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-owned topic and prompt recommendation runtime.

Gateway forwards recommendation.job and broadcasts the events this host pushes.
The pipeline, recommendation database, session history and model resolution stay
in this process, which is allowed to import agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
    UNCONFIGURED_SEARCH_PROVIDER_ID,
)

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


def _build_topic_starter_services(
    topic_starter_store: Any,
    *,
    prepare_service: Any = None,
    outcome_queue: Any = None,
) -> tuple[Any, Any | None, Any]:
    """Always build the prepare lane; leave a misconfigured product lane disabled."""

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
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        OpenJiuwenNativeSearchContractError,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_service import (
        TopicService,
    )

    def permitted_records():
        # One consent read per batch rather than one per session.
        control = load_recommendation_controls()
        return {sid: permitted_recommendation_history(rows, control)
                for sid, rows in load_all_session_records().items()}

    topic_prepare_service = prepare_service
    if topic_prepare_service is None:
        topic_prepare = TopicPreparePipeline(topic_starter_store, records_loader=permitted_records)
        topic_prepare_service = TopicPrepareService(
            topic_starter_store,
            generate=topic_prepare.generate_prepared,
            prepare_state=topic_prepare.refresh_state,
        )
    topic_outcome_queue = outcome_queue
    if topic_outcome_queue is None:
        topic_outcome_queue = NewPageOutcomeQueue()
        topic_outcome_queue.attach_sink(
            TopicStarterStoreOutcomeSink(topic_starter_store)
        )

    topic_pipeline = TopicPipeline(
        topic_starter_store,
        records_loader=permitted_records,
    )
    try:
        search_provider_id = topic_pipeline.search_provider_id
    except OpenJiuwenNativeSearchContractError as exc:
        # Web search is optional enrichment. Without it the product lane still
        # writes personalized host-evidence and hot-list pools; a later configured
        # lane changes the compatibility key and rebuilds.
        logger.warning(
            "[Topic] web search enrichment unavailable; "
            "personalized and hot-list pools still generate: %s",
            exc,
        )
        search_provider_id = UNCONFIGURED_SEARCH_PROVIDER_ID

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
        # Training / outcome labels are not required for live pools; keep them
        # out of recommendation.db and investigate generation via process logs.
        prepare_outcomes=None,
        input_revision_loader=topic_pipeline.scope_input_revision,
        config_loader=recommendation_model_config,
        enabled=bool(load_recommendation_controls()["enabled"]),
    )
    topic_pipeline.on_candidates_ready = topic_service.candidate_generation_ready
    return (
        topic_prepare_service,
        topic_service,
        topic_outcome_queue,
    )


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
        self._push: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def bind_push(self, push: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._push = push

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        push = self._push
        if push is not None:
            await push(event_type, payload)

    def ensure_services(self) -> None:
        if self._services_built:
            if self.topic_service is not None:
                return
            from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
                OpenJiuwenNativeSearchContractError,
                selected_topic_search_provider_id,
            )

            try:
                selected_topic_search_provider_id()
            except OpenJiuwenNativeSearchContractError:
                return
        from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
            TopicStarterStore,
        )
        from jiuwenswarm.common.schema.message import TOPIC_STARTERS_READY_EVENT
        from jiuwenswarm.common.utils import get_agent_workspace_dir

        if self.store is None:
            self.store = TopicStarterStore(get_agent_workspace_dir() / "recommendation.db")
        prepare, service, queue = _build_topic_starter_services(
            self.store,
            prepare_service=self.topic_prepare_service if self._services_built else None,
            outcome_queue=self.topic_outcome_queue if self._services_built else None,
        )
        self.topic_prepare_service = prepare
        self.topic_service = service
        self.topic_outcome_queue = queue
        if service is not None:
            def _on_prepared(
                context_scope: str,
                state: str,
                pool_written: bool,
                *,
                work_mode: str = "work",
            ) -> None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(self.emit(
                    TOPIC_STARTERS_READY_EVENT,
                    {
                        "context_scope": context_scope,
                        "work_mode": work_mode,
                        "state": state,
                        "pool_written": pool_written,
                    },
                ))

            service.on_prepared = _on_prepared
        self._services_built = True

    async def start(self) -> None:
        # Pools fill on enable / chat successor / click — not on process boot
        # or a midnight timer.
        self.ensure_services()

    async def shutdown(self) -> None:
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
            normalize_topic_work_mode,
            scope_for_project,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False,
        ) or {}
        project_id = metadata.get("project_id")
        work_mode = normalize_topic_work_mode(metadata.get("work_mode"))
        # Global welcome only follows non-project chats; a project welcome only
        # follows that project's chats. Never refresh both from one turn.
        if project_id not in (None, "", "default", "default_code"):
            service.schedule_successor(
                context_scope=scope_for_project(str(project_id)),
                project_id=project_id,
                work_mode=work_mode,
                visit_id=request_id,
            )
            return
        service.schedule_successor(
            context_scope="global_new_chat",
            project_id=None,
            work_mode=work_mode,
            visit_id=request_id,
        )

    async def session_left_project(self, project_id: str, session_id: str) -> None:
        """No-op: leaving a project must not auto-refresh topic pools."""

        del project_id, session_id

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
