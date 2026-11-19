# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for named OfficeClaw recovery and naming model resolution."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers import chat, team
from jiuwenswarm.server.runtime import swarm_skill_session, team_binding_store, team_entity_store
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.team_binding_store import TeamSessionBindingError

TEAM = "oc_team_preset-software-dev"
SID = "officeclaw_recovery_test"


@pytest.fixture
def state(monkeypatch, tmp_path):
    owner = {"model_client_config": {
        "model_name": "review-model", "client_provider": "OpenAI",
        "api_key": "dummy-key", "api_base": "https://tenant.example/v1",
    }, "model_config_obj": {}}
    model = {"model_client_config": {"client_provider": "OpenAI"},
             "model_request_config": {"model": "review-model", "temperature": 0.2}}
    snapshot = {"team_name": TEAM, "leader": {"member_name": "leader"},
                "agents": {name: {"model": deepcopy(model)} for name in ["leader", "reviewer"]}}
    config = {"models": {"defaults": [owner]}, "modes": {"team": {TEAM: snapshot}}}
    sessions = tmp_path / "sessions"
    store = team_binding_store.TeamBindingStore(tmp_path / "bindings.json")
    entities = team_entity_store.TeamEntityStore(tmp_path / "teams")
    monkeypatch.setattr(team_binding_store, "get_team_binding_store", lambda: store)
    monkeypatch.setattr(team_entity_store, "get_team_entity_store", lambda: entities)
    monkeypatch.setattr(chat, "_sessions_dir_for_request", lambda request: sessions)
    monkeypatch.setattr(team, "_sessions_dir_for_request", lambda request: sessions)
    monkeypatch.setattr(team, "_effective_config_for_request", lambda request: config)
    monkeypatch.setattr(chat, "_effective_config_for_request", lambda request: config)
    monkeypatch.setattr(swarm_skill_session, "is_identifiable_swarm_skill", lambda skill: skill == "swarm-sop")
    session_metadata.init_session_metadata(
        session_id=SID, channel_id="officeclaw", mode="team", title="original review", sessions_root=sessions,
    )
    history = sessions / SID / "history.json"
    history.write_text('[{"role":"user","content":"original history"}]', encoding="utf-8")
    store.create(team_name=TEAM, template_id=TEAM)
    store.bind_session(team_name=TEAM, session_id=SID)
    normalized = team_entity_store.normalize_team_entity_snapshot(snapshot, config)
    entities.write(team_name=TEAM, template_id=TEAM, template_snapshot=normalized)
    return SimpleNamespace(sessions=sessions, store=store, entities=entities, config=config, history=history,
                           metadata=sessions / SID / "metadata.json", snapshot=sessions / SID / ".team-template-snapshot.json")


def request(**kwargs):
    params = {"mode": "team", "team_name": TEAM, "query": "continue the review"}
    params.update(kwargs.pop("params", {}))
    return AgentRequest(request_id="recovery", channel_id="officeclaw", session_id=kwargs.pop("session_id", SID),
                        req_method=kwargs.pop("req_method", ReqMethod.CHAT_SEND), params=params, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME])
async def test_restores_original_team_before_naming(state, monkeypatch, method):
    before_store, history = state.store.path.read_bytes(), state.history.read_bytes()
    before = json.loads(state.metadata.read_text(encoding="utf-8"))
    monkeypatch.setattr(chat, "_create_generated_team_binding", lambda **kwargs: pytest.fail("must restore, not generate"))
    req = request(req_method=method, params={"team_template_id": "stale-client-template"})

    assert await chat._ensure_auto_team_binding_for_chat(None, req) == TEAM

    restored = json.loads(state.metadata.read_text(encoding="utf-8"))
    assert restored["team_name"] == TEAM
    assert restored["team_template_id"] == TEAM
    assert restored["title"] == before["title"]
    assert restored["last_message_at"] == before["last_message_at"]
    assert req.params["team_template_id"] == TEAM
    assert state.store.path.read_bytes() == before_store
    assert state.history.read_bytes() == history
    snapshot = json.loads(state.snapshot.read_text(encoding="utf-8"))
    assert snapshot["agents"]["leader"]["model"]["ref"].startswith("model-identity-v1:")
    assert "api_key" not in state.snapshot.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restores_missing_metadata_file_without_new_session(state):
    state.metadata.unlink()
    assert await chat._ensure_auto_team_binding_for_chat(None, request()) == TEAM
    restored = json.loads(state.metadata.read_text(encoding="utf-8"))
    assert restored["mode"] == "team"
    assert restored["channel_id"] == "officeclaw"
    assert state.history.exists()


