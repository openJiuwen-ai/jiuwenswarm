# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for invoke_meta (product mcp/run businessCredential handshake)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client import (
    CloudPluginClient,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.external_tool_registry import (
    ExternalToolSpec,
    load_external_tools,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.invoke_tool import InvokeTool
from jiuwenswarm.agents.harness.common.tools.invoke_meta.plugin_skill_catalog import (
    extract_seedance_query_state,
    extract_seedance_task_id,
    invoke_arguments_description,
    invoke_function_name_description,
    invoke_tool_description,
    is_prod_plugin_runtime,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.workspace_context import (
    set_effective_request_workspace_dir,
)


@pytest.fixture(autouse=True)
def _clear_mcp_run_env(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_MCP_RUN", raising=False)
    monkeypatch.setenv("CLAW_BUSINESS_CREDENTIAL", "test-cred")
    monkeypatch.delenv("CLAW_XIAOYI_UID", raising=False)


@pytest.fixture()
def tools_workspace(tmp_path: Path) -> Path:
    tools_dir = tmp_path / "skill" / "references" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "com.demo.plugin__echo_tool.json").write_text(
        json.dumps(
            {
                "pluginId": "com.demo.plugin",
                "toolName": "echo_tool",
                "description": "echo",
                "protocol": "REST",
                "pluginType": "Cloud",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tools_dir / "com.demo.device__device_tool.json").write_text(
        json.dumps(
            {
                "pluginId": "com.demo.device",
                "toolName": "device_tool",
                "pluginType": "Device",
                "parameters": {"type": "object", "properties": {}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _recording_cloud_client(
    return_value: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], type]:
    captured: dict[str, Any] = {"calls": 0}
    payload = return_value or {"success": True, "content": "ok"}

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            captured["calls"] = int(captured["calls"]) + 1
            captured["spec"] = spec
            captured["arguments"] = dict(arguments)
            return payload

    return captured, _FakeCloudClient


def test_is_prod_plugin_runtime_hosts():
    assert is_prod_plugin_runtime(
        "wss://hag-drcn.op.dbankcloud.com/agent-runtime-service-ws/v1/mcp/run"
    )
    assert not is_prod_plugin_runtime(
        "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run"
    )
    assert not is_prod_plugin_runtime("ws://10.33.87.20:18449/agent-runtime-service-ws/v1/mcp/run")
    assert not is_prod_plugin_runtime("wss://example.test/v1/mcp/run")
    assert not is_prod_plugin_runtime("")


@pytest.mark.asyncio
async def test_invoke_requires_function_name():
    tool = InvokeTool()
    result = await tool.invoke({"arguments": {}})
    assert result["success"] is False
    assert "functionName" in result["error"]


@pytest.mark.asyncio
async def test_invoke_agent_missing_agent_id():
    tool = InvokeTool()
    result = await tool.invoke(
        {
            "functionName": "agent_as_a_tool",
            "arguments": {"query": "hello"},
        }
    )
    assert result["success"] is False
    assert "agentId" in result["error"]


def test_load_external_tools(tools_workspace: Path):
    registry = load_external_tools(tools_workspace)
    assert ("com.demo.plugin", "echo_tool") in registry
    assert registry[("com.demo.plugin", "echo_tool")].plugin_type == "Cloud"
    assert ("com.demo.device", "device_tool") in registry


@pytest.mark.asyncio
async def test_invoke_device_plugin_rejected(tools_workspace: Path, monkeypatch):
    set_effective_request_workspace_dir(str(tools_workspace))
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    tool = InvokeTool()
    result = await tool.invoke(
        {
            "functionName": "device_tool",
            "arguments": {"bundleName": "com.demo.device"},
        }
    )
    assert result.get("success") is False
    assert "Device" in result.get("error", "")


@pytest.mark.asyncio
async def test_invoke_plugin_routes_to_cloud_client(tools_workspace: Path, monkeypatch):
    set_effective_request_workspace_dir(str(tools_workspace))
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")

    mock_invoke = AsyncMock(
        return_value={
            "success": True,
            "content": "ok",
            "pluginId": "com.demo.plugin",
            "toolName": "echo_tool",
        }
    )

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        invoke = mock_invoke

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "echo_tool",
                "arguments": {"bundleName": "com.demo.plugin", "text": "hi"},
            }
        )

    assert result.get("success") is True
    assert result.get("content") == "ok"
    mock_invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_invoke_unknown_bundle_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client(
        {"success": True, "content": '{"items":["https://x"]}'}
    )
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "generate",
                "arguments": {
                    "bundleName": "image-generation",
                    "prompt": "a dog",
                },
            }
        )

    assert result.get("success") is True
    assert captured["calls"] == 1
    spec = captured["spec"]
    assert spec.plugin_id == "image-generation"
    assert spec.tool_name == "generate"


@pytest.mark.asyncio
async def test_invoke_wrong_zone_bundle_still_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "seedreamLite4Skill",
                    "bundleName": "xiaoyi",
                    "prompt": "a dog",
                },
            }
        )

    assert result.get("success") is True
    assert captured["spec"].plugin_id == "xiaoyi"
    assert captured["spec"].tool_name == "seedreamLite4Skill"


