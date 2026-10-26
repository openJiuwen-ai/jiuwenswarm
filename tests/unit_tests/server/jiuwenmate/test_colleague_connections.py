"""F_02_02: one colleague configuration, one agent per connection, published together."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import AgentMailRuntimeStore, AgentMailScope
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from jiuwenswarm.server.jiuwenmate.models import CapabilitySelection, ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_ACCESS
from tests.unit_tests.gateway.test_app_web_handlers import FakeChannelManager, FakeWebChannel
from tests.unit_tests.gateway.test_persistent_group_fanout import _Host as FeishuHost
from tests.unit_tests.gateway.test_persistent_group_fanout import _binding, _event
from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service
from tests.unit_tests.server.jiuwenmate.test_web_private_chat import Host as WebHost

NS = "workspace-1"


def settings(**changes) -> ColleagueSettings:
    fields = {
        "name": "会计助手",
        "description": "整理报销。",
        "persona": "你是会计助手。",
        "capabilities": CapabilitySelection(model="deepseek#0"),
    }
    fields.update(changes)
    return ColleagueSettings(**fields)


def prompt(service, agent_id: str) -> str:
    return service.current_snapshot(NS, agent_id).load_spec().system_prompt


def versions(service, *agent_ids: str) -> list[str | None]:
    return [service.get_colleague(NS, agent_id).current_version for agent_id in agent_ids]


# F_02_02-V-01 every connection gets its own agent


def test_each_app_gets_its_own_agent_and_the_same_app_gets_it_again(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    feishu = service.attach_connection(NS, colleague, "feishu", "app-a")
    dingtalk = service.attach_connection(NS, colleague, "dingtalk", "app-b")
    other = service.create_colleague(NS, settings(name="出纳助手")).agent_id

    assert service.attach_connection(NS, colleague, "feishu", "app-a") == feishu
    # The same app under another colleague is another connection; the first agent stays with its colleague.
    moved = service.attach_connection(NS, other, "feishu", "app-a")
    assert len({colleague, feishu, dingtalk, moved}) == 4
    assert service.connection_of(NS, feishu).config_id == colleague
    assert (service.connection_of(NS, dingtalk).channel, service.connection_of(NS, dingtalk).connection_key) == (
        "dingtalk", "app-b",
    )
    assert service.connection_of(NS, colleague) is None
    offices = {service.workspaces.path(NS, agent_id) for agent_id in (colleague, feishu, dingtalk, moved)}
    assert len(offices) == 4
    assert [item.agent_id for item in service.list_colleagues(NS)] == [colleague, other]
    assert {item.agent_id for item in service.list_agents(NS)} == {colleague, feishu, dingtalk, other, moved}
    view = service.get_colleague(NS, feishu)
    assert (view.agent_id, view.config_id, view.name) == (feishu, colleague, "会计助手")
    assert view.workspace_dir == str(service.workspaces.path(NS, feishu))
    with pytest.raises(PersistentAgentError) as unsupported:
        service.attach_connection(NS, colleague, "web", "web")
    assert unsupported.value.code == "BAD_REQUEST"


# F_02_02-V-02 a new connection starts from what was published


def test_a_new_connection_uses_the_published_settings_and_an_unpublished_colleague_stays_unpublished(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings(persona="已发布的人设。")).agent_id
    service.update_settings(NS, colleague, settings(persona="尚未发布的人设。"))

    agent = service.attach_connection(NS, colleague, "dingtalk", "app-b")

    assert "已发布的人设。" in prompt(service, agent) and "尚未发布的人设。" not in prompt(service, agent)
    assert service.get_colleague(NS, colleague).has_unpublished_changes

    unpublished = make_service(tmp_path / "unpublished", FakeModels(default=None)).ensure_default_colleague(NS)
    service = make_service(tmp_path / "unpublished", FakeModels(default=None))
    agent = service.attach_connection(NS, unpublished.agent_id, "feishu", "app-a")
    assert service.get_colleague(NS, agent).current_version is None


# F_02_02-V-03 a publication switches every agent, or none


class FailingFor(FakeModels):
    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id

    def resolve(self, selection, *, client_id):
        if client_id == f"jiuwenmate:{self.agent_id}":
            raise PersistentAgentError("model is not available", code="MODEL_NOT_FOUND")
        return super().resolve(selection, client_id=client_id)


def test_publication_switches_every_agent_and_a_failure_switches_none(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    feishu = service.attach_connection(NS, colleague, "feishu", "app-a")
    dingtalk = service.attach_connection(NS, colleague, "dingtalk", "app-b")
    before = versions(service, colleague, feishu, dingtalk)

    service.update_settings(NS, colleague, settings(persona="新的人设。"))
    with pytest.raises(PersistentAgentError) as failed:
        service.publish(NS, colleague, models=FailingFor(dingtalk))
    assert failed.value.code == "MODEL_NOT_FOUND"
    assert versions(service, colleague, feishu, dingtalk) == before
    assert service.get_colleague(NS, colleague).has_unpublished_changes

    service.publish(NS, colleague)
    after = versions(service, colleague, feishu, dingtalk)
    assert all(new != old for new, old in zip(after, before))
    assert all("新的人设。" in prompt(service, agent) for agent in (colleague, feishu, dingtalk))
    assert not service.get_colleague(NS, colleague).has_unpublished_changes
    # Publishing unchanged settings keeps every version.
    service.publish(NS, colleague)
    assert versions(service, colleague, feishu, dingtalk) == after


# F_02_02-V-04 publication and a new connection never leave agents on different publications


def test_a_connection_added_during_a_publication_makes_it_retry(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    before = versions(service, colleague)
    service.update_settings(NS, colleague, settings(persona="新的人设。"))
    prepare = service._prepare_version
    added = []

    def prepare_and_add(namespace_id, agent, chosen, models):
        if not added:
            added.append(None)
            added[0] = service.attach_connection(NS, colleague, "feishu", "app-a")
        return prepare(namespace_id, agent, chosen, models)

    monkeypatch.setattr(service, "_prepare_version", prepare_and_add)
    with pytest.raises(PersistentAgentError) as conflict:
        service.publish(NS, colleague)
    monkeypatch.setattr(service, "_prepare_version", prepare)

    assert conflict.value.code == "CONFLICT"
    assert versions(service, colleague) == before
    assert "新的人设。" not in prompt(service, added[0])
    service.publish(NS, colleague)
    assert all("新的人设。" in prompt(service, agent) for agent in (colleague, added[0]))


def test_a_publication_during_a_new_connection_gives_it_the_newer_version(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings(persona="旧的人设。")).agent_id
    service.update_settings(NS, colleague, settings(persona="新的人设。"))
    prepare = service._prepare_version
    published = []

    def publish_first(namespace_id, agent, chosen, models):
        if not published:
            published.append(None)
            published[0] = service.publish(NS, colleague)
        return prepare(namespace_id, agent, chosen, models)

    monkeypatch.setattr(service, "_prepare_version", publish_first)
    agent = service.attach_connection(NS, colleague, "dingtalk", "app-b")

    assert published and "新的人设。" in prompt(service, agent)


# F_02_02-V-05 runtime views read their colleague; management works on the colleague only


def test_agents_follow_their_colleague_and_cannot_be_managed_directly(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    agent = service.attach_connection(NS, colleague, "feishu", "app-a")

    service.update_settings(NS, colleague, replace(settings(), group_reply_mode="passive"))
    # A saved reply mode applies at once, as it does for the colleague itself.
    assert service.read_settings(NS, agent).group_reply_mode == "passive"
    service.set_enabled(NS, colleague, enabled=False)
    assert service.get_colleague(NS, agent).enabled is False
    for operation in (
        lambda: service.update_settings(NS, agent, settings()),
        lambda: service.publish(NS, agent),
        lambda: service.set_enabled(NS, agent, enabled=True),
        lambda: service.get_config(NS, agent),
        lambda: service.attach_connection(NS, agent, "dingtalk", "app-b"),
    ):
        with pytest.raises(PersistentAgentError) as refused:
            operation()
        assert refused.value.code == "BAD_REQUEST"


def test_model_refresh_publishes_colleagues_with_all_their_agents(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    agent = service.attach_connection(NS, colleague, "feishu", "app-a")
    before = versions(service, colleague, agent)

    rotated = FakeModels()
    rotated.api_key = "sk-test-rotated"
    assert service.refresh_published(NS, models=rotated) is True
    assert all(new != old for new, old in zip(versions(service, colleague, agent), before))


# F_02_02-V-06 unbind hides the colleague and keeps data; purge removes every agent


def test_unbinding_hides_the_colleague_and_keeps_its_agents(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings(), source_template_id="accountant").agent_id
    agent = service.attach_connection(NS, colleague, "dingtalk", "app-b")
    service.workspaces.ensure(NS, agent)

    service.hide_colleague(NS, colleague)

    assert service.list_colleagues(NS) == []
    assert service.get_colleague(NS, agent).enabled is False
    assert service.current_snapshot(NS, agent).agent_id == agent
    assert service.workspaces.path(NS, agent).is_dir()
    with pytest.raises(PersistentAgentError) as hidden:
        service.get_config(NS, colleague)
    assert hidden.value.code == "NOT_FOUND"
    # The same template may be used again once the colleague is gone from the list.
    service.create_colleague(NS, settings(), source_template_id="accountant", unique_source=True)


@pytest.mark.asyncio
async def test_purging_ends_work_in_both_runtimes_before_removing_every_agent(tmp_path):
    host = WebHost(tmp_path)
    colleague = await host.colleague()
    attached = await host.adapter.attach_connection(
        {"config_id": colleague, "channel": "feishu", "connection_key": "app-a"},
    )
    agent = attached["agent_id"]
    im_runtime = host.restarted_manager()
    await im_runtime.start()
    try:
        await im_runtime.submit_direct_message(
            "local", agent, ConversationRef.web("conv-im"), message_id="msg-im", text="飞书里的任务",
            access=WEB_ACCESS, sender_name="用户",
        )
        for _ in range(400):
            if host.core.started:
                break
            await asyncio.sleep(0.005)
        assert host.core.started == ["飞书里的任务"]

        removed = await host.adapter.remove_colleague(
            {"config_id": colleague, "mode": "purge"}, im_runtime=im_runtime,
        )

        assert removed == {"removed": True}
        assert im_runtime.scheduler.running == []
        service = host.managers[0]._service
        for agent_id in (agent, colleague):
            with pytest.raises(PersistentAgentError) as gone:
                service.get_colleague("local", agent_id)
            assert gone.value.code == "NOT_FOUND"
            assert not service.workspaces.path("local", agent_id).exists()
        assert service.connection_of("local", agent) is None
    finally:
        await im_runtime.aclose()
        await host.adapter.aclose()


# F_02_02-V-08 Feishu accepts only the agent created for the receiving app


@pytest.mark.asyncio
async def test_feishu_accepts_only_the_agent_created_for_the_receiving_app(tmp_path):
    host = FeishuHost(tmp_path)
    await host.start()
    try:
        colleague = host.colleague("会计助手")
        agent = host.service.attach_connection(NS, colleague, "feishu", "app-dev")
        dingtalk = host.service.attach_connection(NS, colleague, "dingtalk", "app-dev")
        for bound in (colleague, dingtalk):
            # The colleague's Web agent, or its DingTalk agent, written into the Feishu entry by hand.
            fanout = host.fanout([_binding("app-dev", bound)])
            assert await fanout.handle(_event()) is InboundDisposition.REJECTED
        assert host.row_counts() == (0, 0, 0)
        moved = host.fanout([_binding("app-other", agent)])
        assert await moved.handle(_event(receiving_app_id="app-other")) is InboundDisposition.REJECTED
        assert await host.fanout([_binding("app-dev", agent)]).handle(_event()) is InboundDisposition.HANDLED
        # An enterprise bot has no recorded connection yet and keeps working, unless it borrows another agent.
        enterprise = host.fanout([_binding("app-enterprise", host.colleague("企业助手"), enterprise=True)])
        assert await enterprise.handle(_event(receiving_app_id="app-enterprise", message_id="om_e")) is (
            InboundDisposition.HANDLED
        )
        borrowed = host.fanout([_binding("app-enterprise", dingtalk, enterprise=True)])
        assert await borrowed.handle(_event(receiving_app_id="app-enterprise", message_id="om_b")) is (
            InboundDisposition.REJECTED
        )
    finally:
        await host.close()


def test_connections_survive_a_reopened_repository(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    agent = service.attach_connection(NS, colleague, "dingtalk", "app-b")

    reopened = make_service(tmp_path)

    assert reopened.connection_of(NS, agent).config_id == colleague
    assert [item.agent_id for item in reopened.connections(NS, colleague)] == [agent]
    stored = json.loads(json.dumps(reopened.get_colleague(NS, agent).to_dict()))
    assert stored["config_id"] == colleague


def assert_nothing_left(service, namespace_id: str, *agent_ids: str) -> None:
    """None of the agents has a row, a connection or an office, and no connection lost its colleague."""
    for agent_id in agent_ids:
        with pytest.raises(PersistentAgentError) as gone:
            service.get_colleague(namespace_id, agent_id)
        assert gone.value.code == "NOT_FOUND"
        assert service.connection_of(namespace_id, agent_id) is None
        assert not service.workspaces.path(namespace_id, agent_id).exists()
    with sqlite3.connect(service._repository.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM colleague_connections WHERE config_id NOT IN (SELECT agent_id FROM colleagues)"
        ).fetchone()[0] == 0


# F_02_02-V-14 a purge that failed once is finished by repeating it


@pytest.mark.parametrize("failure", ["raises", "leaves_the_directory"])
@pytest.mark.asyncio
async def test_a_purge_that_failed_once_is_finished_by_repeating_it(tmp_path, failure):
    host = WebHost(tmp_path)
    colleague = await host.colleague()
    attached = await host.adapter.attach_connection(
        {"config_id": colleague, "channel": "dingtalk", "connection_key": "app-b"},
    )
    agent = attached["agent_id"]
    host.core.release("钉钉空间里的任务")
    sent = (await host.send(agent, "钉钉空间里的任务")).payload["request"]
    await host.settled(sent["request_id"])
    manager = host.managers[0]
    service = manager._service
    (service.workspaces.ensure("local", agent) / "output.txt").write_text("test output", encoding="utf-8")
    window = WindowBackendStore(host.db).bind("local", agent, "session-test")
    await window.set(f"{window._key_prefix}test-key", "test-value")
    profile = manager.cli_runtime._directory("local", agent) / "config"
    profile.mkdir(parents=True)
    (profile / "config.json").write_text("{}", encoding="utf-8")
    AgentMailRuntimeStore(host.db).put(AgentMailScope("local", agent), "connected", None, None)
    remove = service.workspaces.remove
    failed = []

    def fail_once(namespace_id, agent_id):
        if agent_id == agent and not failed:
            failed.append(agent_id)
            if failure == "raises":
                raise OSError("test cleanup failure")
            return  # Like rmtree with errors ignored: the directory stays and nothing is raised.
        remove(namespace_id, agent_id)

    service.workspaces.remove = fail_once
    try:
        with pytest.raises((OSError, PersistentAgentError)):
            await host.adapter.remove_colleague({"config_id": colleague, "mode": "purge"})
        # Nothing reports the colleague as removed while its agent is still there to be found.
        assert service.connection_of("local", agent).config_id == colleague
        assert [item.agent_id for item in service.list_colleagues("local")] == [colleague]

        assert await host.adapter.remove_colleague({"config_id": colleague, "mode": "purge"}) == {"removed": True}

        assert_nothing_left(service, "local", agent, colleague)
        with sqlite3.connect(host.db) as conn:
            for table in ("window_entries", "web_conversations"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE agent_id = ?", (agent,)).fetchone()[0] == 0
        assert AgentMailRuntimeStore(host.db).get(AgentMailScope("local", agent)) is None
        assert not profile.exists()
        # A finished purge is recognized when it is repeated.
        assert await host.adapter.remove_colleague({"config_id": colleague, "mode": "purge"}) == {"removed": True}
    finally:
        await host.adapter.aclose()


# F_02_02-V-15 a deletion whose answer was lost succeeds when Gateway repeats it


@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.asyncio
async def test_a_deletion_whose_answer_was_lost_succeeds_when_it_is_repeated(tmp_path, monkeypatch, mode):
    from jiuwenswarm.common import config as config_module

    host = WebHost(tmp_path)
    colleague, other = await host.colleague(), await host.colleague()
    mine = (await host.adapter.attach_connection(
        {"config_id": colleague, "channel": "dingtalk", "connection_key": "app-mine"},
    ))["agent_id"]
    theirs = (await host.adapter.attach_connection(
        {"config_id": other, "channel": "dingtalk", "connection_key": "app-theirs"},
    ))["agent_id"]
    dingtalk = {"apps": [
        {"client_id": "app-mine", "client_secret": "fake", "persistent_agent_id": mine},
        {"client_id": "app-theirs", "client_secret": "fake", "persistent_agent_id": theirs},
    ]}
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    config_module.dump_yaml_round_trip(path, {"channels": {"dingtalk": dingtalk}})
    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs["dingtalk"] = dingtalk

    class AgentServer:
        """Routes Gateway's requests to the real adapter and loses the first removal answer."""

        lose = True

        async def send_request(self, envelope):
            try:
                if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value:
                    payload = await host.adapter.colleague_connections(envelope.params)
                elif envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value:
                    payload = await host.adapter.remove_colleague(envelope.params)
                    if self.lose:
                        self.lose = False
                        raise ConnectionError("test answer lost after the removal")
                else:
                    payload = {}
            except PersistentAgentError as exc:
                return SimpleNamespace(ok=False, payload={"error": str(exc), "code": exc.code})
            return SimpleNamespace(ok=True, payload=payload)

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm, agent_client=AgentServer()))
    delete = channel.methods["jiuwenmate.colleague.delete"]

    def saved_apps():
        apps = config_module.load_yaml_round_trip(path)["channels"]["dingtalk"]["apps"]
        return [(app["client_id"], app["persistent_agent_id"]) for app in apps]

    try:
        await delete(object(), "delete-1", {"agent_id": colleague, "mode": mode}, "test-session")
        assert channel.responses[-1]["ok"] is False and channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"
        await delete(object(), "delete-2", {"agent_id": colleague, "mode": mode}, "test-session")
        assert channel.responses[-1]["ok"] is True
        assert saved_apps() == [("app-theirs", theirs)]
        service = host._service()
        assert [item.agent_id for item in service.list_colleagues("local")] == [other]
        assert service.get_colleague("local", theirs).enabled is True
        if mode == "unbind":
            assert service.get_colleague("local", mine).enabled is False
            assert service.current_snapshot("local", mine).agent_id == mine
        else:
            assert_nothing_left(service, "local", mine, colleague)
        # An ID that never named a colleague is still refused and removes nothing.
        await delete(object(), "delete-3", {"agent_id": "mate_unknown", "mode": mode}, "test-session")
        assert channel.responses[-1]["ok"] is False and channel.responses[-1]["code"] == "NOT_FOUND"
        assert saved_apps() == [("app-theirs", theirs)]
    finally:
        await host.adapter.aclose()


