# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attaching a client: the app its conversation belongs to, the conflicts, the read models."""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.client_attachments import (
    attach,
    get_attachment_directory,
    load_attached_resource,
    load_client_capabilities,
    registry,
)
from jiuwenswarm.server.client_attachments import directory
from jiuwenswarm.server.client_attachments.directory import LEASE_SECONDS, RENEW_EVERY_SECONDS
from jiuwenswarm.server.runtime import extension_package_manager as packages
from jiuwenswarm.server.runtime.session import session_metadata
from tests.unit_tests.agentserver.client_attachments_support import (  # noqa: F401 - the fixture
    RESOURCE,
    OTHER_APP,
    CLIENT_APP,
    answer,
    answer_async,
    created_for,
    isolated,
    new_conversation,
    renew,
    request,
    stored_metadata,
)

pytestmark = pytest.mark.usefixtures("isolated")

# What JiuwenSwarm ships, read before the ``isolated`` fixture replaces it.
_SHIPPED_APPS = registry.APPS


def test_attach_records_the_attachment_and_announces_it() -> None:
    pushed: list[dict] = []
    attached = answer(request(), pushed)
    assert attached["ok"] is True
    assert attached["payload"]["attachment_id"].startswith("att_")
    assert attached["payload"]["session_id"] == "session-1" and attached["payload"]["client_app"] == CLIENT_APP
    assert (attached["payload"]["lease_seconds"], attached["payload"]["renew_every_seconds"]) == (
        LEASE_SECONDS, RENEW_EVERY_SECONDS)
    assert load_client_capabilities("session-1") == {"supported_tools": ["read_presentation_context"]}
    assert [(m["metadata"]["ws_id"], m["payload"]["event_type"]) for m in pushed] == [
        ("ws-1", "client.attachments_changed")]
    connection, = get_attachment_directory().connections()
    assert (connection.session_id, connection.display_name, connection.client_app) == (
        "session-1", "Roadmap.pptx", CLIENT_APP)


def test_attach_installs_no_plugin_and_equips_nothing() -> None:
    assert answer(request())["ok"] is True

    assert session_metadata.get_session_equipment("session-1", cache_bust=True) == {}
    assert "session_equipment" not in stored_metadata("session-1")
    assert not packages.plugin_package_exists(CLIENT_APP)


@pytest.mark.parametrize("request_", [
    request(owner="user-2"), request(session="missing"),
    request(resource={**RESOURCE, "key": " "}), request(capabilities=["read_presentation_context"]),
    request(socket=""),
])
def test_an_attach_that_does_not_fit_is_refused_before_anything_is_written(request_) -> None:
    refused = answer(request_)
    assert refused["ok"] is False and refused["payload"]["code"]
    assert get_attachment_directory().connections() == ()
    assert load_attached_resource("session-1") is None


def test_attached_clients_name_each_other_s_current_conversation() -> None:
    answer(request())
    answer(request(session="session-2", socket="ws-2",
                   resource={**RESOURCE, "key": "budget", "display_name": "Budget.pptx"}))
    listed = answer(request(ReqMethod.CLIENT_LIST, session_id="session-1"))["payload"]
    assert [(d["display_name"], d["session_id"], d["is_current"]) for d in listed["attachments"]] == [
        ("Budget.pptx", "session-2", False), ("Roadmap.pptx", "session-1", True)]
    # Another user's conversation lists nothing of this user's.
    assert answer(request(ReqMethod.CLIENT_LIST, session_id="foreign", owner="user-2"))["payload"] == {
        "attachments": []}


def test_the_conversation_keeps_the_resource_attached_to_it() -> None:
    attached = answer(request(resource={**RESOURCE, "display_name": "  Roadmap   Q3.pptx "}))["payload"]

    assert load_attached_resource("session-1") == {
        "attachment_id": attached["attachment_id"],
        "resource": {**RESOURCE, "display_name": "Roadmap Q3.pptx"},
    }
    assert load_attached_resource("session-2") is None


# --- the lease ------------------------------------------------------------------------


