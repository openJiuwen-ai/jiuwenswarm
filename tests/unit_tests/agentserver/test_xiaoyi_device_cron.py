from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _extract_legacy_params,
)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    check_plugin_privilege_tool as privilege_module,
)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    utils as device_utils,
)
from jiuwenswarm.common.device_rpc.models import (
    DeviceCommandResponse,
)
from jiuwenswarm.common.invocation_context.models import (
    INVOCATION_CONTEXT_VERSION,
    InvocationContext,
)
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.server.xiaoyi_invocation import (
    XIAOYI_INVOCATION_EXTENSION_KEY,
    XiaoyiInvocationExtension,
)


def _scheduled_invocation(
    *,
    required_intents: list[str],
    include_cron: bool = False,
) -> InvocationContext:
    scheduled_device = {
        "push_id": "push-1",
        "required_intents": required_intents,
    }
    return InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id="invocation-cron-1",
        request_id="cron-run-1",
        session_id="cron-session",
        channel_id="__cron__",
        chat_id=None,
        metadata={
            XIAOYI_INVOCATION_EXTENSION_KEY: asdict(
                XiaoyiInvocationExtension(
                    scheduled_device=scheduled_device,
                    cron={"job_id": "job-1", "run_id": "run-1"}
                    if include_cron
                    else None,
                )
            )
        },
    )


def test_permission_map_exactly_matches_openclaw() -> None:
    assert privilege_module.INTENT_PERMISSION_MAP == {
        "GetCurrentLocation": [
            "ohos.permission.LOCATION",
            "ohos.permission.APPROXIMATELY_LOCATION",
        ],
        "SearchCalendarEvent": ["ohos.permission.READ_WHOLE_CALENDAR"],
        "CreateCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
        "DeleteCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
        "ModifyCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
        "SearchNote": ["ohos.permission.READ_NOTE"],
        "CreateNote": ["ohos.permission.WRITE_NOTE"],
        "ModifyNote": ["ohos.permission.WRITE_NOTE"],
        "SearchContactLocal": ["ohos.permission.READ_CONTACTS"],
        "SearchPhotoVideo": ["ohos.permission.READ_IMAGEVIDEO"],
        "SaveMediaToGallery": ["ohos.permission.WRITE_IMAGEVIDEO"],
        "SearchFile": ["ohos.permission.FILE_ACCESS_MANAGER"],
        "SaveFileToFileManager": ["ohos.permission.FILE_SAVE_MANAGER"],
        "SearchAlarm": ["ohos.permission.READ_ALARM"],
        "CreateAlarm": ["ohos.permission.WRITE_ALARM"],
        "ModifyAlarm": ["ohos.permission.WRITE_ALARM"],
        "DeleteAlarm": ["ohos.permission.WRITE_ALARM"],
        "SearchMessage": ["ohos.permission.READ_SMS"],
        "SendShortMessage": ["ohos.permission.SEND_MESSAGES"],
        "StartCall": ["ohos.permission.PLACE_CALL"],
    }


def test_check_plugin_privilege_command_matches_openclaw() -> None:
    command = privilege_module.build_check_plugin_privilege_command(
        "CreateNote",
        privilege_module.INTENT_PERMISSION_MAP["CreateNote"],
    )

    execute_param = command["payload"]["executeParam"]
    assert command["header"] == {"namespace": "Common", "name": "Action"}
    assert execute_param == {
        "achieveType": "INTENT",
        "actionResponse": True,
        "bundleName": "com.huawei.hmos.vassistant",
        "dimension": "",
        "executeMode": "background",
        "intentName": "CheckPlugInPrivilege",
        "intentParam": {
            "checkIntentName": "CreateNote",
            "permissionId": ["ohos.permission.WRITE_NOTE"],
        },
        "needUnlock": False,
        "permissionId": [],
        "timeOut": 5,
    }
    assert command["payload"]["needUploadResult"] is True


def test_plugin_privilege_result_rejects_explicit_denial() -> None:
    with pytest.raises(RuntimeError, match="denied"):
        privilege_module.ensure_plugin_privilege_granted(
            "CreateNote",
            {"authorized": False},
        )


def test_plugin_privilege_result_rejects_device_error_code() -> None:
    with pytest.raises(RuntimeError, match="code: 1001"):
        privilege_module.ensure_plugin_privilege_granted(
            "CreateNote",
            {"code": 1001, "errorMsg": "permission denied"},
        )