@pytest.mark.asyncio
async def test_missing_owner_does_not_guess_binding_from_request(state):
    state.store.unbind_session(session_id=SID)
    before = state.metadata.read_bytes(), state.store.path.read_bytes()
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.reason == "TEAM_SESSION_BINDING_MISSING"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before


@pytest.mark.asyncio
async def test_conflicting_request_cannot_move_session(state):
    before = state.metadata.read_bytes(), state.store.path.read_bytes()
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request(params={"team_name": "other"}))
    assert error.value.code == "CONFLICT"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("file", ["metadata", "snapshot"])
async def test_corrupt_session_artifact_is_not_overwritten(state, file):
    path = getattr(state, file)
    path.write_text("not-json", encoding="utf-8")
    before_store = state.store.path.read_bytes()
    with pytest.raises(TeamSessionBindingError):
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert path.read_text(encoding="utf-8") == "not-json"
    assert state.store.path.read_bytes() == before_store


@pytest.mark.asyncio
async def test_missing_tenant_directory_never_uses_global_session(state, monkeypatch, tmp_path):
    other = tmp_path / "wrong-tenant"
    monkeypatch.setattr(chat, "_sessions_dir_for_request", lambda request: other)
    before = state.metadata.read_bytes()
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.code == "NOT_FOUND"
    assert not other.exists()
    assert state.metadata.read_bytes() == before


@pytest.mark.asyncio
async def test_unregistered_named_tenant_cannot_restore_using_global_config(state):
    req = request()
    req.agent_id = "unregistered_recovery_agent"
    before = state.metadata.read_bytes(), state.store.path.read_bytes()
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, req)
    assert error.value.reason == "TEAM_SESSION_BINDING_SCOPE_INVALID"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before


@pytest.mark.asyncio
async def test_registered_tenant_restores_against_its_effective_config(state, monkeypatch):
    from jiuwenswarm.server.handlers import _shared
    from jiuwenswarm.server.runtime.tenant_catalog_registry import TenantAgentSpec, TenantCatalogRegistry
    registry = TenantCatalogRegistry()
    config = deepcopy(state.config)
    config["models"]["defaults"][0]["model_client_config"].update(
        model_name="${MODEL_NAME}", api_key="${API_KEY}", api_base="${API_BASE}",
    )
    registry.upsert(TenantAgentSpec(
        service_id="recovery_service", agent_id="agentteam", config=config,
        env={"MODEL_NAME": "review-model", "API_KEY": "dummy-key", "API_BASE": "https://tenant.example/v1"},
    ))
    monkeypatch.setattr(TenantCatalogRegistry, "get_instance", classmethod(lambda cls: registry))
    monkeypatch.setattr(team, "_effective_config_for_request", _shared._effective_config_for_request)
    req = request()
    req.agent_id, req.service_id = "agentteam", "recovery_service"
    assert await chat._ensure_auto_team_binding_for_chat(None, req) == TEAM
    assert state.store.find_by_session(SID).team_name == TEAM
    assert "api_key" not in state.snapshot.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restore_failure_rolls_back_metadata_and_snapshot(state, monkeypatch):
    before = state.metadata.read_bytes(), state.store.path.read_bytes()
    update = session_metadata.update_session_metadata
    def fail_after_write(**kwargs):
        update(**kwargs)
        raise OSError("injected write failure")
    monkeypatch.setattr(session_metadata, "update_session_metadata", fail_after_write)
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.reason == "TEAM_SESSION_BINDING_RESTORE_FAILED"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before
    assert not state.snapshot.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["team_name", "runtime_team_name", "team_template_id"])