def test_a_renew_extends_the_lease_and_does_nothing_else() -> None:
    attachment_id = answer(request())["payload"]["attachment_id"]
    clock = directory.clock  # the ``isolated`` fixture's
    clock.advance(LEASE_SECONDS - 1)
    capabilities = attach.get_agent_sessions_dir() / "session-1" / "client_capabilities.json"
    written = capabilities.stat().st_mtime_ns
    pushed: list[dict] = []

    renewed = answer(renew(attachment_id), pushed)

    assert renewed == {"ok": True, "payload": {"lease_seconds": LEASE_SECONDS,
                                               "renew_every_seconds": RENEW_EVERY_SECONDS}}
    assert pushed == [] and capabilities.stat().st_mtime_ns == written
    assert get_attachment_directory().expire(clock.advance(LEASE_SECONDS - 1)) == []


@pytest.mark.parametrize("renewing", [
    {"attachment_id": "att_unknown"},
    {"session": "session-2"},
])
def test_a_renew_of_no_such_attachment_makes_the_client_attach_again(renewing) -> None:
    attachment_id = answer(request())["payload"]["attachment_id"]

    refused = answer(renew(**{"attachment_id": attachment_id, **renewing}))

    assert refused["ok"] is False and refused["payload"]["code"] == "NOT_ATTACHED"


def test_a_client_whose_resource_another_client_attached_is_told_so_until_it_attaches_again() -> None:
    attachment_id = answer(request())["payload"]["attachment_id"]
    assert answer(request(socket="ws-2"))["ok"] is True  # a copy of the file, open in another window

    assert answer(renew(attachment_id))["payload"]["code"] == "DISPLACED"
    assert answer(renew(attachment_id, socket="ws-2"))["ok"] is True
    # The first window's user continues there: the other one is now displaced.
    assert answer(request())["ok"] is True
    assert answer(renew(attachment_id, socket="ws-2"))["payload"]["code"] == "DISPLACED"
    get_attachment_directory().expire(directory.clock.advance(LEASE_SECONDS))
    assert answer(renew(attachment_id, socket="ws-2"))["payload"]["code"] == "NOT_ATTACHED"


def test_after_the_server_forgot_its_attachments_a_renew_is_refused_and_an_attach_works_again() -> None:
    attachment_id = answer(request())["payload"]["attachment_id"]
    get_attachment_directory().close()  # a restarted Worker

    assert answer(renew(attachment_id))["payload"]["code"] == "NOT_ATTACHED"
    assert answer(request())["payload"]["attachment_id"] == attachment_id
    assert answer(renew(attachment_id))["ok"] is True


def test_conversation_messages_are_owner_checked(monkeypatch) -> None:
    from jiuwenswarm.server.control import history_service

    monkeypatch.setattr(history_service, "load_history_page", lambda sid, page: {"messages": [
        {"role": "assistant", "content": "done", "reasoning_content": "secret"}]})
    page = answer(request(ReqMethod.CLIENT_CONVERSATION_MESSAGES, target_session_id="session-2"))["payload"]
    assert page["messages"][0]["has_reasoning"] is True and "reasoning_content" not in page["messages"][0]
    refused = answer(request(ReqMethod.CLIENT_CONVERSATION_MESSAGES, target_session_id="foreign"))
    assert refused["payload"]["code"] == "NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.parametrize("newest", ["session-1", "session-2"])
def test_a_resource_s_conversations_are_the_owner_s_of_its_app_latest_first(newest) -> None:
    # Either order of last messages differs from the directory scan order in one case.
    older = {"session-1": "session-2", "session-2": "session-1"}[newest]
    titles = {"session-1": "Outline", "session-2": "Q3 plan"}
    for session_id, last_message_at in [(newest, 300.0), (older, 200.0)]:
        (attach.get_agent_sessions_dir() / session_id / "metadata.json").write_text(json.dumps(
            {**new_conversation(session_id, "user-1"), "last_message_at": last_message_at,
             "title": titles[session_id]}))
    answer(request())
    answer(request(session="session-2"))
    answer(request(session="foreign", owner="user-2"))
    created_for("session-3", OTHER_APP)  # the same key, attached in another app
    answer(request(session="session-3", socket="ws-3", capabilities={"supported_tools": ["read_deck"]}))

    listed = answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app=CLIENT_APP, resource_key=" office-deck "))
    assert listed["payload"] == {"conversations": [
        {"session_id": newest, "title": titles[newest], "last_message_at": 300.0},
        {"session_id": older, "title": titles[older], "last_message_at": 200.0}]}
    others = answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app=OTHER_APP, resource_key="office-deck"))
    assert [c["session_id"] for c in others["payload"]["conversations"]] == ["session-3"]
    assert answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app=CLIENT_APP, resource_key="new-deck"))[
        "payload"] == {"conversations": []}
    assert answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app="not-registered", resource_key="office-deck"))[
        "payload"]["code"] == "APP_UNAVAILABLE"
    assert answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app=CLIENT_APP))["payload"]["code"] == "BAD_REQUEST"


