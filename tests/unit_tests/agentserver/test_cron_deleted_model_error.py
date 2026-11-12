# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

CRON_SESSION = "cron_202611120000_test-job"
WEB_SESSION = "web-regular-session"
ERROR_CODE = "model_selection_not_found"
ERROR_MESSAGE = "定时任务指定的模型已被删除，请编辑任务并重新选择模型"


class _StubCatalog:
    """Only ``mdl_alive`` resolves; every other stable id is 'deleted'."""

    def __init__(self, config=None) -> None:
        pass

    def has_selection(self, selection) -> bool:
        return selection.id == "mdl_alive"


def _request(session_id: str, **params):
    return SimpleNamespace(params=dict(params), session_id=session_id)


@pytest.fixture
def adapter(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._last_resolved_model = None
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _StubCatalog)
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: False)
    # Non-cron fallbacks and later gate steps stay out of this unit's scope.
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id, **_: {},
    )
    monkeypatch.setattr(adapter, "_requested_model_name", lambda _request: "stub-model")
    monkeypatch.setattr(adapter, "_scoped_login_auth", lambda _request: None)
    monkeypatch.setattr(adapter, "_note_session_model", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_is_uncredentialed_login_model", lambda _request, _name: False)
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": True)
    return adapter


def test_deleted_model_selection_fails_the_cron_run(adapter):
    request = _request(
        CRON_SESSION,
        model_selection={"type": "model", "id": "mdl_gone"},
    )
    assert adapter._model_config_error(request) == (ERROR_CODE, ERROR_MESSAGE)


def test_alive_model_selection_keeps_the_cron_run(adapter):
    request = _request(
        CRON_SESSION,
        model_selection={"type": "model", "id": "mdl_alive"},
    )
    assert adapter._model_config_error(request) is None


def test_non_cron_session_keeps_the_existing_fallback(adapter):
    # Interactive sessions must keep "session selects deleted model → current
    # default" (#5229 only hardens scheduled-task runs).
    request = _request(
        WEB_SESSION,
        model_selection={"type": "model", "id": "mdl_gone"},
    )
    assert adapter._model_config_error(request) is None


def test_cron_run_without_stable_selection_is_untouched(adapter):
    # Legacy name-only jobs (e.g. login free models) carry no model_selection.
    request = _request(CRON_SESSION, model_name="mimo-v2.5-free")
    assert adapter._model_config_error(request) is None


def test_enterprise_cron_run_keeps_its_own_catalogue_path(adapter, monkeypatch):
    # Enterprise tenants resolve against their own catalogue; a local miss
    # must not fail a job whose model exists on the tenant side.
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: True)
    request = _request(
        CRON_SESSION,
        model_selection={"type": "model", "id": "mdl_gone"},
    )
    assert adapter._model_config_error(request) is None


def test_malformed_selection_keeps_the_existing_value_error_path(adapter):
    request = _request(CRON_SESSION, model_selection={"type": "model"})
    assert adapter._model_config_error(request) is None


def test_attach_only_cron_subscription_skips_the_model_check(adapter):
    from jiuwenswarm.runtime.session.model import TEAM_ATTACH_ONLY_PARAM

    request = _request(
        CRON_SESSION,
        model_selection={"type": "model", "id": "mdl_gone"},
        **{TEAM_ATTACH_ONLY_PARAM: True},
    )
    assert adapter._model_config_error(request) is None
