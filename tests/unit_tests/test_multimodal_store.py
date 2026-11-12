# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Persistence and RPC tests inspect files independently of returned snapshots."""

from copy import deepcopy
from hashlib import sha256
import json
from io import StringIO
import os
from pathlib import Path
from unittest.mock import AsyncMock

from dotenv import dotenv_values
import pytest
import yaml

from jiuwenswarm.common import config_persistence, multimodal_store
from jiuwenswarm.common.config import config_write_lock, resolve_env_vars
from jiuwenswarm.common.multimodal_models import CAPABILITIES, MultimodalError
from jiuwenswarm.common.multimodal_projection import ALIASES, FIELDS
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
from jiuwenswarm.server.runtime.gateway_adapter.multimodal_config_adapter import MultimodalConfigAdapter
from tests.unit_tests.test_multimodal_models import model_for


def test_env_updates_replace_complete_multiline_bindings_and_preserve_other_values():
    original = "# unchanged\nOTHER='a\nb'\nVISION_API_KEY='old\\'key\\\\value\nsecond line'\nVISION_API_KEY=duplicate\nTAIL=keep\n"
    updated = config_persistence.env_content(original, {"VISION_API_KEY": "new\ncredential"})
    assert dotenv_values(stream=StringIO(updated), interpolate=False) == {
        "OTHER": "a\nb", "VISION_API_KEY": "new\ncredential", "TAIL": "keep",
    }
    removed = config_persistence.env_content(updated, {"VISION_API_KEY": None})
    assert removed == "# unchanged\nOTHER='a\nb'\nTAIL=keep\n"


def test_env_only_apply_failure_does_not_publish_or_report_saved(catalogue, monkeypatch):
    path, store = catalogue
    store.initialize()
    upsert(store, model_for(["vision"]))
    original_yaml = path.read_bytes()
    path.with_name(".env").write_text("VISION_API_BASE=https://old.invalid\n")
    old_env = path.with_name(".env").read_bytes()
    monkeypatch.setenv("VISION_API_BASE", "https://old.invalid")
    original_write = config_persistence.atomic_write

    def fail_env(target, content):
        if target.name == ".env":
            raise OSError("disk failure")
        original_write(target, content)

    monkeypatch.setattr(config_persistence, "atomic_write", fail_env)
    with pytest.raises(MultimodalError, match="MM_STORAGE_FAILED"):
        store.mutate("config.apply", {})
    assert path.read_bytes() == original_yaml
    assert path.with_name(".env").read_bytes() == old_env
    assert os.environ["VISION_API_BASE"] == "https://old.invalid"


@pytest.mark.parametrize("committed", [False, True])
def test_env_only_transaction_recovers_at_atomic_env_commit(catalogue, committed):
    path, _ = catalogue
    yaml_hash = sha256(path.read_bytes()).hexdigest()
    old_env, new_env = "VISION_ENABLED=false\n", "VISION_ENABLED=true\n"
    expected = new_env if committed else old_env
    path.with_name(".env").write_text(expected)
    path.with_name(".multimodal-transaction.json").write_text(json.dumps({
        "transaction_id": "env-only", "old_yaml_hash": yaml_hash, "new_yaml_hash": yaml_hash,
        "old_env": old_env, "new_env": new_env,
    }))
    with config_write_lock(config_path=path):
        assert path.with_name(".env").read_text() == expected
    assert not path.with_name(".multimodal-transaction.json").exists()


