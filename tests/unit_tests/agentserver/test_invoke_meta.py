# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for invoke_meta (product mcp/run via desktop PluginWsProxy)."""

from __future__ import annotations

import asyncio
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
from jiuwenswarm.agents.harness.common.tools.invoke_meta.invoke_tool import (
    InvokeTool,
    _resolve_invoke_timeout,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.plugin_skill_catalog import (
    extract_seedance_query_state,
    extract_seedance_task_id,
    invoke_arguments_description,
    invoke_function_name_description,
    invoke_timeout_s_description,
    invoke_tool_description,
    is_prod_plugin_runtime,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.workspace_context import (
    set_effective_request_workspace_dir,
)

_DESKTOP_MCP = "ws://127.0.0.1:19694/agent-runtime-service-ws/v1/mcp/run"


@pytest.fixture(autouse=True)
def _clear_mcp_run_env(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_MCP_RUN", raising=False)
    monkeypatch.delenv("CLAW_XIAOYI_UID", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.secrets_bootstrap.get_secret",
        lambda key, default=None: "pws_test_token" if key == "pluginWsToken" else default,
    )


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


def test_is_prod_plugin_runtime_prefers_upstream_env(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "ws://127.0.0.1:19694/agent-runtime-service-ws/v1/mcp/run",
    )
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_UPSTREAM",
        "wss://hag-drcn.op.dbankcloud.com/agent-runtime-service-ws/v1/mcp/run",
    )
    assert is_prod_plugin_runtime()
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_UPSTREAM",
        "wss://lfhagmirror.hwcloudtest.cn:18449/agent-runtime-service-ws/v1/mcp/run",
    )
    assert not is_prod_plugin_runtime()


def test_mask_secret_matches_desktop():
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        mask_secret,
    )

    assert mask_secret("") == "(空)"
    assert mask_secret(None) == "(空)"
    assert mask_secret("abcdefghijklmnop") == "abcdefghijkl…(len=16)"


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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    assert "禁止臆造" not in invoke_timeout_s_description()
    assert "CloudWsRelay" not in desc
    assert "/ws/link" not in desc
    assert "PluginSkillExecTool" not in desc


@pytest.mark.asyncio
async def test_invoke_logs_mcp_run_transport(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    assert _DESKTOP_MCP in text
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
async def test_invoke_rejects_non_desktop_mcp_run(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_RUN",
        "wss://host:18449/agent-runtime-service-ws/v1/mcp/run",
    )
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
    assert "19694" in str(result.get("error") or "")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_runtime_headers,
    )

    headers = build_runtime_headers(
        extra={"x-plugin-session-id": "pluginabc"},
        url=_DESKTOP_MCP,
    )
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


def test_mcp_run_product_headers_use_plugin_ws_token(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    monkeypatch.setenv("CLAW_XIAOYI_UID", "uid-product")
    monkeypatch.setenv("AGENT_RUNTIME_DEVICE_ID", "dev-product")
    monkeypatch.setenv("OA_API_KEY", "should-not-use")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_runtime_headers,
    )

    headers = build_runtime_headers(
        extra={"x-plugin-session-id": "pluginabc"},
        url=_DESKTOP_MCP,
    )
    assert "businessCredential" not in headers
    assert headers["Authorization"] == "Bearer pws_test_token"
    assert headers["x-uid"] == "uid-product"
    assert headers["x-device-id"] == "dev-product"
    assert headers["x-plugin-session-id"] == "pluginabc"
    assert "x-hag-trace-id" in headers
    assert "x-api-key" not in headers
    assert headers["x-request-from"] == "xiaoyiWork"
    assert "x-sandbox-id" not in headers
    assert "x-relay-role" not in headers


def test_desktop_proxy_headers_use_token_not_business_credential(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    monkeypatch.setenv("CLAW_XIAOYI_UID", "uid-1")
    monkeypatch.setenv("AGENT_RUNTIME_DEVICE_ID", "dev-1")
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        build_runtime_headers,
        handshake_cred_source,
    )

    headers = build_runtime_headers(
        extra={"x-plugin-session-id": "pluginabc"},
        url=_DESKTOP_MCP,
    )
    assert "businessCredential" not in headers
    assert headers["Authorization"] == "Bearer pws_test_token"
    assert headers["x-uid"] == "uid-1"
    assert headers["x-device-id"] == "dev-1"
    assert headers["x-plugin-session-id"] == "pluginabc"
    assert headers["x-request-from"] == "xiaoyiWork"
    assert handshake_cred_source(_DESKTOP_MCP) == "desktop-proxy"


@pytest.mark.asyncio
async def test_invoke_desktop_proxy_skips_business_credential(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    captured, fake = _recording_cloud_client({"success": True, "content": "ok"})
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
                    "prompt": "a dog",
                },
            }
        )
    assert result.get("success") is True
    assert captured["calls"] == 1


