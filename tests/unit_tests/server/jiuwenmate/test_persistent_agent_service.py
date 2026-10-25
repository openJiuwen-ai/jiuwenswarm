"""SW-02: colleague definitions, the default 小九, settings, publication and deletion."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ExecutionRecord,
    RequestRecord,
)
from jiuwenswarm.server.jiuwenmate.models import (
    CapabilitySelection,
    ColleagueSettings,
    PersistentAgentError,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog
from jiuwenswarm.server.jiuwenmate.service import (
    FORMAL_CAPACITY_MARGIN_TOKENS,
    FORMAL_OUTPUT_RESERVE_TOKENS,
    WORKSPACE_PROMPT,
    ConfiguredModelProvider,
    PersistentAgentService,
)
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot, build_invocation

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.persistent_agent.execution.capacity import capacity_limits
from openjiuwen.harness.resources.extension_loader import find_agent_template_manifest, load_agent_template_package
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

SECRET = "sk-test-secret-value"
NS = "workspace-1"


class FakeModels:
    def __init__(self, selections=("deepseek#0", "kimi#1"), default="deepseek#0", *, window=None, max_tokens=None):
        self.selections = set(selections)
        self.default = default
        self.api_key = SECRET
        self.window = window
        self.max_tokens = max_tokens

    def default_selection(self):
        return self.default

    def resolve(self, selection, *, client_id):
        if selection not in self.selections:
            raise PersistentAgentError("model is not available", code="MODEL_NOT_FOUND")
        name = selection.partition("#")[0]
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key=self.api_key,
                api_base="https://example.invalid/v1",
                model_name=name,
            ),
            model_request_config=ModelRequestConfig(
                model=name, context_window=self.window, max_tokens=self.max_tokens
            ),
        )


TEMPLATES = {
    "system-architect": {
        "id": "system-architect",
        "displayName": {"zh": "系统架构工程师"},
        "details": "梳理模块依赖并评审技术方案。",
        "persona": "你是系统架构工程师，负责梳理依赖、评审方案并识别风险。",
    }
}


def make_service(tmp_path, models=None):
    ticks = count()
    base = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    library = tmp_path / "skill-library"
    skill = library / "report-writer"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: report-writer\n---\n写报告。\n", encoding="utf-8")
    return PersistentAgentService(
        PersistentAgentRepository(tmp_path / "agents.sqlite3"),
        TemplateStore(tmp_path / "templates"),
        models=models or FakeModels(),
        template_source=TEMPLATES.get,
        clock=lambda: base + timedelta(seconds=next(ticks)),
        windows=WindowBackendStore(tmp_path / "agents.sqlite3"),
        resources=InstalledResourceCatalog(
            skills_dir=library,
            builtin_dir=tmp_path / "no-builtin",
            state_file=tmp_path / "no-state.json",
        ),
    )


def settings(**changes) -> ColleagueSettings:
    fields = {
        "name": "研发同事",
        "description": "负责整理研发进展。",
        "tags": ("研发工程",),
        "category": "development",
        "capabilities": CapabilitySelection(model="deepseek#0", skills=("report-writer",)),
    }
    fields.update(changes)
    return ColleagueSettings(**fields)


def request_for(snapshot: ConfigSnapshot, request_id: str) -> RequestRecord:
    return RequestRecord(
        namespace_id=NS,
        agent_id=snapshot.agent_id,
        session_id="session-1",
        request_id=request_id,
        source_ref=f"source:{request_id}",
        config_version=snapshot.config_version,
        config_fingerprint=snapshot.fingerprint,
        access=AccessSnapshot(
            principal_ref="user-1",
            startup_scope=frozenset({"chat"}),
            source_scope=frozenset({"chat"}),
            policy_revision="policy-1",
        ),
        request_input=core.RequestInput(kind="scheduled", text="整理进展", reply_target="conversation-1"),
        received_at=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc),
    )


def test_model_refresh_updates_new_requests_but_keeps_the_previous_snapshot(tmp_path):
    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(NS, settings())
    original = service.current_snapshot(NS, colleague.agent_id)
    accepted = request_for(original, "accepted-before-model-save")

    models.window = 131072
    assert service.refresh_published(NS) is True
    current = service.current_snapshot(NS, colleague.agent_id)
    assert current.config_version == "v2"
    assert current.load_spec().model.model_request_config.context_window == 131072
    retained = service.snapshot(NS, colleague.agent_id, accepted.config_version)
    assert retained.fingerprint == accepted.config_fingerprint
    assert retained.load_spec().model.model_request_config.context_window == 1048576
    assert request_for(current, "accepted-after-model-save").config_version == "v2"
    assert service.refresh_published(NS) is True
    assert service.current_snapshot(NS, colleague.agent_id).config_version == "v2"


def test_model_refresh_preserves_drafts_and_reports_partial_application(tmp_path):
    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    clean = service.create_colleague(NS, settings(name="Clean"))
    draft = service.create_colleague(NS, settings(name="Published"))
    service.update_settings(NS, draft.agent_id, settings(name="Unpublished"))

    models.window = 131072
    assert service.refresh_published(NS) is False
    refreshed = service.current_snapshot(NS, clean.agent_id).load_spec()
    assert refreshed.model.model_request_config.context_window == 131072
    assert service.current_snapshot(NS, draft.agent_id).config_version == "v1"
    assert service.current_snapshot(NS, draft.agent_id).load_spec().card.name == "Published"
    assert service.get_colleague(NS, draft.agent_id).name == "Unpublished"
    assert service.get_colleague(NS, draft.agent_id).has_unpublished_changes


def test_model_refresh_reports_a_missing_model_without_losing_other_updates(tmp_path):
    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    missing = service.create_colleague(NS, settings())
    available = service.create_colleague(NS, settings(capabilities=CapabilitySelection(model="kimi#1")))
    models.selections.remove("deepseek#0")
    models.window = 131072

    assert service.refresh_published(NS) is False
    assert service.current_snapshot(NS, missing.agent_id).config_version == "v1"
    refreshed = service.current_snapshot(NS, available.agent_id).load_spec()
    assert refreshed.model.model_request_config.context_window == 131072


def test_model_refresh_continues_after_an_unexpected_colleague_failure(tmp_path, monkeypatch):
    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    failed = service.create_colleague(NS, settings(name="Failed"))
    available = service.create_colleague(NS, settings(name="Available"))
    read_persona = service._templates.read_persona

    def unreadable_persona(agent_id):
        if agent_id == failed.agent_id:
            raise PermissionError("persona unavailable")
        return read_persona(agent_id)

    monkeypatch.setattr(service._templates, "read_persona", unreadable_persona)
    models.window = 131072
    assert service.refresh_published(NS) is False
    assert service.current_snapshot(NS, failed.agent_id).config_version == "v1"
    assert service.current_snapshot(NS, available.agent_id).config_version == "v2"


def test_model_refresh_does_not_publish_a_draft_saved_during_the_refresh(tmp_path, monkeypatch):
    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(NS, settings(name="Published"))
    publish = service.publish

    def save_draft_before_publish(namespace_id, agent_id, **kwargs):
        service.update_settings(namespace_id, agent_id, settings(name="Concurrent draft"))
        return publish(namespace_id, agent_id, **kwargs)

    monkeypatch.setattr(service, "publish", save_draft_before_publish)
    models.window = 131072
    assert service.refresh_published(NS) is False
    assert service.current_snapshot(NS, colleague.agent_id).config_version == "v1"
    assert service.get_colleague(NS, colleague.agent_id).has_unpublished_changes


def test_model_refresh_waits_for_a_persona_save_before_publishing(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(NS, settings())
    editor = make_service(tmp_path, models)
    saving, reading, release = Event(), Event(), Event()
    update = editor._repository.update_settings
    read = service._templates.read_persona

    def pause_after_template_replacement(*args, **kwargs):
        saving.set()
        assert release.wait(5)
        return update(*args, **kwargs)

    def observe_persona_read(agent_id):
        reading.set()
        return read(agent_id)

    monkeypatch.setattr(editor._repository, "update_settings", pause_after_template_replacement)
    monkeypatch.setattr(service._templates, "read_persona", observe_persona_read)
    models.window = 131072
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(editor.update_settings, NS, colleague.agent_id, settings(persona="Unpublished persona"))
        try:
            assert saving.wait(5)
            refreshed = pool.submit(service.refresh_published, NS)
            assert reading.wait(5)
            with pytest.raises(TimeoutError):
                refreshed.result(timeout=0.1)
        finally:
            release.set()
        saved.result(timeout=5)
        assert refreshed.result(timeout=5) is False
    assert service.current_snapshot(NS, colleague.agent_id).config_version == "v1"
    assert service.get_colleague(NS, colleague.agent_id).has_unpublished_changes


class FakePorts:
    async def record(self, execution, event):
        return None

    async def read_execution_log(self, execution, execution_id):
        return ()

    async def admit(self, execution, model_call_id):
        raise AssertionError("not called")

    async def report(self, execution, model_call_id, usage):
        raise AssertionError("not called")


def ports(tmp_path, request: RequestRecord) -> core.PersistentPorts:
    host = FakePorts()
    windows = WindowBackendStore(tmp_path / "agents.sqlite3")
    backend = windows.bind(request.namespace_id, request.agent_id, request.session_id)
    workspace = HostWorkspaceAccess(WorkspaceStore(tmp_path / "workspaces"), request.namespace_id, request.agent_id)
    return core.PersistentPorts(
        commit=host, budget=host, window_backend=backend, workspace_access=workspace, workspace_write_access=workspace
    )


def test_default_colleague_is_created_once_and_published(tmp_path):
    service = make_service(tmp_path)

    first = service.ensure_default_colleague(NS)
    second = service.ensure_default_colleague(NS)

    assert first is not None and first.name == "小九" and first.is_default
    assert second.agent_id == first.agent_id
    assert first.current_version == "v1"
    assert [view.agent_id for view in service.list_colleagues(NS)] == [first.agent_id]


def test_default_colleague_is_shared_by_services_on_the_same_store(tmp_path):
    first = make_service(tmp_path).ensure_default_colleague(NS)
    second = make_service(tmp_path).ensure_default_colleague(NS)

    assert second.agent_id == first.agent_id
    assert len(list((tmp_path / "templates").iterdir())) == 1


def test_deleted_default_colleague_is_not_created_again(tmp_path):
    service = make_service(tmp_path)
    default = service.ensure_default_colleague(NS)

    service.delete_colleague(NS, default.agent_id)

    assert service.ensure_default_colleague(NS) is None
    assert service.list_colleagues(NS) == []


def test_default_colleague_without_models_is_created_unpublished(tmp_path):
    service = make_service(tmp_path, FakeModels(selections=(), default=None))

    default = service.ensure_default_colleague(NS)

    assert default.current_version is None
    with pytest.raises(PersistentAgentError) as caught:
        service.current_snapshot(NS, default.agent_id)
    assert caught.value.code == "NOT_PUBLISHED"


def test_custom_colleague_gets_its_own_core_template_and_first_version(tmp_path):
    service = make_service(tmp_path)

    view = service.create_colleague(NS, settings())
    template = load_agent_template_package(find_agent_template_manifest(tmp_path / "templates" / view.agent_id))
    snapshot = service.current_snapshot(NS, view.agent_id)
    spec = snapshot.load_spec()

    assert view.current_version == "v1" and not view.has_unpublished_changes
    assert template.agent_card.id == view.agent_id
    assert spec.card.id == view.agent_id
    assert spec.system_prompt.startswith(
        "你是研发同事。\n\n负责整理研发进展。\n\n<!-- jiuwenmate-host-guidance -->" + WORKSPACE_PROMPT
    )
    assert spec.skills == ["report-writer"] and "已装配技能" in spec.system_prompt
    assert "群聊必须先由提问者私聊确认" not in spec.system_prompt
    assert set(spec.enabled_features) == {
        "construction",
        "workspace_read",
        "workspace_write",
        "window_management",
    }
    assert spec.enable_task_loop is False
    assert spec.model.model_client_config.client_id == f"jiuwenmate:{view.agent_id}"


def test_colleague_created_from_template_uses_its_persona(tmp_path):
    service = make_service(tmp_path)

    view = service.create_colleague(NS, settings(description=""), template_id="system-architect")

    assert view.source_template_id == "system-architect"
    assert view.description == "梳理模块依赖并评审技术方案。"
    assert service.read_settings(NS, view.agent_id).persona == TEMPLATES["system-architect"]["persona"]


def test_an_expert_can_be_added_only_once(tmp_path):
    service = make_service(tmp_path)
    service.create_colleague(NS, settings(), template_id="system-architect", unique_source=True)

    with pytest.raises(PersistentAgentError) as caught:
        service.create_colleague(NS, settings(name="另一位"), template_id="system-architect", unique_source=True)

    assert caught.value.code == "CONFLICT"
    assert len(service.list_colleagues(NS)) == 1


def test_a_builtin_template_can_be_added_only_once(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(
        NS,
        settings(name="英语表达教练"),
        source_template_id="english-coach",
        unique_source=True,
    )

    assert view.source_template_id == "english-coach"
    with pytest.raises(PersistentAgentError) as caught:
        service.create_colleague(
            NS,
            settings(name="另一位"),
            source_template_id="english-coach",
            unique_source=True,
        )

    assert caught.value.code == "CONFLICT"
    assert len(service.list_colleagues(NS)) == 1


def test_each_colleague_owns_one_template(tmp_path):
    service = make_service(tmp_path)
    first = service.create_colleague(NS, settings(), template_id="system-architect")
    second = service.create_colleague(NS, settings(name="另一位"), template_id="system-architect")

    service.delete_colleague(NS, first.agent_id)

    assert first.agent_id != second.agent_id
    assert not (tmp_path / "templates" / first.agent_id).exists()
    assert (tmp_path / "templates" / second.agent_id / "manifest.json").is_file()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"template_id": "missing-template"}, "TEMPLATE_NOT_FOUND"),
        ({"settings": settings(capabilities=CapabilitySelection(model="unknown#9"))}, "MODEL_NOT_FOUND"),
    ],
)
def test_rejected_creation_leaves_nothing_behind(tmp_path, kwargs, code):
    service = make_service(tmp_path)
    value = kwargs.pop("settings", settings())

    with pytest.raises(PersistentAgentError) as caught:
        service.create_colleague(NS, value, **kwargs)

    assert caught.value.code == code
    assert service.list_colleagues(NS) == []
    assert list((tmp_path / "templates").iterdir()) == []


def test_initial_publish_failure_rolls_back_the_colleague_and_template(tmp_path):
    service = make_service(tmp_path)
    value = settings(capabilities=CapabilitySelection(model="deepseek#0", skills=("not-installed",)))

    with pytest.raises(PersistentAgentError):
        service.create_colleague(NS, value)

    assert service.list_colleagues(NS) == []
    assert list((tmp_path / "templates").iterdir()) == []


def test_settings_change_only_affects_requests_after_publication(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    v1 = service.current_snapshot(NS, view.agent_id)
    pinned = request_for(v1, "request-old")

    updated = service.update_settings(
        NS,
        view.agent_id,
        settings(persona="你是研发同事，只输出要点。", capabilities=CapabilitySelection(model="kimi#1")),
    )

    assert updated.has_unpublished_changes
    assert service.current_snapshot(NS, view.agent_id).config_version == "v1"
    assert service.read_settings(NS, view.agent_id).persona == "你是研发同事，只输出要点。"

    published = service.publish(NS, view.agent_id)
    v2 = service.current_snapshot(NS, view.agent_id)

    assert published.config_version == "v2"
    assert not service.get_colleague(NS, view.agent_id).has_unpublished_changes
    assert v2.load_spec().system_prompt.startswith(
        "你是研发同事，只输出要点。\n\n<!-- jiuwenmate-host-guidance -->" + WORKSPACE_PROMPT
    )
    old_spec, _ = build_invocation(
        service.snapshot(NS, view.agent_id, pinned.config_version),
        pinned,
        ExecutionRecord(request_id="request-old", execution_id="e-1"),
        ports=ports(tmp_path, pinned),
    )
    pending = request_for(v2, "request-new")
    new_spec, _ = build_invocation(
        v2, pending, ExecutionRecord(request_id="request-new", execution_id="e-2"), ports=ports(tmp_path, pending)
    )
    assert old_spec.model.model_client_config.model_name == "deepseek"
    assert new_spec.model.model_client_config.model_name == "kimi"


def test_permission_profile_is_employee_scoped_and_published_into_spec(tmp_path):
    service = make_service(tmp_path)
    first = service.create_colleague(NS, settings())
    second = service.create_colleague(NS, settings(name="运营同事"))
    first_v1 = service.current_snapshot(NS, first.agent_id)
    second_v1 = service.current_snapshot(NS, second.agent_id)

    updated = service.update_settings(NS, first.agent_id, settings(permission_profile="automatic"))
    assert updated.has_unpublished_changes
    assert service.read_settings(NS, first.agent_id).permission_profile == "automatic"
    assert service.read_settings(NS, second.agent_id).permission_profile == "default"
    assert service.current_snapshot(NS, first.agent_id).fingerprint == first_v1.fingerprint

    published = service.publish(NS, first.agent_id)
    first_v2 = service.current_snapshot(NS, first.agent_id)
    first_spec = first_v2.load_spec()
    second_spec = second_v1.load_spec()

    assert published.config_version == "v2"
    assert first_v2.fingerprint != first_v1.fingerprint
    assert first_spec.permission_profile == "automatic"
    assert first_spec.permissions["enabled"] is True
    assert first_spec.permissions["mode"] == "auto"
    assert second_spec.permission_profile == "default"
    assert second_spec.permissions["mode"] == "manual"


def test_publication_fills_the_formal_capacity_check(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    spec = service.current_snapshot(NS, view.agent_id).load_spec()
    request = spec.model.model_request_config

    assert request.context_window == 256 * 1024
    assert request.max_tokens == FORMAL_OUTPUT_RESERVE_TOKENS
    assert spec.capacity_safety_margin_tokens == FORMAL_CAPACITY_MARGIN_TOKENS
    assert spec.model_input_limit_tokens == request.context_window - request.max_tokens - FORMAL_CAPACITY_MARGIN_TOKENS
    limits = capacity_limits(spec)
    assert limits is not None
    assert limits.available_input == spec.model_input_limit_tokens


def test_publication_records_optional_offline_tokenizer_metadata(tmp_path):
    service = make_service(tmp_path, FakeModels(selections=("deepseek-flash#0",), default="deepseek-flash#0"))
    view = service.create_colleague(NS, settings(capabilities=CapabilitySelection(model="deepseek-flash#0")))
    config = service.current_snapshot(NS, view.agent_id).load_spec().context_engine_config

    assert config["model_name"] == "deepseek-flash"
    assert config["model_provider"] == "OpenAI"
    assert config["enable_tokenizer_download"] is False
    assert config["tokenizer_offline"] is True
    assert config["tokenizer_registry"] == [
        {
            "provider": "OpenAI",
            "model": "deepseek-flash",
            "tokenizer_id": "deepseek-ai/DeepSeek-V4-Flash",
            "source": "huggingface",
            "revision": None,
            "artifact_path": None,
            "engine": "tokenizers",
            "fallback_policy": "family_tokenizer_then_default_tiktoken_then_string_length",
            "chat_template": None,
            "sha256": None,
            "family": None,
            "compatible_fallbacks": [],
        }
    ]


def test_publication_does_not_require_tokenizer_download_or_cache(tmp_path, monkeypatch):
    from openjiuwen.core.context_engine.token.tokenizer_manager import TokenizerArtifactManager

    def unavailable(*args, **kwargs):
        pytest.fail("publication must not prepare or download tokenizer artifacts")

    monkeypatch.setattr(TokenizerArtifactManager, "resolve", unavailable)
    service = make_service(tmp_path, FakeModels(selections=("deepseek-flash#0",), default="deepseek-flash#0"))

    colleague = service.create_colleague(NS, settings(capabilities=CapabilitySelection(model="deepseek-flash#0")))
    spec = service.current_snapshot(NS, colleague.agent_id).load_spec()

    assert spec.context_engine_config["enable_tokenizer_download"] is False
    assert spec.context_engine_config["tokenizer_offline"] is True
    assert spec.model.model_client_config.model_name == "deepseek-flash"


def test_publication_keeps_a_configured_output_reserve(tmp_path):
    service = make_service(tmp_path, FakeModels(window=262144, max_tokens=4096))
    view = service.create_colleague(NS, settings())
    spec = service.current_snapshot(NS, view.agent_id).load_spec()

    assert spec.model.model_request_config.context_window == 262144
    assert spec.model.model_request_config.max_tokens == 4096
    assert spec.model_input_limit_tokens == 262144 - 4096 - FORMAL_CAPACITY_MARGIN_TOKENS


def test_an_output_reserve_that_fills_the_window_is_refused(tmp_path):
    service = make_service(tmp_path, FakeModels(window=2000, max_tokens=1500))

    with pytest.raises(PersistentAgentError) as caught:
        service.create_colleague(NS, settings())

    assert caught.value.code == "INVALID_CONFIG"
    assert service.list_colleagues(NS) == []


def test_publishing_unchanged_settings_keeps_the_version(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    service.update_settings(NS, view.agent_id, settings())

    assert service.publish(NS, view.agent_id).config_version == "v1"
    assert not service.get_colleague(NS, view.agent_id).has_unpublished_changes


def test_rotated_model_credentials_need_a_new_version(tmp_path):
    models = FakeModels()
    service = make_service(tmp_path, models)
    view = service.create_colleague(NS, settings())

    models.api_key = "sk-rotated"

    assert service.publish(NS, view.agent_id).config_version == "v2"


def test_failed_settings_store_restores_the_previous_template(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings(persona="原始人设"))

    def fail(*args, **kwargs):
        raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")

    monkeypatch.setattr(service._repository, "update_settings", fail)
    with pytest.raises(PersistentAgentError):
        service.update_settings(NS, view.agent_id, settings(persona="新人设"))

    assert service.read_settings(NS, view.agent_id).persona == "原始人设"
    assert [p.name for p in (tmp_path / "templates").iterdir()] == [view.agent_id]


def test_stale_settings_write_is_refused(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    repository = service._repository
    stale = repository.get(NS, view.agent_id)
    service.update_settings(NS, view.agent_id, settings(name="新名字"))

    with pytest.raises(PersistentAgentError) as caught:
        repository.update_settings(
            NS, view.agent_id, settings(), expected_revision=stale.revision, updated_at=stale.updated_at
        )

    assert caught.value.code == "CONFLICT"


def test_deleted_colleague_and_versions_are_gone(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())

    service.delete_colleague(NS, view.agent_id)

    for call in (
        lambda: service.get_colleague(NS, view.agent_id),
        lambda: service.snapshot(NS, view.agent_id, "v1"),
        lambda: service.delete_colleague(NS, view.agent_id),
    ):
        with pytest.raises(PersistentAgentError) as caught:
            call()
        assert caught.value.code == "NOT_FOUND"


async def test_deleting_a_colleague_purges_only_its_window_data(tmp_path):
    service = make_service(tmp_path)
    gone = service.create_colleague(NS, settings()).agent_id
    kept = service.create_colleague(NS, settings(name="运维同事")).agent_id
    windows = WindowBackendStore(tmp_path / "agents.sqlite3")
    for agent_id in (gone, kept):
        for session_id in ("sess_a", "sess_b"):
            await windows.bind(NS, agent_id, session_id).set(f"pa:{agent_id}:{session_id}:state", "s")

    service.delete_colleague(NS, gone)

    for session_id in ("sess_a", "sess_b"):
        assert await windows.bind(NS, gone, session_id).get_by_prefix("") == {}
        assert await windows.bind(NS, kept, session_id).get(f"pa:{kept}:{session_id}:state") == "s"


def test_namespaces_do_not_see_each_other(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    other_default = service.ensure_default_colleague("workspace-2")

    assert [item.agent_id for item in service.list_colleagues("workspace-2")] == [other_default.agent_id]
    with pytest.raises(PersistentAgentError):
        service.get_colleague("workspace-2", view.agent_id)


def test_public_views_never_carry_model_credentials(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())

    returned = [
        view.to_dict(),
        service.get_colleague(NS, view.agent_id).to_dict(),
        service.publish(NS, view.agent_id).to_dict(),
        service.read_settings(NS, view.agent_id).to_dict(),
    ]

    assert SECRET not in json.dumps(returned, ensure_ascii=False)
    assert SECRET in service.current_snapshot(NS, view.agent_id).spec_json
    assert stat.S_IMODE((tmp_path / "agents.sqlite3").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "data",
    [
        {"name": "x", "unknown": 1},
        {"name": " "},
        {"name": "x", "capabilities": {"model": "m", "secret": "s"}},
        {"name": "x", "tags": "not-a-list"},
    ],
)
def test_malformed_settings_are_rejected(data):
    with pytest.raises(PersistentAgentError):
        ColleagueSettings.from_dict(data)


def test_legacy_settings_default_permission_profile_to_default():
    settings = ColleagueSettings.from_dict({"name": "旧员工"})

    assert settings.permission_profile == "default"
    assert ColleagueSettings.from_dict(settings.to_dict()) == settings


def test_unknown_permission_profile_is_rejected():
    with pytest.raises(PersistentAgentError, match="permission_profile"):
        ColleagueSettings.from_dict({"name": "员工", "permission_profile": "unrestricted"})


def test_configured_model_provider_uses_the_existing_model_catalog():
    entries = [
        {
            "model_client_config": {
                "model_name": "deepseek",
                "client_provider": "OpenAI",
                "api_key": SECRET,
                "api_base": "https://example.invalid/v1",
            },
            "model_config_obj": {},
        },
        {
            "model_client_config": {
                "model_name": "kimi",
                "client_provider": "OpenAI",
                "api_key": "sk-other",
                "api_base": "https://example.invalid/v1",
            },
            "model_config_obj": {},
            "is_default": True,
        },
    ]
    provider = ConfiguredModelProvider(lambda: entries)

    model = provider.resolve("deepseek#0", client_id="jiuwenmate:a")

    assert provider.default_selection() == "kimi#1"
    assert model.model_client_config.api_key == SECRET
    assert model.model_client_config.client_id == "jiuwenmate:a"
    with pytest.raises(PersistentAgentError) as caught:
        provider.resolve("missing", client_id="jiuwenmate:a")
    assert caught.value.code == "MODEL_NOT_FOUND"
