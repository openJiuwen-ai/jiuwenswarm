"""Adapter cache lookup must respect the full Kernel identity."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.runtime import kernel as kernel_module
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


@pytest.mark.asyncio
async def test_permissions_change_refreshes_model_lease_without_closing_an_existing_lease(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    kernel = kernel_module.ResourceKernel()
    monkeypatch.setattr(kernel_module, "process_kernel", lambda: kernel)
    built = []

    def build_model(mcc, mco):
        model = SimpleNamespace(model_client_config=mcc, model_config=mco)
        built.append(model)
        return model

    monkeypatch.setattr(interface_deep, "build_model_from_entry", build_model)
    monkeypatch.setattr(interface_deep, "is_placeholder_model_entry", lambda _: False)
    monkeypatch.setattr(interface_deep, "model_client_config_view", lambda value: value)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._global_index_to_cache_key = {}
    adapter._model_id_to_key = {}
    adapter._model_group_cache = {}
    adapter._last_models_config_fingerprint = None
    adapter._model = None
    monkeypatch.setattr(adapter, "_inject_attribution_to_config", lambda _: None)

    def build_defaults(_config):
        adapter._model_cache["model-a"] = adapter._build_kernel_model(
            {"model_name": "model-a", "api_base": "http://model.invalid"}, {},
            resource_id="stable-model-a",
        )

    monkeypatch.setattr(adapter, "_build_model_cache_from_defaults", build_defaults)
    config = {"models": {}, "permissions": {"mode": "ask"}}
    first = adapter._create_model(deepcopy(config))
    old_lease = next(iter(adapter._kernel_leases.values()))
    assert adapter._create_model(deepcopy(config)) is first
    assert len(built) == 1

    config["permissions"]["mode"] = "auto"
    second = adapter._create_model(deepcopy(config))
    assert second is not first
    assert old_lease.value is first
    assert len(built) == 2
    assert adapter._create_model(deepcopy(config)) is second
    assert len(built) == 2
    for lease in adapter._kernel_leases.values():
        await lease.release()
    await kernel.close()