def test_settings_reads_do_not_initialize_and_startup_failure_keeps_legacy_data(catalogue, monkeypatch):
    path, store = catalogue
    old_yaml = path.read_bytes()
    path.with_name(".env").write_text("VISION_API_BASE=https://old.invalid\nVISION_ENABLED=false\n")
    old_env = path.with_name(".env").read_bytes()
    with pytest.raises(MultimodalError, match="MM_CONFIG_READ_FAILED"):
        store.read()
    assert path.read_bytes() == old_yaml
    original_write = config_persistence.atomic_write
    failed = False

    def fail_yaml_once(target, content):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("startup migration failed")
        original_write(target, content)

    with monkeypatch.context() as failure:
        failure.setattr(config_persistence, "atomic_write", fail_yaml_once)
        with pytest.raises(MultimodalError, match="MM_STORAGE_FAILED"):
            store.initialize()
    assert path.read_bytes() == old_yaml
    assert path.with_name(".env").read_bytes() == old_env
    store.initialize()
    assert len(store.read()["models"]) == 1
    current_yaml = path.read_bytes()
    current_env = path.with_name(".env").read_bytes()
    first = store.read()
    assert store.read() == first
    assert path.read_bytes() == current_yaml
    assert path.with_name(".env").read_bytes() == current_env


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("# preserve this comment\nmodels:\n  defaults: []\nother: keep\n", encoding="utf-8")
    for tool in CAPABILITIES:
        for suffix in (*FIELDS.values(), "ENABLED", *ALIASES.get(tool, ())):
            monkeypatch.setenv(f"{tool.upper()}_{suffix}", "")
    monkeypatch.setattr(multimodal_store, "get_config_file", lambda: path)
    monkeypatch.setattr(multimodal_store, "get_crypto_provider", lambda: None)
    monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", path)
    return path, multimodal_store.MultimodalStore()


def upsert(store, model):
    return store.mutate("models.upsert", {"mode": "create", "model": model})


def test_save_updates_directory_and_original_execution_slots_and_redacts_list(catalogue):
    path, store = catalogue
    store.initialize()
    assert store.read()["models"] == []
    model = model_for(["vision", "audio"])
    model["connection"]["api_key"] = "test'key\\value\nsecond line"
    result, _ = upsert(store, model)
    raw = yaml.safe_load(path.read_text())
    env = dotenv_values(path.with_name(".env"))
    assert raw["multimodal"]["tools"]["audio"]["model_id"] == model["model_id"]
    assert env["VISION_API_KEY"] == model["connection"]["api_key"]
    assert env["AUDIO_API_BASE"] == model["connection"]["api_base"]
    assert raw["models"]["vision"]["model_client_config"]["api_base"] == "${VISION_API_BASE}"
    assert raw["other"] == "keep" and path.read_text().startswith("# preserve this comment")
    assert "api_key" not in result["snapshot"]["models"][0]
    assert model["connection"]["api_key"] not in json.dumps(store.read())
    assert not path.with_name(".multimodal-transaction.json").exists()


def test_history_migration_is_idempotent_and_deleted_model_does_not_reappear(catalogue):
    path, store = catalogue
    path.with_name(".env").write_text("VISION_API_BASE=https://old.invalid\nVISION_ENABLED=false\n", encoding="utf-8")
    store.initialize()
    first = store.read()
    model_id = first["models"][0]["model_id"]
    assert store.read() == first
    assert store.read(model_id)["model"]["connection"]["context_window_tokens"] == ""
    store.mutate("models.delete", {"model_id": model_id, "confirmed_tools": ["vision"]})
    restarted = multimodal_store.MultimodalStore()
    assert restarted.read()["models"] == []
    assert dotenv_values(path.with_name(".env"))["VISION_API_BASE"] == ""


