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
        if platform == "welink":
            return await self.call("channel.welink.assistant_credential_save", {
                "config_id": config_id, "account": app_id,
                "assistant_credential_id": f"test-credential-{config_id}", "assistant_secret": "fake",
            })
        credentials = (
            {"client_id": app_id, "client_secret": "fake"}
            if platform == "dingtalk" else {"app_id": app_id, "app_secret": "fake"}
        )
        return await self.call(f"channel.{platform}.bind_agent", {"config_id": config_id, **credentials})

    async def delete(self, config_id, mode="purge"):
        return await self.call("jiuwenmate.colleague.delete", {"agent_id": config_id, "mode": mode})

    def apps(self, platform):
        section = self.config_module.load_yaml_round_trip(self.path)["channels"].get(platform, {})
        if platform == "welink":
            from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import normalize_welink_section
            return normalize_welink_section(section)["bindings"]
        return section["apps"]

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
        other = await asyncio.wait_for(gateway.bind(platform, gateway.other_id, "app-test" if platform == "welink" else "app-other"), timeout=2)
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


@pytest.mark.parametrize("app_id", ["app-test", "app-new"])
@pytest.mark.parametrize("platform", ["feishu", "dingtalk", "welink"])
@pytest.mark.asyncio
async def test_gateway_binding_after_a_timed_out_deletion_never_names_a_removed_agent(
    colleague_gateway, platform, app_id,
):
    gateway = colleague_gateway
    assert (await gateway.bind(platform, gateway.config_id))["ok"]
    adapter, service = gateway.host.adapter, gateway.host.adapter._colleagues()
    purge = adapter._purge_agent
    purging, resume = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        purging.set()
        await resume.wait()
        await purge(*args, **kwargs)

    adapter._purge_agent = paused
    attach, entered = service.attach_connection, threading.Event()

    def watched(*args):
        entered.set()
        return attach(*args)

    service.attach_connection = watched
    background = []

    async def stop_waiting_for_removal(envelope):
        if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value:
            # AgentServer keeps purging after Gateway has given up on the answer.
            background.append(asyncio.create_task(gateway.forward(envelope)))
            await purging.wait()
            raise asyncio.TimeoutError()
        return await gateway.forward(envelope)

    gateway.client.send_request = stop_waiting_for_removal
    binding = None
    try:
        deleted = await asyncio.wait_for(gateway.delete(gateway.config_id), timeout=5)
        assert not deleted["ok"] and deleted["code"] == "SERVICE_UNAVAILABLE"
        binding = asyncio.create_task(gateway.bind(platform, gateway.config_id, "app-test" if platform == "welink" else app_id))
        # Bounded only so that an unguarded attach shows up; the guarded one cannot enter here.
        assert not await asyncio.to_thread(entered.wait, 0.2)
        resume.set()
        await asyncio.wait_for(asyncio.gather(*background), timeout=5)
        bound = await asyncio.wait_for(binding, timeout=5)
        assert not bound["ok"] and bound["code"] == "NOT_FOUND"
        gateway.assert_removed(gateway.config_id, "purge")
        assert all(app.get("app_id", app.get("client_id")) != app_id for app in gateway.apps(platform))
    finally:
        resume.set()
        await asyncio.gather(*background, *(task for task in (binding,) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("first", ["cli", "assistant"])
@pytest.mark.asyncio
async def test_welink_authorization_steps_share_one_isolated_runtime(colleague_gateway, tmp_path, monkeypatch, first):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login
    from jiuwenswarm.server.jiuwenmate.welink_inbound import credential_dir_for

    gateway = colleague_gateway
    pending = tmp_path / "pending-login"
    pending.mkdir()
    (pending / "auth.json").write_text('{"synthetic":true}')
    session = welink_login.WelinkLoginSession(
        session_id="login-test", agent_id=gateway.config_id, account="", resolved_account="account-a",
        credential_dir=pending, status="success", cli_path="welink-cli",
    )
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "get", lambda key: session)
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "refresh", lambda *args: session.snapshot())

    async def step(kind):
        if kind == "cli":
            return await gateway.call("channel.welink.login_status", {"session_id": session.session_id})
        return await gateway.bind("welink", gateway.config_id, "account-a")

    assert (await step(first))["ok"]
    partial = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    agent_id = partial["bindings"][0]["persistent_agent_id"]
    assert agent_id != gateway.config_id and partial["enabled"] is False
    assert (await step("assistant" if first == "cli" else "cli"))["ok"]
    stored = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    assert stored["bindings"][0]["persistent_agent_id"] == agent_id and stored["enabled"] is True
    assert stored["bindings"][0]["credential_dir"] == str(credential_dir_for(agent_id))
    assert stored["reader_credential_dir"] == str(credential_dir_for("shared-reader"))
    assert (credential_dir_for("shared-reader") / "auth.json").read_text() == '{"synthetic":true}'
    # F_02_02-V-26 the login's own directory is gone, so purge leaves no credentials behind.
    assert not pending.exists()
    if first == "cli":
        # An overlapping poll that passed the saved check before the first save finished.
        session.saved = False
        assert (await step("cli"))["ok"]
        again = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
        assert again["reader_credential_dir"] == stored["reader_credential_dir"] and again["enabled"] is True
        assert (credential_dir_for("shared-reader") / "auth.json").exists()
    service = gateway.host._service()
    connection = service.connection_of("local", agent_id)
    assert (connection.channel, connection.connection_key, connection.config_id) == (
        "welink", f"account-a:{gateway.config_id}", gateway.config_id,
    )
    assert service.workspaces.path("local", agent_id) != service.workspaces.path("local", gateway.config_id)
    monkeypatch.setattr(gateway.config_module, "get_config", lambda: gateway.config_module.load_yaml_round_trip(gateway.path))
    listed = await gateway.host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=gateway.config_id)
    connections = listed.payload["colleague"]["connections"]
    assert [(item["channel"], item["agent_id"]) for item in connections] == [
        ("web", gateway.config_id), ("welink", agent_id),
    ]
    assert (await gateway.delete(gateway.config_id))["ok"]
    gateway.assert_removed(gateway.config_id, "purge")