async def test_failed_readback_restores_original_artifacts(state, monkeypatch, field):
    before = state.metadata.read_bytes(), state.store.path.read_bytes()
    update, read = session_metadata.update_session_metadata, session_metadata.get_session_metadata
    wrote = False
    def write(**kwargs):
        nonlocal wrote
        result = update(**kwargs)
        wrote = True
        return result
    def stale(*args, **kwargs):
        result = read(*args, **kwargs)
        return {**result, field: "wrong"} if wrote else result
    monkeypatch.setattr(session_metadata, "update_session_metadata", write)
    monkeypatch.setattr(session_metadata, "get_session_metadata", stale)
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.reason == "TEAM_SESSION_BINDING_RESTORE_FAILED"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before
    assert not state.snapshot.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["request", "metadata_team", "metadata_template"])
async def test_restore_rejects_conflicting_identity_sources(state, source):
    req = request()
    metadata = json.loads(state.metadata.read_text(encoding="utf-8"))
    if source == "request":
        req.params["team_name"] = "other"
    elif source == "metadata_team":
        metadata["team_name"] = "other"
    else:
        metadata["team_template_id"] = "other"
    state.metadata.write_text(json.dumps(metadata), encoding="utf-8")
    before = state.metadata.read_bytes(), state.store.path.read_bytes(), state.snapshot.exists()

    with pytest.raises(TeamSessionBindingError) as error:
        team._restore_officeclaw_team_session(req, session_id=SID, sessions_root=state.sessions, skill_ids=[])

    assert error.value.code == "CONFLICT"
    assert error.value.reason == "TEAM_SESSION_BINDING_CONFLICT"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes(), state.snapshot.exists()) == before


@pytest.mark.asyncio
async def test_rollback_failure_is_reported_without_changing_owner(state, monkeypatch):
    before = state.store.path.read_bytes()
    def fail(**kwargs): raise OSError("injected failure")
    def rollback(*args, **kwargs): raise OSError("injected rollback failure")
    monkeypatch.setattr(session_metadata, "update_session_metadata", fail)
    monkeypatch.setattr(session_metadata, "restore_session_team_binding_artifacts", rollback)
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.reason == "TEAM_SESSION_BINDING_ROLLBACK_FAILED"
    assert state.store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_concurrent_recovery_is_idempotent(state):
    results = await asyncio.gather(*(chat._ensure_auto_team_binding_for_chat(None, request()) for _ in range(3)))
    assert results == [TEAM] * 3
    assert len(state.store.list()) == 1
    assert state.store.find_by_session(SID).team_name == TEAM


@pytest.mark.asyncio
async def test_missing_metadata_cannot_bypass_bound_swarm_policy(state):
    before = state.metadata.read_bytes()
    with pytest.raises(swarm_skill_session.SwarmSkillSessionError):
        await chat._ensure_auto_team_binding_for_chat(None, request(params={"skills": ["swarm-sop"]}))
    assert state.metadata.read_bytes() == before
    assert state.store.find_by_session(SID).team_name == TEAM


@pytest.mark.asyncio
async def test_swarm_continuation_preserves_origin_and_open_library(state):
    data = json.loads(state.metadata.read_text(encoding="utf-8"))
    data.update(team_origin="session_generated", skill_id="swarm-sop", temporary_team_session=True)
    state.metadata.write_text(json.dumps(data), encoding="utf-8")
    req = request(params={"skills": ["swarm-sop"]})
    assert await chat._ensure_auto_team_binding_for_chat(None, req) == TEAM
    restored = json.loads(state.metadata.read_text(encoding="utf-8"))
    assert restored["team_origin"] == "session_generated"
    assert restored["skill_id"] == "swarm-sop"
    assert restored["temporary_team_session"] is True
    assert "[Team collaboration SOP]" in req.params["query"]


@pytest.mark.asyncio
async def test_officeclaw_swarm_opener_can_create_a_provisional_session(state, monkeypatch):
    from jiuwenswarm.agents.harness.team import team_name_generator
    class Tiny:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def run(self, content): return {"team_name": "swarm-work"}
    monkeypatch.setattr(team_name_generator, "create_tiny_agent", lambda **kwargs: Tiny())
    req = request(session_id="officeclaw_swarm_new", params={"team_name": "", "skills": ["swarm-sop"]})
    assert not (state.sessions / req.session_id).exists()

    binding = await chat._ensure_auto_team_binding_for_chat(None, req)

    assert binding.team_name == "swarm-work"
    metadata = session_metadata.get_session_metadata(req.session_id, cache_bust=True, sessions_root=state.sessions)
    assert metadata["team_origin"] == "session_generated"
    assert metadata["skill_id"] == "swarm-sop"
    assert metadata["temporary_team_session"] is True
    assert state.store.find_by_session(SID).team_name == TEAM
    snapshot = state.entities.get("swarm-work").template_snapshot
    assert all(agent["skills"] == [] for agent in snapshot["agents"].values())