@pytest.mark.asyncio
async def test_invoke_missing_prompt_still_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "seedreamLite4Skill",
                "arguments": {
                    "bundleName": "com.atomicservice.5765880207845681341",
                },
            }
        )

    assert result.get("success") is True
    assert captured["calls"] == 1
    assert "prompt" not in captured["arguments"]


@pytest.mark.asyncio
async def test_invoke_requires_top_level_function_name(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "arguments": {
                    "bundleName": "com.atomicservice.5765880207845681341",
                    "functionName": "SeedreamPro4Skill",
                    "prompt": "Trendy logo",
                }
            }
        )

    assert result.get("success") is False
    assert "functionName" in result.get("error", "")
    assert captured["calls"] == 0


@pytest.mark.asyncio
async def test_invoke_passthrough_does_not_normalize_size(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "SeedreamPro4Skill",
                "arguments": {
                    "bundleName": "com.atomicservice.5765880207845681341",
                    "prompt": "a dog",
                    "size": "4K",
                    "max_images": 99,
                },
            }
        )

    assert result.get("success") is True
    assert captured["arguments"]["size"] == "4K"
    assert captured["arguments"]["max_images"] == 99


@pytest.mark.asyncio
async def test_invoke_plugin_skill_exec_tool_unwraps(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured: dict[str, Any] = {}

    mock_invoke = AsyncMock(
        return_value={"success": True, "content": '{"items":["https://x"]}'}
    )

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            captured["spec"] = spec
            captured["arguments"] = arguments
            return await mock_invoke(spec, arguments, **kwargs)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "seedreamLite4Skill",
                    "bundleName": "com.atomicservice.5765880207845681341",
                    "prompt": "a dog",
                },
            }
        )

    assert result.get("success") is True
    spec = captured["spec"]
    assert isinstance(spec, ExternalToolSpec)
    assert spec.plugin_id == "com.atomicservice.5765880207845681341"
    assert spec.tool_name == "seedreamLite4Skill"


@pytest.mark.asyncio
async def test_invoke_flattened_capability_matches_wrapper(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    seen: list[tuple[str, str, dict[str, Any]]] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            seen.append((spec.plugin_id, spec.tool_name, dict(arguments)))
            return {"success": True, "content": "ok"}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        args = {
            "bundleName": "com.atomicservice.5765880207845681341",
            "prompt": "a dog",
            "size": "1K",
        }
        flat = await tool.invoke(
            {"functionName": "seedreamLite4Skill", "arguments": dict(args)}
        )
        wrapped = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {"functionName": "seedreamLite4Skill", **args},
            }
        )

    assert flat.get("success") is True
    assert wrapped.get("success") is True
    assert seen[0][0] == seen[1][0] == "com.atomicservice.5765880207845681341"
    assert seen[0][1] == seen[1][1] == "seedreamLite4Skill"
    assert seen[0][2]["prompt"] == seen[1][2]["prompt"] == "a dog"
    assert seen[0][2]["functionName"] == seen[1][2]["functionName"] == "seedreamLite4Skill"