def test_list_and_messages_read_the_conversation_off_the_event_loop(monkeypatch) -> None:
    threads: list[str] = []
    real_to_thread = asyncio.to_thread

    async def to_thread(function, *args, **kwargs):
        threads.append(function.__name__)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(attach.asyncio, "to_thread", to_thread)
    answer(request(ReqMethod.CLIENT_LIST, session_id="session-1"))
    answer(request(ReqMethod.CLIENT_CONVERSATIONS, client_app=CLIENT_APP, resource_key="office-deck"))

    assert threads == ["_owned_session", "_conversations"]


# --- one resource, one conversation ---------------------------------------------------


@pytest.mark.parametrize("client_app", ["", "retired-app"])  # a web conversation, one of an app no longer shipped
def test_a_conversation_not_created_by_a_client_app_is_refused(client_app) -> None:
    created_for("session-1", client_app)

    refused = answer(request())

    assert refused["ok"] is False and refused["payload"]["code"] == "CONFLICT"
    assert get_attachment_directory().connections() == ()
    assert load_attached_resource("session-1") is None


def test_a_conversation_attached_to_another_resource_is_refused() -> None:
    assert answer(request())["ok"] is True

    other = answer(request(socket="ws-2", resource={**RESOURCE, "key": "budget"}))

    assert other["payload"]["code"] == "CONFLICT"
    assert [(c.ws_id, c.session_id) for c in get_attachment_directory().connections()] == [("ws-1", "session-1")]
    # The same resource on a new socket (a reloaded task pane) takes the conversation over.
    assert answer(request(socket="ws-3"))["ok"] is True
    assert [c.ws_id for c in get_attachment_directory().connections()] == ["ws-3"]


def test_a_failed_change_notice_does_not_fail_the_attach() -> None:
    async def unreachable(_message) -> bool:
        return False

    assert answer(request(), push=unreachable)["ok"] is True
    assert [c.ws_id for c in get_attachment_directory().connections()] == ["ws-1"]


def test_a_refused_attach_keeps_the_socket_s_earlier_attachment(monkeypatch) -> None:
    assert answer(request())["ok"] is True

    def full(*_args) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(attach, "save_client_capabilities", full)
    refused = answer(request(session="session-2", resource={**RESOURCE, "key": "other"}))

    assert refused["payload"]["code"] == "UNAVAILABLE"
    connection, = get_attachment_directory().connections()
    assert (connection.ws_id, connection.session_id) == ("ws-1", "session-1")


# --- which apps a client may attach with, and what it may declare ----------------------


@pytest.mark.parametrize(("capabilities", "named"), [
    ({"supported_tools": ["read_presentation_context", "format_c_drive", "eject_cd"]}, "eject_cd, format_c_drive"),
    ({"supported_tools": ["read_deck"]}, "read_deck"),  # the other app's tool
    ({"supported_tools": "read_presentation_context"}, "supported_tools"),
])
def test_a_client_declaring_tools_its_app_lacks_is_refused(capabilities, named) -> None:
    refused = answer(request(capabilities=capabilities))

    assert refused["payload"]["code"] == "INVALID_ARGUMENT" and named in refused["payload"]["error"]
    assert get_attachment_directory().connections() == ()


def test_a_conversation_attaches_with_the_tools_of_the_app_it_was_created_for() -> None:
    created_for("session-2", OTHER_APP)

    assert answer(request(session="session-2"))["payload"]["code"] == "INVALID_ARGUMENT"
    attached = answer(request(session="session-2", capabilities={"supported_tools": ["read_deck"]}))
    assert attached["payload"]["client_app"] == OTHER_APP


def test_every_app_jiuwenswarm_ships_has_its_contract_and_template(monkeypatch) -> None:
    from openjiuwen.harness.resources.extension_loader import (
        find_agent_template_manifest,
        load_agent_template_package,
    )

    monkeypatch.setattr(registry, "APPS", _SHIPPED_APPS)
    registry.client_apps.cache_clear()

    apps = registry.client_apps()
    assert apps and all(app.tool_names for app in apps.values())
    for app in apps.values():
        template = load_agent_template_package(find_agent_template_manifest(app.package_dir))
        assert template.prompt_sections and template.rails and template.skills