@pytest.mark.asyncio
async def test_swarm_resume_cannot_open_a_replacement_session(state):
    req = request(session_id="missing_swarm", req_method=ReqMethod.CHAT_RESUME,
                  params={"team_name": "", "skills": ["swarm-sop"]})
    with pytest.raises(TeamSessionBindingError):
        await chat._ensure_auto_team_binding_for_chat(None, req)
    assert not (state.sessions / req.session_id).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["broken-json", {"teams": []}, {"teams": {"broken": 3}}])
async def test_bad_or_ambiguous_binding_store_never_falls_through_to_generation(state, payload):
    before = state.metadata.read_bytes()
    state.store.path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())
    assert error.value.payload == {
        "error": "Unable to read the original team session binding.",
        "code": "INTERNAL_ERROR", "reason": "TEAM_SESSION_BINDING_READ_FAILED",
    }
    assert state.metadata.read_bytes() == before


@pytest.mark.asyncio
async def test_chat_recovery_preserves_ambiguous_owner_classification(state):
    state.store.create(team_name="other", template_id=TEAM)
    data = json.loads(state.store.path.read_text(encoding="utf-8"))
    data["teams"]["other"]["session_ids"] = [SID]
    state.store.path.write_text(json.dumps(data), encoding="utf-8")
    before = state.metadata.read_bytes(), state.store.path.read_bytes()

    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request())

    assert error.value.code == "CONFLICT"
    assert error.value.reason == "TEAM_SESSION_BINDING_AMBIGUOUS"
    assert (state.metadata.read_bytes(), state.store.path.read_bytes()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "ambiguous", "empty_key"])
async def test_naming_rejects_unusable_owner_before_network_or_persistence(state, monkeypatch, problem):
    from jiuwenswarm.agents.harness.team import team_name_generator
    owners = state.config["models"]["defaults"]
    if problem == "missing": owners.clear()
    elif problem == "ambiguous": owners.append(deepcopy(owners[0]))
    else: owners[0]["model_client_config"]["api_key"] = ""
    before = state.store.path.read_bytes()
    monkeypatch.setattr(team_name_generator, "create_tiny_agent", lambda **kwargs: pytest.fail("invalid config must not create TinyAgent"))
    with pytest.raises(Exception, match=r"\[181002\]"):
        await team._create_generated_team_binding(description="review", config_base=state.config)
    assert state.store.path.read_bytes() == before


class Sink:
    def __init__(self): self.sent = []
    async def send_wire(self, wire): self.sent.append(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    "broken-json", {"teams": []}, {"teams": {"broken": 3}},
    {"teams": {"broken": {"template_id": TEAM, "created_at": "invalid"}}},
])
async def test_explicit_bind_classifies_binding_read_failures_without_writes(state, payload):
    state.store.path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    before = state.store.path.read_bytes(), state.metadata.read_bytes(), state.snapshot.exists()
    sink = Sink()
    req = request(req_method=ReqMethod.TEAM_SESSION_BIND, params={"session_id": SID})

    await team.handle_team_session_bind(RequestContext(req, sink, "isolated"))

    details = sink.sent[0]["body"]["details"]
    assert details["code"] == "INTERNAL_ERROR"
    assert details["reason"] == "TEAM_SESSION_BINDING_READ_FAILED"
    assert (state.store.path.read_bytes(), state.metadata.read_bytes(), state.snapshot.exists()) == before