@pytest.mark.asyncio
async def test_invoke_missing_bundle_name_errors(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "seedreamLite4Skill",
                "arguments": {"prompt": "a dog"},
            }
        )

    assert result.get("success") is False
    assert "bundleName" in result.get("error", "")
    assert captured["calls"] == 0


def test_invoke_tool_description_omits_internal_transport():
    desc = invoke_tool_description()
    assert "当前插件运行区：" in desc
    assert "skill_tool" not in desc
    assert "禁止臆造" not in desc
    assert "禁止臆造" not in invoke_arguments_description()
    assert "禁止臆造" not in invoke_function_name_description()
    assert "CloudWsRelay" not in desc
    assert "/ws/link" not in desc
    assert "PluginSkillExecTool" not in desc


@pytest.mark.asyncio
async def test_invoke_logs_mcp_run_transport(monkeypatch):
    mcp = "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run"
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", mcp)
    logged: list[str] = []

    def _info(msg: str, *args: Any, **kwargs: Any) -> None:
        logged.append(msg % args if args else str(msg))

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            return {"success": True, "content": "ok"}

    with (
        patch(
            "jiuwenswarm.agents.harness.common.tools.invoke_meta.invoke_tool.logger.info",
            side_effect=_info,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
            _FakeCloudClient,
        ),
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "seedreamLite4Skill",
                    "bundleName": "com.atomicservice.5765880207845681341",
                    "prompt": "a dog",
                },
            }
        )

    assert result.get("success") is True
    text = "\n".join(logged)
    assert "plugin via mcp/run" in text
    assert mcp in text
    assert "plugin via relay" not in text


