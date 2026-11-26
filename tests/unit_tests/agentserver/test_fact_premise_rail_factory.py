# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Config gating for the fact-premise guard builder.

``_instantiate_rails`` does **not** catch builder exceptions, so the builder is
the only place that can turn a bad config into "no rail" instead of "agent fails
to build". These cases pin that contract plus the default-on semantics.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.deep import rail_factory


def test_builder_defaults_to_enabled() -> None:
    """An absent config block must not silently disable the guard."""
    rail = rail_factory._build_fact_premise_rail({})

    assert rail is not None
    assert rail.priority == 10
    assert rail.inherit_to_subagents is False
    assert rail._force_first_retrieval is False


def test_builder_returns_none_when_disabled() -> None:
    config = {"execution_guard": {"fact_premise_guard": {"enabled": False}}}

    assert rail_factory._build_fact_premise_rail(config) is None


@pytest.mark.parametrize("value", ["false", "off", "0", 0])
def test_builder_disables_on_explicit_falsey_values(value) -> None:
    config = {"execution_guard": {"fact_premise_guard": {"enabled": value}}}

    assert rail_factory._build_fact_premise_rail(config) is None


@pytest.mark.parametrize("value", ["false", "off", "0", 0, "no"])
def test_quoted_false_does_not_turn_on_forced_retrieval(value) -> None:
    """``bool("false")`` is True; the safety switch must not be quoted-open."""
    config = {
        "execution_guard": {"fact_premise_guard": {"force_first_retrieval": value}}
    }

    rail = rail_factory._build_fact_premise_rail(config)

    assert rail is not None
    assert rail._force_first_retrieval is False


@pytest.mark.parametrize("value", ["true", "on", "1", 1])
def test_whitelist_strings_still_enable_forced_retrieval(value) -> None:
    config = {
        "execution_guard": {"fact_premise_guard": {"force_first_retrieval": value}}
    }

    rail = rail_factory._build_fact_premise_rail(config)

    assert rail is not None
    assert rail._force_first_retrieval is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("false", False), ("TRUE", True), ("unexpected", True)],
)
def test_log_when_unverified_uses_strict_bool_with_default(value, expected) -> None:
    config = {
        "execution_guard": {"fact_premise_guard": {"log_when_unverified": value}}
    }

    rail = rail_factory._build_fact_premise_rail(config)

    assert rail is not None
    assert rail._log_when_unverified is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("false", False), ("TRUE", True), ("unexpected", True)],
)
def test_log_unclassified_claims_uses_strict_bool_with_default(value, expected) -> None:
    config = {
        "execution_guard": {"fact_premise_guard": {"log_unclassified_claims": value}}
    }

    rail = rail_factory._build_fact_premise_rail(config)

    assert rail is not None
    assert rail._log_unclassified_claims is expected


def test_builder_reads_force_options() -> None:
    """``force_tool_names`` also accepts a single string for convenience."""
    config = {
        "execution_guard": {
            "fact_premise_guard": {
                "enabled": True,
                "force_first_retrieval": True,
                "force_tool_names": "web_search",
                "log_when_unverified": False,
            }
        }
    }

    rail = rail_factory._build_fact_premise_rail(config, language="en")

    assert rail is not None
    assert rail._force_first_retrieval is True
    assert rail._force_tool_names == ("web_search",)
    assert rail._log_when_unverified is False
    assert rail._language == "en"


def test_builder_is_fail_open_on_broken_config() -> None:
    class BrokenConfig(dict):
        def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("config unavailable")

    assert rail_factory._build_fact_premise_rail(BrokenConfig()) is None


@pytest.mark.parametrize("guard_cfg", [None, [], "enabled"])
def test_builder_ignores_malformed_guard_block(guard_cfg) -> None:
    """A non-dict block must fall back to defaults, not crash assembly."""
    rail = rail_factory._build_fact_premise_rail(
        {"execution_guard": {"fact_premise_guard": guard_cfg}}
    )

    assert rail is not None