@pytest.mark.asyncio
async def test_protected_bind_read_failure_keeps_classification_after_rollback(state, monkeypatch):
    before = state.metadata.read_bytes()
    bind = state.store.bind_session
    def corrupt_before_commit(**kwargs):
        state.store.path.write_text("broken-json", encoding="utf-8")
        return bind(**kwargs)
    monkeypatch.setattr(state.store, "bind_session", corrupt_before_commit)
    sink = Sink()
    req = request(req_method=ReqMethod.TEAM_SESSION_BIND, params={"session_id": SID})

    await team.handle_team_session_bind(RequestContext(req, sink, "isolated"))

    details = sink.sent[0]["body"]["details"]
    assert details["code"] == "INTERNAL_ERROR"
    assert details["reason"] == "TEAM_SESSION_BINDING_READ_FAILED"
    assert state.metadata.read_bytes() == before
    assert not state.snapshot.exists()
    assert state.store.path.read_text(encoding="utf-8") == "broken-json"


@pytest.mark.asyncio
async def test_explicit_officeclaw_bind_rejects_another_team_before_writes(state):
    state.store.create(team_name="other", template_id=TEAM)
    before = state.store.path.read_bytes(), state.metadata.read_bytes()
    sink = Sink()
    req = request(req_method=ReqMethod.TEAM_SESSION_BIND, params={"session_id": SID, "team_name": "other"})
    await team.handle_team_session_bind(RequestContext(req, sink, "isolated"))
    details = sink.sent[0]["body"]["details"]
    assert details["reason"] == "TEAM_SESSION_BINDING_CONFLICT"
    assert (state.store.path.read_bytes(), state.metadata.read_bytes()) == before
    assert not state.entities.exists("other")


@pytest.mark.asyncio
async def test_atomic_bind_conflict_keeps_new_owner_and_rolls_back_own_metadata(state, monkeypatch):
    state.store.create(team_name="other", template_id=TEAM)
    before = state.metadata.read_bytes()
    bind = state.store.bind_session
    def racing_bind(**kwargs):
        # Simulate an independent store writer after the handler's preflight.
        bind(team_name="other", session_id=SID)
        return bind(**kwargs)
    monkeypatch.setattr(state.store, "bind_session", racing_bind)
    sink = Sink()
    req = request(req_method=ReqMethod.TEAM_SESSION_BIND, params={"session_id": SID})
    await team.handle_team_session_bind(RequestContext(req, sink, "isolated"))
    details = sink.sent[0]["body"]["details"]
    assert details["code"] == "CONFLICT"
    assert details["reason"] == "TEAM_SESSION_BINDING_CONFLICT"
    assert state.store.find_by_session(SID).team_name == "other"
    assert state.metadata.read_bytes() == before
    assert not state.snapshot.exists()


@pytest.mark.asyncio
async def test_unknown_generated_source_is_not_restored_as_catalog_team(state):
    state.store.create(team_name="generated-name", template_id=TEAM)
    state.store.bind_session(team_name="generated-name", session_id=SID)
    before = state.metadata.read_bytes()
    with pytest.raises(TeamSessionBindingError) as error:
        await chat._ensure_auto_team_binding_for_chat(None, request(params={"team_name": "generated-name"}))
    assert error.value.reason == "TEAM_SESSION_BINDING_SOURCE_UNKNOWN"
    assert state.metadata.read_bytes() == before
    assert state.store.find_by_session(SID).team_name == "generated-name"


@pytest.mark.asyncio
async def test_verified_generated_team_can_still_use_explicit_conversion(state):
    state.store.create(team_name="other", template_id=TEAM)
    data = json.loads(state.metadata.read_text(encoding="utf-8"))
    data.update(team_name=TEAM, team_origin="session_generated", skill_id="swarm-sop", temporary_team_session=True)
    state.metadata.write_text(json.dumps(data), encoding="utf-8")
    # The existing orphan cleanup may delete this test-only team directory.
    cleanup_dir = state.entities.entity_path(TEAM).parent.parent.resolve()
    assert cleanup_dir.is_relative_to(state.store.path.parent.resolve())
    sink = Sink()
    req = request(req_method=ReqMethod.TEAM_SESSION_BIND, params={"session_id": SID, "team_name": "other"})
    await team.handle_team_session_bind(RequestContext(req, sink, "isolated"))
    assert sink.sent[0]["body"]["result"]["team_name"] == "other"
    assert state.store.find_by_session(SID).team_name == "other"
    assert state.store.get(TEAM) is None
    assert not cleanup_dir.exists()