@pytest.mark.asyncio
async def test_invoke_agent_missing_runtime_baseurl(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_BASEURL", raising=False)
    tool = InvokeTool()
    result = await tool.invoke(
        {
            "functionName": "agent_as_a_tool",
            "arguments": {"agentId": "demo", "query": "hello"},
        }
    )
    assert result.get("success") is False
    assert "AGENT_RUNTIME_BASEURL" in result.get("error", "")


@pytest.mark.asyncio
async def test_invoke_agent_routes_to_runtime(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_BASEURL", "https://useraccess.example")

    async def _fake_run(inputs: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert inputs["agentId"] == "demo"
        return {"result": "agent-ok", "success": True}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.invoke_tool.invoke_remote_agent",
        _fake_run,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "agent_as_a_tool",
                "arguments": {"agentId": "demo", "query": "hello"},
            }
        )

    assert result.get("success") is True
    assert result.get("result") == "agent-ok"


def test_build_request_body_includes_extra_info(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_UID", "uid-1")
    monkeypatch.setenv("AGENT_RUNTIME_DEVICE_ID", "dev-1")
    monkeypatch.setenv("CLAW_DEVICE_HOSTNAME", "DESKTOP-PC")
    monkeypatch.setenv("CLAW_DEVICE_SANDBOX_SYSTEM", "windows")
    spec = ExternalToolSpec(
        plugin_id="com.atomicservice.5765880207845681341",
        tool_name="seedreamLite4Skill",
        description="",
        protocol="WS",
        plugin_type="Cloud",
    )
    body = CloudPluginClient._build_request_body(
        spec,
        {
            "bundleName": "com.atomicservice.5765880207845681341",
            "functionName": "seedreamLite4Skill",
            "prompt": "一只柯基",
        },
        context=None,
        session_id="sess-1",
    )
    assert body["bundleName"] == "com.atomicservice.5765880207845681341"
    assert body["functionName"] == "seedreamLite4Skill"
    assert body["skillName"] == ""
    assert body["turnContinue"] is False
    assert body["progressToken"] == ""
    assert body["arguments"]["prompt"] == "一只柯基"
    assert body["arguments"]["bundleName"] == body["bundleName"]
    assert "extraInfo" in body
    assert body["extraInfo"]["session"]["sessionId"] == "sess-1"
    assert body["extraInfo"]["context"]["userInfo"]["uid"] == "uid-1"
    assert body["extraInfo"]["context"]["deviceInfo"]["x-device-id"] == "dev-1"
    assert body["extraInfo"]["context"]["deviceInfo"]["deviceName"] == "DESKTOP-PC"
    assert body["extraInfo"]["context"]["deviceInfo"]["x-device-type"] == "windows"
    assert body["extraInfo"]["session"]["deviceId"] == "dev-1"


def test_is_final_frame_stream_type_final():
    frame = {
        "event": "text",
        "content": json.dumps(
            {
                "items": ["https://example.com/a.jpg"],
                "streamInfo": {"streamType": "final", "textType": "plainText"},
            }
        ),
    }
    assert CloudPluginClient._is_final_frame(frame) is True
    assert CloudPluginClient._is_final_frame({"event": "finish"}) is True
    assert CloudPluginClient._is_final_frame({"event": "text", "content": "{}"}) is False


def test_resolve_plugin_runtime_url_from_mcp_run_env(monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        is_mcp_run_url,
        resolve_plugin_runtime_url,
    )

    mcp = "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run"
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", mcp)
    monkeypatch.setenv("XIAOYI_RELAY_WS_URL", "ws://127.0.0.1:19690")
    resolved = resolve_plugin_runtime_url()
    assert resolved == mcp
    assert is_mcp_run_url(resolved)
    assert "/agent-runtime-service/v1/mcp/run" not in resolved
    assert "/agent-runtime-service-ws/v1/mcp/run" in resolved


def test_resolve_plugin_runtime_url_empty_when_unset(monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        resolve_plugin_runtime_url,
    )

    monkeypatch.setenv("XIAOYI_RELAY_WS_URL", "ws://127.0.0.1:19690")
    assert resolve_plugin_runtime_url() == ""


@pytest.mark.asyncio
async def test_invoke_requires_mcp_run_url_ignores_relay(monkeypatch):
    monkeypatch.setenv("XIAOYI_RELAY_WS_URL", "ws://127.0.0.1:19690")
    tool = InvokeTool()
    result = await tool.invoke(
        {
            "functionName": "PluginSkillExecTool",
            "arguments": {
                "functionName": "seedreamLite4Skill",
                "bundleName": "com.atomicservice.5765880207845681341",
                "prompt": "a dog",
            },
        }
    )
    assert result.get("success") is False
    assert "AGENT_RUNTIME_MCP_RUN" in str(result.get("error") or "")


@pytest.mark.asyncio
async def test_invoke_requires_business_credential_ignores_oa_key(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "wss://host:18449/agent-runtime-service-ws/v1/mcp/run",
    )
    monkeypatch.delenv("CLAW_BUSINESS_CREDENTIAL", raising=False)
    monkeypatch.setenv("OA_API_KEY", "test-key")
    monkeypatch.setenv("OA_REQUEST_FROM", "jiuwenclaw")
    tool = InvokeTool()
    result = await tool.invoke(
        {
            "functionName": "PluginSkillExecTool",
            "arguments": {
                "functionName": "seedreamLite4Skill",
                "bundleName": "com.atomicservice.5765880207845681341",
                "prompt": "a dog",
            },
        }
    )
    assert result.get("success") is False
    assert "密钥包缺少 businessCredential" in str(result.get("error") or "")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_runtime_headers,
    )

    headers = build_runtime_headers(extra={"x-plugin-session-id": "pluginabc"})
    assert "x-api-key" not in headers
    assert headers["x-request-from"] == "xiaoyiWork"
    assert "x-sandbox-id" not in headers
    assert headers["x-plugin-session-id"] == "pluginabc"


def test_uid_prefers_claw_xiaoyi_uid(monkeypatch):
    monkeypatch.setenv("CLAW_XIAOYI_UID", "claw-uid")
    monkeypatch.setenv("AGENT_RUNTIME_UID", "lab-uid")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        resolve_runtime_uid,
    )

    assert resolve_runtime_uid() == "claw-uid"


