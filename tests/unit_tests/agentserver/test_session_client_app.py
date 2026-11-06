# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A conversation a client app creates (``client_app``: the app's id) shows only in that app.

It stays out of the web and desktop session list, the projects and their batch
archive, and the agent's own ``session_list``; a fork of it belongs to the same app.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.runtime.session_provisioner import (
    RuntimeSessionProvisioner,
    SessionCreateInput,
    SessionProvisionError,
)
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessageService,
    SessionMessageSource,
)
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore
from tests.unit_tests.agentserver.client_attachments_support import CLIENT_APP
from tests.unit_tests.server.test_archive_lifecycle import archive  # noqa: F401 - the fixture

MODE = "agent.work.normal"


class _Manager:
    """The agent manager of a create: the prewarmed conversation it claims is ``app-session``."""

    def __init__(self) -> None:
        self.prewarm_eligible: list[bool] = []

    async def claim_prewarmed_session(self, **kwargs: Any) -> Any:
        self.prewarm_eligible.append(kwargs["prewarm_eligible"])
        return SimpleNamespace(session_id="app-session", prewarm_hit=False, prewarm_status="")

    @staticmethod
    def activate_session_prewarm(_session_id: str) -> None:
        pass

    @staticmethod
    async def release_session_prewarm_claim(_session_id: str) -> None:
        pass


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    session_metadata._METADATA_CACHE.clear()
    yield root
    session_metadata.flush_pending_writes()
    session_metadata._METADATA_CACHE.clear()


def _create(session_id: str, client_app: str = "") -> None:
    session_metadata.init_session_metadata(session_id=session_id, channel_id="web", user_id="user-1", mode=MODE,
                                           client_app=client_app)


async def test_a_client_app_conversation_is_listed_neither_in_the_session_list_nor_in_a_project(sessions) -> None:
    from jiuwenswarm.runtime.session_catalog import SessionGetInput, SessionListInput, get_session, list_sessions
    from jiuwenswarm.server.control.store.project_queries import attribute_session_project

    provisioner = RuntimeSessionProvisioner(agent_manager=_Manager(), plan_controller=SimpleNamespace())
    prepared = await provisioner.prepare_session_create(SessionCreateInput(
        channel_id="web", create_token="token", mode=MODE, work_mode="work",
        work_mode_explicit=False, user_id="user-1", client_app=CLIENT_APP))
    assert prepared.result.session_id == "app-session"
    _create("web-session")
    session_metadata.update_session_metadata(session_id="app-session", title="Roadmap", increment_message_count=True)

    listed, total = session_metadata.get_all_sessions_metadata(limit=20, offset=0)
    assert [s["session_id"] for s in listed] == ["web-session"] and total == 1
    assert [s.session_id for s in list_sessions(SessionListInput(channel_id="web")).sessions] == ["web-session"]
    assert get_session(SessionGetInput(channel_id="web", session_id="app-session")) is not None
    # Filed under no project, not the default one.
    assert attribute_session_project(session_metadata.get_session_metadata("app-session"), set()) == ""
    assert attribute_session_project(session_metadata.get_session_metadata("web-session"), set()) != ""


async def test_the_agent_s_session_list_leaves_them_out_and_a_message_sent_by_id_reaches_them(
        sessions, tmp_path) -> None:
    for session_id, client_app in (("web-a", ""), ("web-b", ""), ("deck-a", CLIENT_APP)):
        _create(session_id, client_app)

    async def execute(_record):
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(store=SessionMessageStore(tmp_path / "messages.sqlite3"),
                                    admission=SessionRunAdmission(), execute=execute, available=True)
    source = SessionMessageSource(session_id="web-a", request_id="request-1", tool_call_id="call-1",
                                  idempotency_key="call-1", user_id="user-1")
    try:
        listed = await service.list_targets(source)
        assert [s["session_id"] for s in listed["sessions"]] == ["web-b"]
        sent = await service.send_message(source, input_mode="follow_up", target_session_id="deck-a",
                                          message="How many slides does the deck have?")
        assert sent["accepted"] is True
    finally:
        await service.stop()


async def test_a_project_s_batch_archive_leaves_client_app_conversations_to_their_app(
        archive,  # noqa: F811 - the pytest fixture imported above
) -> None:
    service, create, root, _runtime = archive
    create("web-chat")
    create("deck-a", client_app=CLIENT_APP)

    result = await service.project_batch("default", "archive", "web")

    assert [item["session_id"] for item in result["results"]] == ["web-chat"]
    assert (root / "sessions" / "deck-a").is_dir() and not (root / "sessions_archived" / "deck-a").exists()


@pytest.mark.parametrize("client_app", [CLIENT_APP, ""])
def test_a_fork_belongs_to_the_app_of_the_conversation_it_forks(sessions, monkeypatch, client_app) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    monkeypatch.setattr(session_ops_service, "get_agent_sessions_dir", lambda: sessions)
    _create("source", client_app)
    session_metadata.flush_pending_writes()

    session_ops_service.fork_session(source_session_id="source", target_session_id="forked", channel_id="web")

    assert session_metadata.get_session_metadata("forked", cache_bust=True).get("client_app") == (client_app or None)


@pytest.mark.parametrize(("client_app", "prewarm_eligible"), [(CLIENT_APP, False), ("", True)])
async def test_only_a_conversation_of_no_client_app_may_take_a_prewarmed_agent(
        sessions, client_app, prewarm_eligible) -> None:
    manager = _Manager()
    provisioner = RuntimeSessionProvisioner(agent_manager=manager, plan_controller=SimpleNamespace())
    await provisioner.prepare_session_create(SessionCreateInput(
        channel_id="web", create_token="token", mode="agent.work.normal", work_mode="work",
        work_mode_explicit=False, user_id="user-1", client_app=client_app))
    assert manager.prewarm_eligible == [prewarm_eligible]


def test_the_client_app_is_a_registered_client_app() -> None:
    assert SessionCreateInput(channel_id="web", client_app=CLIENT_APP).client_app == CLIENT_APP
    with pytest.raises(SessionProvisionError) as refused:
        SessionCreateInput(channel_id="web", client_app="unregistered-app")
    assert refused.value.code == "BAD_REQUEST"


async def test_session_create_hands_the_client_app_to_the_runtime() -> None:
    from tests.unit_tests.agentserver.test_session_create_runtime_boundary import (
        CreateRuntime,
        RecordingWebSocket,
        create_request,
        make_server,
    )

    runtime = CreateRuntime([])
    request = create_request()
    request.params["client_app"] = CLIENT_APP

    await make_server(runtime).handle_session_create_for_test(RecordingWebSocket([]), request, asyncio.Lock())

    assert [provision_input.client_app for provision_input in runtime.inputs] == [CLIENT_APP]


async def test_a_websocket_session_create_hands_the_client_app_to_the_runtime() -> None:
    from tests.unit_tests.agentserver.test_session_create_runtime_boundary import (
        CreateRuntime,
        RecordingWebSocket,
        create_request,
        make_server,
    )

    runtime = CreateRuntime([])
    request = create_request()
    request.params["client_app"] = CLIENT_APP

    await make_server(runtime).handle_ws_unary_for_test(RecordingWebSocket([]), request, asyncio.Lock())

    assert [provision_input.client_app for provision_input in runtime.inputs] == [CLIENT_APP]
