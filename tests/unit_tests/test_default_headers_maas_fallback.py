"""Tip default_headers + MaaS fallback aliases for LLM auth."""

from __future__ import annotations

import json

from jiuwenswarm.common.local_env_config import (
    apply_env_overrides_to_active,
    bind_task_env_overlay,
    effective_tip,
    parse_default_headers,
    read_default_headers,
    read_default_headers_raw,
    reset_task_env_overlay,
    stage_env_overrides,
)


def test_parse_default_headers_requires_object() -> None:
    assert parse_default_headers("") is None
    assert parse_default_headers(None) is None
    assert parse_default_headers('{"Authorization":"Basic abc"}') == {
        "Authorization": "Basic abc"
    }


def test_parse_default_headers_with_dict() -> None:
    raw = {"Authorization": "Bearer xxx", "x-custom": "app"}
    assert parse_default_headers(raw) == {
        "Authorization": "Bearer xxx",
        "x-custom": "app",
    }


def test_parse_default_headers_with_single_header_dict() -> None:
    assert parse_default_headers({"Authorization": "Bearer xxx"}) == {
        "Authorization": "Bearer xxx"
    }


def test_stage_env_overrides_serializes_dict_as_json() -> None:
    headers = {"Authorization": "Bearer xxx", "x-officeace-client": "app"}
    stage_env_overrides({"default_headers": headers})
    stored = effective_tip().get("default_headers", "")
    assert isinstance(stored, str)
    assert json.loads(stored) == headers
    assert read_default_headers() == headers


def test_stage_env_overrides_keeps_json_string() -> None:
    raw = '{"Authorization": "Bearer xxx", "x-custom": "app"}'
    stage_env_overrides({"default_headers": raw})
    stored = effective_tip().get("default_headers", "")
    assert stored == raw
    assert read_default_headers() == {
        "Authorization": "Bearer xxx",
        "x-custom": "app",
    }


def test_apply_env_overrides_serializes_dict_as_json() -> None:
    headers = {"Authorization": "Bearer xxx"}
    apply_env_overrides_to_active({"default_headers": headers})
    stored = effective_tip().get("default_headers", "")
    assert json.loads(stored) == headers
    assert read_default_headers() == headers


def test_read_default_headers_from_overlay_dict() -> None:
    token = bind_task_env_overlay(
        {"default_headers": {"Authorization": "Basic overlay", "x-custom": "app"}}
    )
    try:
        assert read_default_headers() == {
            "Authorization": "Basic overlay",
            "x-custom": "app",
        }
        parsed = json.loads(read_default_headers_raw())
        assert parsed["Authorization"] == "Basic overlay"
    finally:
        reset_task_env_overlay(token)


def test_read_default_headers_prefers_primary_key() -> None:
    token = bind_task_env_overlay(
        {
            "default_headers": '{"Authorization":"Basic primary"}',
            "PETAL_API_KEY": '{"Authorization":"Basic petal"}',
        }
    )
    try:
        assert read_default_headers_raw() == '{"Authorization":"Basic primary"}'
        assert read_default_headers() == {"Authorization": "Basic primary"}
    finally:
        reset_task_env_overlay(token)


def test_read_default_headers_does_not_fall_back_to_petal_headers() -> None:
    token = bind_task_env_overlay(
        {
            "PETAL_SEARCH_HEADERS": '{"Authorization":"Basic search"}',
            "PETAL_API_KEY": '{"Authorization":"Basic petal"}',
        }
    )
    try:
        assert read_default_headers() is None
    finally:
        reset_task_env_overlay(token)


def test_read_default_headers_falls_back_to_huawei_maas_headers() -> None:
    token = bind_task_env_overlay(
        {
            "OFFICE_CLAW_HUAWEI_MAAS_HEADERS_JSON": (
                '{"Authorization":"Basic maas"}'
            )
        }
    )
    try:
        assert read_default_headers() == {"Authorization": "Basic maas"}
    finally:
        reset_task_env_overlay(token)


def test_read_default_headers_does_not_reuse_petal_auth_for_custom_openai() -> None:
    token = bind_task_env_overlay(
        {
            "API_KEY": "sk-custom-openai",
            "PETAL_SEARCH_HEADERS": '{"Authorization":"Basic petal"}',
        }
    )
    try:
        assert read_default_headers() is None
    finally:
        reset_task_env_overlay(token)


def test_read_default_headers_ignores_non_json_petal_key() -> None:
    token = bind_task_env_overlay({"PETAL_API_KEY": "sk-not-json"})
    try:
        assert read_default_headers() is None
    finally:
        reset_task_env_overlay(token)