def test_mcp_run_product_headers_prefer_business_credential(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "wss://host:18449/agent-runtime-service-ws/v1/mcp/run",
    )
    monkeypatch.setenv("CLAW_BUSINESS_CREDENTIAL", "cred-1")
    monkeypatch.setenv("CLAW_XIAOYI_UID", "uid-product")
    monkeypatch.setenv("AGENT_RUNTIME_DEVICE_ID", "dev-product")
    monkeypatch.setenv("OA_API_KEY", "should-not-use")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_runtime_headers,
    )

    headers = build_runtime_headers(extra={"x-plugin-session-id": "pluginabc"})
    assert headers["businessCredential"] == "cred-1"
    assert headers["x-uid"] == "uid-product"
    assert headers["x-device-id"] == "dev-product"
    assert headers["x-plugin-session-id"] == "pluginabc"
    assert "x-hag-trace-id" in headers
    assert "x-api-key" not in headers
    assert headers["x-request-from"] == "xiaoyiWork"
    assert "x-sandbox-id" not in headers
    assert "x-relay-role" not in headers


def test_mcp_run_extra_info_uses_pc_device_fallback(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "wss://host:18449/agent-runtime-service-ws/v1/mcp/run",
    )
    monkeypatch.setenv("AGENT_RUNTIME_UID", "30086000686785686")
    monkeypatch.delenv("AGENT_RUNTIME_DEVICE_ID", raising=False)
    monkeypatch.delenv("X_DEVICE_ID", raising=False)
    monkeypatch.delenv("CLAW_DEVICE_HOSTNAME", raising=False)
    monkeypatch.delenv("CLAW_DEVICE_SANDBOX_SYSTEM", raising=False)
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_plugin_skill_extra_info,
    )

    extra = build_plugin_skill_extra_info(session_id="sess-mcp")
    device = extra["context"]["deviceInfo"]
    assert extra["context"]["userInfo"]["uid"] == "30086000686785686"
    assert extra["session"]["sessionId"] == "sess-mcp"
    assert device["deviceName"] == "sandbox_pc"
    assert device["ohosApiVersion"] == 0
    assert device["x-device-type"] == "pc"
    assert device["sysVersion"] == ""


def test_needs_insecure_ssl_for_test_host_and_ip():
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client import (
        _needs_insecure_ssl,
    )

    assert _needs_insecure_ssl(
        "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run"
    )
    assert _needs_insecure_ssl("wss://10.33.87.20:18449/agent-runtime-service-ws/v1/mcp/run")
    assert not _needs_insecure_ssl("wss://example.com/v1/mcp/run")
    assert not _needs_insecure_ssl("ws://127.0.0.1:19690")


def test_extract_seedance_task_id_from_json_content():
    assert extract_seedance_task_id({"content": '{"task_id":"cgt-1"}'}) == "cgt-1"
    assert extract_seedance_task_id({"content": {"id": "cgt-2"}}) == "cgt-2"
    assert (
        extract_seedance_task_id(
            {"content": '{"items":[{"id":"cgt-20260826000744-5bttn"}]}'}
        )
        == "cgt-20260826000744-5bttn"
    )


