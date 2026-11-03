# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attaching a client: the app plugin it installs and equips, the conflicts, the read models."""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.server.client_attachments.errors import ClientAttachError
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.client_attachments import (
    attach,
    get_attachment_directory,
    load_attached_resource,
    load_client_capabilities,
    plugins,
    registry,
)
from jiuwenswarm.server.client_attachments import directory
from jiuwenswarm.server.client_attachments.directory import LEASE_SECONDS, RENEW_EVERY_SECONDS
from jiuwenswarm.server.runtime import extension_package_manager as packages
from tests.unit_tests.agentserver.client_attachments_support import (  # noqa: F401 - the fixture
    RESOURCE,
    OTHER_PLUGIN,
    PLUGIN,
    answer,
    answer_async,
    isolated,
    renew,
    request,
    write_app,
)

pytestmark = pytest.mark.usefixtures("isolated")

# What JiuwenSwarm ships, read before the ``isolated`` fixture replaces it.
_SHIPPED_APP_PACKAGES = registry.APP_PACKAGES


def test_attach_imports_the_plugin_once_equips_the_conversation_and_announces_the_attachment(isolated) -> None:
    pushed: list[dict] = []
    attached = answer(request(), pushed)
    assert attached["ok"] is True
    assert attached["payload"]["attachment_id"].startswith("att_")
    assert attached["payload"]["session_id"] == "session-1" and attached["payload"]["plugin_id"] == PLUGIN
    assert (attached["payload"]["lease_seconds"], attached["payload"]["renew_every_seconds"]) == (
        LEASE_SECONDS, RENEW_EVERY_SECONDS)
    assert isolated["installed"] == [PLUGIN]
    assert isolated["equipment"]["session-1"]["plugin_names"] == [PLUGIN]
    assert load_client_capabilities("session-1", PLUGIN) == {"supported_tools": ["read_presentation_context"]}
    assert load_client_capabilities("session-1", "another-plugin") is None
    assert [(m["metadata"]["ws_id"], m["payload"]["event_type"]) for m in pushed] == [
        ("ws-1", "client.attachments_changed")]
    connection, = get_attachment_directory().connections()
    assert (connection.session_id, connection.display_name, connection.plugin_id) == (
        "session-1", "Roadmap.pptx", PLUGIN)

    answer(request(session="session-2", socket="ws-2", resource={**RESOURCE, "key": "other"}))
    assert isolated["installed"] == [PLUGIN]


@pytest.mark.parametrize("request_", [
    request(owner="user-2"), request(session="missing"), request(plugin_id="../x"),
    request(plugin_id="not-registered"), request(plugin_id="powerpoint"),  # a directory name, not a plugin id
    request(resource={**RESOURCE, "key": " "}), request(capabilities=["read_presentation_context"]),
    request(socket=""),
])
def test_an_attach_that_does_not_fit_is_refused_before_anything_is_written(isolated, request_) -> None:
    refused = answer(request_)
    assert refused["ok"] is False and refused["payload"]["code"]
    assert isolated["equipment"] == {} and isolated["installed"] == []
    assert get_attachment_directory().connections() == ()
    assert not (isolated["sessions"] / "session-1" / "client_capabilities.json").exists()


def test_a_failed_install_refuses_the_attach(monkeypatch, isolated) -> None:
    def fail(_params) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(packages, "install_plugin_package", fail)
    refused = answer(request())
    assert refused["payload"]["code"] == "PLUGIN_UNAVAILABLE" and isolated["equipment"] == {}


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
    {"socket": "ws-2"},  # the pane's socket reconnected, or another socket names the attachment
    {"session": "session-2"},
])
def test_a_renew_of_no_such_attachment_makes_the_client_attach_again(renewing) -> None:
    attachment_id = answer(request())["payload"]["attachment_id"]

    refused = answer(renew(**{"attachment_id": attachment_id, **renewing}))

    assert refused["ok"] is False and refused["payload"]["code"] == "NOT_ATTACHED"


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


def test_list_and_messages_read_the_conversation_off_the_event_loop(monkeypatch) -> None:
    threads: list[str] = []
    real_to_thread = asyncio.to_thread

    async def to_thread(function, *args, **kwargs):
        threads.append(function.__name__)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(attach.asyncio, "to_thread", to_thread)
    answer(request(ReqMethod.CLIENT_LIST, session_id="session-1"))

    assert threads == ["_owned_session"]


# --- one resource, one conversation ---------------------------------------------------