# F_02_02-V-23 the two WeLink steps may spell one account differently


@pytest.mark.parametrize("first", ["cli", "assistant"])
@pytest.mark.asyncio
async def test_welink_steps_spelling_one_account_differently_share_one_runtime(
    colleague_gateway, tmp_path, monkeypatch, first,
):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login

    gateway = colleague_gateway
    pending = tmp_path / "pending-login"
    pending.mkdir()
    (pending / "auth.json").write_text('{"synthetic":true}')
    # The scan reports one spelling; the user typed the same account in another case.
    session = welink_login.WelinkLoginSession(
        session_id="login-test", agent_id=gateway.config_id, account="", resolved_account="Account-A",
        credential_dir=pending, status="success", cli_path="welink-cli",
    )
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "get", lambda key: session)
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "refresh", lambda *args: session.snapshot())

    async def step(kind):
        if kind == "cli":
            return await gateway.call("channel.welink.login_status", {"session_id": session.session_id})
        return await gateway.bind("welink", gateway.config_id, " account-a ")

    assert (await step(first))["ok"]
    assert (await step("assistant" if first == "cli" else "cli"))["ok"]
    stored = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    assert len(gateway.agents[gateway.config_id]) == 1
    assert stored["enabled"] is True and stored["bindings"][0]["persistent_agent_id"] in gateway.agents[gateway.config_id]
    connection = gateway.host._service().connection_of("local", stored["bindings"][0]["persistent_agent_id"])
    assert (connection.channel, connection.connection_key) == ("welink", f"account-a:{gateway.config_id}")
    monkeypatch.setattr(gateway.config_module, "get_config", lambda: gateway.config_module.load_yaml_round_trip(gateway.path))
    listed = await gateway.host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=gateway.config_id)
    assert [item["channel"] for item in listed.payload["colleague"]["connections"]] == ["web", "welink"]


