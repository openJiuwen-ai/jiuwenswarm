# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real provision commit timing with installed attach observers."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
from jiuwenswarm.runtime.session_provisioner import (
    RuntimeSessionProvisioner, SessionProvisionCommitContext, SessionProvisionCommitTiming, SessionProvisionState, SessionSwitchInput,
)
from jiuwenswarm.server.runtime.session.attach_notifications import ExtensionSessionAttachParticipant


@pytest.mark.asyncio
async def test_switch_commit_observes_view_once_and_abort_never_notifies(tmp_path,monkeypatch,caplog):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    sessions = tmp_path / "sessions"
    target = sessions / "native-view-session"
    target.mkdir(parents=True)
    (target/"metadata.json").write_text(json.dumps({"mode":"agent.work.normal","channel_id":"web","project_dir":str(tmp_path)}),encoding="utf-8")
    monkeypatch.setattr(utils,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(session_metadata,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(session_history,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(assembly,"_ASSEMBLY",None)
    monkeypatch.setattr(assembly,"resolve_install_dir",lambda:tmp_path/"store")
    host = await assembly.install_extensions()
    assert host is not None
    registry = RuntimeParticipantRegistry()
    observer = ExtensionSessionAttachParticipant()
    registry.register_host_foreground(observer)
    provisioner = RuntimeSessionProvisioner(agent_manager=SimpleNamespace(),plan_controller=SimpleNamespace(),participant_registry=registry)
    source = Path(__file__).resolve().parents[3]/"jiuwenswarm/extensions_v2/examples/session_attach"
    logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_attach")
    logger.addHandler(caplog.handler)
    try:
        assert (await host.manager.install([source]))[0].status == "SUCCESS"
        prepared = await provisioner.prepare_session_switch(SessionSwitchInput(channel_id="web",target_session_id="native-view-session",mode="agent.work.normal"))
        assert prepared.state is SessionProvisionState.PREPARED
        assert observer.associations.get("web","native-view") is None
        context = SessionProvisionCommitContext(foreground_scope_id="native-view")
        with caplog.at_level(logging.INFO,logger="jiuwenswarm.server.extensions.example.session_attach"):
            await provisioner.commit_session_provision(prepared,timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,context=context)
            first = observer.associations.get("web","native-view")
            assert first is not None and first.session_id == "native-view-session"
            assert prepared.state is SessionProvisionState.COMMITTED
            await provisioner.commit_session_provision(prepared,timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,context=context)
            aborted = await provisioner.prepare_session_switch(SessionSwitchInput(channel_id="web",target_session_id="native-view-session",mode="agent.work.normal"))
            await provisioner.abort_session_provision(aborted)
        assert observer.associations.get("web","native-view") is first
        assert caplog.text.count("session attach example:") == 1
        assert "accepted=True" in caplog.text
        assert registry.snapshot_activity() == ()
    finally:
        logger.removeHandler(caplog.handler)
        await assembly.shutdown_extensions()
        session_metadata._METADATA_CACHE.clear()


@pytest.mark.asyncio
async def test_replacement_notifies_old_view_only_after_commit(tmp_path, monkeypatch, caplog):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    sessions = tmp_path / "sessions"
    for sid in ("native-first", "native-second"):
        path = sessions / sid
        path.mkdir(parents=True)
        (path / "metadata.json").write_text(json.dumps({"mode":"agent.work.normal", "channel_id":"web", "project_dir":str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda:sessions)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda:sessions)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda:sessions)
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda:tmp_path / "store")
    host = await assembly.install_extensions()
    registry = RuntimeParticipantRegistry()
    observer = ExtensionSessionAttachParticipant()
    registry.register_host_foreground(observer)
    provisioner = RuntimeSessionProvisioner(agent_manager=SimpleNamespace(), plan_controller=SimpleNamespace(), participant_registry=registry)
    source = Path(__file__).resolve().parents[3] / "jiuwenswarm/extensions_v2/examples/session_detach"
    logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_detach")
    logger.addHandler(caplog.handler)
    try:
        assert (await host.manager.install([source]))[0].status == "SUCCESS"
        context = SessionProvisionCommitContext(foreground_scope_id="native-view")
        first = await provisioner.prepare_session_switch(SessionSwitchInput(channel_id="web", target_session_id="native-first", mode="agent.work.normal"))
        await provisioner.commit_session_provision(first, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY, context=context)
        old = observer.associations.get("web", "native-view")
        with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.extensions.example.session_detach"):
            aborted = await provisioner.prepare_session_switch(SessionSwitchInput(channel_id="web", target_session_id="native-second", mode="agent.work.normal"))
            await provisioner.abort_session_provision(aborted)
            assert observer.associations.get("web", "native-view") is old
            assert "session detach example:" not in caplog.text
            second = await provisioner.prepare_session_switch(SessionSwitchInput(channel_id="web", target_session_id="native-second", previous_session_id="native-first", mode="agent.work.normal"))
            assert observer.associations.get("web", "native-view") is old
            await provisioner.commit_session_provision(second, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY, context=context)
            await provisioner.commit_session_provision(second, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY, context=context)
        assert caplog.text.count("session detach example:") == 1
        assert "session=native-first" in caplog.text and "reason=switch accepted=True" in caplog.text
        assert observer.associations.get("web", "native-view").session_id == "native-second"
        assert (sessions / "native-first/metadata.json").is_file()
    finally:
        logger.removeHandler(caplog.handler)
        await assembly.shutdown_extensions()
        session_metadata._METADATA_CACHE.clear()
