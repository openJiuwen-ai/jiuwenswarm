from uuid import uuid4

import pytest

from jiuwenswarm.server.runtime.usage_cost import (
    CostLimitExceededError,
    add_session_usage,
    clear_session_cost,
    get_session_cost_summary,
    raise_if_session_cost_limit_exceeded,
    set_session_cost_limit,
)


def _sid() -> str:
    return f"usage-cost-test-{uuid4().hex}"


@pytest.fixture(autouse=True)
def _enable_usage_cost(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "usage_cost": {
                "enabled": True,
                "mode": "provider_reported",
                "enforce_limits": True,
                "unsupported_provider_behavior": "refuse_limit",
                "currency": "USD",
            }
        },
        raising=False,
    )


def test_cost_limit_is_refused_until_provider_cost_metadata_exists() -> None:
    session_id = _sid()

    summary = add_session_usage(
        session_id,
        {"input_tokens": 10, "output_tokens": 5},
    )

    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 5
    assert summary["total_tokens"] == 15
    assert summary["cost_available"] is False
    assert summary["cost_limit_exceeded"] is False
    with pytest.raises(ValueError, match="provider has not reported"):
        set_session_cost_limit(session_id, 0.01)


def test_cost_totals_are_derived_from_input_and_output_cost() -> None:
    session_id = _sid()

    summary = add_session_usage(
        session_id,
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_cost": 0.006,
            "output_cost": 0.006,
        },
    )

    assert summary["cost_available"] is True
    assert summary["input_cost"] == 0.006
    assert summary["output_cost"] == 0.006
    assert summary["total_cost"] == 0.012
    summary = set_session_cost_limit(session_id, 0.01)
    assert summary["cost_limit_exceeded"] is True

    with pytest.raises(CostLimitExceededError):
        raise_if_session_cost_limit_exceeded(session_id)


def test_explicit_total_cost_takes_precedence_and_limit_can_clear() -> None:
    session_id = _sid()

    summary = add_session_usage(
        session_id,
        {
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 9,
            "input_cost": 0.2,
            "output_cost": 0.2,
            "total_cost": 0.75,
        },
    )
    assert summary["total_tokens"] == 9
    assert summary["total_cost"] == 0.75

    set_session_cost_limit(session_id, 1.0)
    assert raise_if_session_cost_limit_exceeded(session_id)["cost_limit_exceeded"] is False
    cleared = set_session_cost_limit(session_id, None)
    assert cleared["cost_limit"] is None
    assert get_session_cost_summary(session_id)["cost_limit_exceeded"] is False


def test_cost_limit_rejects_non_finite_values() -> None:
    for value in ("inf", "nan"):
        session_id = _sid()
        add_session_usage(session_id, {"total_cost": 0.01})
        with pytest.raises(ValueError):
            set_session_cost_limit(session_id, float(value))


def test_clear_session_cost_removes_totals_and_limit() -> None:
    session_id = _sid()

    add_session_usage(session_id, {"input_tokens": 1, "total_cost": 0.02})
    set_session_cost_limit(session_id, 0.01)

    clear_session_cost(session_id)
    summary = get_session_cost_summary(session_id)

    assert summary["input_tokens"] == 0
    assert summary["total_cost"] == 0.0
    assert summary["cost_limit"] is None
    assert summary["cost_available"] is False


def test_cost_feature_disabled_hides_provider_cost_and_refuses_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"usage_cost": {"enabled": False}},
    )
    session_id = _sid()

    summary = add_session_usage(session_id, {"input_tokens": 1, "total_cost": 0.02})

    assert summary["cost_feature_enabled"] is False
    assert summary["cost_available"] is False
    assert summary["total_cost"] == 0.0
    with pytest.raises(ValueError, match="disabled"):
        set_session_cost_limit(session_id, 0.01)
