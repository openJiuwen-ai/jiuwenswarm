# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kernel reuse preserves the new provider's per-session request identity."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.model_factory import ModelFactory


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_opencode_session_headers_isolate_compiled_models(monkeypatch, group):
    from jiuwenswarm.common.model_catalog import ModelCatalog
    from jiuwenswarm.common.opencode_go import (
        OPENCODE_GO_SESSION_HEADER, derive_session_id,
    )
    from jiuwenswarm.runtime import kernel as kernel_module
    from jiuwenswarm.server.runtime import model_compiler_adapter

    config = {"models": {"defaults": [{
        "model_id": "mdl_session_scoped",
        "model_client_config": {
            "model_name": "fixture", "client_provider": "OpenAI",
            "api_base": "https://model.invalid/v1", "api_key": "fixture-only",
            "vendor_key": "opencode-go",
        },
    }], "groups": [{
        "model_group_id": "mgp_session_scoped",
        "routes": [{"route_id": "primary", "model_id": "mdl_session_scoped"}],
    }]}}
    catalog = ModelCatalog(config)
    kernel = kernel_module.ResourceKernel(max_idle=0)
    monkeypatch.setattr(kernel_module, "process_kernel", lambda: kernel)
    built = []

    def build(resolved):
        selected = resolved.routes[0].model if group else resolved
        result = SimpleNamespace(headers=dict(selected.client_options["custom_headers"]))
        built.append(result)
        return result

    monkeypatch.setattr(model_compiler_adapter, "build_model_from_selection", build)
    identity = kernel_module.KernelIdentity("owner", "data", "project", "agent", "def", "cap", "cfg")
    first = SimpleNamespace(_kernel_identity=identity, _opencode_go_scope=lambda: "session:first")
    second = SimpleNamespace(_kernel_identity=identity, _opencode_go_scope=lambda: "session:second")
    factories = [ModelFactory(lambda *_: None), ModelFactory(lambda *_: None)]
    requested = "model_group:mgp_session_scoped" if group else "model:mdl_session_scoped"
    try:
        a = factories[0].resolve_selection(first, requested, catalog=catalog)
        b = factories[1].resolve_selection(second, requested, catalog=catalog)
        assert a is not b
        assert factories[0].resolve_selection(first, requested, catalog=catalog) is a
        assert a.headers[OPENCODE_GO_SESSION_HEADER] == derive_session_id("session:first")
        assert b.headers[OPENCODE_GO_SESSION_HEADER] == derive_session_id("session:second")
        assert len(built) == 2
        assert "custom_headers" not in config["models"]["defaults"][0]["model_client_config"]
    finally:
        for factory in factories:
            for lease in factory.leases.values():
                await lease.release()
        await kernel.close()