def test_plugin_privilege_result_accepts_success_without_explicit_flag() -> None:
    privilege_module.ensure_plugin_privilege_granted(
        "CreateNote",
        {"code": 0},
    )


@pytest.mark.asyncio
async def test_check_plugin_privilege_returns_raw_outputs(monkeypatch) -> None:
    calls = []

    async def fake_execute(intent_name, command, timeout):
        calls.append((intent_name, command, timeout))
        return {"authorized": False, "reason": "denied"}

    monkeypatch.setattr(privilege_module, "execute_device_command", fake_execute)

    result = await privilege_module.check_plugin_privilege.invoke(
        {"checkIntentName": "CreateNote"}
    )

    assert calls[0][0] == "CheckPlugInPrivilege"
    assert calls[0][2] == 60.0
    assert '"authorized": false' in result["content"][0]["text"]


def test_cron_runtime_attaches_current_xiaoyi_push_id() -> None:
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="session-1",
        mode="agent.fast",
        metadata={"xiaoyi_push_id": "push-1"},
    )
    payload = {
        "schedule": {"kind": "cron", "expr": "0 30 18 * * * 2026"},
        "payload": {
            "kind": "agentTurn",
            "message": "write test to notes",
        },
        "delivery": {"channel": "xiaoyi"},
        "required_device_intents": ["CreateNote"],
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["required_device_intents"] == ["CreateNote"]
    assert out["xiaoyi_push_id"] == "push-1"


def test_cron_runtime_does_not_attach_push_id_without_declared_intents() -> None:
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="session-1",
        mode="agent.fast",
        metadata={"xiaoyi_push_id": "push-1"},
    )

    out = _extract_legacy_params(
        {
            "name": "note",
            "cron_expr": "0 30 18 * * * 2026",
            "timezone": "Asia/Shanghai",
            "description": "write test to notes",
            "targets": "xiaoyi",
        },
        context=context,
        require_schedule=True,
    )

    assert "xiaoyi_push_id" not in out


def test_device_cron_model_round_trip() -> None:
    job = CronJob(
        id="job-1",
        name="note",
        enabled=True,
        cron_expr="0 30 18 * * * 2026",
        timezone="Asia/Shanghai",
        description="write test",
        targets="xiaoyi",
        required_device_intents=["CreateNote"],
        xiaoyi_push_id="push-1",
        wake_offset_seconds=300,
    )

    restored = CronJob.from_dict(job.to_dict())

    assert restored.required_device_intents == ["CreateNote"]
    assert restored.xiaoyi_push_id == "push-1"
    assert restored.wake_offset_seconds == 300


@pytest.mark.asyncio
async def test_execute_device_command_accepts_scheduled_context(monkeypatch) -> None:
    invocation = _scheduled_invocation(required_intents=["CreateNote"])

    class FakeManager:
        async def call(self, **kwargs):
            assert kwargs["context"].source_request_id == "cron-run-1"
            assert kwargs["context"].channel_id == "__cron__"
            assert kwargs["intent_name"] == "CreateNote"
            return DeviceCommandResponse(
                rpc_id="rpc-1",
                operation_id="op-1",
                ok=True,
                result={"code": 0},
            )

    monkeypatch.setattr(
        device_utils,
        "get_current_invocation_context",
        lambda: invocation,
    )
    monkeypatch.setattr(
        device_utils,
        "get_xiaoyi_device_reverse_rpc_client",
        lambda: FakeManager(),
    )

    result = await device_utils.execute_device_command(
        "CreateNote",
        {"command": "value"},
    )

    assert result == {"code": 0}


@pytest.mark.asyncio
async def test_execute_device_command_rejects_undeclared_intent(monkeypatch) -> None:
    monkeypatch.setattr(
        device_utils,
        "get_current_invocation_context",
        lambda: _scheduled_invocation(required_intents=["CreateNote"]),
    )

    with pytest.raises(RuntimeError, match="not allowed"):
        await device_utils.execute_device_command(
            "CreateAlarm",
            {"command": "value"},
        )


@pytest.mark.asyncio
async def test_execute_device_command_rejects_empty_scheduled_intents(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        device_utils,
        "get_current_invocation_context",
        lambda: _scheduled_invocation(required_intents=[], include_cron=True),
    )

    with pytest.raises(RuntimeError, match="recreate the cron job"):
        await device_utils.execute_device_command("CreateNote", {})
