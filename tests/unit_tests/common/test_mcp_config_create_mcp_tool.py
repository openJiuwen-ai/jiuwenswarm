# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.common.mcp_config import (
    _check_dangerous_args,
    _is_blocked_host,
    _loopback_mcp_allowed,
    _normalize_mcp_client_type,
    _normalize_stdio_command_kind,
    _optional_auth_dict,
    _path_is_under_trusted_root,
    _pick_mcp_url,
    _trusted_cat_cafe_stdio_roots,
    _validate_cat_cafe_request_scoped_stdio,
    _validate_request_scoped_remote_mcp,
    create_mcp_tool,
)
from openjiuwen.core.foundation.tool import McpServerConfig


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
            ("playwright", "playwright"),
            ("openapi", "openapi"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_mcp_client_type(raw) == expected


class TestNormalizeStdioCommandKind:
    @pytest.mark.parametrize(
        "command, expected",
        [
            ("node", "node"),
            ("node.exe", "node"),
            ("python", "python"),
            ("python3", "python"),
            ("python3.11", "python"),
            ("C:\\Program Files\\node.exe", "node"),
            ("/usr/local/bin/node", "node"),
            ("npx", "npx"),
            ("npx.exe", "npx"),
            ("npx.cmd", "npx"),
            ("/usr/local/bin/npx", "npx"),
            ("uvx", "uvx"),
            ("uvx.exe", "uvx"),
            ("/home/u/.local/bin/uvx", "uvx"),
        ],
    )
    def test_valid_commands(self, command, expected):
        assert _normalize_stdio_command_kind(command) == expected

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="缺少"):
            _normalize_stdio_command_kind("")

    def test_whitespace_command_raises(self):
        with pytest.raises(ValueError, match="缺少"):
            _normalize_stdio_command_kind("   ")

    def test_none_command_raises(self):
        with pytest.raises(ValueError, match="缺少"):
            _normalize_stdio_command_kind(None)

    @pytest.mark.parametrize("cmd", ["bash", "sh", "ruby", "cmd", "powershell", "deno"])
    def test_unsupported_command_raises(self, cmd):
        with pytest.raises(ValueError, match="不支持"):
            _normalize_stdio_command_kind(cmd)


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


class TestCheckDangerousArgs:
    def test_safe_args_pass(self):
        _check_dangerous_args("t", ["-y", "@scope/pkg", "/tmp"])

    def test_eval_flag_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            _check_dangerous_args("t", ["-e", "code"])

    def test_command_flag_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            _check_dangerous_args("t", ["-c", "print(1)"])

    def test_module_flag_allowed(self):
        _check_dangerous_args("t", ["-m", "os"])

    def test_eval_equals_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            _check_dangerous_args("t", ["--eval=code"])

    def test_command_equals_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            _check_dangerous_args("t", ["-c=print(1)"])

    def test_non_list_args_noop(self):
        _check_dangerous_args("t", "not-a-list")

    def test_empty_args_pass(self):
        _check_dangerous_args("t", [])


class TestTrustedCatCafeStdioRoots:
    def test_returns_list_of_paths(self):
        roots = _trusted_cat_cafe_stdio_roots()
        assert isinstance(roots, list)
        for r in roots:
            assert isinstance(r, Path)

    def test_no_duplicates(self):
        roots = _trusted_cat_cafe_stdio_roots()
        keys = [str(r) for r in roots]
        assert len(keys) == len(set(keys))