@pytest.mark.asyncio
async def test_welink_switching_account_or_colleague_never_reuses_an_agent(colleague_gateway):
    gateway = colleague_gateway
    first = await gateway.bind("welink", gateway.config_id, "account-a")
    same = await gateway.bind("welink", gateway.config_id, "account-a")
    second = await gateway.bind("welink", gateway.config_id, "account-b")
    moved = await gateway.bind("welink", gateway.other_id, "account-a")
    first_id = first["payload"]["persistent_agent_id"]
    assert same["payload"]["persistent_agent_id"] == first_id
    assert second["ok"] is False and second["code"] == "BAD_REQUEST"
    assert moved["ok"] is True
    assert moved["payload"]["persistent_agent_id"] != first_id
    assert gateway.host._service().connection_of("local", first_id).config_id == gateway.config_id


@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.asyncio
async def test_welink_scan_completed_after_deletion_cannot_restore_a_binding(colleague_gateway, monkeypatch, mode):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_assistant_login

    gateway = colleague_gateway
    session = SimpleNamespace(agent_id=gateway.config_id, account="account-late", saved=False,
                              status="success", error="", credentials=lambda: ("test-id", "fake"))
    session.snapshot = lambda: {"status": session.status, "error": session.error}
    monkeypatch.setattr(welink_assistant_login.ASSISTANT_LOGIN_MANAGER, "get", lambda key: session)
    monkeypatch.setattr(welink_assistant_login.ASSISTANT_LOGIN_MANAGER, "refresh", lambda *args: session.snapshot())
    assert (await gateway.delete(gateway.config_id, mode))["ok"]
    response = await gateway.call("channel.welink.assistant_login_status", {"session_id": "late"})
    assert response["ok"] and response["payload"]["status"] == "failed"
    assert not session.saved and gateway.apps("welink") == []
    gateway.assert_removed(gateway.config_id, mode)


@pytest.mark.parametrize("registered_channel,registered_account", [(None, None), ("feishu", "account-a"), ("welink", "account-b")])
@pytest.mark.asyncio
async def test_welink_rejects_missing_or_wrong_connection_before_reading_data(monkeypatch, registered_channel, registered_account):
    from datetime import datetime, timezone
    from jiuwenswarm.server.jiuwenmate import welink_inbound

    connection = None if registered_channel is None else SimpleNamespace(channel=registered_channel, connection_key=registered_account)
    service = SimpleNamespace(connection_of=lambda *args: connection)
    binding = welink_inbound.WelinkBinding(account="account-a", agent_id="agent-a")
    monkeypatch.setattr(welink_inbound, "welink_binding_for_agent", lambda agent: binding)
    event = welink_inbound.WelinkInboundEvent(
        account="account-a", expected_agent_id="agent-a", message_id="message-a", chat_id="group-a",
        sender_id="human-a", sender_name="Human", text="synthetic task", received_at=datetime.now(timezone.utc),
    )
    assert await welink_inbound.accept_welink_message(SimpleNamespace(_service=service), event) == InboundDisposition.REJECTED


@pytest.mark.asyncio
async def test_welink_cli_login_stages_credentials_outside_runtime_workspaces(colleague_gateway, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login
    from jiuwenswarm.server.jiuwenmate.welink_inbound import credential_dir_for

    captured = {}

    def start(**fields):
        captured.update(fields)
        return SimpleNamespace(snapshot=lambda: {"session_id": "staged", "status": "pending"})

    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "start", start)
    gateway = colleague_gateway
    response = await gateway.call("channel.welink.login_start", {"config_id": gateway.config_id})
    assert response["ok"]
    assert captured["agent_id"] == gateway.config_id
    assert captured["credential_dir"].parent == credential_dir_for(gateway.config_id).parent
    assert captured["credential_dir"].name.startswith("pending-")
    assert captured["credential_dir"] != credential_dir_for(gateway.config_id)


# F_02_02-V-21 each completed CLI login keeps its own credentials while saving


