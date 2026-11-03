# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The runtime state file must not claim a concrete model for a group session."""

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _adapter(router) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_client_config = SimpleNamespace(intelli_router=router)
    adapter._model_request_config = SimpleNamespace(model_name="GLM-5.3")
    return adapter


def _fake_catalog(monkeypatch, display_name: str) -> None:
    class _Catalog:
        def __init__(self) -> None:
            pass

        def get_group(self, group_id: str) -> dict:
            return {"model_group_id": group_id, "display_name": display_name}

        def group_display_name(self, group_id: str) -> str:
            return display_name or group_id

    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)


def test_group_session_names_the_group_not_a_model(monkeypatch) -> None:
    """The router picks per call; the file names the group, never a concrete model."""
    _fake_catalog(monkeypatch, "test2")
    adapter = _adapter(SimpleNamespace(model_group_id="mgp_1"))

    assert adapter._resolve_runtime_state_model("cn") == "模型组 test2"
    assert adapter._resolve_runtime_state_model("en") == "model group test2"


def test_group_without_a_catalog_entry_falls_back_to_its_id(monkeypatch) -> None:
    class _Catalog:
        def get_group(self, group_id: str) -> dict:
            raise LookupError(group_id)

        def group_display_name(self, group_id: str) -> str:
            raise LookupError(group_id)

    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    adapter = _adapter(SimpleNamespace(model_group_id="mgp_x"))

    assert adapter._resolve_runtime_state_model("cn") == "模型组 mgp_x"


def test_plain_session_keeps_its_model_name() -> None:
    """A session without a model group still records its concrete model."""
    adapter = _adapter(None)

    assert adapter._resolve_runtime_state_model("cn") == "GLM-5.3"


def test_moa_session_stops_naming_the_stale_group(monkeypatch) -> None:
    """With MoA active the state file names the aggregator's model, not the group from before."""
    _fake_catalog(monkeypatch, "test2")
    adapter = _adapter(None)
    adapter._last_requested_model_ref = "model_group:mgp_1"
    adapter._moa_runtime = SimpleNamespace(aggregator=object())
    adapter.moa_access_kwargs = lambda _request: {}

    class _Request:
        params: dict = {}

    adapter._resolve_model_for_request(_Request())

    assert adapter._last_requested_model_ref == ""
    assert adapter._resolve_runtime_state_model("cn") == "GLM-5.3"

def test_group_selection_names_the_group_even_though_the_client_is_direct(monkeypatch) -> None:
    """A model-group selection compiles to the first model as a direct client; the file still names the group."""
    _fake_catalog(monkeypatch, "test2")
    adapter = _adapter(None)
    adapter._last_requested_model_ref = "model_group:mgp_1"

    assert adapter._resolve_runtime_state_model("cn") == "模型组 test2"
    assert adapter._resolve_runtime_state_model("en") == "model group test2"


def test_pinned_group_route_keeps_the_concrete_model(monkeypatch) -> None:
    """A selection pinned to one route of the group is that route's model, not the group."""
    _fake_catalog(monkeypatch, "test2")
    adapter = _adapter(None)
    adapter._last_requested_model_ref = "model_group:mgp_1:rte_a"

    assert adapter._resolve_runtime_state_model("cn") == "GLM-5.3"