def test_extract_seedance_query_state():
    status, url = extract_seedance_query_state(
        {
            "content": json.dumps(
                {"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}
            )
        }
    )
    assert status == "succeeded"
    assert url == "https://cdn.example/a.mp4"


def test_extract_seedance_query_state_from_items():
    status, url = extract_seedance_query_state(
        {
            "content": json.dumps(
                {
                    "items": [
                        {
                            "id": "cgt-20260826000744-5bttn",
                            "status": "succeeded",
                            "video_url": "https://cdn.example/a.mp4",
                        }
                    ]
                }
            )
        }
    )
    assert status == "succeeded"
    assert url == "https://cdn.example/a.mp4"


def _seedance_task_args(**extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "functionName": "seedanceMiniTask",
        "bundleName": "com.atomicservice.5765880207845681341",
        "content": [{"type": "text", "text": "一只在月光下奔跑的狐狸"}],
        "duration": 10,
    }
    args.update(extra)
    return args


@pytest.mark.asyncio
async def test_invoke_seedance_submit_does_not_auto_poll(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    names: list[str] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            names.append(spec.tool_name)
            return {
                "success": True,
                "content": json.dumps({"items": [{"id": "cgt-1"}]}),
            }

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {"functionName": "PluginSkillExecTool", "arguments": _seedance_task_args()}
        )

    assert result.get("success") is True
    assert names == ["seedanceMiniTask"]
    assert "cgt-1" in str(result.get("content", ""))


@pytest.mark.asyncio
async def test_invoke_seedance_wait_is_stripped_from_plugin_args(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    names: list[str] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            names.append(spec.tool_name)
            assert "wait" not in arguments
            return {"success": True, "content": json.dumps({"task_id": "cgt-9"})}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": _seedance_task_args(wait=False),
            }
        )

    assert result.get("success") is True
    assert names == ["seedanceMiniTask"]
    assert "cgt-9" in str(result.get("content", ""))


@pytest.mark.asyncio
async def test_invoke_seedance_string_content_passthrough(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    seen: list[dict[str, Any]] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            seen.append(dict(arguments))
            return {
                "success": True,
                "content": json.dumps({"items": [{"id": "cgt-str"}]}),
            }

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": _seedance_task_args(
                    wait=False,
                    content="一只在月光下奔跑的狐狸",
                ),
            }
        )

    assert result.get("success") is True
    assert seen
    assert seen[0]["content"] == "一只在月光下奔跑的狐狸"


_ATOMIC_BUNDLE = "com.atomicservice.5765880207845681341"


def _music_vocal_args(**extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "functionName": "musicGeneration",
        "bundleName": _ATOMIC_BUNDLE,
        "prompt": "华语流行，轻快温暖",
        "audio_setting": {
            "sample_rate": 44100,
            "bitrate": 256000,
            "format": "mp3",
        },
        "aigc_watermark": True,
        "lyrics_optimizer": False,
        "is_instrumental": False,
        "lyrics": "[Verse]\n清晨的风穿过窗台",
    }
    args.update(extra)
    return args


def _lyrics_write_args(**extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "functionName": "lyricsGeneration",
        "bundleName": _ATOMIC_BUNDLE,
        "prompt": "华语流行，轻快温暖",
        "mode": "write_full_song",
    }
    args.update(extra)
    return args


def test_invoke_tool_description_points_at_skills_not_recipes():
    text = invoke_tool_description()
    assert "当前插件运行区：" in text
    assert "skill_tool" not in text
    assert "禁止臆造" not in text
    assert "lyricsGeneration" not in text
    assert "musicGeneration" not in text
    assert "PluginSkillExecTool" not in text
    assert "完整句子" not in text


@pytest.mark.asyncio
async def test_invoke_lyrics_generation_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    seen: list[dict[str, Any]] = []
    timeouts: list[Any] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            timeouts.append(kwargs.get("timeout"))

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            seen.append(dict(arguments))
            assert spec.tool_name == "lyricsGeneration"
            return {"success": True, "content": '{"lyrics":"[Verse] hi"}'}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {"functionName": "PluginSkillExecTool", "arguments": _lyrics_write_args()}
        )

    assert result.get("success") is True
    assert seen
    assert seen[0]["prompt"] == "华语流行，轻快温暖"
    assert seen[0]["mode"] == "write_full_song"
    assert "content" not in seen[0]
    assert timeouts == [None]