def test_a_conversation_of_another_application_s_client_is_refused(isolated) -> None:
    isolated["equipment"]["session-1"] = {"plugin_names": ["some-skill-pack", OTHER_PLUGIN]}

    refused = answer(request())

    assert refused["ok"] is False and refused["payload"]["code"] == "CONFLICT"
    assert isolated["installed"] == [] and get_attachment_directory().connections() == ()
    assert isolated["equipment"]["session-1"]["plugin_names"] == ["some-skill-pack", OTHER_PLUGIN]


def test_a_conversation_last_attached_by_another_application_is_refused() -> None:
    attach.save_client_capabilities("session-1", OTHER_PLUGIN, {"supported_tools": []},
                                    attachment_id="att_wps", resource=RESOURCE)

    assert answer(request())["payload"]["code"] == "CONFLICT"
    assert load_client_capabilities("session-1", OTHER_PLUGIN) == {"supported_tools": []}


def test_equipping_never_stacks_a_second_app_plugin(isolated) -> None:
    isolated["equipment"]["session-1"] = {"plugin_names": [OTHER_PLUGIN]}

    with pytest.raises(ClientAttachError):
        plugins.equip("session-1", PLUGIN, None)
    plugins.equip("session-1", OTHER_PLUGIN, None)
    assert isolated["equipment"]["session-1"]["plugin_names"] == [OTHER_PLUGIN]


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
def test_a_client_declaring_tools_its_app_lacks_is_refused(isolated, capabilities, named) -> None:
    refused = answer(request(capabilities=capabilities))

    assert refused["payload"]["code"] == "INVALID_ARGUMENT" and named in refused["payload"]["error"]
    assert isolated["installed"] == [] and get_attachment_directory().connections() == ()


def test_apps_of_every_shipped_package_can_attach(isolated, monkeypatch) -> None:
    more = isolated["importable"] / "docapps" / "more"
    write_app(more, "sheets", "office-sheets", ["read_sheet"])
    monkeypatch.setattr(registry, "APP_PACKAGES", ("docapps/apps", "docapps/more"))

    assert answer(request(plugin_id="office-sheets", capabilities={"supported_tools": ["read_sheet"]}))["ok"] is True
    assert answer(request(session="session-2", socket="ws-2", resource={**RESOURCE, "key": "b"}))["ok"] is True


def test_no_shipped_package_lets_no_client_attach(monkeypatch) -> None:
    monkeypatch.setattr(registry, "APP_PACKAGES", ())

    assert answer(request())["payload"]["code"] == "PLUGIN_UNAVAILABLE"


def test_a_shipped_package_must_mark_the_copy_an_attach_installs_as_jiuwenswarm_s(isolated, monkeypatch) -> None:
    # Unmarked, the copy the first attach installs would read as a user's own plugin and refuse every later attach.
    package = write_app(isolated["importable"] / "docapps" / "unmarked", "sheets", "office-sheets", ["read_sheet"])
    (package / "manifest.json").write_text(json.dumps({"id": "office-sheets"}))
    monkeypatch.setattr(registry, "APP_PACKAGES", ("docapps/unmarked",))

    with pytest.raises(RuntimeError, match="managed_by"):
        registry.app_plugins()


def test_every_package_jiuwenswarm_ships_marks_its_installed_copy(monkeypatch) -> None:
    monkeypatch.setattr(registry, "APP_PACKAGES", _SHIPPED_APP_PACKAGES)

    registry.app_plugins()


# --- the app plugin JiuwenSwarm ships -----------------------------------------------------


def _installed_copy(isolated, tmp_path, monkeypatch, manifest: dict, files: dict[str, str]):
    copy = tmp_path / "workspace" / "plugins" / "local" / PLUGIN
    copy.mkdir(parents=True)
    (copy / "manifest.json").write_text(json.dumps(manifest))
    for name, text in files.items():
        (copy / name).write_text(text)
    isolated["installed"].append(PLUGIN)
    monkeypatch.setattr(packages, "resolve_plugin_dir", lambda pid: copy)
    return copy