@pytest.mark.asyncio
async def test_invoke_desktop_proxy_requires_plugin_ws_token(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    with patch("jiuwenswarm.common.secrets_bootstrap.get_secret", return_value=None):
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
    assert "pluginWsToken" in str(result.get("error") or "")


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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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


def test_invoke_tool_card_exposes_timeout_s_and_exempts_ability_manager():
    tool = InvokeTool()
    props = tool.card.input_params["properties"]
    assert "timeout_s" in props
    assert props["timeout_s"]["type"] == "number"
    assert "3600" in props["timeout_s"]["description"]
    assert "60" in props["timeout_s"]["description"]
    assert tool.card.properties["resilience"]["timeout_s"] is None


def test_resolve_invoke_timeout_priority():
    assert _resolve_invoke_timeout("lyricsGeneration", {}, None) == (300.0, True, False)
    assert _resolve_invoke_timeout("musicGeneration", {}, None) == (600.0, False, False)
    assert _resolve_invoke_timeout(
        "seedreamLite4Skill", {"max_images": 10}, None
    ) == (600.0, False, False)
    assert _resolve_invoke_timeout(
        "seedreamLite4Skill", {"max_images": 1}, None
    ) == (300.0, True, False)
    assert _resolve_invoke_timeout(
        "seedreamLite4Skill", {}, None
    ) == (300.0, True, False)
    assert _resolve_invoke_timeout("musicGeneration", {}, 900) == (900.0, False, True)
    assert _resolve_invoke_timeout(
        "seedreamLite4Skill", {"max_images": 10}, 900
    ) == (900.0, False, True)
    assert _resolve_invoke_timeout("lyricsGeneration", {}, 99999) == (3600.0, False, True)
    assert _resolve_invoke_timeout("lyricsGeneration", {}, 300) == (300.0, False, True)


@pytest.mark.asyncio
async def test_invoke_timeout_s_top_level_not_sent_to_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
                    "bundleName": _ATOMIC_BUNDLE,
                    "prompt": "a dog",
                    "max_images": 10,
                },
                "timeout_s": 900,
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] == 900.0
    assert "timeout_s" not in captured["arguments"]
    assert captured["arguments"]["max_images"] == 10
    assert captured["arguments"]["prompt"] == "a dog"


@pytest.mark.asyncio
async def test_invoke_timeout_s_in_arguments_stripped_from_plugin(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": _music_vocal_args(timeout_s=900),
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] == 900.0
    assert "timeout_s" not in captured["arguments"]
    assert captured["arguments"]["prompt"] == "华语流行，轻快温暖"


@pytest.mark.asyncio
async def test_invoke_timeout_s_clamped_to_hard_cap(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "lyricsGeneration",
                "arguments": _lyrics_write_args(),
                "timeout_s": 99999,
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] == 3600.0
    assert "timeout_s" not in captured["arguments"]


@pytest.mark.asyncio
async def test_invoke_invalid_timeout_s_falls_back_to_default_rules(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "PluginSkillExecTool",
                "arguments": _lyrics_write_args(timeout_s="not-a-number"),
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] is None
    assert "timeout_s" not in captured["arguments"]


@pytest.mark.asyncio
async def test_invoke_seedream_max_images_10_uses_batch_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
                    "bundleName": _ATOMIC_BUNDLE,
                    "prompt": "家庭相册",
                    "max_images": 10,
                },
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] == 600.0
    assert captured["arguments"]["max_images"] == 10
    assert "timeout_s" not in captured["arguments"]


@pytest.mark.asyncio
async def test_invoke_seedream_single_image_keeps_client_timeout_none(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
                    "bundleName": _ATOMIC_BUNDLE,
                    "prompt": "一只柯基",
                    "max_images": 1,
                },
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] is None
    assert captured["arguments"]["max_images"] == 1


@pytest.mark.asyncio
async def test_invoke_explicit_300_still_passed_to_client(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    captured, fake = _recording_cloud_client()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        fake,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "lyricsGeneration",
                "arguments": _lyrics_write_args(),
                "timeout_s": 300,
            }
        )

    assert result.get("success") is True
    assert captured["timeout"] == 300.0