@pytest.mark.asyncio
async def test_invoke_lyrics_missing_prompt_still_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "lyricsGeneration",
                    "bundleName": _ATOMIC_BUNDLE,
                    "content": {"mode": "write_full_song"},
                },
            }
        )

    assert result.get("success") is True
    assert captured["calls"] == 1
    assert captured["spec"].tool_name == "lyricsGeneration"
    assert captured["arguments"]["content"] == {"mode": "write_full_song"}


@pytest.mark.asyncio
async def test_invoke_music_generation_reaches_plugin_with_long_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    seen: list[dict[str, Any]] = []
    timeouts: list[Any] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            timeouts.append(kwargs.get("timeout"))

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            seen.append(dict(arguments))
            assert spec.tool_name == "musicGeneration"
            return {"success": True, "content": '{"items":["https://cdn.example/a.mp3"]}'}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {"functionName": "PluginSkillExecTool", "arguments": _music_vocal_args()}
        )

    assert result.get("success") is True
    assert seen
    assert seen[0]["prompt"] == "华语流行，轻快温暖"
    assert seen[0]["lyrics"].startswith("[Verse]")
    assert "content" not in seen[0]
    assert timeouts == [600.0]


@pytest.mark.asyncio
async def test_invoke_music_instrumental_keeps_top_level_prompt(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    seen: list[dict[str, Any]] = []

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            seen.append(dict(arguments))
            return {"success": True, "content": "ok"}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "musicGeneration",
                    "bundleName": _ATOMIC_BUNDLE,
                    "prompt": "轻快的钢琴背景乐",
                    "is_instrumental": True,
                },
            }
        )

    assert result.get("success") is True
    assert seen
    assert seen[0]["prompt"] == "轻快的钢琴背景乐"
    assert seen[0]["is_instrumental"] is True
    assert "content" not in seen[0]


@pytest.mark.asyncio
async def test_invoke_music_missing_prompt_still_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "musicGeneration",
                    "bundleName": _ATOMIC_BUNDLE,
                    "is_instrumental": True,
                },
            }
        )

    assert result.get("success") is True
    assert captured["calls"] == 1
    assert captured["arguments"]["is_instrumental"] is True


@pytest.mark.asyncio
async def test_invoke_wrong_bundle_for_music_still_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", "wss://example.test/v1/mcp/run")
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "musicGeneration",
                    "bundleName": "xiaoyi",
                    "prompt": "钢琴",
                    "is_instrumental": True,
                },
            }
        )

    assert result.get("success") is True
    assert captured["spec"].plugin_id == "xiaoyi"
    assert captured["spec"].tool_name == "musicGeneration"


def test_design_system_prompt_points_at_skills_not_catalog():
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        build_design_system_prompt,
    )

    prompt = build_design_system_prompt()
    assert "seedance-video-gen" in prompt
    assert "seedream-image-gen" in prompt
    assert "music-generation" in prompt
    assert "skill_tool" in prompt
    assert "ppt-creation" in prompt
    assert "分镜" in prompt
    assert "then call `invoke`" not in prompt
    assert "to call `invoke`" not in prompt
    assert "`image-generation`" not in prompt
    assert "PluginSkillExecTool" not in prompt
    assert "seedanceMiniTask" not in prompt
    assert "seedreamLite4Skill" not in prompt
    assert "SeedreamPro4Skill" not in prompt
    assert "# Doing tasks" not in prompt
    assert "com.atomicservice.5765880207845681341" not in prompt
    assert "com.huawei.pluginPlatform" not in prompt
    assert "`functionName`" not in prompt
    assert "`bundleName`" not in prompt
    assert "query step" not in prompt
    assert "Confirm before generating" not in prompt
    assert "vocal/instrumental" not in prompt
    assert "lyrics markdown" in prompt


_PROD_MCP = "wss://hag-drcn.op.dbankcloud.com/agent-runtime-service-ws/v1/mcp/run"
_PLUGIN_PLATFORM = "com.huawei.pluginPlatform"


