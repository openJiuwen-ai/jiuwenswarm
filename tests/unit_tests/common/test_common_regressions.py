# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 12 small test modules)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime import browser_tools
from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    service as service_module,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserRunGuardrails,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.service import (
    BrowserService,
)
import threading
import time
from agent_gateway.auth import session_store as store_mod
from agent_gateway.auth.account_kit import Credential, LoginOutcome, OAuthError
from agent_gateway.auth.service import AuthService
import logging
from e2a.message import ReqMethod as ProtocolReqMethod
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common import filesystem_paths
import json
from jiuwenswarm.common import utils
import os
from unittest.mock import AsyncMock
from dotenv import dotenv_values
import yaml
from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.browser_settings import JEV_PROVIDER_DEFAULTS, resolve_jev_connection
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
import jiuwenswarm.common.config as cfg_mod
from jiuwenswarm.server.control import config_service
from jiuwenswarm.common import config
from agent_gateway.common.volcengine_endpoints import normalize_ark_api_base
from websockets.exceptions import ConnectionClosedError
from agent_gateway.common.ws_diagnostics import (
    describe_ws_exception,
    format_ws_diagnostics,
)


# --- from test_agent_core_browser_runtime_contract.py ---

@pytest.mark.asyncio
async def test_direct_mcp_command_does_not_require_npx_and_reaches_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bundled Node launch contract must survive agent-core upgrades."""
    executable = Path(sys.executable).resolve()
    monkeypatch.setenv("BROWSER_DRIVER", "remote")
    monkeypatch.setenv(
        "BROWSER_PROFILE_STORE_PATH",
        str(tmp_path / "browser-profiles.json"),
    )

    original_which = service_module.shutil.which

    def which_without_npx(command: str, *args: Any, **kwargs: Any) -> str | None:
        if command == "npx":
            return None
        return original_which(command, *args, **kwargs)

    monkeypatch.setattr(service_module.shutil, "which", which_without_npx)
    monkeypatch.setattr(
        browser_tools,
        "ensure_browser_runtime_client_patch",
        lambda: None,
    )

    registered: dict[str, Any] = {}

    async def start_runner() -> None:
        return None

    async def add_mcp_server(config: McpServerConfig, *, tag: str) -> None:
        registered["config"] = config
        registered["params"] = dict(config.params)
        registered["tag"] = tag
        return None

    monkeypatch.setattr(
        service_module,
        "Runner",
        SimpleNamespace(
            start=start_runner,
            resource_mgr=SimpleNamespace(add_mcp_server=add_mcp_server),
        ),
    )
    monkeypatch.setattr(
        service_module.BROWSER_SERVICE_REGISTRY,
        "activate_binding",
        lambda *_: False,
    )

    mcp_cfg = McpServerConfig(
        server_id="playwright_official_stdio",
        server_name="playwright-official",
        server_path="stdio://playwright",
        client_type="stdio",
        params={
            "command": str(executable),
            "args": ["node_modules/@playwright/mcp/cli.js"],
            "cwd": str(tmp_path),
            "env": {},
            "timeout_s": 30,
        },
    )
    browser_service = BrowserService(
        provider="test",
        api_key="test",
        api_base="https://example.invalid",
        model_name="test",
        mcp_cfg=mcp_cfg,
        guardrails=BrowserRunGuardrails(),
    )
    browser_service._task_binding_ref_count = 1
    monkeypatch.setattr(browser_service, "_acquire_registry_lease", lambda: None)

    async def no_managed_driver() -> bool:
        return False

    monkeypatch.setattr(
        browser_service,
        "_ensure_managed_driver_started",
        no_managed_driver,
    )
    monkeypatch.setattr(browser_service, "_ensure_screenshots_dir", lambda: None)

    await browser_service.ensure_runtime_ready()

    assert registered["config"] is mcp_cfg
    assert registered["tag"] == "browser.service"
    assert registered["params"]["command"] == str(executable)

# --- from test_auth_service_callback.py ---

class _Pending:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str | None, OAuthError | None]] = []

    def begin_callback(self, state):
        return SimpleNamespace(code_verifier="verifier")

    def finish(self, state, session_id=None, error=None):
        self.finished.append((state, session_id, error))

class _Flow:
    def __init__(self) -> None:
        self.pending = _Pending()

    def exchange_code(self, code, verifier):
        return LoginOutcome(
            credential=Credential(id_token="id", refresh_token="rt", expires_at=time.time() + 3600),
            user_id="openid-1",
            user_name="u",
        )

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    return store_mod.AuthSessionStore()

def test_login_result_is_released_without_waiting_for_model_discovery(store, monkeypatch):
    flow = _Flow()
    service = AuthService(flow=flow, store=store)
    discovery_started = threading.Event()
    release = threading.Event()

    def _slow_discovery(session_id=None):
        discovery_started.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(service, "refresh_model_catalog", _slow_discovery)
    try:
        session = service.complete_callback("state-1", code="code-1")
        assert flow.pending.finished == [("state-1", session.session_id, None)], "认领结果要立刻登记"
        assert discovery_started.wait(2), "模型目录仍要在后台拉"
    finally:
        release.set()

def test_session_store_failure_is_reported_to_the_waiting_client(store, monkeypatch):
    flow = _Flow()
    service = AuthService(flow=flow, store=store)

    def _broken(_outcome):
        raise OSError("disk full")

    monkeypatch.setattr(store, "create", _broken)
    with pytest.raises(OAuthError) as excinfo:
        service.complete_callback("state-1", code="code-1")
    assert excinfo.value.code == "session_store_failed"
    [(state, session_id, error)] = flow.pending.finished
    assert state == "state-1" and session_id is None and error is excinfo.value

# --- from test_caplog_reaches_jiuwenswarm_logs.py ---

def test_a_warning_from_the_jiuwenswarm_logger_reaches_caplog(caplog):
    """``setup_logger()`` turns propagation off on the ``jiuwenswarm`` logger at import
    time, and ``caplog`` listens on the root logger; without the fixture the record is
    printed to stderr and ``caplog.records`` stays empty."""
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm"):
        logging.getLogger("jiuwenswarm.common.utils").warning("visible to the test")
    assert any(r.message == "visible to the test" for r in caplog.records)

def test_a_test_without_caplog_leaves_propagation_alone():
    """Propagation also feeds pytest's live-log handler, which reinstalls its own
    stderr over one a test has replaced; a test that never asks for ``caplog`` must
    not pay for it."""
    assert logging.getLogger("jiuwenswarm").propagate is False

# --- from test_enterprise_protocol_identity.py ---

@pytest.mark.parametrize("name", [
    "CHAT_SEND", "A4P_CONFIG_GET", "A4P_CONFIG_UPDATE", "WORKSPACE_TREE",
    "FILE_READ_UPLOAD_CHUNK", "SKILLS_ENTERPRISE_INSTALL",
])
def test_rpc_members_share_identity_across_protocol_consumers(name):
    local = getattr(ReqMethod, name)
    assert ReqMethod is ProtocolReqMethod
    assert ProtocolReqMethod(local.value) is local

@pytest.mark.asyncio
async def test_application_plugin_awaits_host_attachment_persistence():
    from agent_gateway.extensions.sdk import ApplicationPluginServices
    from jiuwenswarm.extensions.sdk import ApplicationPluginServices as LegacyServices

    assert LegacyServices is ApplicationPluginServices
    params = {}

    async def persist(items, session_id):
        items["persisted_session"] = session_id

    service = ApplicationPluginServices(media_attachment_normalizer=persist)
    await service.normalize_media_attachments(params, "session-1030")
    assert params == {"persisted_session": "session-1030"}

# --- from test_filesystem_paths.py ---

@pytest.mark.parametrize(("source", "expected"), [
    (r"C:\data\team\..\member", r"\\?\C:\data\member"),
    ("C:/data/team", r"\\?\C:\data\team"),
    ("C:\\", "\\\\?\\C:\\"),
    (r"D:\用户目录\expert team", r"\\?\D:\用户目录\expert team"),
    (r"\\server\share\team", r"\\?\UNC\server\share\team"),
    (r"\\server\share", r"\\?\UNC\server\share"),
    (r"\\server\share\专家目录", r"\\?\UNC\server\share\专家目录"),
    (r"\\?\C:\data\team", r"\\?\C:\data\team"),
    (r"\\?\UNC\server\share\team", r"\\?\UNC\server\share\team"),
    (r"\\.\pipe\channel", r"\\.\pipe\channel"),
])
def test_extended_windows_path(monkeypatch, source, expected):
    monkeypatch.setattr(filesystem_paths, "sys", SimpleNamespace(platform="win32"))
    assert filesystem_paths.extended_path(source) == expected

def test_extended_path_preserves_non_windows_relative_paths(monkeypatch):
    monkeypatch.setattr(filesystem_paths, "sys", SimpleNamespace(platform="darwin"))
    assert filesystem_paths.extended_path(Path("relative/team")) == "relative/team"

# --- from test_jev_log_integrity.py ---

@pytest.mark.parametrize("marker", ["BROWSER_POLICY_RESPONSE", "BROWSER_POLICY", "model_end"])
def test_real_decimal_shape_and_pii_survive_both_redaction_layers(marker):
    payload = {
        "confidence": 0.99, "probability_margin": 0.020000000000000018,
        "nested": {"value": 0.123456789012345678, "email": "private@example.test",
                   "id_number": 110101199001010010, "phone": "13812345678"},
        "input_tokens": 1342, "output_tokens": 19, "api_key": 'secret"with\\escape',
        "authorization_outcome": "allow", "args": ["--token", "private-token-value"],
    }
    message = f"[{marker}] " + json.dumps(payload)
    first = utils.mask_sensitive(message)
    record = logging.LogRecord("test", logging.INFO, "", 1, first, (), None)
    utils.SensitiveDataFilter().filter(record)
    parsed = json.loads(record.getMessage().split("] ", 1)[1])
    assert parsed["probability_margin"] == payload["probability_margin"]
    assert parsed["input_tokens"] == 1342 and parsed["output_tokens"] == 19
    assert parsed["nested"]["value"] == payload["nested"]["value"]
    assert parsed["nested"]["email"] == parsed["nested"]["phone"] == parsed["nested"]["id_number"] == "******"
    assert parsed["authorization_outcome"] == "allow"
    assert "private-token-value" not in record.getMessage()
    assert "secret" not in parsed["api_key"]
    assert first == record.getMessage()

def test_nested_credentials_keep_structure_and_mask_every_value():
    payload = {
        "credentials": {"primary": "private-key", "alternatives": ["backup-key"]},
        "tokens_used": 42,
    }
    first = utils.mask_sensitive(json.dumps(payload))
    parsed = json.loads(first)
    assert isinstance(parsed["credentials"], dict)
    assert isinstance(parsed["credentials"]["alternatives"], list)
    assert parsed["credentials"]["primary"].startswith("******(fp:")
    assert parsed["credentials"]["alternatives"][0].startswith("******(fp:")
    assert "private-key" not in first and "backup-key" not in first
    assert parsed["tokens_used"] == 42
    assert utils.mask_sensitive(first) == first

# --- from test_jev_model_clear.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(JEV_PROVIDER_DEFAULTS))
async def test_clear_jev_retains_connection_and_other_credentials(tmp_path, monkeypatch, provider):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    credentials = {preset["api_key_env"]: "synthetic-key" for preset in JEV_PROVIDER_DEFAULTS.values()}
    credentials["API_KEY"] = "synthetic-primary-key"
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    env_path.write_text("".join(f"{name}={value}\n" for name, value in credentials.items()), encoding="utf-8")
    original = {
        "browser": {"chrome_path": "/custom/chrome", "headless": False, "decision": {
            **JEV_PROVIDER_DEFAULTS[provider], "provider": provider, "mode": "hybrid",
            "api_base": "https://jev.example.test/v1", "display_name": "Old Jev",
            "min_confidence": 0.55, "request_timeout_ms": 4500,
        }},
        "experimental": {"multi_model_collaboration_enabled": True,
                         "multi_model_collaboration": {"jev_enabled": True, "verifier_interval": 5}},
    }
    config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: config_path)
    monkeypatch.setattr(handlers, "ENV_FILE", env_path)
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    callback = AsyncMock(return_value=True)
    adapter = ConfigAdapter(on_config_saved=callback)

    disabled = await adapter.handle(AgentRequest(
        request_id="clear-browser", channel_id="web", session_id="session", req_method=ReqMethod.PATH_SET,
        params={"chrome_path": "/custom/chrome", "headless": False,
                "decision_mode": "llm", "decision_display_name": ""},
    ))
    assert disabled.ok is True
    assert disabled.payload["decision_display_name"] == ""
    assert disabled.metadata["browser_runtime_restart"] is True

    key = JEV_PROVIDER_DEFAULTS[provider]["api_key_env"]
    cleared = await adapter.handle(AgentRequest(
        request_id="clear-key", channel_id="web", session_id="session", req_method=ReqMethod.CONFIG_SAVE_ALL,
        params={"config": {f"jev_{provider}_api_key": "", "moa_jev_enabled": "false"}},
    ))
    assert cleared.ok is True
    callback.assert_awaited_once()
    assert callback.call_args.kwargs["env_updates"] == {key: ""}
    expected_credentials = {**credentials, key: ""}
    assert dotenv_values(env_path) == expected_credentials
    assert {name: os.environ[name] for name in credentials} == expected_credentials
    expected_decision = {**original["browser"]["decision"], "mode": "llm", "display_name": ""}
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["browser"] == {**original["browser"], "decision": expected_decision}
    assert persisted["experimental"] == {
        "multi_model_collaboration_enabled": True,
        "multi_model_collaboration": {"jev_enabled": False, "verifier_interval": 5},
    }
    restored = await adapter.handle(AgentRequest(
        request_id="read-browser", channel_id="web", session_id="session", req_method=ReqMethod.PATH_GET, params={},
    ))
    assert restored.payload["jev_key_configured"] is False
    assert restored.payload["decision_display_name"] == ""
    with pytest.raises(ValueError, match="Jev API key"):
        resolve_jev_connection(persisted)

    template = tmp_path / "template.yaml"
    resource = Path(__file__).parents[3] / "jiuwenswarm/resources/config.yaml"
    template.write_text(yaml.safe_dump({"browser": yaml.safe_load(resource.read_text())["browser"]}), encoding="utf-8")
    config_module.migrate_config_from_template(template, config_path)
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for name, value in expected_decision.items():
        assert migrated["browser"]["decision"][name] == value

def test_startup_defaults_keep_existing_model_and_empty_display_name(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"browser": {"decision": {
        "provider": "custom", "model": "private-jev-model", "api_base": "https://private.example.test/v1",
    }}}), encoding="utf-8")
    resource = Path(__file__).parents[3] / "jiuwenswarm/resources/config.yaml"
    config_module.migrate_config_from_template(resource, config_path)
    decision = yaml.safe_load(config_path.read_text(encoding="utf-8"))["browser"]["decision"]
    assert decision["model"] == "private-jev-model"
    assert decision["display_name"] == ""

# --- from test_sandbox_fail_closed_config.py ---

def test_fail_closed_string_false_is_off(monkeypatch):
    monkeypatch_target = "jiuwenswarm.common.config.get_config"
    import pytest
    for raw, expected in [("false", False), ("0", False), ("no", False),
                          ("true", True), (True, True), (False, False), (None, False)]:
        cfg = {"sandbox": {"team_fail_closed": raw}} if raw is not None else {}
        monkeypatch.setattr(cfg_mod, "get_config", lambda cfg=cfg: cfg)
        assert cfg_mod.get_sandbox_team_fail_closed() == expected, f"{raw!r} -> {expected}"

# --- from test_task_asr_config_read.py ---

@pytest.mark.parametrize('saved', [
    {'asr_api_base': 'https://asr.example/v1', 'asr_api_key': 'saved-secret', 'asr_model': 'saved-model'},
    {'asr_api_base': '', 'asr_api_key': '', 'asr_model': ''},
])
def test_asr_saved_config_overrides_stale_process_environment(monkeypatch, tmp_path, saved):
    from jiuwenswarm.common.security import base_crypto

    monkeypatch.setattr(base_crypto, "get_crypto_provider", lambda: None)
    path = tmp_path / '.env'
    env_keys = {'asr_api_base': 'ASR_API_BASE', 'asr_api_key': 'ASR_API_KEY', 'asr_model': 'ASR_MODEL_NAME'}
    path.write_text('\n'.join(f'{env_keys[key]}="{value}"' for key, value in saved.items()), encoding='utf-8')
    monkeypatch.setattr(config_service, 'get_env_file', lambda: path)
    monkeypatch.setattr(config_service, '_read_raw_config', lambda: {})
    for env_key in env_keys.values():
        monkeypatch.setenv(env_key, 'stale-gateway-value')
    payload = config_service.get_panel()
    assert {key: payload[key] for key in saved} == saved

@pytest.mark.parametrize("fails", [False, True])
def test_asr_key_is_decrypted_at_config_owner(monkeypatch, tmp_path, fails):
    from jiuwenswarm.common.security import base_crypto

    class OwnerCrypto:
        def decrypt(self, value):
            assert value == "owner-ciphertext"
            if fails:
                raise ValueError("sensitive provider details")
            return "owner-secret"

    path = tmp_path / ".env"
    path.write_text('ASR_API_KEY="owner-ciphertext"', encoding="utf-8")
    monkeypatch.setattr(config_service, "get_env_file", lambda: path)
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    monkeypatch.setattr(base_crypto, "get_crypto_provider", lambda: OwnerCrypto())
    if fails:
        payload = config_service.get_panel()
        assert payload["asr_api_key"] == ""
        assert payload["asr_credential_error"] == "ASR credential decryption failed at AgentServer"
        assert "asr_api_key_decrypted" not in payload
    else:
        payload = config_service.get_panel()
        assert payload["asr_api_key"] == "owner-secret"
        assert payload["asr_api_key_decrypted"] is True

# --- from test_ttse_config_panel.py ---

@pytest.fixture
def panel_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("react:\n  ttse:\n    enabled: false\n    evolve_enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "get_env_file", lambda: tmp_path / ".env")
    original = handlers.provider
    a2ui = SimpleNamespace(get_a2ui_config_payload=lambda raw: {}, get_default_a2ui_config_payload=lambda: {})
    monkeypatch.setattr(handlers, "provider", lambda domain: a2ui if domain == "a2ui" else original(domain))
    return path

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_ttse_switch_round_trips_from_runtime_save_to_front_read(panel_config, enabled):
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(
        channel, None, "save", {"config": {"ttse_enabled": str(enabled).lower()}}, None,
    )
    assert channel.send_response.call_args.kwargs["ok"] is True
    saved = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
    assert saved["react"]["ttse"] == {"enabled": enabled, "evolve_enabled": False}

    await handlers.config_get_handler(channel, None, "runtime-read", {}, None)
    runtime_payload = channel.send_response.call_args.kwargs["payload"]
    response = await config_service.handle_config_request(AgentRequest(
        request_id="front-read", channel_id="web", req_method=ReqMethod.CONFIG_GET, params={},
    ))
    assert response.ok is True
    assert response.payload["ttse_enabled"] == runtime_payload["ttse_enabled"] == str(enabled).lower()

@pytest.mark.parametrize("content", ["react: {}\n", "react: [invalid]\n"])
def test_ttse_switch_front_read_defaults_to_false(panel_config, content):
    panel_config.write_text(content, encoding="utf-8")
    assert config_service.get_panel()["ttse_enabled"] == "false"

# --- from test_volcengine_endpoints.py ---

@pytest.mark.parametrize("host", ["ark.cn-beijing.volces.com", "ark.ap-southeast.bytepluses.com"])
def test_ark_base_preserves_configured_region(host):
    expected = f"https://{host}/api/v3"
    assert normalize_ark_api_base(f"https://{host}") == expected
    assert normalize_ark_api_base(f"{expected}/") == expected
    assert normalize_ark_api_base(f"HTTPS://{host.upper()}/api/v3") == expected

@pytest.mark.parametrize("base", [
    "https://proxy.example/api/v3",
    "https://ark.cn-beijing.volces.com.evil.example/api/v3",
    "https://ark.cn-beijing.volces.com@evil.example/api/v3",
    "http://ark.cn-beijing.volces.com/api/v3",
    "https://ark.cn-beijing.volces.com:8443/api/v3",
    "https://ark.cn-beijing.volces.com/api/coding/v3",
    "https://ark.cn-beijing.volces.com/api/compatible",
    "https://ark.cn-beijing.volces.com/API/V3",
    "https://ark.cn-beijing.volces.com/api/v3?redirect=1",
])
def test_ark_generation_rejects_other_products_and_non_official_addresses(base):
    with pytest.raises(ValueError, match="official regional Ark"):
        normalize_ark_api_base(base)

# --- from test_ws_diagnostics.py ---

def test_format_ws_exception_diagnostics_includes_close_fields():
    exc = ConnectionClosedError(None, None)

    text = format_ws_diagnostics(describe_ws_exception(exc))

    assert "exc_type='ConnectionClosedError'" in text
    assert "message='no close frame received or sent'" in text
    assert "close_code=1006" in text
    assert "close_reason=''" in text
    assert "rcvd=None" in text
    assert "sent=None" in text