@pytest.mark.asyncio
async def test_invoke_timeout_error_returns_resolved_seconds(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

    class _TimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def invoke(self, spec: ExternalToolSpec, arguments: dict, **kwargs: Any):
            raise TimeoutError()

    with patch(
        "jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client.CloudPluginClient",
        _TimeoutClient,
    ):
        tool = InvokeTool()
        result = await tool.invoke(
            {
                "functionName": "seedreamLite4Skill",
                "arguments": {
                    "bundleName": _ATOMIC_BUNDLE,
                    "prompt": "家庭相册",
                    "max_images": 10,
                },
            }
        )

    assert result.get("success") is False
    assert result.get("error") == "seedreamLite4Skill timed out after 600s"
    assert "3600" not in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_invoke_music_instrumental_keeps_top_level_prompt(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    monkeypatch.setenv("AGENT_RUNTIME_MCP_UPSTREAM", _PROD_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    monkeypatch.setenv("AGENT_RUNTIME_MCP_UPSTREAM", _PROD_MCP)
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
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_UPSTREAM",
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
    assert _PLUGIN_PLATFORM not in prompt
    assert "seedreamBatch5" not in prompt
    assert "com.atomicservice.5765880207845681341" not in prompt
    assert "seedreamLite4Skill" not in prompt
    assert "PluginSkillExecTool" not in prompt


def _plugin_spec() -> ExternalToolSpec:
    return ExternalToolSpec(
        plugin_id="com.demo.plugin",
        tool_name="echo_tool",
        description="",
        protocol="WS",
        plugin_type="Cloud",
    )


@pytest.mark.asyncio
async def test_handshake_reject_logs_masked_status_no_false_succeed(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

    class InvalidStatusCode(Exception):
        def __init__(self) -> None:
            super().__init__("server rejected WebSocket handshake")
            self.status_code = 401
            self.headers = {
                "businessCredential": "super-secret-credential-value",
                "x-hag-trace-id": "trace-abc",
            }

    class _FailingConnect:
        async def __aenter__(self):
            raise InvalidStatusCode()

        async def __aexit__(self, *args: Any):
            return False

    infos: list[str] = []

    def _capture(msg: str, *args: Any, **_kwargs: Any) -> None:
        infos.append(msg % args if args else msg)

    from jiuwenswarm.agents.harness.common.tools.invoke_meta import cloud_plugin_client as cpc

    client = CloudPluginClient(base_url=_DESKTOP_MCP)
    client._connect = lambda url, **kwargs: _FailingConnect()  # type: ignore[method-assign]
    with patch.object(cpc.logger, "info", side_effect=_capture):
        result = await client.invoke(_plugin_spec(), {"text": "hi"})
    text = "\n".join(infos)
    assert "phase=handshake_reject" in text
    assert "status=401" in text
    assert "super-secret-credential-value" not in text
    assert "len=29" in text
    assert "trace-abc" in text
    assert "WS connect succeed" not in text
    assert "phase=handshake_ok" not in text
    assert result.get("success") is False


@pytest.mark.asyncio
async def test_handshake_ok_logged_only_after_connect(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

    class _FakeWs:
        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> str:
            return json.dumps({"event": "finish", "type": "normal", "content": "ok"})

    class _OkConnect:
        async def __aenter__(self):
            return _FakeWs()

        async def __aexit__(self, *args: Any):
            return False

    infos: list[str] = []

    def _capture(msg: str, *args: Any, **_kwargs: Any) -> None:
        infos.append(msg % args if args else msg)

    from jiuwenswarm.agents.harness.common.tools.invoke_meta import cloud_plugin_client as cpc
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        mask_secret,
    )

    client = CloudPluginClient(base_url=_DESKTOP_MCP)
    client._connect = lambda url, **kwargs: _OkConnect()  # type: ignore[method-assign]
    with patch.object(cpc.logger, "info", side_effect=_capture):
        result = await client.invoke(_plugin_spec(), {"text": "hi"})
    text = "\n".join(infos)
    assert f"cred={mask_secret('')}" in text
    assert "credSrc=desktop-proxy" in text
    assert "phase=handshake_ok" in text
    assert "WS connect succeed" not in text
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_desktop_proxy_handshake_summary_empty_cred(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

    class _FakeWs:
        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> str:
            return json.dumps({"event": "finish", "type": "normal", "content": "ok"})

    class _OkConnect:
        async def __aenter__(self):
            return _FakeWs()

        async def __aexit__(self, *args: Any):
            return False

    infos: list[str] = []

    def _capture(msg: str, *args: Any, **_kwargs: Any) -> None:
        infos.append(msg % args if args else msg)

    from jiuwenswarm.agents.harness.common.tools.invoke_meta import cloud_plugin_client as cpc
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
        mask_secret,
    )

    with patch(
        "jiuwenswarm.common.secrets_bootstrap.get_secret",
        side_effect=lambda key, default=None: "pws_test_token" if key == "pluginWsToken" else default,
    ):
        client = CloudPluginClient(base_url=_DESKTOP_MCP)
        client._connect = lambda url, **kwargs: _OkConnect()  # type: ignore[method-assign]
        with patch.object(cpc.logger, "info", side_effect=_capture):
            result = await client.invoke(_plugin_spec(), {"text": "hi"})
    text = "\n".join(infos)
    assert f"cred={mask_secret('')}" in text
    assert "credSrc=desktop-proxy" in text
    assert "phase=handshake_ok" in text
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_receive_frames_reraises_cancelled_error(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MCP_RUN", _DESKTOP_MCP)

    class _CancelWs:
        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> str:
            raise asyncio.CancelledError()

    class _CancelConnect:
        async def __aenter__(self):
            return _CancelWs()

        async def __aexit__(self, *args: Any):
            return False

    client = CloudPluginClient(base_url=_DESKTOP_MCP)
    with pytest.raises(asyncio.CancelledError):
        await client._receive_frames(_CancelConnect(), "{}", _plugin_spec())

