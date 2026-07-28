from uuid import uuid4

import pytest

from jiuwenswarm.server.runtime.usage_cost import (
    add_session_usage,
    get_session_cost_summary,
    set_session_cost_limit,
)


def _sid() -> str:
    return f"usage-cost-test-{uuid4().hex}"


def test_cost_limit_is_ignored_until_provider_cost_metadata_exists() -> None:
    session_id = _sid()

    set_session_cost_limit(session_id, 0.01)
    summary = add_session_usage(
        session_id,
        {"input_tokens": 10, "output_tokens": 5},
    )

    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 5
    assert summary["total_tokens"] == 15
    assert summary["cost_available"] is False
    assert summary["cost_limit"] == 0.01
    assert summary["cost_limit_exceeded"] is False


def test_cost_totals_are_derived_from_input_and_output_cost() -> None:
    session_id = _sid()

    set_session_cost_limit(session_id, 0.01)
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
    assert summary["cost_limit_exceeded"] is True


def test_explicit_total_cost_takes_precedence_and_limit_can_clear() -> None:
    session_id = _sid()

    set_session_cost_limit(session_id, 1.0)
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

    cleared = set_session_cost_limit(session_id, None)
    assert cleared["cost_limit"] is None
    assert get_session_cost_summary(session_id)["cost_limit_exceeded"] is False


def test_cost_limit_rejects_non_finite_values() -> None:
    for value in ("inf", "nan"):
        with pytest.raises(ValueError):
            set_session_cost_limit(_sid(), float(value))