@pytest.mark.parametrize("failure_target", [".env", "config.yaml"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_precommit_failure_rolls_back_both_files_and_does_not_publish_runtime(catalogue, monkeypatch, failure_target, newline):
    path, store = catalogue
    store.initialize()
    store.read()
    original_yaml = path.read_bytes()
    path.with_name(".env").write_bytes(f"UNRELATED=keep{newline}".encode())
    original_env = path.with_name(".env").read_bytes()
    atomic_write = config_persistence.atomic_write
    failed = False

    def fail_once(target, content):
        nonlocal failed
        if target.name == failure_target and not failed:
            failed = True
            raise OSError("disk failure containing a secret that must not escape")
        atomic_write(target, content)

    monkeypatch.setattr(config_persistence, "atomic_write", fail_once)
    with pytest.raises(MultimodalError, match="MM_STORAGE_FAILED") as error:
        upsert(store, model_for(["vision"]))
    assert "secret" not in str(error.value)
    assert path.read_bytes() == original_yaml
    assert path.with_name(".env").read_bytes() == original_env
    assert os.environ["VISION_API_BASE"] == ""
    assert not path.with_name(".multimodal-transaction.json").exists()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_recovery_uses_yaml_commit_point_before_any_other_config_write(catalogue, committed, newline):
    path, _ = catalogue
    old_yaml, new_yaml = path.read_bytes(), b"other: new\n"
    old_env, new_env = f"UNRELATED=old{newline}", f"UNRELATED=new{newline}"
    path.write_bytes(new_yaml if committed else old_yaml)
    path.with_name(".env").write_bytes((old_env if committed else new_env).encode())
    path.with_name(".multimodal-transaction.json").write_text(json.dumps({
        "transaction_id": "test-recovery", "old_yaml_hash": sha256(old_yaml).hexdigest(),
        "new_yaml_hash": sha256(new_yaml).hexdigest(), "old_env": old_env, "new_env": new_env,
    }))
    with config_write_lock(config_path=path):
        assert path.with_name(".env").read_bytes() == (new_env if committed else old_env).encode()
    assert not path.with_name(".multimodal-transaction.json").exists()


def test_unrecognized_recovery_state_blocks_without_overwriting_files(catalogue):
    path, store = catalogue
    path.with_name(".multimodal-transaction.json").write_text('{"transaction_id":"damaged"}')
    original = path.read_bytes()
    with pytest.raises(MultimodalError, match="MM_RECOVERY_REQUIRED"):
        store.read()
    assert path.read_bytes() == original


def test_unknown_schema_and_malformed_yaml_are_not_reinitialized(catalogue):
    path, store = catalogue
    for content, code in (("multimodal: {schema_version: 8}\n", "MM_UNSUPPORTED_SCHEMA"), ("models: [\n", "MM_CONFIG_READ_FAILED")):
        path.write_text(content)
        with pytest.raises(MultimodalError, match=code):
            store.read()
        assert path.read_text() == content


def test_config_apply_preserves_catalogue(catalogue):
    _, store = catalogue
    store.initialize()
    upsert(store, model_for(["vision"]))
    before = store.read()
    result, _ = store.mutate("config.apply", {})
    assert result["snapshot"] == before and result["changed"] is False


def test_crypto_encrypts_catalogue_and_env_but_not_context_or_yaml_references(catalogue, monkeypatch):
    from types import SimpleNamespace

    path, store = catalogue
    def decrypt(value):
        assert value.startswith("enc:"), "provider must not be asked to decrypt ordinary settings"
        return value[4:]
    monkeypatch.setattr(multimodal_store, "get_crypto_provider", lambda: SimpleNamespace(
        encrypt=lambda value: f"enc:{value}", decrypt=decrypt,
    ))
    model = model_for(["vision"])
    store.initialize()
    upsert(store, model)
    persisted = yaml.safe_load(path.read_text())
    env = dotenv_values(path.with_name(".env"))
    assert persisted["multimodal"]["models"][0]["connection"]["api_key"] == "enc:test-key"
    assert env["VISION_API_KEY"] == "enc:test-key"
    assert env["VISION_CONTEXT_WINDOW_TOKENS"] == "262144"
    assert persisted["models"]["vision"]["model_client_config"]["api_key"] == "${VISION_API_KEY}"
    assert store.read(model["model_id"])["model"]["connection"]["api_key"] == "test-key"
    assert os.environ["VISION_API_KEY"] == "test-key"


def test_migration_decrypts_only_managed_values_and_merges_effective_credentials(catalogue, monkeypatch):
    from types import SimpleNamespace

    path, store = catalogue
    path.write_text("models:\n  defaults:\n    - model_client_config: {api_key: unrelated-plain-key}\n")
    path.with_name(".env").write_text(
        "VISION_API_KEY=enc:one:key\nAUDIO_API_KEY=enc:two:key\n"
        "VISION_CONTEXT_WINDOW_TOKENS=512\nAUDIO_CONTEXT_WINDOW_TOKENS=512\nUNRELATED_TOKEN=unrelated\n"
    )
    def decrypt(value):
        assert value.startswith("enc:")
        return value.rsplit(":", 1)[-1]
    monkeypatch.setattr(multimodal_store, "get_crypto_provider", lambda: SimpleNamespace(
        encrypt=lambda value: f"enc:new:{value}", decrypt=decrypt,
    ))
    store.initialize()
    state = store.read()
    assert len(state["models"]) == 1
    assert state["models"][0]["capabilities"] == ["vision", "audio"]
    assert yaml.safe_load(path.read_text())["models"]["defaults"][0]["model_client_config"]["api_key"] == "unrelated-plain-key"


async def test_rpc_saved_but_reload_failed_can_retry_apply_without_duplicate_model(catalogue):
    path, _ = catalogue
    multimodal_store.initialize_multimodal_models()
    callback = AsyncMock(side_effect=RuntimeError("sensitive exception must not be returned"))
    adapter = MultimodalConfigAdapter(on_config_saved=callback)
    response = await adapter.handle(AgentRequest(request_id="1", channel_id="web", req_method=ReqMethod.MULTIMODAL_MODELS_UPSERT,
                                               params={"mode": "create", "model": model_for(["vision"])}))
    assert response.ok and response.payload["saved"] is True
    assert response.payload["applied_without_restart"] is False
    persisted = yaml.safe_load(path.read_text())["multimodal"]
    assert len(persisted["models"]) == 1
    callback.side_effect = None
    callback.return_value = True
    applied = await adapter.handle(AgentRequest(request_id="2", req_method=ReqMethod.MULTIMODAL_CONFIG_APPLY,
                                              params={}))
    assert applied.ok and applied.payload["applied_without_restart"] is True
    assert yaml.safe_load(path.read_text())["multimodal"] == persisted
    assert len(applied.payload["snapshot"]["models"]) == 1
    assert callback.call_args.kwargs["reload_options"] == {"reload_scopes": ["multimodal"]}


@pytest.mark.parametrize("applied", [False, True])
async def test_unchanged_save_after_startup_uses_current_reload_result(catalogue, applied):
    path, store = catalogue
    store.initialize()
    model = model_for(["vision"])
    upsert(store, model)
    multimodal_store.initialize_multimodal_models()
    before = path.read_bytes(), path.with_name(".env").read_bytes()
    callback = AsyncMock(return_value=applied)
    adapter = MultimodalConfigAdapter(on_config_saved=callback)
    editable = await adapter.handle(AgentRequest(request_id="read", req_method=ReqMethod.MULTIMODAL_MODELS_GET,
                                                params={"model_id": model["model_id"]}))
    response = await adapter.handle(AgentRequest(request_id="save", req_method=ReqMethod.MULTIMODAL_MODELS_UPSERT,
                                                params={"mode": "update", "model": editable.payload["model"]}))
    assert response.ok and response.payload["saved"] is True
    assert response.payload["changed"] is False
    assert response.payload["applied_without_restart"] is applied
    callback.assert_awaited_once()
    assert callback.call_args.kwargs["reload_options"] == {"reload_scopes": ["multimodal"]}
    assert (path.read_bytes(), path.with_name(".env").read_bytes()) == before
    assert set(editable.payload) == {"model", "references"}
    assert set(response.payload["snapshot"]) == {"schema_version", "models", "tools"}


def test_legacy_revision_metadata_is_retired_without_reimporting_models(catalogue):
    path, store = catalogue
    store.initialize()
    model = model_for(["vision"])
    upsert(store, model)
    raw = yaml.safe_load(path.read_text())
    expected = deepcopy(raw["multimodal"])
    raw["multimodal"]["revision"] = 42
    path.write_text(yaml.safe_dump(raw))
    before = path.read_bytes()
    assert set(store.read()) == {"schema_version", "models", "tools"}
    assert path.read_bytes() == before
    multimodal_store.initialize_multimodal_models()
    assert yaml.safe_load(path.read_text())["multimodal"] == expected


async def test_legacy_mixed_write_rejected_before_any_other_side_effect(catalogue):
    path, store = catalogue
    store.initialize()
    store.read()
    original = path.read_bytes()
    response = await ConfigAdapter().handle(AgentRequest(request_id="legacy", req_method=ReqMethod.CONFIG_SAVE_ALL,
                                                       params={"config": {"vision_model": "bypass", "language": "en"}, "models": []}))
    assert response.ok is False and response.payload["code"] == "MM_LEGACY_WRITE_UNSUPPORTED"
    assert response.payload["affected_tools"] == ["vision"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("enterprise", [False, True])
async def test_gateway_real_registration_preserves_confirmation_payload(catalogue, monkeypatch, enterprise):
    from types import SimpleNamespace
    from agent_gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    from openjiuwen_protocol.e2a.agent_models import AgentResponse

    methods = {}
    channel = SimpleNamespace(channel_id="web", register_method=lambda name, fn, local_only=False: methods.update({name: fn}),
                              on_connect=lambda fn: None, on_disconnect=lambda fn: None, send_response=AsyncMock())
    async def request(req):
        return AgentResponse(request_id=req.request_id, channel_id="web", ok=False,
                             payload={"code": "MM_CONFIRM_REQUIRED", "affected_tools": ["vision", "audio"]})
    client = SimpleNamespace(server_ready=True, send_request=AsyncMock(side_effect=request))
    monkeypatch.setattr("agent_gateway.enterprise.is_enterprise", lambda: enterprise)
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    assert len([name for name in methods if name.startswith("multimodal.")]) == 6
    await methods["multimodal.models.delete"](SimpleNamespace(), "gateway-delete", {"model_id": "some-id"}, None)
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["payload"]["affected_tools"] == ["vision", "audio"]
    assert response["payload"]["code"] == "MM_CONFIRM_REQUIRED"


@pytest.mark.parametrize("enterprise", [False, True])
def test_catalogue_initializer_is_independent_of_edition(catalogue, monkeypatch, enterprise):
    path, store = catalogue
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise" if enterprise else "personal")
    multimodal_store.initialize_multimodal_models()
    assert store.read()["schema_version"] == 1
    assert yaml.safe_load(path.read_text())["multimodal"]["models"] == []


def test_default_template_import_keeps_tools_unbound_until_real_model_is_added(catalogue):
    path, store = catalogue
    template = Path(__file__).resolve().parents[2] / "jiuwenswarm/resources/.env.template"
    path.with_name(".env").write_bytes(template.read_bytes())
    store.initialize()
    assert store.read()["models"] == []
    model = model_for(["vision", "audio", "video"])
    result, _ = upsert(store, model)
    for tool in ("vision", "audio", "video"):
        assert result["snapshot"]["tools"][tool] == {"model_id": model["model_id"], "enabled": True}


@pytest.mark.parametrize("encrypted", [False, True])
def test_migration_resolves_dotenv_references_before_filtering_and_decrypting(catalogue, monkeypatch, encrypted):
    from types import SimpleNamespace

    path, store = catalogue
    monkeypatch.setenv("MM_TEST_ORIGIN", "https://effective.invalid")
    monkeypatch.setenv("VISION_API_BASE", "https://stale.invalid")
    monkeypatch.delenv("MM_TEST_MISSING_MODEL", raising=False)
    key = "enc:secret" if encrypted else "secret"
    path.with_name(".env").write_text(
        f"SHARED_BASE=${{MM_TEST_ORIGIN}}/v1\nSHARED_KEY={key}\n"
        "VISION_API_BASE=${SHARED_BASE}\nVISION_API_KEY=${SHARED_KEY}\n"
        "VISION_MODEL_NAME=${MM_TEST_MISSING_MODEL:-effective-model}\nVISION_PROVIDER=OpenAI\nVISION_ENABLED=true\n"
    )
    path.write_text("models:\n  vision:\n    model_client_config:\n"
                    "      api_base: ${VISION_API_BASE}\n      api_key: ${VISION_API_KEY}\n"
                    "      model_name: ${VISION_MODEL_NAME}\n      client_provider: ${VISION_PROVIDER}\n")
    if encrypted:
        def decrypt(value):
            assert value.startswith("enc:")
            return value[4:]
        monkeypatch.setattr(multimodal_store, "get_crypto_provider", lambda: SimpleNamespace(
            encrypt=lambda value: f"enc:{value}", decrypt=decrypt,
        ))
    store.initialize()
    snapshot = store.read()
    model_id = snapshot["models"][0]["model_id"]
    connection = store.read(model_id)["model"]["connection"]
    assert connection["api_base"] == "https://effective.invalid/v1"
    assert connection["api_key"] == "secret"
    assert connection["model_name"] == "effective-model"
    store.mutate("config.apply", {})
    persisted_env = dotenv_values(path.with_name(".env"), interpolate=False)
    assert persisted_env["VISION_API_BASE"] == "https://effective.invalid/v1"
    assert persisted_env["VISION_API_KEY"] == key
    assert os.environ["VISION_API_BASE"] == "https://effective.invalid/v1"
    assert os.environ["VISION_API_KEY"] == "secret"


async def test_migrated_vision_edit_reaches_existing_execution_reader(catalogue):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    path, store = catalogue
    path.with_name(".env").write_text(
        "VISION_API_BASE=https://unused.invalid\nVISION_BASE_URL=https://old.invalid/v1\n"
        "VISION_API_KEY=key\nVISION_MODEL_NAME=unused\nVISION_MODEL=old-model\n"
        "VISION_PROVIDER=OpenAI\nVISION_ENABLED=true\n"
    )
    store.initialize()
    snapshot = store.read()
    model_id = snapshot["models"][0]["model_id"]
    edited = store.read(model_id)["model"]
    assert edited["connection"]["api_base"] == "https://old.invalid/v1"
    assert edited["connection"]["model_name"] == "old-model"
    edited["connection"].update(api_base="https://new.invalid/v1", model_name="new-model", context_window_tokens=262144)
    response = await MultimodalConfigAdapter(on_config_saved=AsyncMock(return_value=True)).handle(
        AgentRequest(request_id="edit-migrated", req_method=ReqMethod.MULTIMODAL_MODELS_UPSERT,
                     params={"mode": "update", "model": edited})
    )
    assert response.ok and response.payload["applied_without_restart"]
    env = dotenv_values(path.with_name(".env"), interpolate=False)
    assert "VISION_BASE_URL" not in env and "VISION_MODEL" not in env
    raw = yaml.safe_load(path.read_text())
    consumer = JiuWenSwarmDeepAdapter._build_vision_model_config(resolve_env_vars(raw))
    assert consumer.base_url == "https://new.invalid/v1"
    assert consumer.model == "new-model"
    assert raw["multimodal"]["models"][0]["model_id"] == model_id


def test_existing_catalogue_aliases_are_normalized_without_reimporting_or_changing_bindings(catalogue):
    path, store = catalogue
    model = model_for(["vision", "audio"])
    store.initialize()
    upsert(store, model)
    raw = yaml.safe_load(path.read_text())
    raw["multimodal"]["models"][0]["legacy_options"] = {
        "environment_overrides": {"base_url": "https://effective.invalid", "model": "effective-model",
                                  "transcription_model": "transcription", "question_answering_model": "qa"},
    }
    raw["multimodal"]["tools"]["audio"]["enabled"] = False
    path.write_text(yaml.safe_dump(raw))
    reopened = multimodal_store.MultimodalStore()
    editable = reopened.read(model["model_id"])["model"]
    assert editable["connection"]["api_base"] == "https://effective.invalid"
    assert editable["connection"]["model_name"] == "effective-model"
    result, _ = reopened.mutate("config.apply", {})
    persisted = yaml.safe_load(path.read_text())["multimodal"]
    assert persisted["tools"] == raw["multimodal"]["tools"]
    assert len(persisted["models"]) == 1
    assert persisted["models"][0]["model_id"] == model["model_id"]
    assert persisted["models"][0]["connection"] == editable["connection"]
    assert persisted["models"][0]["legacy_options"]["environment_overrides"] == {
        "transcription_model": "transcription", "question_answering_model": "qa",
    }
    assert result["snapshot"]["tools"]["audio"]["enabled"] is False


def test_crlf_journal_survives_interruption_before_env_replace_and_next_save_recovers(catalogue, monkeypatch):
    path, store = catalogue
    store.initialize()
    store.read()
    old_env = b"# Windows line endings\r\nUNRELATED=keep\r\n"
    env_path = path.with_name(".env")
    env_path.write_bytes(old_env)
    old_yaml = path.read_bytes()
    original_write = config_persistence.atomic_write

    def interrupt_before_env(target, content):
        if target.name == ".env":
            raise SystemExit("simulated interruption")
        original_write(target, content)

    model = model_for(["vision"])
    with monkeypatch.context() as interruption:
        interruption.setattr(config_persistence, "atomic_write", interrupt_before_env)
        with pytest.raises(SystemExit):
            upsert(store, model)
    journal_path = path.with_name(".multimodal-transaction.json")
    assert json.loads(journal_path.read_text())["old_env"].encode() == old_env
    assert env_path.read_bytes() == old_env and path.read_bytes() == old_yaml
    result, _ = upsert(store, model)
    assert result["snapshot"]["tools"]["vision"]["model_id"] == model["model_id"]
    assert not journal_path.exists()
    assert dotenv_values(env_path)["UNRELATED"] == "keep"


@pytest.mark.parametrize("tool", ["vision", "audio", "video"])
@pytest.mark.parametrize("explicit", [None, "true", "false"])
def test_startup_fills_missing_switch_from_complete_yaml_and_preserves_explicit_choice(
    catalogue, monkeypatch, tool, explicit,
):
    path, store = catalogue
    for capability in CAPABILITIES:
        for suffix in (*FIELDS.values(), "ENABLED", *ALIASES.get(capability, ())):
            monkeypatch.delenv(f"{capability.upper()}_{suffix}", raising=False)
    path.write_text(yaml.safe_dump({"models": {tool: {"model_client_config": {
        "api_base": "https://yaml-only.invalid/v1", "api_key": "verification",
        "model_name": "yaml-only-model", "client_provider": "OpenAI",
    }}}}))
    env_path = path.with_name(".env")
    env_path.write_text("" if explicit is None else f"{tool.upper()}_ENABLED={explicit}\n")
    store.initialize()
    binding = store.read()["tools"][tool]
    assert binding["model_id"] is not None
    assert binding["enabled"] is (explicit != "false")
    assert dotenv_values(env_path)[f"{tool.upper()}_ENABLED"] == ("false" if explicit == "false" else "true")
    assert dotenv_values(env_path)[f"{tool.upper()}_MODEL_NAME"] == "yaml-only-model"


@pytest.mark.parametrize("tool,expected_tools", [("video", 1), ("visual_gen", 1), ("video_gen", 2)])
def test_team_tool_factory_consumes_projected_config_and_respects_switches(catalogue, tool, expected_tools):
    from types import SimpleNamespace
    from jiuwenswarm.agents.swarm.providers import tools

    path, store = catalogue
    store.initialize()
    upsert(store, model_for([tool]))
    factory = {"video": tools._build_video_tools, "visual_gen": tools._build_visual_gen_tools,
               "video_gen": tools._build_video_gen_tools}[tool]
    context = SimpleNamespace(config=resolve_env_vars(yaml.safe_load(path.read_text())))
    assert len(factory(context)) == expected_tools
    store.mutate("tools.update", {"tool_id": tool, "enabled": False})
    context.config = resolve_env_vars(yaml.safe_load(path.read_text()))
    assert factory(context) == []


@pytest.mark.parametrize("case", [f"H{number:02}" for number in range(1, 21)])
def test_historical_acceptance_at_startup(catalogue, monkeypatch, case):
    path, store = catalogue
    raw = {"models": {"defaults": [{"model_id": "ordinary", "name": "chat"}]},
           "model_groups": [{"name": "group"}], "topic": {"enabled": False}}
    env = {"UNRELATED": "keep", "IMAGE_GEN_API_BASE": "https://outside.invalid"}
    expected_count = 1
    expected_switches = {tool: False for tool in CAPABILITIES}

    def legacy(tool, name="shared", enabled=False, base="https://legacy.invalid/v1"):
        prefix = tool.upper()
        env.update({f"{prefix}_API_BASE": base, f"{prefix}_API_KEY": "verification",
                    f"{prefix}_MODEL_NAME": name, f"{prefix}_PROVIDER": "OpenAI",
                    f"{prefix}_ENABLED": str(enabled).lower()})
        expected_switches[tool] = enabled

    if case == "H01":
        expected_count = 0
    elif case == "H02":
        for tool in CAPABILITIES:
            legacy(tool, name=tool, enabled=True)
        expected_count = 5
    elif case == "H03":
        legacy("vision", enabled=False)
    elif case == "H04":
        legacy("vision", enabled=True)
        legacy("audio", enabled=False)
        legacy("visual_gen", name="image", enabled=True)
        legacy("video_gen", name="video", enabled=False)
        expected_count = 3
    elif case == "H05":
        legacy("vision", enabled=True)
        env.pop("VISION_ENABLED")
    elif case == "H06":
        legacy("vision")
        legacy("audio", base="https://different.invalid/v1")
        expected_count = 2
    elif case == "H07":
        for tool in CAPABILITIES:
            legacy(tool, enabled=tool in ("vision", "video_gen"))
        expected_count = 2
    elif case == "H08":
        legacy("vision", enabled=True)
        env.update(VISION_CONTEXT_WINDOW_TOKENS="4096", VISION_ENDPOINT_PROFILE="custom",
                   VISION_VENDOR_KEY="vendor", VISION_PLAN="pro")
        raw["models"]["vision"] = {"model_client_config": {"timeout": 13}}
    elif case == "H09":
        legacy("video_gen", enabled=True)
        env.update(VIDEO_GEN_PROTOCOL="vllm-omni", VIDEO_GEN_API_KEY="", VIDEO_GEN_MODEL_NAME="")
    elif case == "H10":
        legacy("vision", enabled=True)
        raw["models"] = [{"vision": {"model_config": {"api_base": "${VISION_API_BASE}",
                         "api_key": "${VISION_API_KEY}", "model_name": "${VISION_MODEL_NAME}",
                         "client_provider": "${VISION_PROVIDER}"}}}]
    elif case == "H11":
        legacy("vision", enabled=True)
        env.update(VISION_MODEL_NAME="your-vision-model-name", VISION_MODEL="real-model",
                   VISION_BASE_URL="https://effective.invalid/v1")
    elif case in ("H12", "H13", "H14", "H16", "H17", "H18"):
        legacy("vision", enabled=case != "H18")
    elif case == "H15":
        env.update(VISION_API_BASE="https://partial.invalid", VISION_ENABLED="true")
        expected_switches["vision"] = True
    elif case == "H19":
        legacy("vision", enabled=True)
        legacy("visual_gen", enabled=True)
        expected_count = 2
    elif case == "H20":
        legacy("vision")
        legacy("audio")
        env.update(VISION_CONTEXT_WINDOW_TOKENS="1024", AUDIO_CONTEXT_WINDOW_TOKENS="2048")
        expected_count = 2
    path.write_text(yaml.safe_dump(raw))
    for tool in CAPABILITIES:
        for suffix in (*FIELDS.values(), "ENABLED", *ALIASES.get(tool, ())):
            monkeypatch.delenv(f"{tool.upper()}_{suffix}", raising=False)
    env_path = path.with_name(".env")
    env_path.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    before_yaml, before_env = path.read_bytes(), env_path.read_bytes()
    if case == "H14":
        atomic_write = config_persistence.atomic_write
        failed = False

        def fail_once(target, content):
            nonlocal failed
            if target == path and not failed:
                failed = True
                raise OSError("migration interrupted")
            atomic_write(target, content)

        with monkeypatch.context() as failure:
            failure.setattr(config_persistence, "atomic_write", fail_once)
            with pytest.raises(MultimodalError, match="MM_STORAGE_FAILED"):
                store.initialize()
        assert (path.read_bytes(), env_path.read_bytes()) == (before_yaml, before_env)
    store.initialize()
    first = store.read()
    assert len(first["models"]) == expected_count
    assert {tool: first["tools"][tool]["enabled"] for tool in CAPABILITIES} == expected_switches
    assert dotenv_values(env_path)["UNRELATED"] == "keep"
    assert dotenv_values(env_path)["IMAGE_GEN_API_BASE"] == "https://outside.invalid"
    persisted = yaml.safe_load(path.read_text())
    assert persisted["model_groups"] == raw["model_groups"] and persisted["topic"] == raw["topic"]
    if isinstance(raw["models"], dict):
        assert persisted["models"]["defaults"] == raw["models"]["defaults"]
    if case == "H08":
        imported = persisted["multimodal"]["models"][0]
        assert imported["connection"]["context_window_tokens"] == "4096"
        assert imported["connection"]["endpoint_profile"] == "custom"
        assert imported["legacy_options"]["client_options"]["timeout"] == 13
    if case == "H11":
        assert first["models"][0]["model_name"] == "real-model"
        assert first["models"][0]["api_base"] == "https://effective.invalid/v1"
    if case == "H13":
        store.mutate("models.delete", {"model_id": first["models"][0]["model_id"],
                     "confirmed_tools": ["vision"]})
    if case == "H18":
        old_id = first["tools"]["vision"]["model_id"]
        upsert(store, model_for(["vision"]))
        assert store.read()["tools"]["vision"] == {"model_id": old_id, "enabled": False}
    current = yaml.safe_load(path.read_text())["multimodal"]
    restarted = multimodal_store.MultimodalStore()
    restarted.initialize()
    assert yaml.safe_load(path.read_text())["multimodal"] == current
