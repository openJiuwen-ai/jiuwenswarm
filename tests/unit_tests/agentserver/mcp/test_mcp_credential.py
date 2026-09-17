# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the credential layer: kind detection + ${VAR} resolution + store.

P1 scope (form B, static-token MCPs): the framework must (1) detect
that an MCP's mcp.json declares a ${VAR} placeholder (env/headers/url),
(2) store a user-supplied token, (3) resolve the placeholder into the real
value before writing config.yaml. Without this, tianyancha/gildata/gmail/jira
etc. (15 connectors) connect but every MCP call sends the literal
'${TIANYANCHA_API_KEY}' string and 401s.

Design: a CredentialProvider abstraction with one subclass per credential
acquisition strategy (none/token/cli_oauth). This file covers the token
subclass + the shared detection/resolution helpers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import credential as cred


# --- detect_credential_kind: classify an MCP by its package contents ---

def test_detect_kind_none_for_no_placeholder(tmp_path: Path) -> None:
    """A free-connect remote MCP (notion-like) has only url -> 'none'."""
    pkg = _mk_pkg(tmp_path, "notion", mcp={"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("notion") == "none"


def test_detect_kind_token_for_env_placeholder(tmp_path: Path) -> None:
    """jira/gmail-style: env has ${VAR} -> 'token'."""
    pkg = _mk_pkg(tmp_path, "jira", mcp={
        "mcpServers": {"jira": {"command": "npx", "args": ["atlassian-jira-mcp-server"],
                                 "env": {"ATLASSIAN_API_TOKEN": "${ATLASSIAN_API_TOKEN}"}}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("jira") == "token"


def test_detect_kind_token_for_headers_placeholder(tmp_path: Path) -> None:
    """tianyancha-style: headers.Authorization has ${VAR} -> 'token'."""
    pkg = _mk_pkg(tmp_path, "tyc-mcp", mcp={
        "mcpServers": {"tyc-mcp": {"type": "streamableHttp", "url": "https://mcp.tianyancha.com/v1",
                                    "headers": {"Authorization": "Bearer ${TIANYANCHA_API_KEY}"}}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("tyc-mcp") == "token"


def test_detect_kind_token_for_url_placeholder(tmp_path: Path) -> None:
    """gildata-style: url query has ${VAR} -> 'token'."""
    pkg = _mk_pkg(tmp_path, "gildata", mcp={
        "mcpServers": {"gildata": {"type": "streamableHttp",
                                    "url": "https://api.gildata.com/mcp?token=${GILDATA_TOKEN}"}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("gildata") == "token"


def test_detect_kind_cli_oauth_for_cli_json(tmp_path: Path) -> None:
    """feishu/dingtalk: has cli.json -> 'cli_oauth' (CLI manages its own OAuth)."""
    pkg = _mk_pkg(tmp_path, "feishu", mcp=None, cli={"runtime": {"type": "node"},
        "init": {"win32": "npm install -g @larksuite/cli"},
        "versionCheck": {"command": {"win32": "lark-cli.cmd --version"}, "minVersion": "1.0.77"}})
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("feishu") == "cli_oauth"


# --- resolve_placeholders: substitute ${VAR} from the store ---

def test_resolve_placeholders_in_env(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("jira", "ATLASSIAN_API_TOKEN", "secret123")
    entry = {"name": "jira", "transport": "stdio", "command": "npx",
             "args": ["atlassian-jira-mcp-server"],
             "env": {"ATLASSIAN_API_TOKEN": "${ATLASSIAN_API_TOKEN}"}}
    resolved = cred.resolve_placeholders(entry, store)
    assert resolved["env"]["ATLASSIAN_API_TOKEN"] == "secret123"


def test_resolve_placeholders_in_headers(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("tyc-mcp", "TIANYANCHA_API_KEY", "key-abc")
    entry = {"name": "tyc-mcp", "transport": "streamable-http", "url": "https://mcp.tianyancha.com/v1",
             "headers": {"Authorization": "Bearer ${TIANYANCHA_API_KEY}"}}
    resolved = cred.resolve_placeholders(entry, store)
    assert resolved["headers"]["Authorization"] == "Bearer key-abc"


def test_resolve_placeholders_in_url(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("gildata", "GILDATA_TOKEN", "tok-xyz")
    entry = {"name": "gildata", "transport": "streamable-http",
             "url": "https://api.gildata.com/mcp?token=${GILDATA_TOKEN}"}
    resolved = cred.resolve_placeholders(entry, store)
    assert resolved["url"] == "https://api.gildata.com/mcp?token=tok-xyz"


def test_resolve_placeholders_missing_token_keeps_literal(tmp_path: Path) -> None:
    """A placeholder with no stored value is left as-is (caller decides to error)."""
    store = cred.CredentialStore(workspace_dir=tmp_path)
    entry = {"name": "x", "env": {"K": "${MISSING}"}}
    resolved = cred.resolve_placeholders(entry, store)
    assert resolved["env"]["K"] == "${MISSING}"


def test_resolve_placeholders_no_placeholders_returns_copy(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    entry = {"name": "notion", "url": "https://mcp.notion.com/mcp"}
    resolved = cred.resolve_placeholders(entry, store)
    assert resolved == entry
    assert resolved is not entry  # always a fresh copy


# --- CredentialStore: persistence ---

def test_store_save_and_get_roundtrip(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("tyc-mcp", "TIANYANCHA_API_KEY", "v1")
    assert store.get_token("tyc-mcp", "TIANYANCHA_API_KEY") == "v1"


def test_store_get_missing_returns_none(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    assert store.get_token("x", "Y") is None


def test_store_delete_removes_all_for_connector(tmp_path: Path) -> None:
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("jira", "ATLASSIAN_API_TOKEN", "s")
    store.save_token("jira", "ATLASSIAN_USER_EMAIL", "e@x.com")
    store.delete_mcp("jira")
    assert store.get_token("jira", "ATLASSIAN_API_TOKEN") is None
    assert store.get_token("jira", "ATLASSIAN_USER_EMAIL") is None


def test_store_persists_across_instances(tmp_path: Path) -> None:
    """Two store instances over the same workspace share state (file-backed)."""
    cred.CredentialStore(workspace_dir=tmp_path).save_token("x", "K", "v")
    assert cred.CredentialStore(workspace_dir=tmp_path).get_token("x", "K") == "v"


def test_store_reads_file_with_utf8_bom(tmp_path: Path) -> None:
    """A credential file written with a UTF-8 BOM (e.g. by legacy tooling /
    PowerShell Set-Content -Encoding utf8) must still parse. Regression: the
    reader used plain utf-8 + json.load, which raised JSONDecodeError on the
    BOM and silently returned {} — tokens became invisible."""
    store = cred.CredentialStore(workspace_dir=tmp_path)
    p = store._path("baidu-netdisk")
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write a UTF-8 BOM (EF BB BF) + JSON body.
    body = '{"BAIDU_ACCESS_TOKEN": "real-token"}'
    p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert store.get_token("baidu-netdisk", "BAIDU_ACCESS_TOKEN") == "real-token"
    assert store.get_all("baidu-netdisk") == {"BAIDU_ACCESS_TOKEN": "real-token"}


def test_store_save_is_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    """_save 用 tempfile + os.replace 原子写: 不残留 .tmp 文件."""
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("tyc-mcp", "TIANYANCHA_API_KEY", "v1")

    # credentials 目录下应只有最终的 <name>.json,无 .tmp 残留.
    cred_files = list((tmp_path / "mcp" / "credentials").iterdir())
    assert any(p.name == "tyc-mcp.json" for p in cred_files)
    assert not any(p.suffix == ".tmp" or ".tmp" in p.name for p in cred_files), \
        f"temp file leaked: {cred_files}"


def test_store_save_preserves_old_on_overwrite_failure(tmp_path: Path) -> None:
    """写入过程中 os.replace 失败时,旧值应仍可读(原子性保证)."""
    store = cred.CredentialStore(workspace_dir=tmp_path)
    store.save_token("tyc-mcp", "TIANYANCHA_API_KEY", "old-value")
    assert store.get_token("tyc-mcp", "TIANYANCHA_API_KEY") == "old-value"

    # 模拟 os.replace 抛错(目标文件被占用等),save_token 应捕获并 log,
    # 旧值仍在(原子写失败不破坏旧文件).
    with patch("jiuwenswarm.server.runtime.mcp.credential.os.replace",
                side_effect=OSError("replace failed")):
        store.save_token("tyc-mcp", "TIANYANCHA_API_KEY", "new-value")

    assert store.get_token("tyc-mcp", "TIANYANCHA_API_KEY") == "old-value"
    # 临时文件应被清理,不残留.
    cred_files = list((tmp_path / "mcp" / "credentials").iterdir())
    assert not any(".tmp" in p.name for p in cred_files), \
        f"temp file leaked on failure: {cred_files}"


# --- detect placeholders in raw mcp.json (the probe used by detect_credential_kind) ---

def test_extract_placeholders_finds_all_env_keys(tmp_path: Path) -> None:
    """extract_placeholders takes a server cfg (the inner mcpServers[name] dict),
    not the full mcp.json wrapper."""
    server_cfg = {"env": {"A": "${A}", "B": "plain", "C": "${C}"},
                  "headers": {"X": "Bearer ${X}"},
                  "url": "https://x.com/m?k=${K}"}
    found = cred.extract_placeholders(server_cfg)
    assert found == {"A", "C", "X", "K"}


# --- helpers ---

def _mk_pkg(workspace: Path, name: str, *, mcp: dict | None, cli: dict | None = None) -> Path:
    pkg = workspace / "mcp" / "mcp_builtins" / name
    pkg.mkdir(parents=True, exist_ok=True)
    if mcp is not None:
        import json
        (pkg / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    if cli is not None:
        import json
        (pkg / "cli.json").write_text(json.dumps(cli), encoding="utf-8")
    return pkg


# --- token-schema.json: skill-only connectors (ctrip-wendao/netease-mail) ---

def test_detect_kind_token_for_token_schema_no_mcp(tmp_path: Path) -> None:
    """ctrip-wendao/netease-mail style: no mcp.json, has token-schema.json -> 'token'."""
    pkg = _mk_pkg(tmp_path, "ctrip-wendao", mcp=None)
    import json
    (pkg / "token-schema.json").write_text(json.dumps({
        "fields": [{"key": "WENDAO_API_KEY", "type": "password", "required": True}]
    }), encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.detect_credential_kind("ctrip-wendao") == "token"


def test_load_token_schema_returns_fields(tmp_path: Path) -> None:
    """load_token_schema returns the field list (key/label/required/...)."""
    pkg = _mk_pkg(tmp_path, "ctrip-wendao", mcp=None)
    import json
    (pkg / "token-schema.json").write_text(json.dumps({
        "title": "Ctrip Wendao",
        "docUrl": "https://www.ctrip.com/wendao/openclaw",
        "fields": [
            {"key": "WENDAO_API_KEY", "label": "API Token", "type": "password", "required": True},
            {"key": "WENDAO_OPTIONAL", "label": "Opt", "type": "text", "required": False},
        ],
    }), encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        schema = cred.load_token_schema("ctrip-wendao")
    assert schema is not None
    assert schema["title"] == "Ctrip Wendao"
    assert schema["docUrl"] == "https://www.ctrip.com/wendao/openclaw"
    keys = [f["key"] for f in schema["fields"]]
    assert keys == ["WENDAO_API_KEY", "WENDAO_OPTIONAL"]


def test_load_token_schema_missing_returns_none(tmp_path: Path) -> None:
    """No token-schema.json -> None."""
    _mk_pkg(tmp_path, "plain", mcp={"mcpServers": {"plain": {"url": "https://x"}}})
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.load_token_schema("plain") is None


def test_required_tokens_from_token_schema(tmp_path: Path) -> None:
    """required_tokens_from_schema extracts only required field keys."""
    pkg = _mk_pkg(tmp_path, "ctrip-wendao", mcp=None)
    import json
    (pkg / "token-schema.json").write_text(json.dumps({
        "fields": [
            {"key": "WENDAO_API_KEY", "required": True},
            {"key": "OPT", "required": False},
        ],
    }), encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        assert cred.required_tokens_from_schema("ctrip-wendao") == ["WENDAO_API_KEY"]


# --- build_credentials_prompt: docUrl + field hints for the modal ---

def test_build_prompt_with_schema_attaches_doc_and_field_hints(tmp_path: Path) -> None:
    """A schema-backed MCP surfaces docUrl/docLabel + per-field render hints."""
    pkg = _mk_pkg(tmp_path, "ctrip-wendao", mcp=None)
    import json
    (pkg / "token-schema.json").write_text(json.dumps({
        "title": "携程问道授权",
        "docUrl": "https://www.ctrip.com/wendao/openclaw",
        "docLabel": "去申请",
        "fields": [
            {"key": "WENDAO_API_KEY", "label": "API Token", "type": "password",
             "placeholder": "paste here", "required": True},
            {"key": "OPT", "required": False},
        ],
    }), encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        out = cred.build_credentials_prompt("ctrip-wendao", ["WENDAO_API_KEY"])
    assert out["credentials_required"] is True
    assert out["required_tokens"] == ["WENDAO_API_KEY"]
    assert out["credential_kind"] == "token"
    assert out["title"] == "携程问道授权"
    assert out["doc_url"] == "https://www.ctrip.com/wendao/openclaw"
    assert out["doc_label"] == "去申请"
    assert out["fields"]["WENDAO_API_KEY"]["label"] == "API Token"
    assert out["fields"]["WENDAO_API_KEY"]["placeholder"] == "paste here"
    # Non-missing optional field must NOT appear in the hints.
    assert "OPT" not in out.get("fields", {})


def test_build_prompt_without_schema_falls_back_to_bare_keys(tmp_path: Path) -> None:
    """A form-B MCP with no schema still returns a minimal payload."""
    _mk_pkg(tmp_path, "plain", mcp={"mcpServers": {"plain": {
        "url": "https://x", "headers": {"X": "Bearer ${X}"}
    }}})
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        out = cred.build_credentials_prompt("plain", ["X"])
    assert out["credentials_required"] is True
    assert out["required_tokens"] == ["X"]
    assert out["credential_kind"] == "token"
    # No schema -> no doc/field hints.
    assert "doc_url" not in out
    assert "fields" not in out
    assert "title" not in out


def test_build_prompt_deveco_has_no_doc_url_only_label(tmp_path: Path) -> None:
    """deveco-toolbox schema carries a docLabel hint but no docUrl (local path)."""
    pkg = _mk_pkg(tmp_path, "deveco-toolbox", mcp=None)
    import json
    (pkg / "token-schema.json").write_text(json.dumps({
        "docLabel": "请填写本地 DevEco 安装路径",
        "fields": [
            {"key": "DEVECO_PATH", "type": "text", "required": True,
             "placeholder": "C:\\Program Files\\Huawei\\DevEco Studio"},
        ],
    }), encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=tmp_path):
        out = cred.build_credentials_prompt("deveco-toolbox", ["DEVECO_PATH"])
    assert "doc_url" not in out
    assert out["doc_label"] == "请填写本地 DevEco 安装路径"
    assert out["fields"]["DEVECO_PATH"]["type"] == "text"

