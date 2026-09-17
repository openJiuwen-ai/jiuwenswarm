from __future__ import annotations

import pytest

from jiuwenswarm.common.task_loop_config import (
    resolve_task_loop_completion_timeout,
)


@pytest.mark.parametrize("config", [{}, {"completion_timeout": None}, {"completion_timeout": ""}])
def test_completion_timeout_defaults_to_unlimited(config: dict[str, object]) -> None:
    assert resolve_task_loop_completion_timeout(config) is None


@pytest.mark.parametrize("value", [3600, 3600.0, "3600"])
def test_completion_timeout_preserves_explicit_positive_value(value: object) -> None:
    assert resolve_task_loop_completion_timeout({"completion_timeout": value}) == 3600.0


@pytest.mark.parametrize("value", [0, -1, True, "invalid", "nan", "inf"])
def test_completion_timeout_rejects_invalid_value(value: object) -> None:
    with pytest.raises(ValueError, match="completion_timeout"):
        resolve_task_loop_completion_timeout({"completion_timeout": value})
