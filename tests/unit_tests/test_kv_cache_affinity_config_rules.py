from __future__ import annotations

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.kv_cache_affinity_config import (
    normalize_affinity_request,
    validate_affinity_invariant,
)


def _config(
    provider: str,
    *,
    affinity: bool = True,
    release: bool = False,
) -> dict:
    return {
        "models": {
            "defaults": [
                {
                    "is_default": True,
                    "model_client_config": {"client_provider": provider},
                }
            ]
        },
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": affinity,
                "enable_kv_cache_release": release,
            }
        },
        "channels": {},
    }


def test_runtime_config_fails_closed_for_non_ascend_provider() -> None:
    config = _config("OpenAI")

    config_module._normalize_config(config)

    assert (
        config["react"]["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
        is False
    )


def test_affinity_invariant_reports_all_failures() -> None:
    valid, failures = validate_affinity_invariant(
        _config("OpenAI", release=True)
    )

    assert valid is False
    assert failures == [
        "enable_kv_cache_release must be false",
        "default provider must be AscendAffinity, got OpenAI",
    ]


def test_affinity_request_selects_ascend_provider() -> None:
    params = {"kv_cache_affinity_enabled": "true"}

    normalize_affinity_request(params)

    assert params["model_provider"] == "AscendAffinity"


def test_explicit_other_provider_disables_affinity() -> None:
    params = {
        "kv_cache_affinity_enabled": "true",
        "model_provider": "OpenAI",
    }

    normalize_affinity_request(params)

    assert params["kv_cache_affinity_enabled"] == "false"