def test_an_earlier_release_s_copy_is_refreshed_in_place_and_stays_installed(isolated, tmp_path, monkeypatch) -> None:
    (isolated["shipped"] / "rails.json").write_text('{"rails": ["new"]}')
    copy = _installed_copy(isolated, tmp_path, monkeypatch, {"metadata": {"managed_by": "client_attachments"}},
                           {"rails.json": '{"rails": ["old"]}', "dropped.txt": "gone in this release"})

    def no_reimport(_params) -> None:
        raise AssertionError("a plugin other conversations may be loading is never imported again")

    monkeypatch.setattr(packages, "import_plugin_package", no_reimport)
    held = (copy / "rails.json").open()  # a turn of another conversation reading the old copy

    assert answer(request())["ok"] is True

    assert held.read() == '{"rails": ["old"]}'
    held.close()
    assert (copy / "rails.json").read_text() == '{"rails": ["new"]}' and not (copy / "dropped.txt").exists()
    assert isolated["installed"] == [PLUGIN]
    assert [p.name for p in copy.parent.iterdir()] == [PLUGIN]  # no staging left behind


def test_an_unchanged_copy_is_not_read_again_on_the_next_attach(isolated, tmp_path, monkeypatch) -> None:
    copy = _installed_copy(isolated, tmp_path, monkeypatch, {}, {})
    for source in isolated["shipped"].iterdir():
        (copy / source.name).write_bytes(source.read_bytes())
    assert answer(request())["ok"] is True
    reads: list[str] = []
    read_bytes = type(copy).read_bytes
    monkeypatch.setattr(type(copy), "read_bytes", lambda self: reads.append(self.name) or read_bytes(self))

    assert answer(request(session="session-2", socket="ws-2", resource={**RESOURCE, "key": "b"}))["ok"] is True
    assert reads == []


def test_a_users_own_plugin_with_the_same_id_is_left_alone_and_the_attach_refused(
        isolated, tmp_path, monkeypatch) -> None:
    copy = _installed_copy(isolated, tmp_path, monkeypatch, {"id": PLUGIN}, {"mine.txt": "my own plugin"})

    refused = answer(request())

    assert refused["payload"]["code"] == "PLUGIN_UNAVAILABLE"
    assert (copy / "mine.txt").read_text() == "my own plugin"
    assert get_attachment_directory().connections() == () and isolated["equipment"] == {}


def test_an_installed_plugin_that_cannot_be_read_refuses_the_attach_with_the_reason(isolated, monkeypatch) -> None:
    isolated["installed"].append(PLUGIN)

    def conflict(pid):
        raise ValueError(f"plugin package conflict: {pid} exists in both local and built_in")

    def no_reimport(_params) -> None:
        raise AssertionError("an installed plugin is never imported over")

    monkeypatch.setattr(packages, "resolve_plugin_dir", conflict)
    monkeypatch.setattr(packages, "import_plugin_package", no_reimport)

    refused = answer(request())

    assert refused["payload"]["code"] == "PLUGIN_UNAVAILABLE"
    assert "exists in both local and built_in" in json.dumps(refused["payload"])
    assert get_attachment_directory().connections() == () and isolated["equipment"] == {}


def test_a_copy_windows_holds_open_stays_in_use_and_the_attach_goes_on(isolated, tmp_path, monkeypatch) -> None:
    (isolated["shipped"] / "rails.json").write_text('{"rails": ["new"]}')
    copy = _installed_copy(isolated, tmp_path, monkeypatch, {"metadata": {"managed_by": "client_attachments"}},
                           {"rails.json": '{"rails": ["old"]}'})
    real_replace = plugins.os.replace

    def locked(source, target):
        if str(source) == str(copy):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_replace(source, target)

    monkeypatch.setattr(plugins.os, "replace", locked)

    assert answer(request())["ok"] is True
    assert (copy / "rails.json").read_text() == '{"rails": ["old"]}'  # refreshed by a later attach
    assert [p.name for p in copy.parent.iterdir()] == [PLUGIN]


def test_a_copy_that_cannot_be_put_back_is_kept_rather_than_deleted(isolated, tmp_path, monkeypatch) -> None:
    (isolated["shipped"] / "rails.json").write_text('{"rails": ["new"]}')
    copy = _installed_copy(isolated, tmp_path, monkeypatch, {"metadata": {"managed_by": "client_attachments"}},
                           {"rails.json": '{"rails": ["old"]}'})
    real_replace = plugins.os.replace

    def nothing_lands_on_the_copy(source, target):
        if str(target) == str(copy):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_replace(source, target)

    monkeypatch.setattr(plugins.os, "replace", nothing_lands_on_the_copy)
    answer(request())

    kept = [path for path in copy.parent.rglob("rails.json") if path.read_text() == '{"rails": ["old"]}']
    assert kept, "the installed copy was deleted"