@pytest.mark.asyncio
async def test_invoke_prod_seedream_batch5_reaches_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _PROD_MCP)
    captured: dict[str, Any] = {}

    class _FakeCloudClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            captured["spec"] = spec
            captured["arguments"] = arguments
            return {"success": True, "content": '{"items":["https://x"]}'}

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _FakeCloudClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "seedreamBatch5",
                    "bundleName": _PLUGIN_PLATFORM,
                    "prompt": "一只柯基",
                    "size": "2K",
                },
            }
        )

    assert result.get("success") is True
    spec = captured["spec"]
    assert spec.plugin_id == _PLUGIN_PLATFORM
    assert spec.tool_name == "seedreamBatch5"
    assert captured["arguments"]["size"] == "2K"


@pytest.mark.asyncio
async def test_invoke_prod_accepts_atomic_seedream_passthrough(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _PROD_MCP)
    captured, fake = _recording_cloud_client(
        {"success": True, "content": '{"items":["https://x"]}'}
    )
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "seedreamLite4Skill",
                    "bundleName": "com.atomicservice.5765880207845681341",
                    "prompt": "一只柯基",
                },
            }
        )

    assert result.get("success") is True
    assert captured["spec"].plugin_id == "com.atomicservice.5765880207845681341"
    assert captured["spec"].tool_name == "seedreamLite4Skill"


@pytest.mark.asyncio
async def test_invoke_test_zone_accepts_prod_seedream_passthrough(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run",
    )
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": {
                    "functionName": "SeedreamPro_5",
                    "bundleName": _PLUGIN_PLATFORM,
                    "prompt": "一只柯基",
                },
            }
        )

    assert result.get("success") is True
    assert captured["spec"].plugin_id == _PLUGIN_PLATFORM
    assert captured["spec"].tool_name == "SeedreamPro_5"


def test_invoke_tool_description_prod_uses_zone_sentence(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _PROD_MCP)
    text = invoke_tool_description()
    assert "当前插件运行区：现网" in text
    assert "「现网」表" in text
    assert "seedreamBatch5" not in text
    assert "SeedreamPro_5" not in text
    assert _PLUGIN_PLATFORM not in text
    assert "com.example.aikitdemo" not in text
    assert "seedreamLite4Skill" not in text
    assert "com.atomicservice.5765880207845681341" not in text
    assert "WIDTHxHEIGHT" not in text


def test_design_system_prompt_prod_omits_catalog_names(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _PROD_MCP)
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        build_design_system_prompt,
    )

    prompt = build_design_system_prompt()
    assert "seedream-image-gen" in prompt
    assert "seedance-video-gen" in prompt
    assert "music-generation" in prompt
    assert _PLUGIN_PLATFORM not in prompt
    assert "seedreamBatch5" not in prompt
    assert "com.atomicservice.5765880207845681341" not in prompt
    assert "seedreamLite4Skill" not in prompt
    assert "PluginSkillExecTool" not in prompt


def test_skills_goal_override_uses_real_skill_slugs():
    from jiuwenswarm.agents.harness.common.prompt.skills_goal_override import (
        _STATIC_BLOCK,
        _STATIC_SKILL_NAMES,
    )

    assert "seedream-image-gen" in _STATIC_SKILL_NAMES
    assert "seedance-video-gen" in _STATIC_SKILL_NAMES
    assert "music-generation" in _STATIC_SKILL_NAMES
    assert "xiaoyi-image-understanding-win" in _STATIC_SKILL_NAMES
    assert "image-generation" not in _STATIC_SKILL_NAMES
    for text in _STATIC_BLOCK.values():
        assert "seedream-image-gen" in text
        assert "skill_tool" in text
        assert "PluginSkillExecTool" not in text
        assert "com.atomicservice" not in text
        assert "seedreamLite4Skill" not in text
        assert "then call `invoke`" not in text
        assert "再调用 `invoke`" not in text
        assert "Deliver the video file" in text or "交付视频文件" in text

