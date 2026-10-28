"""Legacy YAML must not prevent configuring models through the settings RPCs."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.config_panel import models_handlers, model_resource_handlers
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_config_validation import validate_models_config


def _model(model_id="mdl_existing", name="existing"):
    return {
        "model_id": model_id,
        "model_client_config": {
            "model_name": name, "api_base": "https://model.test/v1",
            "client_provider": "OpenAI", "api_key": "test-secret",
        },
        "model_config_obj": None,
    }


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(config_module, "get_crypto_provider", lambda: None)
    monkeypatch.setattr(models_handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PLATFORM", raising=False)
    return path


async def _list_models():
    channel = SimpleNamespace(send_response=AsyncMock())
    await models_handlers.models_list_handler(channel, SimpleNamespace(), "r", {}, "s")
    response = channel.send_response.call_args.kwargs
    assert response["ok"], response
    rows = response["payload"]["models"]
    # The settings parser rejects the entire response if any of these is null.
    for row in rows:
        for field in ("model_name", "api_base", "api_key", "model_provider", "reasoning_level"):
            assert isinstance(row[field], str), (field, row)
    return rows


async def _resource(operation, params):
    channel = SimpleNamespace(send_response=AsyncMock())
    await model_resource_handlers.model_resource_handler(
        channel, SimpleNamespace(), "r", params, "s", operation=operation,
    )
    return channel.send_response.call_args.kwargs


@pytest.mark.asyncio
async def test_list_preserves_origin_indices_and_agentos_identity(config_file):
    empty = {"model_id": "mdl_empty", "model_client_config": None, "model_config_obj": None}
    null_fields = _model("mdl_fields", "nullable")
    null_fields["model_client_config"].update(api_base=None, api_key=None, client_provider=None)
    data = {"channels": None, "models": {
        "defaults": [None, _model(), empty, null_fields],
        "agentos": [_model("mdl_backup", "backup")], "groups": None,
    }}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = config_file.read_bytes()

    rows = await _list_models()
    assert [(row["model_name"], row["origin_index"]) for row in rows] == [
        ("existing", 1), ("", 2), ("nullable", 3), ("backup", 4),
    ]
    assert rows[-1]["is_agentos"] is True
    for model_id in ("mdl_empty", "mdl_fields"):
        detail = await _resource("models.get", {"model_id": model_id})
        assert detail["ok"], detail
        client = detail["payload"]["model"]["model_client_config"]
        assert isinstance(client["model_name"], str)
        assert isinstance(client["api_base"], str)
    detail = await _resource("models.get", {"model_id": "mdl_backup"})
    assert detail["ok"] and detail["payload"]["model"]["read_only"]
    assert config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("models", [None, {"defaults": None, "agentos": None, "groups": None}])
async def test_empty_yaml_models_can_be_configured(config_file, models):
    config_file.write_text(yaml.safe_dump({"models": models}), encoding="utf-8")
    await _list_models()
    assert config_module.migrate_model_business_ids()
    assert not config_module.migrate_model_business_ids()
    saved = await _resource("models.upsert", {"model": _model("mdl_new", "new")})
    assert saved["ok"], saved
    rows = await _list_models()
    assert [row["model_name"] for row in rows] == ["new"]


@pytest.mark.asyncio
async def test_migrate_then_add_edit_delete_preserves_valid_configuration(config_file):
    existing = _model()
    existing["custom_extension"] = {"keep": "value"}
    existing["model_client_config"]["custom_headers"] = {"X-Provider": "value"}
    data = {"other_section": {"keep": True}, "models": {
        "defaults": [None, existing], "agentos": [None, _model("mdl_backup", "backup")],
        "groups": None,
    }}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert config_module.migrate_model_business_ids()
    first = config_file.read_bytes()
    assert not config_module.migrate_model_business_ids()
    assert config_file.read_bytes() == first
    rows = await _list_models()
    assert [row["model_name"] for row in rows] == ["existing", "backup"]

    created = await _resource("models.upsert", {"model": _model("mdl_new", "new")})
    assert created["ok"], created
    edited = await _resource("models.upsert", {"model": {"model_id": "mdl_existing", "alias": "edited"}})
    assert edited["ok"], edited
    assert "api_key" not in edited["payload"]["model"]["model_client_config"]
    deleted = await _resource("models.delete", {"model_id": "mdl_new"})
    assert deleted["ok"], deleted
    refused = await _resource("models.delete", {"model_id": "mdl_backup"})
    assert not refused["ok"]
    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert persisted["other_section"] == data["other_section"]
    stored = persisted["models"]["defaults"][0]
    assert stored["model_id"] == "mdl_existing"
    assert stored["model_client_config"] == existing["model_client_config"]
    assert stored["custom_extension"] == existing["custom_extension"]
    assert stored["model_config_obj"] == {}
    assert stored["alias"] == "edited"
    assert not list(config_file.parent.glob("config.yaml.models-*.bak"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model_client_config", "model_config_obj", "model_detail"])
@pytest.mark.parametrize("invalid", ["bad", ["bad"], 7, False])
async def test_invalid_model_objects_are_rejected_without_writing(config_file, field, invalid):
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [_model()]}}), encoding="utf-8")
    before = config_file.read_bytes()
    submitted = _model("mdl_bad", "bad")
    submitted[field] = invalid
    response = await _resource("models.upsert", {"model": submitted})
    assert not response["ok"]
    assert field in response["error"] and "must be an object" in response["error"]
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("field", ["model_client_config", "model_config_obj", "model_detail"])
@pytest.mark.parametrize("invalid", ["bad", ["bad"], 7, False])
def test_candidate_validation_rejects_wrong_object_types(field, invalid):
    entry = _model()
    entry[field] = invalid
    assert f"defaults[0].{field} must be an object" in validate_models_config({"defaults": [entry]})


def test_catalog_ignores_empty_items_without_mutating_input():
    data = {"models": {"defaults": [None, _model()], "agentos": None}}
    original = deepcopy(data)
    catalog = ModelCatalog(data)
    assert [row["model_id"] for row in catalog.list_public_models()] == ["mdl_existing"]
    assert data == original


@pytest.mark.asyncio
async def test_empty_model_can_be_opened_edited_and_saved(config_file):
    data = {"models": {"defaults": [{
        "model_id": "mdl_empty", "model_client_config": None, "model_config_obj": None,
    }]}}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    detail = await _resource("models.get", {"model_id": "mdl_empty"})
    assert detail["ok"], detail
    client = detail["payload"]["model"]["model_client_config"]
    assert client["model_name"] == client["api_base"] == ""
    saved = await _resource("models.upsert", {"model": _model("mdl_empty", "configured")})
    assert saved["ok"], saved
    assert [row["model_name"] for row in await _list_models()] == ["configured"]


@pytest.mark.asyncio
async def test_legacy_replace_resolves_ids_across_null_items(config_file, monkeypatch):
    monkeypatch.setenv("ISSUE_5102_TEST_KEY", "test-secret")
    entry = _model()
    entry["model_client_config"]["api_key"] = "${ISSUE_5102_TEST_KEY}"
    data = {"models": {"defaults": [None, entry], "agentos": [None]}}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    rows = await _list_models()
    rows[0]["alias"] = "renamed"
    channel = SimpleNamespace(send_response=AsyncMock())
    await models_handlers.models_replace_all_handler(
        channel, SimpleNamespace(), "r", {"models": rows}, "s",
    )
    response = channel.send_response.call_args.kwargs
    assert response["ok"], response
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))["models"]
    assert len(stored["defaults"]) == 1
    assert stored["defaults"][0]["model_id"] == "mdl_existing"
    assert stored["defaults"][0]["alias"] == "renamed"
    assert stored["defaults"][0]["model_client_config"]["api_key"] == "${ISSUE_5102_TEST_KEY}"
    assert stored["agentos"] == []


@pytest.mark.asyncio
async def test_explicit_null_can_still_clear_model_request_overrides(config_file):
    entry = _model()
    entry["model_config_obj"] = {"temperature": 0.7}
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [entry]}}), encoding="utf-8")
    response = await _resource("models.upsert", {"model": {
        "model_id": "mdl_existing", "model_config_obj": None,
    }})
    assert response["ok"], response
    assert response["payload"]["model"]["model_config_obj"] == {}
    assert (await _list_models())[0]["temperature"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model_client_config", "model_config_obj", "model_detail"])
@pytest.mark.parametrize("invalid", ["bad", ["bad"], 7, False])
async def test_existing_wrong_types_are_backed_up_and_remain_editable(config_file, field, invalid):
    healthy = _model()
    healthy["model_config_obj"] = {"temperature": 0.4}
    damaged = _model("mdl_damaged", "damaged")
    damaged[field] = invalid
    data = {"models": {"defaults": ["invalid-entry", healthy, damaged]}}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = config_file.read_bytes()

    rows = await _list_models()
    assert [row["model_id"] for row in rows] == ["mdl_existing", "mdl_damaged"]
    assert config_file.read_bytes() == before
    assert not list(config_file.parent.glob("config.yaml.models-*.bak"))

    assert config_module.migrate_model_business_ids()
    backups = list(config_file.parent.glob("config.yaml.models-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert stored["models"]["defaults"][0] == healthy
    assert stored["models"]["defaults"][1][field] == {}
    assert not config_module.migrate_model_business_ids()
    opened = await _resource("models.get", {"model_id": "mdl_damaged"})
    assert opened["ok"], opened
    saved = await _resource("models.upsert", {"model": _model("mdl_damaged", "repaired")})
    assert saved["ok"], saved
    assert [row["model_name"] for row in await _list_models()] == ["existing", "repaired"]
    assert list(config_file.parent.glob("config.yaml.models-*.bak")) == backups


def test_failed_recovery_backup_leaves_configuration_unchanged(config_file, monkeypatch):
    import shutil

    entry = _model()
    entry["model_config_obj"] = "bad"
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [entry]}}), encoding="utf-8")
    before = config_file.read_bytes()

    def fail_backup(*args):
        raise PermissionError("test backup denied")

    monkeypatch.setattr(shutil, "copy2", fail_backup)
    with pytest.raises(PermissionError, match="backup denied"):
        config_module.migrate_model_business_ids()
    assert config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model_name", "api_base", "api_key", "client_provider", "custom_headers"])
async def test_invalid_client_field_cannot_be_written(config_file, field):
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [_model()]}}), encoding="utf-8")
    before = config_file.read_bytes()
    submitted = _model("mdl_bad", "bad")
    submitted["model_client_config"][field] = ["bad"]
    response = await _resource("models.upsert", {"model": submitted})
    assert not response["ok"]
    assert field in response["error"]
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("channels", [None, {}, {"feishu": {"send_file_allowed": False}}])
def test_channel_normalization_preserves_file_delivery_defaults(channels):
    from jiuwenswarm.agents.harness.common.tools.file_delivery_policy import is_send_file_enabled

    data = {"channels": deepcopy(channels)}
    original_channels = data["channels"]
    config_module._normalize_config(data)
    if original_channels is not None:
        assert data["channels"] is original_channels
    for channel in ("web", "feishu"):
        expected = not (channel == "feishu" and bool(channels))
        assert is_send_file_enabled(data, channel) is expected
    assert is_send_file_enabled(data, "xiaoyi") is False
    snapshot = deepcopy(data)
    config_module._normalize_config(data)
    assert data == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model_name", "api_base", "api_key", "client_provider", "model_provider"])
@pytest.mark.parametrize("invalid", [["bad"], {"bad": "value"}, 7, False])
async def test_invalid_flat_client_field_cannot_be_written(config_file, field, invalid):
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [_model()]}}), encoding="utf-8")
    before = config_file.read_bytes()
    response = await _resource("models.upsert", {"model": {"model_id": "mdl_bad", field: invalid}})
    assert not response["ok"]
    assert field in response["error"] and "must be a string" in response["error"]
    assert config_file.read_bytes() == before


@pytest.mark.asyncio
async def test_flat_model_strings_and_null_values_remain_supported(config_file):
    config_file.write_text(yaml.safe_dump({"models": {"defaults": []}}), encoding="utf-8")
    response = await _resource("models.upsert", {"model": {
        "model_id": "mdl_flat", "model_name": "flat-model", "api_base": "https://model.test/v1",
        "model_provider": "OpenAI", "api_key": None, "model_config_obj": None,
    }})
    assert response["ok"], response
    rows = await _list_models()
    assert rows[0]["model_name"] == "flat-model"
    assert rows[0]["model_provider"] == "OpenAI"
    assert rows[0]["api_key"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("section,field,repaired", [
    ("model_client_config", "model_name", ""),
    ("model_client_config", "api_base", ""),
    ("model_client_config", "api_key", ""),
    ("model_client_config", "client_provider", ""),
    ("model_client_config", "custom_headers", {}),
    ("model_detail", "fallback_tag", ""),
    ("model_detail", "model_description", ""),
])
async def test_existing_invalid_subfields_are_backed_up_on_save(config_file, section, field, repaired):
    healthy = _model()
    healthy["model_config_obj"] = {"temperature": 0.4}
    damaged = _model("mdl_damaged", "damaged")
    damaged.setdefault(section, {})[field] = ["bad"]
    config_file.write_text(yaml.safe_dump({"models": {"defaults": [healthy, damaged]}}), encoding="utf-8")
    before = config_file.read_bytes()
    await _list_models()
    detail = await _resource("models.get", {"model_id": "mdl_damaged"})
    assert detail["ok"], detail
    assert config_file.read_bytes() == before
    assert not list(config_file.parent.glob("config.yaml.models-*.bak"))
    saved = await _resource("models.upsert", {"model": {"model_id": "mdl_damaged", "alias": "editable"}})
    assert saved["ok"], saved
    backups = list(config_file.parent.glob("config.yaml.models-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))["models"]["defaults"]
    assert stored[0] == healthy
    assert stored[1][section][field] == repaired
    assert stored[1]["alias"] == "editable"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "invalid-group", ["invalid-group"]])
@pytest.mark.parametrize("operation", [
    "migration", "models.upsert", "models.delete", "groups.upsert", "groups.delete",
])
async def test_invalid_old_group_items_do_not_block_model_configuration(config_file, invalid, operation):
    from jiuwenswarm.common.model_selection import ModelSelection

    healthy = {
        "model_group_id": "mgp_healthy", "display_name": "Healthy group", "enabled": True,
        "routes": [{"route_id": "rte_existing", "model_id": "mdl_existing"}],
        "request_config": {"temperature": 0.4}, "custom_extension": {"keep": True},
    }
    target = {
        "model_group_id": "mgp_target",
        "routes": [{"route_id": "rte_target", "model_id": "mdl_existing"}],
    }
    data = {"models": {
        "defaults": [_model(), _model("mdl_spare", "spare")],
        "groups": [invalid, healthy, target],
    }}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = config_file.read_bytes()
    catalog = ModelCatalog()
    assert len(catalog.list_public_groups()) == 2
    references = catalog.find_references(ModelSelection(type="model", id="mdl_existing"))
    assert {ref.scope_id for ref in references if ref.scope == "model_group"} == {"mgp_healthy", "mgp_target"}
    await _list_models()
    assert config_file.read_bytes() == before
    assert not list(config_file.parent.glob("config.yaml.models-*.bak"))

    if operation == "migration":
        assert config_module.migrate_model_business_ids()
        assert not config_module.migrate_model_business_ids()
    else:
        params = {
            "models.upsert": {"model": _model("mdl_new", "new")},
            "models.delete": {"model_id": "mdl_spare"},
            "groups.upsert": {"group": {**target, "display_name": "Edited group"}},
            "groups.delete": {"model_group_id": "mgp_target"},
        }[operation]
        response = await _resource(operation, params)
        assert response["ok"], response
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))["models"]
    assert all(isinstance(group, dict) for group in stored["groups"])
    assert stored["groups"][0] == healthy
    backups = list(config_file.parent.glob("config.yaml.models-*.bak"))
    if invalid is None:
        assert not backups
    else:
        assert len(backups) == 1 and backups[0].read_bytes() == before


@pytest.mark.asyncio
async def test_new_invalid_group_route_is_rejected_without_repair(config_file):
    data = {"models": {"defaults": [_model()], "groups": []}}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = config_file.read_bytes()
    response = await _resource("groups.upsert", {"group": {
        "model_group_id": "mgp_bad", "routes": [None],
    }})
    assert not response["ok"]
    assert "route[0] must be an object" in response["error"]
    assert config_file.read_bytes() == before
