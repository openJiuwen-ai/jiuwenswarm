"""A model picked by stable ID sends its reasoning level the way the legacy path does.

``reasoning_level`` is JiuwenSwarm's own hint (set from the model settings);
the provider SDK does not know it and refuses the call with
``unexpected keyword argument 'reasoning_level'``. Both model paths turn it
into the ``reasoning`` request field before anything reaches the SDK.
"""

from copy import deepcopy

import pytest

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver


@pytest.fixture
def catalog(monkeypatch):
    state = {
        "models": {
            "defaults": [
                {
                    "model_id": "mdl_r",
                    "alias": "R",
                    "is_default": True,
                    "model_client_config": {
                        "model_name": "deepseek/deepseek-v4.1-flash",
                        "client_provider": "OpenAI",
                        "api_base": "https://openrouter.ai/api/v1",
                        "api_key": "test-key",
                    },
                    "model_config_obj": {
                        "temperature": 0.95,
                        "reasoning_level": "high",
                    },
                }
            ],
            "agentos": [],
            "groups": [
                {
                    "model_group_id": "mgp_r",
                    "name": "G",
                    "enabled": True,
                    "request_config": {"reasoning_level": "low"},
                    "routes": [
                        {
                            "route_id": "rt_1",
                            "model_id": "mdl_r",
                            "enabled": True,
                            "request_overrides": {"reasoning_level": "medium"},
                        }
                    ],
                }
            ],
        }
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.model_catalog.get_config", lambda: deepcopy(state)
    )
    return ModelCatalog()


def test_a_model_by_id_turns_the_reasoning_level_into_the_request_field(
    catalog,
) -> None:
    from jiuwenswarm.server.runtime.model_compiler_adapter import (
        build_model_from_selection,
    )

    resolved = ModelSelectionResolver(catalog).resolve(
        ModelSelection(type="model", id="mdl_r")
    )
    assert "reasoning_level" not in resolved.request_defaults
    assert resolved.request_defaults["reasoning"]["effort"] == "high"
    assert resolved.request_defaults["temperature"] == 0.95

    sent = build_model_from_selection(resolved).model_config.model_dump(
        exclude_none=True
    )
    assert "reasoning_level" not in sent and sent["reasoning"]["effort"] == "high"


def test_a_model_group_turns_every_reasoning_level_into_the_request_field(
    catalog,
) -> None:
    resolved = ModelSelectionResolver(catalog).resolve(
        ModelSelection(type="model_group", id="mgp_r")
    )
    assert resolved.request_config == {
        "reasoning": {"mode": "enabled", "effort": "low"}
    }
    (route,) = resolved.routes
    assert route.request_overrides == {
        "reasoning": {"mode": "enabled", "effort": "medium"}
    }
    assert "reasoning_level" not in route.model.request_defaults