@pytest.mark.asyncio
async def test_welink_pending_login_reuses_its_session_but_a_completed_login_does_not(colleague_gateway, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login

    gateway = colleague_gateway
    manager = welink_login.WelinkLoginManager()
    spawned = []
    monkeypatch.setattr(manager, "_logout_other_account", lambda *args: None)

    def spawn(session, cli_path, env_name):
        spawned.append(session)
        session.credential_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(welink_login, "LOGIN_MANAGER", manager)
    params = {"config_id": gateway.config_id, "account": "account-a"}
    first = await gateway.call("channel.welink.login_start", params)
    repeated = await gateway.call("channel.welink.login_start", params)
    assert first["payload"]["session_id"] == repeated["payload"]["session_id"]
    assert len(spawned) == 1
    spawned[0].status = "success"
    assert not spawned[0].saved
    restarted = await gateway.call("channel.welink.login_start", params)
    assert restarted["payload"]["session_id"] != first["payload"]["session_id"]
    assert len(spawned) == 2
    assert spawned[0].credential_dir != spawned[1].credential_dir
    assert len(list(spawned[0].credential_dir.parent.iterdir())) == 2


@pytest.mark.asyncio
async def test_welink_completed_login_does_not_copy_another_accounts_credentials(colleague_gateway, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login

    gateway = colleague_gateway
    manager = welink_login.WelinkLoginManager()
    spawned = []
    monkeypatch.setattr(manager, "_logout_other_account", lambda *args: None)

    def spawn(session, cli_path, env_name):
        account = "account-a" if not spawned else "account-b"
        spawned.append(session)
        session.credential_dir.mkdir(parents=True, exist_ok=True)
        (session.credential_dir / "auth.json").write_text(json.dumps({"synthetic_account": account}))
        session.resolved_account = account
        session.status = "success"

    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(welink_login, "LOGIN_MANAGER", manager)
    first = await gateway.call("channel.welink.login_start", {"config_id": gateway.config_id})
    assert first["ok"]
    entered, resume = asyncio.Event(), asyncio.Event()
    paused = False

    async def pause_attach(envelope):
        nonlocal paused
        response = await gateway.forward(envelope)
        if envelope.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value and not paused:
            paused = True
            entered.set()
            await resume.wait()
        return response

    gateway.client.send_request = pause_attach
    saving = asyncio.create_task(gateway.call("channel.welink.login_status", {
        "session_id": first["payload"]["session_id"],
    }))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = await gateway.call("channel.welink.login_start", {"config_id": gateway.config_id})
        assert second["ok"] and len(spawned) == 2
        assert spawned[0].session_id != spawned[1].session_id
        resume.set()
        assert (await asyncio.wait_for(saving, 3))["payload"]["status"] == "success"
        assert (await gateway.bind("welink", gateway.config_id, "account-a"))["ok"]
        stored = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
        assert stored["enabled"] is True
        credential = json.loads((Path(stored["reader_credential_dir"]) / "auth.json").read_text())
        assert credential["synthetic_account"] == stored["account"] == "account-a"
    finally:
        resume.set()
        await asyncio.gather(saving, return_exceptions=True)


# F_02_02-V-29 authorization retries reload even when Gateway remembers the failed snapshot


@pytest.mark.parametrize("operation", ["save", "revoke"])
@pytest.mark.asyncio
async def test_welink_authorization_retry_reapplies_a_failed_configuration(colleague_gateway, operation):
    from unittest.mock import MagicMock
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    initial = colleague_gateway
    bound = await initial.bind("welink", initial.config_id, "account-a")
    assert bound["ok"]
    ready = dict(initial.manager.get_conf("welink"))
    ready.update(enabled=True, cli_authorized=True, reader_credential_dir="synthetic-cli-dir")
    initial.config_module.update_config(lambda data: {
        **data, "channels": {**data["channels"], "welink": ready},
    })
    attempts = []
    seen = dict(ready)

    async def reload(config):
        nonlocal seen
        current = dict(config["welink"])
        forced = manager.pop_channel_restart_pending()
        changed = current != seen
        seen = current
        if not changed and "welink" not in forced:
            return
        attempts.append(current)
        if len(attempts) == 1:
            raise RuntimeError("injected reload failure")

    manager = ChannelManager(MagicMock(), config={**initial.manager.configs, "welink": ready}, on_config_updated=reload)
    gateway = _ColleagueGateway(initial.host, initial.channel, manager, initial.path, initial.config_module)
    gateway.request_number = initial.request_number
    if operation == "save":
        method = "channel.welink.assistant_credential_save"
        params = {"config_id": initial.config_id, "account": "account-a",
                  "assistant_credential_id": "updated-credential", "assistant_secret": "fake"}
    else:
        method = "channel.welink.revoke"
        params = {"persistent_agent_id": ready["bindings"][0]["persistent_agent_id"]}
    assert not (await gateway.call(method, params))["ok"]
    assert (await gateway.call(method, params))["ok"]
    assert len(attempts) == 2
    assert manager.get_conf("welink") == gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]


@pytest.mark.asyncio
async def test_welink_login_does_not_report_saved_when_pending_cleanup_fails(colleague_gateway, monkeypatch, tmp_path):
    import shutil
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login

    gateway = colleague_gateway
    pending = tmp_path / "pending-cleanup-failure"
    pending.mkdir()
    (pending / "auth.json").write_text('{"synthetic":true}')
    session = SimpleNamespace(agent_id=gateway.config_id, account="account-a", resolved_account="account-a",
                              credential_dir=pending, cli_path="welink-cli", env_name="pro",
                              saved=False, status="success", error="")
    session.snapshot = lambda: {"status": session.status, "error": session.error}
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "get", lambda key: session)
    monkeypatch.setattr(welink_login.LOGIN_MANAGER, "refresh", lambda *args: session.snapshot())
    remove = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if Path(path) == pending:
            if kwargs.get("ignore_errors"):
                return
            raise OSError("injected pending cleanup failure")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_cleanup)
        response = await gateway.call("channel.welink.login_status", {"session_id": "cleanup-failure"})
    assert response["payload"]["status"] == "failed"
    assert not session.saved
    assert gateway.apps("welink") == []