# F_02_02-V-16 adding a connection while its colleague is deleted leaves nothing behind


def test_a_connection_is_not_added_to_a_colleague_deleted_after_the_check(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    reached, resume = threading.Event(), threading.Event()
    insert = service._repository.insert_connection_agent

    def paused(definition, connection):
        reached.set()
        assert resume.wait(5)
        return insert(definition, connection)

    service._repository.insert_connection_agent = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        attaching = pool.submit(service.attach_connection, NS, colleague, "dingtalk", "app-race")
        assert reached.wait(5)
        make_service(tmp_path).delete_colleague(NS, colleague, record_removal=True)
        resume.set()
        with pytest.raises(PersistentAgentError) as refused:
            attaching.result(5)
    assert refused.value.code == "NOT_FOUND"
    assert_nothing_left(service, NS, colleague)
    with sqlite3.connect(service._repository.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM colleagues").fetchone()[0] == 0


def test_a_new_agent_deleted_while_its_version_is_prepared_leaves_no_office(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    reached, resume = threading.Event(), threading.Event()
    prepare = service._prepare_version

    def paused(namespace_id, agent, chosen, models):
        reached.set()
        assert resume.wait(5)
        return prepare(namespace_id, agent, chosen, models)

    service._prepare_version = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        attaching = pool.submit(service.attach_connection, NS, colleague, "dingtalk", "app-race")
        assert reached.wait(5)
        other = make_service(tmp_path)
        agent = other.connections(NS, colleague)[0].agent_id
        other.delete_colleague(NS, agent)
        other.delete_colleague(NS, colleague, record_removal=True)
        resume.set()
        with pytest.raises(PersistentAgentError) as refused:
            attaching.result(5)
    # Preparing the version recreated the deleted agent's office; the failed attach removed it.
    assert refused.value.code == "NOT_FOUND"
    assert_nothing_left(service, NS, agent, colleague)


@pytest.mark.asyncio
async def test_a_connection_added_while_its_colleague_is_purged_is_purged_too(tmp_path):
    host = WebHost(tmp_path)
    colleague = await host.colleague()
    first = (await host.adapter.attach_connection(
        {"config_id": colleague, "channel": "feishu", "connection_key": "app-a"},
    ))["agent_id"]
    purge = host.adapter._purge_agent
    added = []

    async def add_during_purge(manager, agent_id, im_runtime, *, record_removal=False):
        # The purge has read the connections; another app is bound before the colleague's row goes.
        if not added:
            added.append(None)
            added[0] = (await host.adapter.attach_connection(
                {"config_id": colleague, "channel": "dingtalk", "connection_key": "app-late"},
            ))["agent_id"]
        await purge(manager, agent_id, im_runtime, record_removal=record_removal)

    host.adapter._purge_agent = add_during_purge
    try:
        assert await host.adapter.remove_colleague({"config_id": colleague, "mode": "purge"}) == {"removed": True}
        assert_nothing_left(host._service(), "local", first, added[0], colleague)
    finally:
        await host.adapter.aclose()


def test_a_colleague_keeps_its_row_and_files_while_it_has_connections(tmp_path):
    service = make_service(tmp_path)
    colleague = service.create_colleague(NS, settings()).agent_id
    agent = service.attach_connection(NS, colleague, "dingtalk", "app-b")

    with pytest.raises(PersistentAgentError) as refused:
        service.delete_colleague(NS, colleague)

    assert refused.value.code == "CONFLICT"
    assert service.connection_of(NS, agent).config_id == colleague
    assert service.read_settings(NS, colleague).persona == "你是会计助手。"
    with pytest.raises(PersistentAgentError) as conflict:
        service._repository.delete(NS, colleague)
    assert conflict.value.code == "CONFLICT"


def test_deletion_tells_a_hidden_or_purged_colleague_from_an_unknown_id(tmp_path):
    service = make_service(tmp_path)
    hidden = service.create_colleague(NS, settings()).agent_id
    agent = service.attach_connection(NS, hidden, "dingtalk", "app-b")
    purged = service.create_colleague(NS, settings(name="出纳助手")).agent_id
    service.hide_colleague(NS, hidden)
    service.hide_colleague(NS, hidden)
    service.delete_colleague(NS, purged, record_removal=True)

    assert [item.agent_id for item in service.removal_connections(NS, hidden)] == [agent]
    assert service.removal_connections(NS, purged) is None
    with pytest.raises(PersistentAgentError) as unknown:
        service.removal_connections(NS, "mate_unknown")
    assert unknown.value.code == "NOT_FOUND"
    with pytest.raises(PersistentAgentError) as connected:
        service.attach_connection(NS, hidden, "feishu", "app-new")
    assert connected.value.code == "NOT_FOUND"


# F_02_02-V-18 Gateway keeps attach, saving the app and deletion in one order


class _ColleagueGateway:
    """Real Gateway handlers and Adapter, with replaceable transport and no platform network."""

    def __init__(self, host, channel, manager, path, config_module):
        self.host = host
        self.channel = channel
        self.manager = manager
        self.path = path
        self.config_module = config_module
        self.client = SimpleNamespace(send_request=self.forward)
        self.calls = []
        self.agents = {}
        self.request_number = 0
        _register_web_handlers(WebHandlersBindParams(
            channel=channel, channel_manager=manager, agent_client=self.client,
        ))

    async def forward(self, envelope):
        self.calls.append(envelope.method)
        try:
            if envelope.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value:
                payload = await self.host.adapter.attach_connection(envelope.params)
                self.agents.setdefault(envelope.params["config_id"], set()).add(payload["agent_id"])
            elif envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value:
                payload = await self.host.adapter.colleague_connections(envelope.params)
            elif envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value:
                payload = await self.host.adapter.remove_colleague(envelope.params)
            else:
                # Optional CLI preparation is queued separately; this test never installs a CLI.
                payload = {"status": "pending"}
        except PersistentAgentError as exc:
            return SimpleNamespace(ok=False, payload={"error": str(exc), "code": exc.code})
        return SimpleNamespace(ok=True, payload=payload)

    async def call(self, method, params):
        self.request_number += 1
        request_id = f"connection-test-{self.request_number}"
        await self.channel.methods[method](object(), request_id, params, "test-session")
        return next(response for response in self.channel.responses if response["id"] == request_id)

    async def bind(self, platform, config_id, app_id="app-test"):
        credentials = (
            {"client_id": app_id, "client_secret": "fake"}
            if platform == "dingtalk" else {"app_id": app_id, "app_secret": "fake"}
        )
        return await self.call(f"channel.{platform}.bind_agent", {"config_id": config_id, **credentials})

    async def delete(self, config_id, mode="purge"):
        return await self.call("jiuwenmate.colleague.delete", {"agent_id": config_id, "mode": mode})

    def apps(self, platform):
        return self.config_module.load_yaml_round_trip(self.path)["channels"][platform]["apps"]

    def assert_bindings_live(self):
        service = self.host._service()
        for platform in ("feishu", "dingtalk"):
            for app in self.apps(platform):
                agent_id = app.get("persistent_agent_id")
                if agent_id:
                    connection = service.connection_of("local", agent_id)
                    assert connection is not None
                    assert connection.channel == platform
                    assert connection.connection_key == app.get("app_id", app.get("client_id"))
                    assert service.get_colleague("local", agent_id).enabled

    def assert_removed(self, config_id, mode):
        service = self.host._service()
        agent_ids = self.agents.get(config_id, set())
        for platform in ("feishu", "dingtalk"):
            assert not any(app.get("persistent_agent_id") in agent_ids | {config_id} for app in self.apps(platform))
            identity_key = "app_id" if platform == "feishu" else "client_id"
            assert any(app[identity_key] == f"ordinary-{platform}" for app in self.apps(platform))
        if mode == "purge":
            assert_nothing_left(service, "local", *agent_ids, config_id)
        else:
            assert not service.get_colleague("local", config_id).enabled
            for agent_id in agent_ids:
                assert not service.get_colleague("local", agent_id).enabled
                assert service.connection_of("local", agent_id).config_id == config_id
                assert service.workspaces.path("local", agent_id).exists()


@pytest.fixture
async def colleague_gateway(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    host = WebHost(tmp_path)
    config_id, other_id = await host.colleague(), await host.colleague()
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    channels = {
        "feishu": {"apps": [{"app_id": "ordinary-feishu", "app_secret": "fake"}]},
        "dingtalk": {"apps": [{"client_id": "ordinary-dingtalk", "client_secret": "fake", "allow_from": []}]},
    }
    config_module.dump_yaml_round_trip(path, {"channels": channels})
    manager = FakeChannelManager()
    manager.configs.update(channels)
    gateway = _ColleagueGateway(host, FakeWebChannel(), manager, path, config_module)
    gateway.config_id, gateway.other_id = config_id, other_id
    try:
        yield gateway
    finally:
        await host.adapter.aclose()


@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.asyncio
async def test_gateway_serializes_a_binding_with_colleague_deletion(colleague_gateway, platform, mode):
    gateway = colleague_gateway
    entered, resume, delete_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pause_attach_answer(envelope):
        response = await gateway.forward(envelope)
        if envelope.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value:
            entered.set()
            await resume.wait()
        return response

    async def delete():
        delete_started.set()
        return await gateway.delete(gateway.config_id, mode)

    gateway.client.send_request = pause_attach_answer
    binding = asyncio.create_task(gateway.bind(platform, gateway.config_id))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        deletion = asyncio.create_task(delete())
        await asyncio.wait_for(delete_started.wait(), timeout=2)
        # Let an unguarded handler reach its transport task; no wall-clock delay is needed.
        await asyncio.sleep(0)
        assert ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value not in gateway.calls
        resume.set()
        bound, removed = await asyncio.wait_for(asyncio.gather(binding, deletion), timeout=5)
        assert bound["ok"] and removed["ok"]
        gateway.assert_removed(gateway.config_id, mode)
    finally:
        resume.set()
        tasks = [binding] if deletion is None else [binding, deletion]
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.asyncio
async def test_gateway_rejects_a_binding_waiting_for_colleague_deletion(colleague_gateway, platform, mode):
    gateway = colleague_gateway
    entered, resume, bind_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pause_connection_list(envelope):
        response = await gateway.forward(envelope)
        if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value:
            entered.set()
            await resume.wait()
        return response

    async def bind():
        bind_started.set()
        return await gateway.bind(platform, gateway.config_id)

    gateway.client.send_request = pause_connection_list
    deletion = asyncio.create_task(gateway.delete(gateway.config_id, mode))
    binding = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        binding = asyncio.create_task(bind())
        await asyncio.wait_for(bind_started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value not in gateway.calls
        resume.set()
        removed, bound = await asyncio.wait_for(asyncio.gather(deletion, binding), timeout=5)
        assert removed["ok"]
        assert not bound["ok"] and bound["code"] == "NOT_FOUND"
        gateway.assert_removed(gateway.config_id, mode)
    finally:
        resume.set()
        tasks = [deletion] if binding is None else [deletion, binding]
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.asyncio
async def test_gateway_removes_an_already_saved_binding(colleague_gateway, platform):
    gateway = colleague_gateway
    assert (await gateway.bind(platform, gateway.config_id))["ok"]
    gateway.assert_bindings_live()
    assert (await gateway.delete(gateway.config_id))["ok"]
    gateway.assert_removed(gateway.config_id, "purge")


@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.parametrize("operation", ["bind", "delete"])
@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel"])
@pytest.mark.asyncio
async def test_gateway_operation_failure_releases_its_colleague(
    colleague_gateway, platform, operation, failure,
):
    gateway = colleague_gateway
    if operation == "delete":
        assert (await gateway.bind(platform, gateway.config_id))["ok"]
    method = (
        ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value if operation == "bind"
        else ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value
    )
    entered, never = asyncio.Event(), asyncio.Event()
    failed = False

    async def fail_once(envelope):
        nonlocal failed
        if envelope.method == method and not failed:
            failed = True
            if failure == "exception":
                raise ConnectionError("test transport failure")
            entered.set()
            if failure == "timeout":
                await asyncio.wait_for(never.wait(), timeout=0.02)
            await never.wait()
        return await gateway.forward(envelope)

    gateway.client.send_request = fail_once
    operation_task = asyncio.create_task(
        gateway.bind(platform, gateway.config_id) if operation == "bind" else gateway.delete(gateway.config_id),
    )
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), timeout=2)
        operation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation_task
    else:
        response = await asyncio.wait_for(operation_task, timeout=2)
        assert not response["ok"] and response["code"] == "SERVICE_UNAVAILABLE"
        assert failed
    if operation == "delete":
        assert (await asyncio.wait_for(gateway.bind(platform, gateway.config_id), timeout=2))["ok"]
        gateway.assert_bindings_live()
    assert (await asyncio.wait_for(gateway.delete(gateway.config_id), timeout=5))["ok"]
    gateway.assert_removed(gateway.config_id, "purge")


@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.asyncio
async def test_gateway_does_not_block_another_colleague(colleague_gateway, platform):
    gateway = colleague_gateway
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause_one_colleague(envelope):
        response = await gateway.forward(envelope)
        if (
            envelope.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value
            and envelope.params["config_id"] == gateway.config_id
        ):
            entered.set()
            await resume.wait()
        return response

    gateway.client.send_request = pause_one_colleague
    binding = asyncio.create_task(gateway.bind(platform, gateway.config_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        other = await asyncio.wait_for(gateway.bind(platform, gateway.other_id, "app-other"), timeout=2)
        assert other["ok"] and not binding.done()
        resume.set()
        assert (await asyncio.wait_for(binding, timeout=2))["ok"]
        gateway.assert_bindings_live()
        assert (await gateway.delete(gateway.config_id))["ok"]
        gateway.assert_removed(gateway.config_id, "purge")
        gateway.assert_bindings_live()
        assert gateway.host._service().get_colleague("local", gateway.other_id).enabled
    finally:
        resume.set()
        await asyncio.gather(binding, return_exceptions=True)