class TestPathIsUnderTrustedRoot:
    def test_subpath_is_under_root(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        child = root / "sub" / "file.py"
        assert _path_is_under_trusted_root(child, [root])

    def test_unrelated_path_is_not_under_root(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        other = tmp_path / "other" / "file.py"
        assert not _path_is_under_trusted_root(other, [root])

    def test_root_itself_is_under_root(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert _path_is_under_trusted_root(root, [root])


class TestValidateCatCafeRequestScopedStdio:
    def test_npx_skips_path_check(self):
        _validate_cat_cafe_request_scoped_stdio({
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/etc", "/tmp"],
        })

    def test_uvx_skips_path_check(self):
        _validate_cat_cafe_request_scoped_stdio({
            "command": "uvx",
            "args": ["mcp-server-fetch", "/var/data"],
        })

    def test_npx_untrusted_cwd_allowed(self):
        _validate_cat_cafe_request_scoped_stdio({
            "command": "npx",
            "args": ["-y", "pkg"],
            "cwd": "/untrusted/random/dir",
        })

    def test_npx_dangerous_command_arg_still_blocked(self):
        with pytest.raises(ValueError, match="python -c"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "npx",
                "args": ["-c", "code"],
            })

    def test_uvx_dangerous_command_arg_still_blocked(self):
        with pytest.raises(ValueError, match="python -c"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "uvx",
                "args": ["--command", "code"],
            })

    def test_node_path_check_still_active(self):
        with pytest.raises(ValueError, match="受信根"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "node",
                "args": ["/untrusted/script.js"],
            })

    def test_python_path_check_still_active(self):
        with pytest.raises(ValueError, match="受信根"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "python",
                "args": ["/untrusted/script.py"],
            })

    def test_node_eval_still_blocked(self):
        with pytest.raises(ValueError, match="node -e"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "node",
                "args": ["-e", "console.log(1)"],
            })

    def test_args_not_list_raises(self):
        with pytest.raises(ValueError, match="args 须为列表"):
            _validate_cat_cafe_request_scoped_stdio({
                "command": "npx",
                "args": "not-a-list",
            })


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
    def test_basic_stdio(self):
        cfg = json.dumps({"name": "my-tool", "command": "node", "args": ["server.js"]})
        result = create_mcp_tool(cfg)
        assert isinstance(result, McpServerConfig)
        assert result.client_type == "stdio"
        assert result.server_name == "my-tool"
        assert result.server_path == "stdio://my-tool"
        assert result.params["command"] == "node"
        assert result.params["args"] == ["server.js"]

    def test_stdio_with_python(self):
        cfg = json.dumps({"name": "py-tool", "command": "python", "args": ["-m", "mymod"]})
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "python"

    def test_stdio_with_env(self):
        cfg = json.dumps({
            "name": "env-tool",
            "command": "node",
            "args": ["s.js"],
            "env": {"KEY": "VAL"},
        })
        result = create_mcp_tool(cfg)
        assert result.params["env"] == {"KEY": "VAL"}

    def test_stdio_with_cwd(self):
        cfg = json.dumps({
            "name": "cwd-tool",
            "command": "node",
            "args": ["s.js"],
            "cwd": "/tmp/work",
        })
        result = create_mcp_tool(cfg)
        assert result.params["cwd"] == "/tmp/work"

    def test_stdio_with_server_id(self):
        cfg = json.dumps({
            "name": "sid-tool",
            "server_id": "custom-id",
            "command": "node",
            "args": ["s.js"],
        })
        result = create_mcp_tool(cfg)
        assert result.server_id == "custom-id"

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="缺少 'name'"):
            create_mcp_tool(json.dumps({"command": "node", "args": ["s.js"]}))

    def test_missing_command_for_stdio_raises(self):
        with pytest.raises(ValueError, match="缺少"):
            create_mcp_tool(json.dumps({"name": "t", "command": "", "args": ["s.js"]}))

    def test_invalid_args_type_raises(self):
        with pytest.raises(ValueError, match="必须是列表"):
            create_mcp_tool(json.dumps({"name": "t", "command": "node", "args": "not-list"}))

    def test_unsupported_command_raises(self):
        with pytest.raises(ValueError, match="不支持"):
            create_mcp_tool(json.dumps({"name": "t", "command": "ruby", "args": ["s.rb"]}))

    def test_dangerous_eval_arg_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            create_mcp_tool(json.dumps({"name": "t", "command": "node", "args": ["-e", "code"]}))

    def test_dangerous_command_arg_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            create_mcp_tool(json.dumps({"name": "t", "command": "python", "args": ["-c", "print(1)"]}))

    def test_array_config(self):
        cfg = json.dumps([{"name": "arr-tool", "command": "node", "args": ["s.js"]}])
        result = create_mcp_tool(cfg)
        assert result.server_name == "arr-tool"

    def test_empty_array_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            create_mcp_tool("[]")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="无效的 JSON"):
            create_mcp_tool("not-json")


class TestCreateMcpToolNpxUvx:
    def test_npx_config(self):
        cfg = json.dumps({
            "name": "npx-tool",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "npx"
        assert result.params["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

    def test_uvx_config(self):
        cfg = json.dumps({"name": "uvx-tool", "command": "uvx", "args": ["mcp-server-fetch"]})
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["command"] == "uvx"
        assert result.params["args"] == ["mcp-server-fetch"]

    def test_npx_with_env_and_cwd(self):
        cfg = json.dumps({
            "name": "npx-env",
            "command": "npx",
            "args": ["-y", "@scope/pkg"],
            "env": {"API_KEY": "secret"},
            "cwd": "/some/dir",
        })
        result = create_mcp_tool(cfg)
        assert result.params["env"] == {"API_KEY": "secret"}
        assert result.params["cwd"] == "/some/dir"

    def test_npx_absolute_path(self):
        cfg = json.dumps({"name": "npx-abs", "command": "/usr/local/bin/npx", "args": ["-y", "pkg"]})
        result = create_mcp_tool(cfg)
        assert result.params["command"] == "/usr/local/bin/npx"

    def test_uvx_absolute_path(self):
        cfg = json.dumps({"name": "uvx-abs", "command": r"C:\Users\me\.local\bin\uvx.exe", "args": ["pkg"]})
        result = create_mcp_tool(cfg)
        assert result.params["command"] == r"C:\Users\me\.local\bin\uvx.exe"

    def test_npx_dangerous_eval_arg_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            create_mcp_tool(json.dumps({"name": "t", "command": "npx", "args": ["-e", "code"]}))

    def test_uvx_dangerous_command_arg_blocked(self):
        with pytest.raises(ValueError, match="危险标志"):
            create_mcp_tool(json.dumps({"name": "t", "command": "uvx", "args": ["-c", "print(1)"]}))


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

    def test_stdio_timeout_s_passthrough(self):
        cfg = json.dumps({
            "name": "slow-stdio",
            "command": "node",
            "args": ["s.js"],
            "timeout_s": 120,
        })
        result = create_mcp_tool(cfg)
        assert result.client_type == "stdio"
        assert result.params["timeout_s"] == 120

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
            "command": "node",
            "args": ["s.js"],
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