# F_02_02-V-22 deletion follows persisted bindings even after a real reload rollback


@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.parametrize("failed_reloads", [1, 2])
@pytest.mark.asyncio
async def test_welink_delete_clears_persisted_binding_after_reload_rollback(colleague_gateway, mode, failed_reloads):
    from unittest.mock import MagicMock
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    initial = colleague_gateway
    reloads = []

    async def reload(config):
        forced = manager.pop_channel_restart_pending()
        current = dict(config["welink"])
        # Gateway remembers the requested configuration before it starts/stops channels.
        changed = not reloads or current != reloads[-1]
        if not changed and "welink" not in forced:
            return
        reloads.append(current)
        if len(reloads) <= failed_reloads:
            raise RuntimeError("injected reload failure")

    manager = ChannelManager(MagicMock(), config=initial.manager.configs, on_config_updated=reload)
    gateway = _ColleagueGateway(initial.host, initial.channel, manager, initial.path, initial.config_module)
    assert not (await gateway.bind("welink", initial.config_id, "account-a"))["ok"]
    saved = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    agent_id = saved["bindings"][0]["persistent_agent_id"]
    assert manager.get_conf("welink") == {}
    deleted = await gateway.delete(initial.config_id, mode)
    if failed_reloads == 2:
        assert not deleted["ok"]
        service = gateway.host._service()
        assert service.get_config("local", initial.config_id).enabled
        assert service.connection_of("local", agent_id) is not None
        assert manager.get_conf("welink") == {}
        deleted = await gateway.delete(initial.config_id, mode)
    assert deleted["ok"]
    remaining = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    assert remaining == manager.get_conf("welink")
    assert remaining["bindings"] == [] and remaining["enabled"] is False
    assert remaining["account"] == saved["account"]
    assert len(reloads) == failed_reloads + 1
    gateway.assert_removed(initial.config_id, mode)


