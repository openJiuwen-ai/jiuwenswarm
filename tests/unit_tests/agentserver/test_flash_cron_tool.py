from __future__ import annotations

from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from jiuwenswarm.agents.harness.flash.tools.cron_flash import (
    _cron_dispatch,
    _merged_cron_description,
    _merged_cron_input_params,
    _schema_style,
    _translate_to_native,
    build_cron_flash_tool,
)

class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_job(self, params, *, context=None):
        self.calls.append(("add", params))
        return params

    async def update_job(self, job_id, patch, *, context=None):
        self.calls.append(("update", job_id, patch))
        return patch

    async def wake(self, text, *, context=None, mode=None):
        self.calls.append(("wake", text, mode))
        return {"queued": True}

    async def status(self):
        self.calls.append(("status",))
        return {"running": False, "job_count": 3, "run_count": 0}

    async def list_jobs(self, *, include_disabled=True):
        self.calls.append(("list", include_disabled))
        return []

    async def get_job(self, job_id):
        self.calls.append(("get", job_id))
        return {}

    async def delete_job(self, job_id):
        self.calls.append(("remove", job_id))
        return True

    async def toggle_job(self, job_id, enabled):
        self.calls.append(("toggle", job_id, enabled))
        return {}

    async def preview_job(self, job_id, count=5):
        self.calls.append(("preview", job_id, count))
        return []

    async def run_now(self, job_id):
        self.calls.append(("run", job_id))
        return "run-id"


def test_description_is_default_and_not_duplicated(monkeypatch) -> None:
    monkeypatch.delenv("CRON_FLASH_SCHEMA_STYLE", raising=False)
    assert _schema_style() == "description"
    params = _merged_cron_input_params("cn")
    card_description = _merged_cron_description("cn")
    assert params["description"] != card_description
    assert "job 对象字段" in params["description"]
    assert "job 对象字段" not in card_description


def test_schema_enforces_one_action_complete_schedule_and_bounds(monkeypatch) -> None:
    monkeypatch.delenv("CRON_FLASH_SCHEMA_STYLE", raising=False)
    schema = _merged_cron_input_params("cn")
    validate = Draft202012Validator(schema).validate

    validate({"status": {}})
    validate({
        "add": {
            "name": "report",
            "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"},
            "description": "write report",
        },
    })
    validate({
        "add": {
            "name": "reminder",
            "schedule": {"kind": "at", "at": "2026-12-25T10:00:00+08:00"},
            "description": "buy gifts",
        },
    })

    invalid = [
        {},
        {"status": {}, "list": {}},
        {"action": "status"},
        {"add": {"name": "n", "schedule": {}, "description": "d"}},
        {"add": {"name": "n", "schedule": {"kind": "at"}, "description": "d"}},
        {"add": {"name": "n", "schedule": {"kind": "cron", "at": "x"}, "description": "d"}},
        {"update": {"job_id": "j"}},
        {"preview": {"job_id": "j", "count": 51}},
    ]
    for item in invalid:
        with pytest.raises(ValidationError):
            validate(item)


def test_english_schema_has_english_field_descriptions(monkeypatch) -> None:
    monkeypatch.setenv("CRON_FLASH_SCHEMA_STYLE", "properties")
    params = _merged_cron_input_params("en")
    assert params["properties"]["add"]["properties"]["name"]["description"].startswith("Job name")


@pytest.mark.asyncio
async def test_dispatch_rejects_zero_or_multiple_actions() -> None:
    backend = _Backend()
    with pytest.raises(ValueError, match="no cron action"):
        await _cron_dispatch(backend, None, {})
    with pytest.raises(ValueError, match="multiple cron actions"):
        await _cron_dispatch(backend, None, {"status": {}, "wake": {"text": "hello"}})


@pytest.mark.asyncio
async def test_legacy_add_job_shape_is_rejected() -> None:
    backend = _Backend()
    with pytest.raises(ValueError, match="unsupported top-level"):
        await _cron_dispatch(backend, SimpleNamespace(mode="team"), {
            "action": "add",
            "job": {
                "name": "report",
                "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *", "tz": "UTC"},
                "description": "write report",
            },
        })
    assert backend.calls == []


@pytest.mark.asyncio
async def test_all_exposed_actions_dispatch_to_backend() -> None:
    backend = _Backend()
    context = SimpleNamespace(mode="agent")
    inputs = [
        {"status": {}},
        {"list": {"include_disabled": False}},
        {"add": {"name": "n", "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"}, "description": "d"}},
        {"get": {"job_id": "j"}},
        {"update": {"job_id": "j", "enabled": False}},
        {"remove": {"job_id": "j"}},
        {"toggle": {"job_id": "j", "enabled": False}},
        {"preview": {"job_id": "j", "count": 3}},
        {"run": {"job_id": "j"}},
        {"wake": {"text": "hello"}},
    ]
    for item in inputs:
        await _cron_dispatch(backend, context, item)

    assert [call[0] for call in backend.calls] == [
        "status", "list", "add", "get", "update", "remove", "toggle", "preview", "run", "wake"
    ]


@pytest.mark.asyncio
async def test_status_only_exposes_truthful_job_count() -> None:
    result = await _cron_dispatch(_Backend(), None, {"status": {}})
    assert result == {"job_count": 3}


@pytest.mark.asyncio
async def test_tool_card_validation_preserves_nested_schedule_fields() -> None:
    backend = _Backend()
    tool = build_cron_flash_tool(
        backend,
        context=SimpleNamespace(mode="agent"),
        agent_id="flash-agent",
    )

    await tool.invoke({
        "add": {
            "name": "report",
            "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"},
            "description": "write report",
        },
    })
    assert backend.calls[0][0] == "add"


def test_translate_at_and_timeout() -> None:
    context = SimpleNamespace(mode="team")
    translated = _translate_to_native(
        {
            "name": "one-shot",
            "schedule": {"kind": "at", "at": "2026-12-25T10:00:00+08:00"},
            "description": "buy gifts",
            "timeout_seconds": 900,
        },
        is_update=False,
        context=context,
    )
    assert translated == {
        "cron_expr": "0 0 10 25 12 ? 2026",
        "description": "buy gifts",
        "name": "one-shot",
        "timeout_seconds": 900,
        "mode": "team",
    }


def test_translate_flash_context_uses_supported_default_mode() -> None:
    translated = _translate_to_native(
        {
            "name": "flash-created",
            "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"},
            "description": "run task",
        },
        is_update=False,
        context=SimpleNamespace(mode="flash"),
    )
    assert translated["mode"] == "agent"


@pytest.mark.asyncio
async def test_dispatch_rejects_empty_update_and_invalid_types() -> None:
    backend = _Backend()
    with pytest.raises(ValueError, match="at least one field"):
        await _cron_dispatch(backend, None, {"update": {"job_id": "j"}})
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        await _cron_dispatch(backend, None, {"toggle": {"job_id": "j", "enabled": "false"}})
    with pytest.raises(ValueError, match="between 1 and 50"):
        await _cron_dispatch(backend, None, {"preview": {"job_id": "j", "count": 0}})
