# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.mcp_config import (
    _is_blocked_host,
    _loopback_mcp_allowed,
    _normalize_mcp_client_type,
    _optional_auth_dict,
    _pick_mcp_url,
    _validate_request_scoped_remote_mcp,
    build_mcp_server_config,
    create_mcp_tool,
)

class TestNormalizeMcpClientType:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, "stdio"),
            ("", "stdio"),
            ("stdio", "stdio"),
            ("STDIO", "stdio"),
            ("sse", "sse"),
            ("SSE", "sse"),
            ("streamableHttp", "streamable-http"),
            ("streamable_http", "streamable-http"),
            ("streamable-http", "streamable-http"),
            ("StreamableHTTP", "streamable-http"),
            # TC_MCP_CALL_005：模板 transport=http 是 Streamable HTTP 别名
            ("http", "streamable-http"),
            ("HTTP", "streamable-http"),
            ("playwright", "playwright"),
            ("openapi", "openapi"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_mcp_client_type(raw) == expected


class TestPickMcpUrl:
    def test_valid_url(self):
        assert _pick_mcp_url({"url": "  http://example.com  "}) == "http://example.com"

    def test_missing_url(self):
        assert _pick_mcp_url({}) == ""

    def test_non_string_url(self):
        assert _pick_mcp_url({"url": 123}) == ""

    def test_empty_url(self):
        assert _pick_mcp_url({"url": "   "}) == ""


class TestOptionalAuthDict:
    def test_missing_key(self):
        assert _optional_auth_dict({}, "auth_headers") == {}

    def test_valid_dict(self):
        result = _optional_auth_dict({"auth_headers": {"Authorization": "Bearer x"}}, "auth_headers")
        assert result == {"Authorization": "Bearer x"}

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="必须是 JSON 对象"):
            _optional_auth_dict({"auth_headers": "not-a-dict"}, "auth_headers")


