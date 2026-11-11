"""Employee permission rail factory wires the isolated auto reviewer correctly."""

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.server.jiuwenmate.permission_runtime import (
    EmployeePermissionRailFactory,
)


class RebuildableModel:
    """Reviewer model stub that rebuilds without sharing the original instance."""

    def __init__(self, *, model_client_config, model_config=None) -> None:
        self.model_client_config = model_client_config
        self.model_config = model_config


def _permissions(**auto_options) -> dict:
    auto = {"reviewer_min_confidence": 0.0, **auto_options}
    return {"enabled": True, "mode": "auto", "auto": auto}


def _build(permissions, model):
    return EmployeePermissionRailFactory.build(
        execution=None,
        permissions=permissions,
        model=model,
        model_name="reviewer-test",
        workspace_root=None,
        sys_operation=None,
        tool_names=("read_file",),
    )


def test_employee_rail_factory_wires_isolated_reviewer(tmp_path) -> None:
    model = RebuildableModel(
        model_client_config={"model_name": "reviewer-test"},
        model_config={"temperature": 0.5},
    )
    rail = _build(
        _permissions(
            reviewer_timeout_ms=4321,
            reviewer_output_format="prompt",
        ),
        model,
    )

    assert isinstance(rail, AutoPermissionInterruptRail)
    reviewer = rail.auto_reviewer
    assert reviewer is not None
    # The reviewer must not share the Agent model object.
    assert reviewer.client._model is not model
    assert reviewer.client._model.model_client_config == model.model_client_config
    assert reviewer.client._model.model_config == model.model_config
    assert reviewer.timeout_seconds == pytest.approx(4.321)
    assert reviewer.client.output_format == "prompt"
    # Legacy min_confidence options must not leak into the reviewer.
    assert not hasattr(reviewer, "min_confidence")


def test_employee_rail_factory_fails_closed_without_rebuildable_model() -> None:
    rail = _build(_permissions(), object())

    assert isinstance(rail, AutoPermissionInterruptRail)
    assert rail.auto_reviewer is None


def test_employee_rail_factory_reviewer_language_follows_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = RebuildableModel(model_client_config={"model_name": "reviewer-test"})
    config: dict = {"preferred_language": "en"}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    rail = _build(_permissions(), model)

    assert rail.auto_reviewer is not None
    assert rail.auto_reviewer.client._display_language() == "en"

    config.clear()
    assert rail.auto_reviewer.client._display_language() == "zh"
