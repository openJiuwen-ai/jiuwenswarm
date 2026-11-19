# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team context limits preserve product/member bounds without mutating inputs."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

import pytest

from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.context_engine.schema.config import ContextEngineConfig

from jiuwenswarm.agents.swarm.config_specs import (
    _team_context_engine_config,
    build_member_deep_agent_spec,
)


_MODEL = "team-context-test-model"


@pytest.fixture
def context_logs(caplog, monkeypatch):
    # Application logging does not propagate to pytest's root capture handler.
    target = logging.getLogger("jiuwenswarm.agents.swarm.config_specs")
    monkeypatch.setattr(target, "handlers", [caplog.handler])
    monkeypatch.setattr(target, "propagate", False)
    with caplog.at_level(logging.INFO, logger=target.name):
        yield caplog


@pytest.mark.parametrize("as_model", [False, True], ids=["dict", "model"])
@pytest.mark.parametrize(
    ("model_window", "product_window", "member_window", "expected"),
    [
        (1_000_000, None, None, 200_000),
        (131_072, None, None, 131_072),
        (1_000_000, 131_072, None, 131_072),
        (1_000_000, None, 100_000, 100_000),
        (1_000_000, 131_072, 180_000, 131_072),
        (1_000_000, 180_000, 100_000, 100_000),
        (1_000_000, 500_000, 500_000, 200_000),
        (100_000, 131_072, 180_000, 100_000),
    ],
)
def test_team_window_preserves_every_smaller_bound(
    as_model: bool,
    model_window: int,
    product_window: int | None,
    member_window: int | None,
    expected: int,
) -> None:
    config = {"react": {"context_engine_config": {"context_window_tokens": product_window}}}
    member_config: Any = {
        "model_name": _MODEL,
        "model_context_window_tokens": {_MODEL: model_window},
        "context_window_tokens": member_window,
    }
    if as_model:
        member_config = ContextEngineConfig.model_validate(member_config)
    base = DeepAgentSpec(context_engine_config=member_config)

    result = _team_context_engine_config(config, base)

    assert result.context_window_tokens == expected


@pytest.mark.parametrize("source", ["product", "member"])
@pytest.mark.parametrize("invalid", [None, "", "invalid", 0, -1, True, 1.5, [], {}])
def test_invalid_explicit_window_is_ignored(source: str, invalid: Any) -> None:
    product_config = {"context_window_tokens": invalid if source == "product" else 131_072}
    member_config = {"context_window_tokens": invalid} if source == "member" else {}

    result = _team_context_engine_config(
        {"react": {"context_engine_config": product_config}},
        DeepAgentSpec(context_engine_config=member_config),
    )

    assert result.context_window_tokens == (200_000 if source == "product" else 131_072)


def test_numeric_string_window_is_normalized() -> None:
    result = _team_context_engine_config(
        {"react": {"context_engine_config": {"context_window_tokens": "131072"}}},
        DeepAgentSpec(context_engine_config={"context_window_tokens": "180000"}),
    )

    assert result.context_window_tokens == 131_072


@pytest.mark.parametrize("as_model", [False, True], ids=["dict", "model"])
@pytest.mark.parametrize("member_reload", [None, False])
def test_product_fields_survive_unset_member_defaults_and_inputs_are_isolated(
    as_model: bool, member_reload: bool | None,
) -> None:
    product_config = {
        "context_window_tokens": 131_072,
        "enable_reload": True,
        "default_window_round_num": 3,
        "model_name": _MODEL,
        "model_context_window_tokens": {_MODEL: 500_000},
        "compression_recall_config": {"enabled": True, "chunk_overlap_tokens": 500},
        # Product-only processor settings must not leak into the SDK config.
        "enabled": True,
        "dialogue_compressor_config": {"trigger_ratio": 0.8},
    }
    config = {"react": {"context_engine_config": product_config}}
    member_config: Any = {"context_window_tokens": 180_000}
    if member_reload is not None:
        member_config["enable_reload"] = member_reload
    if as_model:
        member_config = ContextEngineConfig.model_validate(member_config)
    base = DeepAgentSpec(context_engine_config=member_config)
    before_config = deepcopy(config)
    before_base = deepcopy(base.model_dump())

    result = _team_context_engine_config(config, base)

    assert result.context_window_tokens == 131_072
    assert result.enable_reload is (True if member_reload is None else member_reload)
    assert result.default_window_round_num == 3
    assert result.compression_recall_config.enabled is True
    assert result.compression_recall_config.chunk_overlap_tokens == 500
    assert "dialogue_compressor_config" not in result.model_dump()
    result.model_context_window_tokens[_MODEL] = 1
    result.compression_recall_config.enabled = False
    assert config == before_config
    assert base.model_dump() == before_base


@pytest.mark.parametrize(
    ("product_window", "member_window", "model_window", "expected"),
    [
        (500_000, 600_000, 1_000_000, 200_000),
        (131_072, 180_000, 1_000_000, 131_072),
        (131_072, None, 100_000, 100_000),
    ],
)
def test_truncated_explicit_window_is_logged(
    context_logs, product_window: int, member_window: int | None,
    model_window: int, expected: int,
) -> None:
    _team_context_engine_config(
        {"react": {"context_engine_config": {"context_window_tokens": product_window}}},
        DeepAgentSpec(context_engine_config={
            "model_name": _MODEL,
            "model_context_window_tokens": {_MODEL: model_window},
            "context_window_tokens": member_window,
        }),
    )

    records = [r for r in context_logs.records if "Team context window capped" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    for value in (
        f"model={_MODEL}", f"product_window={product_window}",
        f"member_window={member_window}", f"model_window={model_window}",
        "team_limit=200000", f"window={expected}",
    ):
        assert value in message


@pytest.mark.parametrize("product_window", [None, 131_072])
def test_window_without_explicit_truncation_is_not_logged(context_logs, product_window) -> None:
    _team_context_engine_config(
        {"react": {"context_engine_config": {"context_window_tokens": product_window}}},
        DeepAgentSpec(),
    )

    assert not any("Team context window capped" in r.getMessage() for r in context_logs.records)


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_product_window_reaches_member_spec_and_survives_serialization(mode, role) -> None:
    config = {"react": {"context_engine_config": {"context_window_tokens": 131_072}}}
    base = DeepAgentSpec()

    spec = build_member_deep_agent_spec(config, mode, role, base)
    restored = DeepAgentSpec.model_validate_json(spec.model_dump_json())

    assert spec.context_engine_config.context_window_tokens == 131_072
    assert ContextEngineConfig.model_validate(
        restored.context_engine_config,
    ).context_window_tokens == 131_072
    assert base.context_engine_config is None