class TestIsBlockedHost:
    def test_localhost_blocked(self):
        assert _is_blocked_host("localhost")

    def test_loopback_ipv4_blocked(self):
        for host in ("127.0.0.1", "127.1.2.3"):
            assert _is_blocked_host(host)

    def test_metadata_endpoint_blocked(self):
        assert _is_blocked_host("metadata.google.internal")
        assert _is_blocked_host("metadata.azure.com")

    def test_private_network_blocked(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            assert _is_blocked_host(host)

    def test_link_local_blocked(self):
        assert _is_blocked_host("169.254.169.254")

    def test_external_host_allowed(self):
        assert not _is_blocked_host("mcp.example.com")
        assert not _is_blocked_host("8.8.8.8")

    def test_empty_host_allowed(self):
        assert not _is_blocked_host("")

    def test_loopback_allowed_when_env_set(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        assert not _is_blocked_host("127.0.0.1")
        assert not _is_blocked_host("localhost")

    def test_metadata_still_blocked_when_loopback_allowed(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        assert _is_blocked_host("metadata.google.internal")
        assert _is_blocked_host("169.254.169.254")

    def test_private_still_blocked_when_loopback_allowed(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        assert _is_blocked_host("10.0.0.1")
        assert _is_blocked_host("192.168.1.1")


class TestLoopbackMcpAllowed:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", raising=False)
        assert not _loopback_mcp_allowed()

    def test_env_true(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        assert _loopback_mcp_allowed()

    def test_env_yes(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "yes")
        assert _loopback_mcp_allowed()


class TestValidateRequestScopedRemoteMcp:
    def test_external_url_allowed(self):
        _validate_request_scoped_remote_mcp("ok", {"url": "https://mcp.example.com/sse"})

    def test_loopback_ipv4_blocked(self):
        for host in ("127.0.0.1", "127.1.2.3"):
            with pytest.raises(ValueError, match="SSRF"):
                _validate_request_scoped_remote_mcp("t", {"url": f"http://{host}/sse"})

    def test_metadata_endpoint_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://169.254.169.254/latest/meta-data/"})

    def test_private_network_blocked(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            with pytest.raises(ValueError, match="SSRF"):
                _validate_request_scoped_remote_mcp("t", {"url": f"http://{host}/mcp"})

    def test_localhost_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://localhost:8080/sse"})

    def test_unspecified_address_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://0.0.0.0/mcp"})

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://[::1]/mcp"})

    def test_no_url_allowed(self):
        _validate_request_scoped_remote_mcp("t", {"type": "sse"})

    def test_non_dict_cfg_noop(self):
        _validate_request_scoped_remote_mcp("t", "not-a-dict")

    def test_auth_headers_value_blocked(self):
        with pytest.raises(ValueError, match="凭证外泄"):
            _validate_request_scoped_remote_mcp("t", {
                "url": "https://mcp.example.com/sse",
                "auth_headers": {"X-Redirect": "http://169.254.169.254/"},
            })

    def test_auth_query_params_value_blocked(self):
        with pytest.raises(ValueError, match="凭证外泄"):
            _validate_request_scoped_remote_mcp("t", {
                "url": "https://mcp.example.com/mcp",
                "auth_query_params": {"target": "127.0.0.1"},
            })

    def test_auth_headers_external_value_allowed(self):
        _validate_request_scoped_remote_mcp("t", {
            "url": "https://mcp.example.com/sse",
            "auth_headers": {"Authorization": "Bearer valid-token"},
            "auth_query_params": {"token": "abc123"},
        })

    def test_loopback_allowed_when_env_set(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        _validate_request_scoped_remote_mcp("t", {"url": "http://127.0.0.1:9999/sse"})
        _validate_request_scoped_remote_mcp("t", {"url": "http://localhost:8080/mcp"})
        _validate_request_scoped_remote_mcp("t", {"url": "http://[::1]/mcp"})

    def test_metadata_still_blocked_when_loopback_allowed(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://169.254.169.254/meta-data/"})
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://metadata.google.internal/"})

    def test_private_still_blocked_when_loopback_allowed(self, monkeypatch):
        monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://10.0.0.1/sse"})
        with pytest.raises(ValueError, match="SSRF"):
            _validate_request_scoped_remote_mcp("t", {"url": "http://192.168.1.1/mcp"})


class TestCreateMcpToolStdio:
    """个人版用户连接器 create_mcp_tool 接受白名单 stdio（方案 §5.2）。"""

    def test_basic_stdio(self):
        cfg = json.dumps({"name": "my-tool", "command": "node", "args": ["server.js"]})
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "node"
        assert result.params["args"] == ["server.js"]

    def test_explicit_stdio_type(self):
        cfg = json.dumps({
            "name": "my-tool",
            "type": "stdio",
            "command": "node",
            "args": ["server.js"],
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"

    def test_stdio_with_python(self):
        cfg = json.dumps({"name": "py-tool", "command": "python", "args": ["-m", "mymod"]})
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "python"

    def test_npx_config(self):
        cfg = json.dumps({
            "name": "npx-tool",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "npx"

    def test_uvx_config(self):
        cfg = json.dumps({"name": "uvx-tool", "command": "uvx", "args": ["mcp-server-fetch"]})
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="缺少 'name'"):
            create_mcp_tool(json.dumps({"command": "node", "args": ["s.js"]}))

    def test_array_stdio_config(self):
        cfg = json.dumps([{"name": "arr-tool", "command": "node", "args": ["s.js"]}])
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"

    def test_empty_array_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            create_mcp_tool("[]")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="无效的 JSON"):
            create_mcp_tool("not-json")

    def test_dangerous_eval_arg_blocked(self):
        cfg = json.dumps({"name": "bad", "command": "node", "args": ["-e", "1"]})
        with pytest.raises(ValueError, match="危险"):
            create_mcp_tool(cfg)

    def test_enterprise_rejects_stdio(self, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.mcp_config.is_enterprise", lambda: True
        )
        cfg = json.dumps({
            "name": "local",
            "type": "stdio",
            "command": "node",
            "args": ["s.js"],
        })
        with pytest.raises(ValueError, match="不支持本地 stdio"):
            create_mcp_tool(cfg)

    def test_enterprise_unknown_type_not_misreported_as_stdio(self, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.mcp_config.is_enterprise", lambda: True
        )
        cfg = json.dumps({
            "name": "weird",
            "type": "websocket",
            "command": "node",
            "args": ["s.js"],
        })
        with pytest.raises(ValueError, match="不支持 type=") as exc_info:
            create_mcp_tool(cfg)
        assert "本地 stdio" not in str(exc_info.value)

    def test_unknown_type_rejected_in_personal(self):
        cfg = json.dumps({
            "name": "weird",
            "type": "websocket",
            "command": "node",
            "args": ["s.js"],
        })
        with pytest.raises(ValueError, match="不支持 type="):
            create_mcp_tool(cfg)

    def test_enterprise_still_allows_remote(self, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.mcp_config.is_enterprise", lambda: True
        )
        cfg = json.dumps({
            "name": "remote",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "sse"


class TestCreateMcpToolSse:
    def test_basic_sse(self):
        cfg = json.dumps({
            "name": "sse-tool",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "sse"
        assert result.server_path == "http://127.0.0.1:3001/sse"
        assert result.server_name == "sse-tool"

    def test_sse_with_auth(self):
        cfg = json.dumps({
            "name": "sse-auth",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
            "auth_headers": {"Authorization": "Bearer xxx"},
            "auth_query_params": {"token": "yyy"},
        })
        result = create_mcp_tool(cfg)
        assert result.auth_headers == {"Authorization": "Bearer xxx"}
        assert result.auth_query_params == {"token": "yyy"}

    def test_sse_headers_alias_maps_to_auth_headers(self):
        cfg = json.dumps({
            "name": "sse-headers-alias",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
            "headers": {"Authorization": "Bearer from-headers"},
        })
        result = create_mcp_tool(cfg)
        assert result.auth_headers == {"Authorization": "Bearer from-headers"}

    def test_sse_missing_url_raises(self):
        with pytest.raises(ValueError, match="需要 url"):
            create_mcp_tool(json.dumps({"name": "sse-no-url", "type": "sse"}))

    def test_sse_with_server_id(self):
        cfg = json.dumps({
            "name": "sse-tool",
            "server_id": "custom-sse-id",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "custom-sse-id"

    def test_sse_default_server_id(self):
        cfg = json.dumps({
            "name": "sse-tool",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "sse-tool"


class TestCreateMcpToolStreamableHttp:
    def test_basic_streamable_http(self):
        cfg = json.dumps({
            "name": "sh-tool",
            "type": "streamableHttp",
            "url": "http://127.0.0.1:3002/mcp",
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "streamable-http"
        assert result.server_path == "http://127.0.0.1:3002/mcp"

    def test_streamable_http_with_auth(self):
        cfg = json.dumps({
            "name": "sh-auth",
            "type": "streamable_http",
            "url": "http://127.0.0.1:3002/mcp",
            "auth_headers": {"Authorization": "Bearer z"},
            "auth_query_params": {"token": "w"},
        })
        result = create_mcp_tool(cfg)
        assert result.auth_headers == {"Authorization": "Bearer z"}
        assert result.auth_query_params == {"token": "w"}

    def test_streamable_http_missing_url_raises(self):
        with pytest.raises(ValueError, match="需要 url"):
            create_mcp_tool(json.dumps({"name": "sh-no-url", "type": "streamableHttp"}))

    def test_streamable_http_with_server_id(self):
        cfg = json.dumps({
            "name": "sh-tool",
            "server_id": "custom-sh-id",
            "type": "streamableHttp",
            "url": "http://127.0.0.1:3002/mcp",
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "custom-sh-id"


class TestCreateMcpToolPlaywright:
    def test_basic_playwright(self):
        cfg = json.dumps({
            "name": "pw-tool",
            "type": "playwright",
            "url": "http://127.0.0.1:3003/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "playwright"
        assert result.server_path == "http://127.0.0.1:3003/sse"

    def test_playwright_missing_url_raises(self):
        with pytest.raises(ValueError, match="需要 url"):
            create_mcp_tool(json.dumps({"name": "pw-no-url", "type": "playwright"}))

    def test_playwright_with_server_id(self):
        cfg = json.dumps({
            "name": "pw-tool",
            "server_id": "custom-pw-id",
            "type": "playwright",
            "url": "http://127.0.0.1:3003/sse",
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "custom-pw-id"


class TestCreateMcpToolOpenapi:
    def test_basic_openapi(self):
        cfg = json.dumps({
            "name": "oa-tool",
            "type": "openapi",
            "url": "http://127.0.0.1:3004/api",
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "openapi"
        assert result.server_path == "http://127.0.0.1:3004/api"

    def test_openapi_missing_url_raises(self):
        with pytest.raises(ValueError, match="需要 url"):
            create_mcp_tool(json.dumps({"name": "oa-no-url", "type": "openapi"}))

    def test_openapi_with_server_id(self):
        cfg = json.dumps({
            "name": "oa-tool",
            "server_id": "custom-oa-id",
            "type": "openapi",
            "url": "http://127.0.0.1:3004/api",
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "custom-oa-id"


class TestCreateMcpToolTimeoutPassthrough:
    """前端下发的 timeout_s 必须透传进 params（供 _run_mcp_worker 按连接器超时调用）。"""

    def test_sse_timeout_s_passthrough(self):
        cfg = json.dumps({
            "name": "slow-sse",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
            "timeout_s": 300.5,
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "sse"
        assert result.params["timeout_s"] == 300.5

    @pytest.mark.parametrize("bad", [0, -5, "120", True, None])
    def test_invalid_timeout_s_dropped(self, bad):
        cfg = json.dumps({
            "name": "bad-timeout",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
            "timeout_s": bad,
        })
        result = create_mcp_tool(cfg)
        assert "timeout_s" not in result.params

    def test_no_timeout_s_no_param(self):
        cfg = json.dumps({
            "name": "no-timeout",
            "type": "sse",
            "url": "http://127.0.0.1:3001/sse",
        })
        result = create_mcp_tool(cfg)
        assert "timeout_s" not in result.params


class TestCreateMcpToolDefaultType:
    def test_no_type_defaults_to_stdio(self):
        cfg = json.dumps({
            "name": "default-tool",
            "command": "node",
            "args": ["s.js"],
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"


class TestStdioServerParameters:
    def test_cwd_and_env_optional(self):
        from jiuwenswarm.common.mcp_config import _stdio_server_parameters

        params = _stdio_server_parameters({"command": "python", "args": ["echo_mcp.py"]})
        assert params.command == "python"
        assert params.args == ["echo_mcp.py"]
        assert params.cwd is None
        assert params.env is None


class TestBuildMcpServerConfigStdio:
    def test_stdio_transport_builds(self):
        cfg = build_mcp_server_config(
            {
                "name": "local",
                "transport": "stdio",
                "command": "node",
                "args": ["s.js"],
            }
        )
        assert cfg is not None
        assert cfg.client_type == "stdio"
        assert cfg.params["command"] == "node"

    def test_enterprise_rejects_stdio_transport(self, monkeypatch):
        warned: list[tuple] = []

        def _capture_warning(msg, *args, **kwargs):
            warned.append((msg, args))

        monkeypatch.setattr(
            "jiuwenswarm.common.mcp_config.is_enterprise", lambda: True
        )
        monkeypatch.setattr(
            "jiuwenswarm.common.mcp_config.logger.warning", _capture_warning
        )
        cfg = build_mcp_server_config(
            {
                "name": "local",
                "transport": "stdio",
                "command": "node",
                "args": ["s.js"],
            }
        )
        assert cfg is None
        assert len(warned) == 1
        msg, args = warned[0]
        assert "rejects local stdio" in msg
        assert args == ("local",)


class TestBuildMcpServerConfigAuthHeaders:
    """企业模板 headers / 连接器 auth_headers → SDK auth_headers。"""

    def test_enterprise_headers_map_to_auth_headers(self):
        cfg = build_mcp_server_config(
            {
                "name": "qa-bearer-streamable-http",
                "transport": "streamable-http",
                "url": "http://192.168.1.96:18016/mcp",
                "headers": {"Authorization": "Bearer sds-dev-mcp-bearer-token"},
                "timeout_s": 10,
                "enabled": True,
            },
            server_id_scope="jiuwenswarm",
        )
        assert cfg is not None
        assert cfg.auth_headers == {
            "Authorization": "Bearer sds-dev-mcp-bearer-token",
        }
        assert "headers" not in (cfg.params or {})
        assert cfg.params.get("timeout_s") == 10

    def test_fractional_timeout_s_preserved(self):
        cfg = build_mcp_server_config(
            {
                "name": "frac-timeout",
                "transport": "streamable-http",
                "url": "http://192.168.1.96:18014/mcp",
                "timeout_s": 0.5,
            }
        )
        assert cfg is not None
        assert cfg.params.get("timeout_s") == 0.5

    def test_auth_headers_alias_preferred_over_headers(self):
        cfg = build_mcp_server_config(
            {
                "name": "prefer-auth",
                "transport": "sse",
                "url": "http://192.168.1.96:18015/sse",
                "headers": {"Authorization": "Bearer from-headers"},
                "auth_headers": {"Authorization": "Bearer from-auth-headers"},
            }
        )
        assert cfg is not None
        assert cfg.auth_headers == {
            "Authorization": "Bearer from-auth-headers",
        }

    def test_query_params_map_to_auth_query_params(self):
        cfg = build_mcp_server_config(
            {
                "name": "qa-query",
                "transport": "http",
                "url": "http://example.com/mcp",
                "query_params": {"token": "abc"},
            }
        )
        assert cfg is not None
        # http 别名须归一为 SDK 注册名，否则 ResourceMgr 报 Unsupported MCP client type
        assert cfg.client_type == "streamable-http"
        assert cfg.auth_query_params == {"token": "abc"}

    def test_http_alias_maps_to_streamable_http(self):
        """对齐 TC_MCP_CALL_005：transport=http 不得原样传给 SDK。"""
        for alias in ("http", "HTTP", "streamable_http"):
            cfg = build_mcp_server_config(
                {
                    "name": "qa-streamable-http",
                    "transport": alias,
                    "url": "http://192.168.1.96:18016/mcp",
                    "timeout_s": 10,
                    "enabled": True,
                },
                server_id_scope="jiuwenswarm",
            )
            assert cfg is not None, alias
            assert cfg.client_type == "streamable-http", alias
            assert cfg.params.get("timeout_s") == 10

    def test_non_dict_headers_ignored(self):
        cfg = build_mcp_server_config(
            {
                "name": "bad-headers",
                "transport": "sse",
                "url": "http://example.com/sse",
                "headers": "not-a-dict",
            }
        )
        assert cfg is not None
        assert cfg.auth_headers == {}