@pytest.mark.asyncio
async def test_welink_delete_does_not_clear_another_configs_persisted_binding(colleague_gateway):
    from unittest.mock import MagicMock
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    initial = colleague_gateway
    reloads = []

    async def reload(config):
        reloads.append(dict(config["welink"]))
        if len(reloads) == 2:
            raise RuntimeError("injected reload failure")

    manager = ChannelManager(MagicMock(), config=initial.manager.configs, on_config_updated=reload)
    gateway = _ColleagueGateway(initial.host, initial.channel, manager, initial.path, initial.config_module)
    assert (await gateway.bind("welink", initial.config_id, "account-a"))["ok"]
    stale = manager.get_conf("welink")
    assert not (await gateway.bind("welink", initial.other_id, "account-a"))["ok"]
    stored = dict(gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"])
    assert len(stored["bindings"]) == 2
    assert len(stale["bindings"]) == 1
    assert manager.get_conf("welink") == stale
    assert (await gateway.delete(initial.config_id))["ok"]
    remaining = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    assert len(remaining["bindings"]) == 1
    assert remaining["bindings"][0] == stored["bindings"][1]
    assert manager.get_conf("welink") == remaining
    connection = gateway.host._service().connection_of("local", remaining["bindings"][0]["persistent_agent_id"])
    assert connection.config_id == initial.other_id
    gateway.assert_removed(initial.config_id, "purge")


# F_02_02-V-25 deleting a colleague whose WeLink slot moved on leaves the current binding running


@pytest.mark.asyncio
async def test_welink_delete_leaves_another_colleagues_slot_running(colleague_gateway):
    from unittest.mock import MagicMock
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    initial = colleague_gateway
    reloads = []
    failing = False

    async def reload(config):
        reloads.append(dict(config.get("welink") or {}))
        if failing:
            raise RuntimeError("the other colleague's WeLink cannot start")

    manager = ChannelManager(MagicMock(), config=initial.manager.configs, on_config_updated=reload)
    gateway = _ColleagueGateway(initial.host, initial.channel, manager, initial.path, initial.config_module)
    assert (await gateway.bind("welink", initial.config_id, "account-a"))["ok"]
    assert (await gateway.bind("welink", initial.other_id, "account-a"))["ok"]
    stored = dict(gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"])
    before = len(reloads)
    assert (await gateway.delete(initial.config_id))["ok"]
    assert len(reloads) == before + 1
    remaining = gateway.config_module.load_yaml_round_trip(gateway.path)["channels"]["welink"]
    assert remaining["bindings"] == [stored["bindings"][1]]
    assert manager.get_conf("welink") == remaining
    gateway.assert_removed(initial.config_id, "purge")


@pytest.mark.parametrize("account", ["account-a", "Account-A"])
@pytest.mark.asyncio
async def test_welink_inbound_stores_its_request_under_the_connection_agent(colleague_gateway, monkeypatch, account):
    from datetime import datetime, timezone
    from jiuwenswarm.server.jiuwenmate import welink_inbound
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository

    gateway = colleague_gateway
    bound = await gateway.bind("welink", gateway.config_id, "account-a")
    agent_id = bound["payload"]["persistent_agent_id"]
    # The registered key and the saved account may differ in case.
    binding = welink_inbound.WelinkBinding(account=account, agent_id=agent_id, config_id=gateway.config_id, assistant_credential_id="ak-a")
    monkeypatch.setattr(welink_inbound, "welink_binding_for_agent", lambda agent: binding)
    manager = await gateway.host.adapter._runtime()
    fanout = SimpleNamespace(
        _service=gateway.host._service(), _sessions=ExecutionRepository(gateway.host.db),
        _groups=GroupChatRepository(gateway.host.db), _agent_manager=manager,
        _clock=lambda: datetime.now(timezone.utc),
    )
    event = welink_inbound.WelinkInboundEvent(
        account=account, expected_agent_id=agent_id, assistant_credential_id="ak-a", message_id="message-isolated", chat_id="group-a",
        sender_id="human-a", sender_name="Human", text="synthetic task", received_at=datetime.now(timezone.utc),
    )
    assert await welink_inbound.accept_welink_message(fanout, event) == InboundDisposition.HANDLED
    assert await welink_inbound.accept_welink_message(fanout, event) == InboundDisposition.HANDLED
    with sqlite3.connect(gateway.host.db) as conn:
        rows = conn.execute("SELECT record_json FROM requests").fetchall()
    assert len(rows) == 1
    records = [json.loads(row[0]) for row in rows]
    assert all(item["agent_id"] == agent_id and item["agent_id"] != gateway.config_id for item in records)
    assert records[0]["route_snapshot"]["channel"] == "welink"
    assert records[0]["route_snapshot"]["app_id"] == "ak-a"


@pytest.mark.asyncio
async def test_welink_two_colleagues_share_reader_and_reject_duplicate_ak_before_attach(colleague_gateway):
    gateway = colleague_gateway
    async def save(config_id, credential):
        return await gateway.call('channel.welink.assistant_credential_save', {
            'config_id': config_id, 'account': 'same-reader',
            'assistant_credential_id': credential, 'assistant_secret': 'fake-secret',
            'assistant_account': 'member-' + credential,
        })

    first = await save(gateway.config_id, 'ak1')
    second = await save(gateway.other_id, 'ak2')
    assert first['ok'] and second['ok']
    current = gateway.config_module.load_yaml_round_trip(gateway.path)['channels']['welink']
    assert len(current['bindings']) == 2
    assert {item['assistant_credential_id'] for item in current['bindings']} == {'ak1', 'ak2'}
    before_calls = len(gateway.calls)
    rejected = await save(gateway.other_id, 'ak1')
    assert rejected['ok'] is False
    assert rejected['code'] == 'BAD_REQUEST'
    assert len(gateway.calls) == before_calls
    assert gateway.config_module.load_yaml_round_trip(gateway.path)['channels']['welink'] == current
    updated = await save(gateway.config_id, 'ak1')
    assert updated['ok'] is True
    assert 'fake-secret' not in str(updated)


@pytest.mark.asyncio
async def test_welink_legacy_authorization_update_preserves_its_runtime(colleague_gateway):
    gateway = colleague_gateway
    attached = await gateway.host.adapter.attach_connection({
        'config_id': gateway.config_id, 'channel': 'welink', 'connection_key': 'reader',
    })
    legacy = dict(account='reader', persistent_agent_id=attached['agent_id'],
                  assistant_credential_id='legacy-ak', assistant_secret='old-secret',
                  credential_dir='/legacy', cli_authorized=True, enabled=True)
    gateway.config_module.update_config(lambda data: {**data, 'channels': {**data['channels'], 'welink': legacy}})
    gateway.manager.configs['welink'] = legacy
    saved = await gateway.call('channel.welink.assistant_credential_save', {
        'config_id': gateway.config_id, 'account': 'reader',
        'assistant_credential_id': 'legacy-ak', 'assistant_secret': 'new-secret',
    })
    assert saved['ok'] is True
    assert saved['payload']['persistent_agent_id'] == attached['agent_id']
    section = gateway.config_module.load_yaml_round_trip(gateway.path)['channels']['welink']
    assert len(section['bindings']) == 1
    assert section['bindings'][0]['credential_dir'] == '/legacy'


@pytest.mark.asyncio
async def test_purging_legacy_welink_colleague_preserves_shared_reader_credentials(colleague_gateway):
    from jiuwenswarm.server.jiuwenmate.welink_inbound import credential_dir_for

    gateway = colleague_gateway
    bound = await gateway.bind('welink', gateway.config_id, 'reader')
    agent_id = bound['payload']['persistent_agent_id']
    directory = credential_dir_for(agent_id)
    directory.mkdir(parents=True)
    (directory / 'auth.json').write_text('{"synthetic":true}')
    gateway.config_module.update_config(lambda data: {
        **data, 'channels': {**data['channels'], 'welink': {
            **data['channels']['welink'], 'reader_credential_dir': str(directory), 'cli_authorized': True,
        }},
    })
    removed = await gateway.delete(gateway.config_id)
    assert removed['ok'] is True
    assert (directory / 'auth.json').read_text() == '{"synthetic":true}'
    assert gateway.host._service().connection_of('local', agent_id) is None


@pytest.mark.asyncio
async def test_welink_qr_completion_saves_optional_member_account(colleague_gateway, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_assistant_login

    gateway = colleague_gateway
    session = SimpleNamespace(agent_id=gateway.config_id, account='reader', saved=False,
                              status='success', error='', credentials=lambda: ('qr-ak', 'qr-secret'))
    session.snapshot = lambda: {'status': session.status, 'error': session.error}
    monkeypatch.setattr(welink_assistant_login.ASSISTANT_LOGIN_MANAGER, 'get', lambda key: session)
    monkeypatch.setattr(welink_assistant_login.ASSISTANT_LOGIN_MANAGER, 'refresh', lambda *args: session.snapshot())
    result = await gateway.call('channel.welink.assistant_login_status', {
        'session_id': 'qr', 'assistant_account': 'dig-reader-assistant',
    })
    assert result['ok'] is True
    stored = gateway.config_module.load_yaml_round_trip(gateway.path)['channels']['welink']
    assert stored['bindings'][0]['assistant_account'] == 'dig-reader-assistant'