@pytest.mark.asyncio
async def test_cancelled_naming_does_not_create_binding(state, monkeypatch):
    from jiuwenswarm.agents.harness.team import team_name_generator
    class Tiny:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def run(self, content): raise asyncio.CancelledError()
    monkeypatch.setattr(team_name_generator, "create_tiny_agent", lambda **kwargs: Tiny())
    before = state.store.path.read_bytes()
    with pytest.raises(asyncio.CancelledError):
        await team._create_generated_team_binding(description="review", config_base=state.config)
    assert state.store.path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [None, [], {"team": []}])
async def test_legacy_template_naming_builds_only_a_private_modes_mapping(state, monkeypatch, modes):
    from jiuwenswarm.agents.harness.team import team_name_generator
    config = {"models": deepcopy(state.config["models"]),
              "team": deepcopy(state.config["modes"]["team"][TEAM]), "modes": modes}
    before = deepcopy(config)
    class Tiny:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def run(self, content): return {"team_name": "legacy-review"}
    monkeypatch.setattr(team_name_generator, "create_tiny_agent", lambda **kwargs: Tiny())
    binding, _ = await team._create_generated_team_binding(description="review", config_base=config)
    assert binding.team_name == "legacy-review"
    assert state.entities.get("legacy-review") is not None
    assert config == before


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [True, False])
async def test_pipeline_returns_classified_error_and_does_not_execute(state, monkeypatch, is_stream):
    from jiuwenswarm.server import pipeline
    state.store.unbind_session(session_id=SID)
    async def no_hook(request): pass
    async def no_handler(ctx, request): return False
    async def unexpected(*args, **kwargs): pytest.fail("rejected chat must not execute")
    monkeypatch.setattr(pipeline, "_trigger_before_chat_request_hook", no_hook)
    monkeypatch.setattr(pipeline, "dispatch_with_context", no_handler)
    monkeypatch.setattr(pipeline, "_handle_stream", unexpected)
    monkeypatch.setattr(pipeline, "_handle_unary", unexpected)
    req = request(is_stream=is_stream)
    sink = Sink()
    await pipeline.dispatch_parsed_request(RequestContext(req, sink, "isolated"), req)
    assert len(sink.sent) == 1
    details = sink.sent[0]["body"]["details"]
    assert details["code"] == "NOT_FOUND"
    assert details["reason"] == "TEAM_SESSION_BINDING_MISSING"


@pytest.mark.asyncio
async def test_naming_resolves_partial_models_before_real_schema_validation(state, monkeypatch):
    from jiuwenswarm.agents.harness.team import config_loader, team_name_generator
    monkeypatch.setattr(config_loader, "read_default_headers", lambda: {"Authorization": "Basic dummy"})
    bad = deepcopy(state.config["models"]["defaults"][0])
    bad["model_client_config"].update(model_name="old-placeholder", api_key="", api_base="")
    state.config["models"]["defaults"].insert(0, bad)
    before = deepcopy(state.config)
    captured = []
    class Tiny:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def run(self, content): return {"team_name": "code-review"}
    def create(**kwargs):
        model = kwargs["model_resolver"](kwargs["model_name"])
        captured.append(model)
        return Tiny()
    monkeypatch.setattr(team_name_generator, "create_tiny_agent", create)

    binding, _ = await team._create_generated_team_binding(description="review code", config_base=state.config)
    duplicate, _ = await team._create_generated_team_binding(description="review code", config_base=state.config)

    assert binding.team_name == "code-review"
    assert duplicate.team_name == "code-review_2"
    assert captured[0].model_request_config.model_name == "review-model"
    assert captured[0].model_request_config.temperature == 0.2
    assert captured[0].model_client_config.api_key == "dummy-key"
    assert captured[0].model_client_config.api_base == "https://tenant.example/v1"
    assert captured[0].model_client_config.custom_headers["Authorization"] == "Basic dummy"
    assert state.config == before
    persisted = state.entities.get("code-review").template_snapshot
    assert "ref" in persisted["agents"]["leader"]["model"]
    assert "api_key" not in json.dumps(persisted)
