from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.auth import auth_callback
from openjiuwen.core.foundation.tool.auth.auth import ToolAuthConfig

from jiuwenswarm.enterprise.config import config_provider as providers
from jiuwenswarm.common.mcp_config import preflight_mcp_server_reachable
from jiuwenswarm.common.mcp_dynamic_token_patch import (
    TOKEN_HEADER_NAME,
    TOKEN_HEADER_PREFIX,
    TOKEN_RESPONSE_FIELD,
    _ACTIVE_PROVIDERS,
    _LATEST_PROVIDERS,
    _MAX_PENDING_NOTIF_TOKENS,
    _MAX_PENDING_TURN_TOKENS,
    _PENDING_TAG_SERVER,
    _registry_token_url,
    _reset_dynamic_token_state_for_tests,
    _token_form_fields,
    apply_dispatched_credentials,
    apply_mcp_dynamic_token_patch,
    extract_dispatched_credentials,
    fetch_dynamic_token,
    is_dynamic_token_server,
    mark_dynamic_token_server,
    read_instance_credentials,
    unmark_dynamic_token_server,
    DynamicTokenAuthStrategy,
    DynamicTokenProvider,
    InstanceCredentials,
    McpDynamicTokenUnavailableError,
)


# ---------------------------------------------------------------------------
# 隔离：快照/恢复 SDK registry 槽位 + 清空补丁态
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry_and_state():
    saved = dict(auth_callback.AuthStrategyRegistry._strategies)
    yield
    auth_callback.AuthStrategyRegistry._strategies.clear()
    auth_callback.AuthStrategyRegistry._strategies.update(saved)
    _reset_dynamic_token_state_for_tests()


def _dispatch_credentials(
    client_id: str | None = "demo_agent1", client_secret: str | None = "demo-agent-secret1"
) -> None:
    """模拟企业配置下发实例级凭据（client_id=None = 合并未携带，清除）。"""
    if client_id is None:
        apply_dispatched_credentials(None)
        return
    entry: dict[str, str] = {"client_id": client_id}
    if client_secret is not None:
        entry["client_secret"] = client_secret
    enterprise = SimpleNamespace(
        agent_template={"data": {"custom_config": [entry]}},
        resource_id="bot-a",
        ref_template_id="tpl-a",
    )
    apply_dispatched_credentials(enterprise)


def _auth_config(tool_id: str | None = "srv-1") -> ToolAuthConfig:
    return ToolAuthConfig(
        auth_type="header_and_query",
        config={
            "auth_headers": {"Authorization": "Bearer static"},
            "auth_query_params": {"api-key": "static-key"},
        },
        tool_type="web-tools",
        tool_id=tool_id,
    )


# ---------------------------------------------------------------------------
# 1. 实例凭据解析
# ---------------------------------------------------------------------------


def test_credentials_none_when_section_missing():
    _dispatch_credentials(None)
    assert read_instance_credentials() is None


def test_credentials_none_when_incomplete():
    # 只有 client_id、缺 secret → None
    _dispatch_credentials("demo_agent1", None)
    assert read_instance_credentials() is None
    # 条目字段为空 → None
    apply_dispatched_credentials(
        SimpleNamespace(
            agent_template={
                "data": {"custom_config": [{"client_id": "", "client_secret": "s"}]}
            }
        )
    )
    assert read_instance_credentials() is None


def test_credentials_carried_by_dispatch():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    creds = read_instance_credentials()
    assert creds is not None
    assert creds.client_id == "demo_agent1"
    assert creds.client_secret == "demo-agent-secret1"


def test_credentials_hot_replaced_by_latest_dispatch():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    _dispatch_credentials("demo_agent2", "demo-agent-secret2")
    creds = read_instance_credentials()
    assert creds is not None
    assert creds.client_id == "demo_agent2"
    assert creds.client_secret == "demo-agent-secret2"


def test_credentials_cleared_by_sectionless_dispatch():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_dispatched_credentials(None)  # 合并未携带（企业配置未加载）→ 清除
    assert read_instance_credentials() is None


def test_code_contract_constants():
    """§2.2 契约写死为代码常量（url/凭据之外不可配置）。"""
    assert TOKEN_RESPONSE_FIELD == "access_token"
    assert TOKEN_HEADER_NAME == "X-Actor-Token"  # §2.1 智能体负载授权头
    assert TOKEN_HEADER_PREFIX == "Bearer "
    creds = read_instance_credentials() or InstanceCredentials(
        client_id="demo_agent1", client_secret="demo-agent-secret1"
    )
    form = _token_form_fields(creds)
    assert form["grant_type"] == "client_credentials"  # 固定字面量，不可配置


# ---------------------------------------------------------------------------
# 1.1 模板行提取（Agent 模板 data.custom_config 对象数组）
# ---------------------------------------------------------------------------


def test_extract_strips_whitespace():
    creds = extract_dispatched_credentials(
        {
            "data": {
                "custom_config": [
                    {"client_id": "  demo_agent1 ", "client_secret": "\tsecret "},
                ]
            }
        }
    )
    assert creds == {"client_id": "demo_agent1", "client_secret": "secret"}


def test_extract_takes_first_valid_entry():
    row = {
        "data": {
            "custom_config": [
                {"client_id": "demo_agent1"},  # 缺 secret → 跳过
                "not-a-dict",  # 非 dict → 跳过
                {"client_id": "demo_agent2", "client_secret": "demo-agent-secret2"},
                {"client_id": "demo_agent3", "client_secret": "demo-agent-secret3"},
            ]
        }
    }
    creds = extract_dispatched_credentials(row)
    assert creds == {"client_id": "demo_agent2", "client_secret": "demo-agent-secret2"}


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"data": None},
        {"data": {}},  # 无 custom_config
        {"data": {"custom_config": None}},
        {"data": {"custom_config": "not-a-list"}},
        {"data": {"custom_config": [{"client_id": "only-id"}]}},  # 缺 secret
        {"data": {"custom_config": [{"client_id": "", "client_secret": "s"}]}},
        {"data": {"custom_config": [{"client_secret": "only-secret"}]}},
        {"data": {"custom_config": [{"client_id": "id", "client_secret": "   "}]}},
    ],
)
def test_extract_none_when_absent_or_malformed(row):
    assert extract_dispatched_credentials(row) is None


@contextlib.contextmanager
def _patch_logs(level: int = logging.WARNING):
    """Collect records from the patch module's logger directly.

    ``caplog`` attaches to the root logger, and ``setup_logger`` sets
    ``propagate = False`` on ``jiuwenswarm`` at import, so its records only
    reach the root on pytest 9.1+ (ZYQ locks 9.0.x). Attaching here holds on
    every pytest version.
    """
    logger = logging.getLogger("jiuwenswarm.common.mcp_dynamic_token_patch")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level)
    original_level = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_apply_warns_when_custom_config_present_but_invalid():
    with _patch_logs() as records:
        apply_dispatched_credentials(
            SimpleNamespace(
                agent_template={"data": {"custom_config": [{"client_id": "only-id"}]}},
                resource_id="bot-a",
                ref_template_id="tpl-a",
            )
        )
    assert read_instance_credentials() is None
    assert any("no valid client_id/client_secret pair" in r.getMessage() for r in records)
    assert any("bot-a" in r.getMessage() for r in records)
    # 凭据值不得出现在任何日志里
    text = " ".join(r.getMessage() for r in records)
    assert "demo-agent-secret1" not in text
    assert "only-id" not in text


def test_apply_silent_when_custom_config_absent():
    with _patch_logs() as records:
        apply_dispatched_credentials(SimpleNamespace(agent_template={"enabled": True}))
    assert records == []


# ---------------------------------------------------------------------------
# 1.2 企业配置合并管线接线（config_provider._apply_enterprise_policy）
# ---------------------------------------------------------------------------


def _enterprise_policy(agent_template: dict | None) -> Any:
    from jiuwenswarm.enterprise.config.effective.schemas import (
        EffectiveEnterpriseConfig,
        RoutingContext,
    )

    return EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="group-a", bot_id="bot-a", user_id="user-a"),
        resource_id="bot-a",
        ref_template_id="tpl-a",
        agent_template=agent_template,
        mcp=[],
    )


def test_pipeline_feeds_credentials_from_agent_template(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    policy = _enterprise_policy(
        {
            "data": {
                "custom_config": [
                    {"client_id": "demo_agent1", "client_secret": "demo-agent-secret1"},
                ]
            }
        }
    )
    providers._apply_enterprise_policy(
        {"mcp": {"servers": [{"name": "local", "transport": "stdio"}]}}, policy
    )
    creds = read_instance_credentials()
    assert creds is not None
    assert creds.client_id == "demo_agent1"
    assert creds.client_secret == "demo-agent-secret1"


def test_pipeline_without_credentials_clears_dispatched_state(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    assert read_instance_credentials() is not None

    providers._apply_enterprise_policy({}, _enterprise_policy({"enabled": True}))
    assert read_instance_credentials() is None


# ---------------------------------------------------------------------------
# 2. apply 幂等 + registry 槽位
# ---------------------------------------------------------------------------


def test_apply_replaces_header_query_slot_and_is_idempotent():
    assert apply_mcp_dynamic_token_patch() is True
    assert apply_mcp_dynamic_token_patch() is True  # 二次调用 no-op
    strategies = auth_callback.AuthStrategyRegistry._strategies
    assert (
        strategies[auth_callback.AuthType.HEADER_AND_QUERY] is DynamicTokenAuthStrategy
    )
    # SSL 槽未被触碰（restful_api 路径不受影响）
    assert strategies[auth_callback.AuthType.SSL] is auth_callback.SSLAuthStrategy


# ---------------------------------------------------------------------------
# 3. strategy 三分支
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_falls_back_when_credentials_missing():
    _dispatch_credentials(None)
    apply_mcp_dynamic_token_patch()

    result = await DynamicTokenAuthStrategy().authenticate(_auth_config())
    assert result.success is True
    assert isinstance(result.auth_data["auth_provider"], auth_callback.AuthHeaderAndQueryProvider)


@pytest.mark.asyncio
async def test_strategy_falls_back_for_unmarked_server():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()

    result = await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-other"))
    assert result.success is True
    assert isinstance(result.auth_data["auth_provider"], auth_callback.AuthHeaderAndQueryProvider)


@pytest.mark.asyncio
async def test_strategy_falls_back_for_empty_tool_id():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server("srv-1", "http://kc.example/token")

    result = await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id=None))
    assert isinstance(result.auth_data["auth_provider"], auth_callback.AuthHeaderAndQueryProvider)


@pytest.mark.asyncio
async def test_strategy_returns_dynamic_provider_for_marked_server():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server("srv-1", "http://kc.example/token")

    result = await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-1"))
    assert result.success is True
    assert isinstance(result.auth_data["auth_provider"], DynamicTokenProvider)
    provider = result.auth_data["auth_provider"]
    # 静态头透传进 provider（X-Actor-Token 在 auth flow 里追加）
    assert provider.headers == {"Authorization": "Bearer static"}
    assert provider.query_params == {"api-key": "static-key"}
    # token 端点随标记登记
    assert provider._token_url == "http://kc.example/token"


# ---------------------------------------------------------------------------
# 4. async_auth_flow：静态铺放 + query 合并 + X-Actor-Token 追加 / 失败置空
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_flow_static_headers_then_dynamic_actor_header():
    provider = _make_provider(token="tok-1")
    request = httpx.Request("POST", "https://gw.example/mcp", json={"jsonrpc": "2.0"})

    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()

    # 静态头先铺（用户授权 Authorization 原样保留）
    assert sent.headers["X-Static"] == "s"
    assert sent.headers["Authorization"] == "Bearer static"
    # 动态头：智能体负载授权（§2.1 双矩阵，与静态 Authorization 并存）
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"
    # query 参数合并
    assert "qk=qv" in str(sent.url)


@pytest.mark.asyncio
async def test_auth_flow_sends_empty_header_when_fetch_fails():
    """核心语义：取不到 token 不阻断请求——动态头置空，照常发起调用。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    token_url = "http://token-svc.example/token"
    provider = DynamicTokenProvider(
        {"Authorization": "Bearer static", "X-Static": "s"},
        {},
        token_url,
        "srv-1",
    )

    async def _failing_fetch() -> str:
        raise McpDynamicTokenUnavailableError(token_url, "connection refused")

    provider._fetch_token = _failing_fetch  # type: ignore[method-assign]

    request = httpx.Request("POST", "https://gw.example/mcp", json={"jsonrpc": "2.0"})
    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()

    # 动态头存在但为空；其余静态头照常，请求未被阻断
    assert sent.headers["X-Actor-Token"] == ""
    assert sent.headers["Authorization"] == "Bearer static"
    assert sent.headers["X-Static"] == "s"


# ---------------------------------------------------------------------------
# 5. fetch：JSON / 纯文本 / 非 2xx / 异常 / 缺字段 / form 契约
# ---------------------------------------------------------------------------


class _FakeTokenClient:
    def __init__(self, status_code: int = 200, text: str = '{"access_token": "abc"}') -> None:
        self.status_code = status_code
        self.text = text
        self.calls: list[tuple[str, str, dict]] = []

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return SimpleNamespace(status_code=self.status_code, text=self.text)


TOKEN_URL = "http://token-svc.example/token"


def _creds() -> InstanceCredentials:
    return InstanceCredentials(
        client_id="demo_agent1", client_secret="demo-agent-secret1"
    )


@pytest.mark.asyncio
async def test_fetch_json_token():
    fake = _FakeTokenClient()
    result = await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake)

    assert result == "abc"
    method, url, kwargs = fake.calls[0]
    assert method == "POST"  # §2.2 固定 POST
    assert url == "http://token-svc.example/token"
    # grant_type 固定；client_id/client_secret 来自实例凭据
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "client_id": "demo_agent1",
        "client_secret": "demo-agent-secret1",
    }


@pytest.mark.asyncio
async def test_fetch_plain_text_fallback():
    fake = _FakeTokenClient(text="raw-token-value")
    assert await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake) == "raw-token-value"


@pytest.mark.asyncio
async def test_fetch_rejects_html_body():
    fake = _FakeTokenClient(text="<html><body>gateway error</body></html>")
    with pytest.raises(McpDynamicTokenUnavailableError):
        await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake)


@pytest.mark.asyncio
async def test_fetch_rejects_non_string_access_token():
    fake = _FakeTokenClient(text='{"access_token": {"nested": true}}')
    with pytest.raises(McpDynamicTokenUnavailableError):
        await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake)


@pytest.mark.asyncio
async def test_fetch_http_error():
    fake = _FakeTokenClient(status_code=401)
    with pytest.raises(McpDynamicTokenUnavailableError) as exc_info:
        await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake)
    assert "http://token-svc.example/token" in str(exc_info.value)
    assert "401" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_missing_field():
    fake = _FakeTokenClient(text='{"other": 1}')
    with pytest.raises(McpDynamicTokenUnavailableError):
        await fetch_dynamic_token(TOKEN_URL, _creds(), client=fake)


@pytest.mark.asyncio
async def test_fetch_transport_error():
    class _BoomClient:
        async def post(self, url: str, **kwargs):
            raise RuntimeError("connection refused")

    with pytest.raises(McpDynamicTokenUnavailableError) as exc_info:
        await fetch_dynamic_token(TOKEN_URL, _creds(), client=_BoomClient())
    assert "connection refused" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 6. single-flight：并发合并（非缓存）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_flight_merges_concurrent_fetches():
    provider = DynamicTokenProvider({}, {}, _creds(), "srv-1")
    calls = {"count": 0}

    async def _slow_fetch() -> str:
        calls["count"] += 1
        await asyncio.sleep(0.02)
        return f"tok-{calls['count']}"

    provider._fetch_token = _slow_fetch  # type: ignore[method-assign]

    results = await asyncio.gather(provider._get_token(), provider._get_token())
    assert calls["count"] == 1  # 并发合并为一次 fetch
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_negative_cache_window():
    """负缓存：fetch 失败后窗口内直接空头降级（不再打 token 服务），窗口后重试。"""
    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    provider = DynamicTokenProvider({}, {}, "http://token-svc.example/token", "srv-1")
    calls = {"count": 0}

    async def _failing_fetch() -> str:
        calls["count"] += 1
        raise McpDynamicTokenUnavailableError(
            "http://token-svc.example/token", "connection refused"
        )

    provider._fetch_token = _failing_fetch  # type: ignore[method-assign]

    # 第一次失败：进入负缓存窗口
    request = httpx.Request("POST", "https://gw.example/mcp")
    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()
    assert sent.headers["X-Actor-Token"] == ""
    assert calls["count"] == 1

    # 窗口内的后续请求：静默空头（不再打 token 服务）
    flow = provider.async_auth_flow(httpx.Request("POST", "https://gw.example/mcp"))
    sent = await flow.__anext__()
    await flow.aclose()
    assert sent.headers["X-Actor-Token"] == ""
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_sequential_requests_refetch():
    provider = DynamicTokenProvider({}, {}, _creds(), "srv-1")
    calls = {"count": 0}

    async def _fast_fetch() -> str:
        calls["count"] += 1
        return f"tok-{calls['count']}"

    provider._fetch_token = _fast_fetch  # type: ignore[method-assign]

    assert await provider._get_token() == "tok-1"
    assert await provider._get_token() == "tok-2"  # 顺序请求仍每次实时取
    assert calls["count"] == 2


# ---------------------------------------------------------------------------
# 7. registry refcount
# ---------------------------------------------------------------------------


def test_registry_refcount():
    url = "http://kc.example/token"
    assert mark_dynamic_token_server("srv-1", url) is True
    assert mark_dynamic_token_server("srv-1", url) is True
    assert is_dynamic_token_server("srv-1")

    unmark_dynamic_token_server("srv-1")
    assert is_dynamic_token_server("srv-1")  # 还有一方持有

    unmark_dynamic_token_server("srv-1")
    assert is_dynamic_token_server("srv-1") is False  # 归零摘除


def test_registry_unmark_unknown_is_noop():
    unmark_dynamic_token_server("never-marked")  # 不抛 KeyError
    assert is_dynamic_token_server("never-marked") is False


def test_mark_noop_when_url_empty():
    assert mark_dynamic_token_server("srv-1", "") is False  # 条目无 url → no-op
    assert is_dynamic_token_server("srv-1") is False
    assert mark_dynamic_token_server("", "") is False  # 空 id → no-op


def test_registry_holds_per_server_token_url():
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    assert mark_dynamic_token_server("srv-1", "http://kc-a/token")
    assert mark_dynamic_token_server("srv-2", "http://kc-b/token")
    # 每个网关各自的 token 端点独立登记
    from jiuwenswarm.common.mcp_dynamic_token_patch import _DYNAMIC_SERVERS

    assert _DYNAMIC_SERVERS["srv-1"]["token_url"] == "http://kc-a/token"
    assert _DYNAMIC_SERVERS["srv-2"]["token_url"] == "http://kc-b/token"


# ---------------------------------------------------------------------------
# 8. 重连回归（核心）：注册栈外查表仍命中动态 token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_outside_registration_stack_still_dynamic():
    """模拟 mcp_call_timeout_patch 强制重连：TOOL_AUTH 在 call_tool 上下文触发
    （无注册栈帧），策略按 tool_id 查注册表必须仍然命中动态 token。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server("srv-1", "http://kc.example/token")

    strategy = DynamicTokenAuthStrategy()
    first = await strategy.authenticate(_auth_config(tool_id="srv-1"))
    second = await strategy.authenticate(_auth_config(tool_id="srv-1"))

    assert first.success and second.success
    p1 = first.auth_data["auth_provider"]
    p2 = second.auth_data["auth_provider"]
    assert isinstance(p1, DynamicTokenProvider)
    assert isinstance(p2, DynamicTokenProvider)
    # 每次连接新建 provider（登记表按连接内聚——并发 session 交替不串身份）
    assert p1 is not p2
    assert p1.server_id == p2.server_id == "srv-1"


# ---------------------------------------------------------------------------
# 9. preflight 集成
# ---------------------------------------------------------------------------


def _gateway_config() -> McpServerConfig:
    return McpServerConfig(
        server_name="mock-gw",
        client_type="streamable-http",
        server_path="https://gw.example/mcp",
        auth_headers={"Authorization": "Bearer static"},
    )


class _FakeStreamResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


class _RecordingAsyncClient:
    requests: list[tuple[str, str, dict]] = []
    status_code = 200

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def get(self, url: str, **kwargs):
        _RecordingAsyncClient.requests.append(("GET", url, kwargs))
        return SimpleNamespace(status_code=_RecordingAsyncClient.status_code, text="ok")

    async def post(self, url: str, **kwargs):
        _RecordingAsyncClient.requests.append(("POST", url, kwargs))
        return SimpleNamespace(status_code=_RecordingAsyncClient.status_code, text="ok")

    def stream(self, method: str, url: str, **kwargs):
        _RecordingAsyncClient.requests.append((method, url, kwargs))
        return _FakeStreamResponse(_RecordingAsyncClient.status_code)


@pytest.fixture()
def _patch_httpx(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingAsyncClient)
    _RecordingAsyncClient.requests = []
    _RecordingAsyncClient.status_code = 200


@pytest.mark.asyncio
async def test_preflight_dynamic_token_success_adds_header(monkeypatch, _patch_httpx):
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _fake_fetch(token_url, creds, *, client=None, timeout_s=None, ssl_verify=True):
        return "fresh-tok"

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _fake_fetch)

    ok, reason = await preflight_mcp_server_reachable(
        _gateway_config(), dynamic_token_url="http://kc.example/token"
    )

    assert ok is True
    gateway_call = _RecordingAsyncClient.requests[0]
    # 静态用户授权头保留，动态负载授权头注入
    assert gateway_call[2]["headers"]["Authorization"] == "Bearer static"
    assert gateway_call[2]["headers"]["X-Actor-Token"] == "Bearer fresh-tok"


@pytest.mark.asyncio
async def test_preflight_dynamic_token_failure_probes_with_empty_header(monkeypatch, _patch_httpx):
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _failing_fetch(token_url, creds, *, client=None, timeout_s=None, ssl_verify=True):
        raise McpDynamicTokenUnavailableError(token_url, "connection refused")

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _failing_fetch)

    # 网关探活本身 200：token 服务挂了不影响注册判定
    ok, reason = await preflight_mcp_server_reachable(
        _gateway_config(), dynamic_token_url="http://kc.example/token"
    )

    assert ok is True
    gateway_call = _RecordingAsyncClient.requests[0]
    assert gateway_call[2]["headers"]["X-Actor-Token"] == ""  # 动态头置空
    assert gateway_call[2]["headers"]["Authorization"] == "Bearer static"  # 静态头保留


@pytest.mark.asyncio
async def test_preflight_default_does_not_fetch_token(monkeypatch, _patch_httpx):
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _must_not_fetch(token_url, creds, *, client=None, timeout_s=None, ssl_verify=True):
        raise AssertionError("dynamic_token_url=None 时不应取 token")

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _must_not_fetch)

    ok, _reason = await preflight_mcp_server_reachable(_gateway_config())

    assert ok is True
    # 静态头照旧，无动态头
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer static"
    assert "X-Actor-Token" not in headers


# ---------------------------------------------------------------------------
# 10. 凭据热更（provider 现读实例凭据）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_picks_up_hot_reloaded_credentials(monkeypatch):
    """凭据热更：provider 缓存复用，但实例凭据每次 fetch 现读——立即生效。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    token_url = "http://token-svc.example/token"
    provider = DynamicTokenProvider({}, {}, token_url, "srv-1")

    captured: dict[str, Any] = {}

    async def _capture(client: Any, token_url: str, creds: InstanceCredentials) -> str:
        captured["client_id"] = creds.client_id
        return "tok"

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "_request_token", _capture)

    # 凭据热更：上游重发快照（换 client）
    _dispatch_credentials("demo_agent2", "demo-agent-secret2")

    request = httpx.Request("POST", "https://gw.example/mcp")
    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()

    assert captured["client_id"] == "demo_agent2"  # 热更后的凭据
    assert sent.headers["X-Actor-Token"] == "Bearer tok"


# ---------------------------------------------------------------------------
# 11. 逐轮用户授权：凭据按连接绑定（写流代理 → provider 实例登记表 → 消费）
# ---------------------------------------------------------------------------
#
# HTTP 实际由建连时的后台任务（post_writer）发送，provider 在那个上下文里
# 无法知道"这个请求是谁发的"。写流代理在调用者上下文按消息形态登记，登记
# 表内聚在**本连接的 provider 实例**上（TOOL_AUTH 每次连接新建）：并发
# session 交替时各连接的请求 id 空间 / 通知 FIFO / last-known 互不可见，
# 同 id 撞号、通知错配、GET 重连串身份从根上消除。


@contextlib.contextmanager
def _turn_ext(values: dict[str, str] | None):
    """模拟 AgentServer 入口 lift：把请求级 ext 写入 ContextVar，结束还原。"""
    from jiuwenswarm.common import request_ext

    token = request_ext.set_current(values)
    try:
        yield
    finally:
        request_ext.reset_ext(token)


def _jsonrpc_request(request_id: int = 1) -> httpx.Request:
    """工具调用 POST（与 SDK post_writer 的请求体同构）。"""
    return httpx.Request(
        "POST",
        "https://gw.example/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {}},
    )


def _initialize_request() -> httpx.Request:
    return httpx.Request(
        "POST",
        "https://gw.example/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )


def _notification_request() -> httpx.Request:
    return httpx.Request(
        "POST",
        "https://gw.example/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )


def _response_request(request_id: int = 1) -> httpx.Request:
    """响应 POST（回服务器 ping 等）：有 id 无 method。"""
    return httpx.Request(
        "POST",
        "https://gw.example/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}},
    )


async def _run_flow(provider: DynamicTokenProvider, request: httpx.Request) -> httpx.Request:
    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()
    return sent


def _bare_provider(server_id: str = "srv-1", fetch_token: str = "tok-1") -> DynamicTokenProvider:
    """构造测试用 provider（不触发企业凭据读取，fetch 固定返回）。"""
    provider = DynamicTokenProvider(
        {"Authorization": "Bearer static", "X-Static": "s"},
        {"qk": "qv"},
        "http://token-svc.example/token",
        server_id,
    )

    async def _fake_fetch() -> str:
        return fetch_token

    provider._fetch_token = _fake_fetch  # type: ignore[method-assign]
    return provider


def _make_provider(token: str = "tok-1") -> DynamicTokenProvider:
    """带企业凭据的 provider（原 _make_provider 口径，供 actor 头断言用）。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    return _bare_provider("srv-1", fetch_token=token)


def test_current_user_authorization_reads_ext_value():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization

    with _turn_ext({"X-User-Token": "Bearer user-tok-1"}):
        assert _current_user_authorization() == "Bearer user-tok-1"


def test_current_user_authorization_empty_without_ext():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization

    with _turn_ext(None):
        assert _current_user_authorization() == ""
    with _turn_ext({"Other-Field": "x"}):
        assert _current_user_authorization() == ""


@pytest.mark.parametrize(
    "bad",
    [
        "Bearer a\nEvil: 1",
        "   ",
        "Bearer\ttok",
        "Bearer tok\x7f",
        "a\x00b",
        "Bearer token-中文",
        123,
        4.5,
        ["Bearer x"],
        {"t": 1},
        True,
        None,
    ],
)
def test_current_user_authorization_rejects_unsafe_values(bad):
    """换行/控制字符/非可打印 ASCII/非字符串值拒绝注入。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization

    with _turn_ext({"X-User-Token": bad}):
        assert _current_user_authorization() == ""


def test_current_user_authorization_max_length_boundary():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _MAX_USER_TOKEN_LEN,
        _current_user_authorization,
    )

    exact = "x" * _MAX_USER_TOKEN_LEN
    with _turn_ext({"X-User-Token": exact}):
        assert _current_user_authorization() == exact


# ---------------------------------------------------------------------------
# 11.1 provider 实例登记表：精确 / FIFO / 哨兵 / 容量 / last-known
# ---------------------------------------------------------------------------


def test_request_token_record_then_consume_exact():
    provider = _bare_provider()
    provider.record_request_token(7, "Bearer t7")
    assert provider._consume_request_token(7) == "Bearer t7"
    assert provider._consume_request_token(7) == ""  # 一次性消费


def test_request_token_same_id_fifo_for_retries():
    """同连接同 id 重复登记（重试类场景）按 FIFO 消费。"""
    provider = _bare_provider()
    provider.record_request_token(1, "Bearer first")
    provider.record_request_token(1, "Bearer second")
    assert provider._consume_request_token(1) == "Bearer first"
    assert provider._consume_request_token(1) == "Bearer second"


def test_request_token_pending_cap_evicts_oldest():
    provider = _bare_provider()
    for i in range(_MAX_PENDING_TURN_TOKENS + 4):
        provider.record_request_token(i, f"Bearer t{i}")
    assert provider._consume_request_token(0) == ""  # 最旧被逐出
    last = _MAX_PENDING_TURN_TOKENS + 3
    assert provider._consume_request_token(last) == f"Bearer t{last}"


def test_notification_fifo_pairs_sends_with_posts():
    provider = _bare_provider()
    assert provider._pop_notification_token() == ""  # 空队列出队 → 无令牌
    provider.push_notification_token("Bearer conn-A")
    provider.push_notification_token("")  # 无令牌通知的哨兵
    provider.push_notification_token("Bearer conn-B")
    assert provider._pop_notification_token() == "Bearer conn-A"
    assert provider._pop_notification_token() == ""  # 哨兵：该通知不带令牌
    assert provider._pop_notification_token() == "Bearer conn-B"
    assert provider._pop_notification_token() == ""


def test_notification_fifo_cap_evicts_oldest():
    provider = _bare_provider()
    for i in range(_MAX_PENDING_NOTIF_TOKENS + 4):
        provider.push_notification_token(f"Bearer n{i}")
    assert provider._pop_notification_token() == "Bearer n4"  # 最旧 4 条被逐出


def test_last_known_tracks_latest_non_empty():
    provider = _bare_provider()
    assert provider._last_turn_token == ""
    provider.record_request_token(1, "Bearer first")
    provider.record_request_token(2, "Bearer second")
    provider.push_notification_token("")  # 哨兵不更新 last-known
    assert provider._last_turn_token == "Bearer second"


def test_two_sessions_same_id_isolated():
    """并发 session 核心：两连接同 id 撞号——各 provider 登记互不可见，
    消费顺序无关，各自拿到各自的令牌。"""
    provider_a = _bare_provider("srv-shared")
    provider_b = _bare_provider("srv-shared")

    provider_a.record_request_token(1, "Bearer U1")  # session A 的 id=1
    provider_b.record_request_token(1, "Bearer U2")  # session B 的 id=1（撞号）

    # B 的 POST 先出网（顺序反转也不串）
    assert provider_b._consume_request_token(1) == "Bearer U2"
    assert provider_a._consume_request_token(1) == "Bearer U1"


def test_two_sessions_notification_fifo_isolated():
    provider_a = _bare_provider("srv-shared")
    provider_b = _bare_provider("srv-shared")
    provider_a.push_notification_token("Bearer U1")
    provider_b.push_notification_token("Bearer U2")
    # B 的通知 POST 先出网
    assert provider_b._pop_notification_token() == "Bearer U2"
    assert provider_a._pop_notification_token() == "Bearer U1"


def test_two_sessions_last_known_isolated():
    """GET 重连（last-known）按连接取值：A 的重连不带走 B 的令牌。"""
    provider_a = _bare_provider("srv-shared")
    provider_b = _bare_provider("srv-shared")
    provider_a.record_request_token(1, "Bearer U1")
    provider_b.record_request_token(1, "Bearer U2")
    assert provider_a._last_turn_token == "Bearer U1"
    assert provider_b._last_turn_token == "Bearer U2"


# ---------------------------------------------------------------------------
# 11.2 写流代理：请求按 id 登记、通知入队（无令牌入哨兵）、响应跳过
# ---------------------------------------------------------------------------


class _FakeRequestRoot:
    def __init__(self, request_id: Any = None, method: str | None = None) -> None:
        self.id = request_id
        self.method = method


class _FakeSessionMessage:
    def __init__(self, root: Any) -> None:
        self.message = SimpleNamespace(root=root)


class _FakeSendStream:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed = False

    async def send(self, item: Any) -> None:
        self.sent.append(item)

    async def aclose(self) -> None:
        self.closed = True


def _tagged_stream(provider: DynamicTokenProvider) -> tuple[Any, _FakeSendStream]:
    from jiuwenswarm.common.mcp_dynamic_token_patch import _TurnTokenTaggingStream

    inner = _FakeSendStream()
    return _TurnTokenTaggingStream(inner, provider), inner


@pytest.mark.asyncio
async def test_tagging_stream_records_outbound_request_token():
    provider = _bare_provider()
    stream, inner = _tagged_stream(provider)
    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(5, "tools/call")))

    assert len(inner.sent) == 1  # 消息照常下发
    assert provider._consume_request_token(5) == "Bearer turn-A"


@pytest.mark.asyncio
async def test_tagging_stream_notification_pushes_sentinel_without_ext():
    """无令牌的通知也入队（"" 哨兵）：其 POST 不误配他人登记值（评审①）。"""
    provider = _bare_provider()
    stream, _inner = _tagged_stream(provider)
    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(5, "tools/call")))
    await stream.send(
        _FakeSessionMessage(_FakeRequestRoot(None, "notifications/cancelled"))
    )

    # 无令牌通知的 POST 出队得到哨兵 ""，不继承 A；A 的登记不受影响
    assert provider._pop_notification_token() == ""
    assert provider._consume_request_token(5) == "Bearer turn-A"


@pytest.mark.asyncio
async def test_tagging_stream_skips_response_and_bool_id():
    """响应（有 id 无 method）不登记；非法 id 不进任何队列（防污染配对）。"""
    provider = _bare_provider()
    stream, _inner = _tagged_stream(provider)
    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(7, None)))  # 响应
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(True, "tools/call")))

    assert provider._consume_request_token(7) == ""
    assert provider._pop_notification_token() == ""
    assert provider._last_turn_token == ""


@pytest.mark.asyncio
async def test_tagging_stream_delegates_and_install_idempotent():
    provider = _bare_provider()
    stream, inner = _tagged_stream(provider)
    await stream.aclose()
    assert inner.closed is True

    session = SimpleNamespace(_write_stream=_FakeSendStream())
    from jiuwenswarm.common.mcp_dynamic_token_patch import _install_turn_token_tagging

    _install_turn_token_tagging(session, provider)
    first = session._write_stream
    _install_turn_token_tagging(session, provider)
    assert session._write_stream is first  # 不二次包装


# ---------------------------------------------------------------------------
# 11.3 provider 消费：按消息形态精确归属（评审①②③回归）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_flow_overrides_with_exact_registered_token():
    provider = _make_provider()
    provider.record_request_token(1, "Bearer turn-A")

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer turn-A"  # 完整值（含 Bearer 前缀）
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"  # 平级动态头并存
    assert sent.headers["X-Static"] == "s"
    assert "qk=qv" in str(sent.url)  # auth_query_params 合并不受影响


@pytest.mark.asyncio
async def test_auth_flow_no_token_call_does_not_inherit_inflight_token():
    """评审①回归：A 的调用在途（已登记未出网），无令牌的并发调用不带 A。"""
    provider = _make_provider()
    provider.record_request_token(1, "Bearer A")  # A 已登记、POST 尚未出网

    sent_no_token = await _run_flow(provider, _jsonrpc_request(request_id=2))
    assert sent_no_token.headers["Authorization"] == "Bearer static"  # 不带 A

    sent_a = await _run_flow(provider, _jsonrpc_request(request_id=1))
    assert sent_a.headers["Authorization"] == "Bearer A"  # A 出网精确命中


@pytest.mark.asyncio
async def test_auth_flow_notification_pairs_with_its_own_send():
    """评审②回归：initialize 带用户令牌，notifications/initialized 同样带上。"""
    provider = _make_provider()
    stream, _inner = _tagged_stream(provider)

    # 连接期：initialize（请求）+ notifications/initialized（通知）都在
    # 调用者上下文经写流发出（真实 SDK 的 initialize() 正是如此）
    with _turn_ext({"X-User-Token": "Bearer conn-tok"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(0, "initialize")))
        await stream.send(
            _FakeSessionMessage(_FakeRequestRoot(None, "notifications/initialized"))
        )

    sent_init = await _run_flow(provider, _initialize_request())
    assert sent_init.headers["Authorization"] == "Bearer conn-tok"

    sent_notif = await _run_flow(provider, _notification_request())
    assert sent_notif.headers["Authorization"] == "Bearer conn-tok"  # FIFO 配对


@pytest.mark.asyncio
async def test_auth_flow_notification_without_token_gets_sentinel():
    """无令牌连接的通知：出队得到哨兵 → 静态快照，不继承他人登记值。"""
    provider = _make_provider()
    provider.record_request_token(1, "Bearer A")  # 他人在途
    provider.push_notification_token("")  # 无令牌通知的哨兵

    sent = await _run_flow(provider, _notification_request())
    assert sent.headers["Authorization"] == "Bearer static"


@pytest.mark.asyncio
async def test_auth_flow_sse_reconnect_uses_latest_token():
    """评审③回归：令牌换成 B 后，SSE 重连（GET）带 B，不再用旧值 A。"""
    provider = _make_provider()
    stream, _inner = _tagged_stream(provider)

    # 连接 A：initialize 登记 A → 初始 GET 带 A
    with _turn_ext({"X-User-Token": "Bearer A"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(0, "initialize")))
    sent_get1 = await _run_flow(provider, httpx.Request("GET", "https://gw.example/mcp"))
    assert sent_get1.headers["Authorization"] == "Bearer A"

    # 换成 B：B 的调用精确消费
    with _turn_ext({"X-User-Token": "Bearer B"}):
        await stream.send(_FakeSessionMessage(_FakeRequestRoot(1, "tools/call")))
    sent_call = await _run_flow(provider, _jsonrpc_request(request_id=1))
    assert sent_call.headers["Authorization"] == "Bearer B"

    # SSE 重连（GET）→ last-known = B
    sent_get2 = await _run_flow(provider, httpx.Request("GET", "https://gw.example/mcp"))
    assert sent_get2.headers["Authorization"] == "Bearer B"


@pytest.mark.asyncio
async def test_auth_flow_response_post_does_not_consume_id_registry():
    """响应 POST（result，id 撞号）不消费客户端请求登记：其 id 属服务器空间。"""
    provider = _make_provider()
    provider.record_request_token(5, "Bearer A")

    await _run_flow(provider, _response_request(request_id=5))  # last-known
    sent_req = await _run_flow(provider, _jsonrpc_request(request_id=5))
    assert sent_req.headers["Authorization"] == "Bearer A"  # 登记仍可精确消费


@pytest.mark.asyncio
async def test_auth_flow_delete_uses_last_known():
    provider = _make_provider()
    provider.record_request_token(1, "Bearer A")

    sent = await _run_flow(provider, httpx.Request("DELETE", "https://gw.example/mcp"))
    assert sent.headers["Authorization"] == "Bearer A"


@pytest.mark.asyncio
async def test_auth_flow_unparseable_body_falls_back_to_last_known():
    provider = _make_provider()
    provider.record_request_token(1, "Bearer A")

    sent = await _run_flow(
        provider,
        httpx.Request("POST", "https://gw.example/mcp", content=b"not-json"),
    )
    assert sent.headers["Authorization"] == "Bearer A"


@pytest.mark.asyncio
async def test_auth_flow_keeps_static_without_any_registration():
    """预热连接未发生过带令牌的请求：保留条目静态快照头。"""
    provider = _make_provider()

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer static"
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_auth_flow_actor_failure_with_user_token():
    """取 actor token 失败 + 用户令牌在途：双头语义互不干扰。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    token_url = "http://token-svc.example/token"
    provider = DynamicTokenProvider(
        {"Authorization": "Bearer static"}, {}, token_url, "srv-1"
    )

    async def _failing_fetch() -> str:
        raise McpDynamicTokenUnavailableError(token_url, "connection refused")

    provider._fetch_token = _failing_fetch  # type: ignore[method-assign]
    provider.record_request_token(1, "Bearer turn-A")

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))
    assert sent.headers["Authorization"] == "Bearer turn-A"  # 用户授权不受影响
    assert sent.headers["X-Actor-Token"] == ""  # 动态头置空降级


@pytest.mark.asyncio
async def test_auth_flow_adds_authorization_when_static_empty():
    """条目没有静态 auth_headers：本轮用户令牌照样补上 Authorization。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    provider = DynamicTokenProvider({}, {}, "http://token-svc.example/token", "srv-1")

    async def _fake_fetch() -> str:
        return "tok-1"

    provider._fetch_token = _fake_fetch  # type: ignore[method-assign]
    provider.record_request_token(1, "Bearer turn-A")

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))
    assert sent.headers["Authorization"] == "Bearer turn-A"
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_auth_flow_overrides_lowercase_authorization_key():
    """静态头键为小写 authorization：覆盖后仅一个 authorization 头且为新值。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    provider = DynamicTokenProvider(
        {"authorization": "Bearer static"},
        {},
        "http://token-svc.example/token",
        "srv-1",
    )

    async def _fake_fetch() -> str:
        return "tok-1"

    provider._fetch_token = _fake_fetch  # type: ignore[method-assign]
    provider.record_request_token(1, "Bearer turn-A")

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))
    assert sent.headers.get_list("authorization") == ["Bearer turn-A"]  # 无重复头
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


# ---------------------------------------------------------------------------
# 11.4 并发 session 交替（真实 asyncio，两连接同网关）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sessions_alternating_calls_exact():
    """两 session 交替调用同一网关（各自连接、同 id 空间）：交替与并发均精确。"""
    provider_a = _bare_provider("srv-shared")
    provider_b = _bare_provider("srv-shared")
    stream_a, _sa = _tagged_stream(provider_a)
    stream_b, _sb = _tagged_stream(provider_b)

    async def call(stream, provider, request_id: int, token: str) -> str:
        from jiuwenswarm.common import request_ext

        tok = request_ext.set_current({"X-User-Token": token})
        try:
            await stream.send(_FakeSessionMessage(_FakeRequestRoot(request_id, "tools/call")))
            sent = await _run_flow(provider, _jsonrpc_request(request_id=request_id))
            return sent.headers["Authorization"]
        finally:
            request_ext.reset_ext(tok)

    # 顺序交替 A → B → A → B
    assert await call(stream_a, provider_a, 1, "Bearer U1") == "Bearer U1"
    assert await call(stream_b, provider_b, 1, "Bearer U2") == "Bearer U2"
    assert await call(stream_a, provider_a, 2, "Bearer U1") == "Bearer U1"
    assert await call(stream_b, provider_b, 2, "Bearer U2") == "Bearer U2"

    # 并发重叠（同 id 撞号 + 完全在途重叠）
    r1, r2 = await asyncio.gather(
        call(stream_a, provider_a, 3, "Bearer U1"),
        call(stream_b, provider_b, 3, "Bearer U3"),
    )
    assert r1 == "Bearer U1"
    assert r2 == "Bearer U3"


@pytest.mark.asyncio
async def test_concurrent_sessions_stress_interleaved():
    """20 session × 10 轮并发交替：全部精确归属，结束后登记清空。"""
    providers = [_bare_provider("srv-stress") for _ in range(20)]
    streams = [_tagged_stream(p)[0] for p in providers]

    async def call(session_idx: int, request_id: int, token: str) -> str:
        from jiuwenswarm.common import request_ext

        tok = request_ext.set_current({"X-User-Token": token})
        try:
            await streams[session_idx].send(
                _FakeSessionMessage(_FakeRequestRoot(request_id, "tools/call"))
            )
            sent = await _run_flow(providers[session_idx], _jsonrpc_request(request_id=request_id))
            return sent.headers["Authorization"]
        finally:
            request_ext.reset_ext(tok)

    tasks = []
    for s in range(20):
        for k in range(10):
            rid = s * 100 + k  # 各 session id 空间独立，撞号也安全
            tasks.append(call(s, rid % 7, f"Bearer S{s}T{k}"))  # 故意让 id 跨 session 撞号
    results = await asyncio.gather(*tasks)
    assert all(r.startswith("Bearer S") for r in results)
    # 各 session 登记清空（全部消费完毕）
    assert all(not p._pending_turn_tokens for p in providers)


# ---------------------------------------------------------------------------
# 11.5 接缝安装：authenticate 直持 provider → ClientSession 构造消费安装
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_sets_pending_provider_marker():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _PENDING_TAG_SERVER

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server("srv-1", "http://kc.example/token")

    assert _PENDING_TAG_SERVER.get() is None
    await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-1"))
    marked = _PENDING_TAG_SERVER.get()
    assert marked is not None and marked.server_id == "srv-1"
    assert marked is _LATEST_PROVIDERS["srv-1"]
    _PENDING_TAG_SERVER.set(None)  # 测试上下文清理


@pytest.mark.asyncio
async def test_authenticate_creates_fresh_provider_per_connect():
    """每次 connect（含自愈重连）新建 provider——登记表按连接隔离的前提。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server("srv-1", "http://kc.example/token")

    first = await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-1"))
    second = await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-1"))
    p1 = first.auth_data["auth_provider"]
    p2 = second.auth_data["auth_provider"]
    assert isinstance(p1, DynamicTokenProvider) and isinstance(p2, DynamicTokenProvider)
    assert p1 is not p2  # 不再跨连接复用（旧实现按 sid 缓存复用）
    _PENDING_TAG_SERVER.set(None)


@pytest.mark.asyncio
async def test_authenticate_fallback_clears_stale_marker():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _PENDING_TAG_SERVER

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()  # srv-other 未标记 → 回退原策略

    _PENDING_TAG_SERVER.set(object())  # 陈旧标记
    await DynamicTokenAuthStrategy().authenticate(_auth_config(tool_id="srv-other"))
    assert _PENDING_TAG_SERVER.get() is None


@pytest.mark.asyncio
async def test_client_session_init_installs_tagging_before_first_send():
    """接缝一（主）：ClientSession 构造消费归属标记（直持 provider）装代理。

    用真实 mcp ClientSession + anyio 内存流验证：构造完成后写流已是登记
    代理且绑定本次连接的 provider；标记一次性消费，后续无关会话不安装。
    """
    import anyio
    from mcp.client.session import ClientSession

    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _PENDING_TAG_SERVER,
        _TurnTokenTaggingStream,
    )

    apply_mcp_dynamic_token_patch()  # 安装 ClientSession.__init__ 接缝

    inbound_send, inbound_receive = anyio.create_memory_object_stream(10)
    outbound_send, _outbound_receive = anyio.create_memory_object_stream(10)

    provider = _bare_provider("srv-real")
    _PENDING_TAG_SERVER.set(provider)
    session = ClientSession(read_stream=inbound_receive, write_stream=outbound_send)
    assert isinstance(session._write_stream, _TurnTokenTaggingStream)
    assert session._write_stream._provider is provider
    assert session._write_stream._inner is outbound_send
    assert _PENDING_TAG_SERVER.get() is None  # 一次性消费

    session2 = ClientSession(read_stream=inbound_receive, write_stream=outbound_send)
    assert not isinstance(session2._write_stream, _TurnTokenTaggingStream)


def test_session_tagging_patched_on_real_classes():
    """接缝幂等标记：ClientSession.__init__ 与两个 SDK client 的 connect。"""
    from mcp.client.session import ClientSession
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    apply_mcp_dynamic_token_patch()

    assert getattr(ClientSession.__init__, "_jw_turn_token_tagged", False)
    for cls, name in (
        (StreamableHttpClient, "connect"),
        (SseClient, "_do_connect"),
    ):
        method = getattr(cls, name)
        assert getattr(method, "_jw_turn_token_tagged", False), (cls.__name__, name)
        assert asyncio.iscoroutinefunction(method), (cls.__name__, name)


def test_reapply_does_not_double_wrap_after_reset():
    """reset（_PATCHED=False）后再 apply：接缝不二次包装（标记判重）。"""
    from mcp.client.session import ClientSession

    apply_mcp_dynamic_token_patch()
    first = ClientSession.__init__
    _reset_dynamic_token_state_for_tests()  # _PATCHED=False，但类包装仍在
    apply_mcp_dynamic_token_patch()
    assert ClientSession.__init__ is first  # 不叠加新包装层


def test_unmark_evicts_all_session_providers():
    """unmark 归零：逐出该网关的全部存活 provider（多会话）并清 latest。"""
    provider_a = _bare_provider("srv-evict")
    provider_b = _bare_provider("srv-evict")
    other = _bare_provider("srv-other")
    _ACTIVE_PROVIDERS.update({provider_a, provider_b, other})
    _LATEST_PROVIDERS["srv-evict"] = provider_b

    assert mark_dynamic_token_server("srv-evict", "http://kc.example/token")
    unmark_dynamic_token_server("srv-evict")

    assert provider_a not in _ACTIVE_PROVIDERS
    assert provider_b not in _ACTIVE_PROVIDERS
    assert other in _ACTIVE_PROVIDERS  # 其他网关不受影响
    assert "srv-evict" not in _LATEST_PROVIDERS
    _ACTIVE_PROVIDERS.discard(other)  # 测试清理


# ---------------------------------------------------------------------------
# 11.6 preflight：用户授权覆盖仅限双 token 网关（评审问题回归）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_dual_token_gateway_carries_turn_user_token(monkeypatch, _patch_httpx):
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _fake_fetch(token_url, creds, *, client=None, timeout_s=None, ssl_verify=True):
        return "fresh-tok"

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _fake_fetch)

    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(
            _gateway_config(), dynamic_token_url="http://kc.example/token"
        )

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer turn-user-tok"
    assert headers["X-Actor-Token"] == "Bearer fresh-tok"


@pytest.mark.asyncio
async def test_preflight_plain_gateway_keeps_static_authorization(_patch_httpx):
    """普通 MCP 服务（无 dynamic_token_url）绝不被覆盖。"""
    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(_gateway_config())

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer static"
    assert "X-Actor-Token" not in headers


@pytest.mark.asyncio
async def test_preflight_marked_gateway_without_creds_keeps_static(_patch_httpx):
    _dispatch_credentials(None)

    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(
            _gateway_config(), dynamic_token_url="http://kc.example/token"
        )

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer static"
    assert "X-Actor-Token" not in headers


@pytest.mark.asyncio
async def test_preflight_dual_token_actor_failure_with_user_token(monkeypatch, _patch_httpx):
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _failing_fetch(token_url, creds, *, client=None, timeout_s=None, ssl_verify=True):
        raise McpDynamicTokenUnavailableError(token_url, "connection refused")

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _failing_fetch)

    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(
            _gateway_config(), dynamic_token_url="http://kc.example/token"
        )

    assert ok is True  # token 服务失败不判不可达
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer turn-user-tok"
    assert headers["X-Actor-Token"] == ""


# ---------------------------------------------------------------------------
# 11.7 请求级 ext 抬升链路（agent_ws_server 入口依赖的原语）
# ---------------------------------------------------------------------------


def test_request_ext_lift_roundtrip():
    from jiuwenswarm.common.request_ext import get_ext, lift_from_metadata, reset_ext

    assert lift_from_metadata(None) is None
    assert lift_from_metadata({"no-ext": 1}) is None
    assert lift_from_metadata({"ext": "not-a-dict"}) is None

    token = lift_from_metadata(
        {"ext": {"X-User-Token": "Bearer t", "X-Trace-Tag": "trace"}}
    )
    assert token is not None
    assert get_ext() == {"X-User-Token": "Bearer t", "X-Trace-Tag": "trace"}
    reset_ext(token)
    assert get_ext() == {}


# ---------------------------------------------------------------------------
# 12. 真实连接生命周期
#
# 前文的接缝测试只验证了 ClientSession **构造**与直接 send；真实 SDK 的
# ``BaseSession._receive_loop`` 会 ``async with (read_stream, write_stream)``
# 持有写流（上下文管理器协议按类型查找特殊方法），且 SseClient 的
# connect/reconnect 跑在长期 owner task 中（ContextVar 为创建时快照）。
# 本节用真实 ClientSession / SseClient / StreamableHttpClient + 本地
# MCP 服务器覆盖这两条真实路径。
# ---------------------------------------------------------------------------


def _auth_for(records: list[dict[str, Any]], method: str) -> str | None:
    """取最后一条指定 JSON-RPC 方法（或 ``GET /sse``）请求的 Authorization。"""
    for record in reversed(records):
        if record["method"] == method:
            return record["authorization"]
    return None


async def _wait_record(
    records: list[dict[str, Any]], method: str, timeout: float = 5.0
) -> None:
    """轮询等待指定方法的探针记录落盘。

    POST 由传输的 post_writer 异步发送，``connect``/``reconnect`` 返回不保证
    请求已发出——直接断言会与在途请求竞态（旧会话晚到的握手请求会污染
    ``records.clear()`` 之后的记录）。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _auth_for(records, method) is not None:
            return
        await asyncio.sleep(0.05)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _serve(app: Any):
    """在独立端口运行真实 uvicorn 服务器，yield (base_url, records)。"""
    import uvicorn
    from sse_starlette.sse import AppStatus

    # A previous in-process Uvicorn shutdown leaves this process-wide flag set.
    # Each probe server needs a fresh SSE lifetime; restore the caller's state.
    previous_should_exit = AppStatus.should_exit
    AppStatus.should_exit = False
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn test server failed to start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:  # noqa: BLE001 - 关停失败不影响断言结果
            task.cancel()
        finally:
            AppStatus.should_exit = previous_should_exit


def _build_sse_gateway_app(records: list[dict[str, Any]]) -> Any:
    """最小真实 SSE MCP 服务器（initialize/tools）并记录每个请求的鉴权头。"""
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/messages/")
    server = Server("dynamic-token-sse-probe")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="echo",
                description="echo",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [types.TextContent(type="text", text="ok")]

    def _record(method: str, request_id: Any, headers: dict[str, str]) -> None:
        records.append(
            {
                "method": method,
                "id": request_id,
                "authorization": headers.get("authorization"),
                "actor": headers.get("x-actor-token"),
            }
        )

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (read, write):
            await server.run(
                read, write, server.create_initialization_options()
            )

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        headers = {
            key.decode().lower(): value.decode()
            for key, value in scope.get("headers", [])
        }
        if path == "/sse":
            _record("GET /sse", None, headers)
            await handle_sse(scope, receive, send)
            return
        if path.startswith("/messages"):
            chunks: list[bytes] = []

            async def recv():
                message = await receive()
                if message.get("type") == "http.request":
                    chunks.append(message.get("body", b"") or b"")
                return message

            await sse.handle_post_message(scope, recv, send)
            try:
                payload = json.loads(b"".join(chunks))
            except (TypeError, ValueError):
                payload = {}
            _record(
                str(payload.get("method") or "POST"),
                payload.get("id"),
                headers,
            )
            return
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


def _build_streamable_gateway_app(records: list[dict[str, Any]]) -> Any:
    """最小真实 streamable-http MCP 服务器（FastMCP）并记录请求头。"""
    from mcp.server.fastmcp import FastMCP

    gateway = FastMCP("dynamic-token-streamable-probe")

    @gateway.tool()
    def echo(text: str = "x") -> str:
        return "ok"

    inner = gateway.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = {
            key.decode().lower(): value.decode()
            for key, value in scope.get("headers", [])
        }
        chunks: list[bytes] = []

        async def recv():
            message = await receive()
            if message.get("type") == "http.request":
                chunks.append(message.get("body", b"") or b"")
            return message

        await inner(scope, recv, send)
        try:
            payload = json.loads(b"".join(chunks))
        except (TypeError, ValueError):
            payload = {}
        records.append(
            {
                "method": str(payload.get("method") or scope.get("method")),
                "id": payload.get("id"),
                "authorization": headers.get("authorization"),
                "actor": headers.get("x-actor-token"),
            }
        )

    return app


def _enable_real_dynamic_gateway(monkeypatch, server_id: str, actor: str = "actor-tok") -> None:
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server(server_id, "http://token.invalid/token")

    async def _fake_request_token(client: Any, token_url: str, creds: Any) -> str:
        return actor

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "_request_token", _fake_request_token)


def _sse_server_config(url: str, server_id: str) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        server_name=server_id,
        server_path=url,
        client_type="sse",
        auth_headers={"Authorization": "Bearer static"},
        auth_query_params={},
    )


@pytest.mark.asyncio
async def test_real_client_session_lifecycle_with_tagging_proxy():
    """真实 ClientSession 完整生命周期：构造 → async with（接收循环持有写流）→ 退出。

    回归：``_TurnTokenTaggingStream`` 未实现 ``__aenter__/__aexit__`` 时，
    ``BaseSession._receive_loop`` 的 ``async with (read, write)`` 会因特殊方法
    查找失败而崩溃（不会走 ``__getattr__``），SSE / streamable-http 建连在
    真实 SDK 上直接失败。
    """
    import anyio
    import mcp.types as types
    from mcp.client.session import ClientSession

    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _PENDING_TAG_SERVER,
        _TurnTokenTaggingStream,
    )

    apply_mcp_dynamic_token_patch()
    inbound_send, inbound_receive = anyio.create_memory_object_stream(10)
    outbound_send, outbound_receive = anyio.create_memory_object_stream(10)

    provider = _bare_provider("srv-lifecycle")
    _PENDING_TAG_SERVER.set(provider)
    session = ClientSession(read_stream=inbound_receive, write_stream=outbound_send)
    assert isinstance(session._write_stream, _TurnTokenTaggingStream)

    try:
        async with session:
            with _turn_ext({"X-User-Token": "Bearer lifecycle"}):
                await session.send_notification(
                    types.ClientNotification(types.InitializedNotification())
                )
            message = await outbound_receive.receive()
            assert message.message.root.method == "notifications/initialized"
            # 发送侧在真实会话路径上完成登记（通知 → FIFO，"" 哨兵不适用）
            assert list(provider._pending_notif_tokens) == ["Bearer lifecycle"]
    finally:
        await inbound_send.aclose()
        await outbound_send.aclose()


@pytest.mark.asyncio
async def test_real_sse_client_connect_and_call_carry_turn_user_token(monkeypatch):
    """真实 SseClient.connect：GET /sse 与 initialize 带当轮用户令牌。"""
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    from jiuwenswarm.common.mcp_dynamic_token_patch import _CONNECT_USER_TOKEN, _UNSET

    _enable_real_dynamic_gateway(monkeypatch, "srv-real-sse")
    records: list[dict[str, Any]] = []

    async with _serve(_build_sse_gateway_app(records)) as base_url:
        client = SseClient(_sse_server_config(base_url + "/sse", "srv-real-sse"))
        try:
            with _turn_ext({"X-User-Token": "Bearer A"}):
                assert await client.connect(timeout=10) is True

            assert _auth_for(records, "GET /sse") == "Bearer A"
            assert _auth_for(records, "initialize") == "Bearer A"
            assert all(record["actor"] == "Bearer actor-tok" for record in records)

            records.clear()
            with _turn_ext({"X-User-Token": "Bearer B"}):
                await client.call_tool("echo", {}, timeout=10)
            assert _auth_for(records, "tools/call") == "Bearer B"
            assert _CONNECT_USER_TOKEN.get() is _UNSET  # 连接期覆盖不泄漏
        finally:
            await client.disconnect(timeout=5)


@pytest.mark.asyncio
async def test_real_sse_client_reconnect_uses_current_callers_token(monkeypatch):
    """真实 SseClient.reconnect：owner task 上下文冻结不得沿用首连旧值。

    - 首连 A（owner task 在 A 的上下文创建）；
    - 轮次 B 重连：GET /sse、initialize 必须是 B（回归：owner task 快照）；
    - 无令牌轮次重连：回退静态头，不能残留 B。
    """
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    _enable_real_dynamic_gateway(monkeypatch, "srv-real-sse-reconnect")
    records: list[dict[str, Any]] = []

    async with _serve(_build_sse_gateway_app(records)) as base_url:
        client = SseClient(
            _sse_server_config(base_url + "/sse", "srv-real-sse-reconnect")
        )
        try:
            with _turn_ext({"X-User-Token": "Bearer A"}):
                assert await client.connect(timeout=10) is True

            records.clear()
            with _turn_ext({"X-User-Token": "Bearer B"}):
                assert await client.reconnect(timeout=10) is True
            assert _auth_for(records, "GET /sse") == "Bearer B"
            assert _auth_for(records, "initialize") == "Bearer B"

            records.clear()
            assert await client.reconnect(timeout=10) is True  # 无 ext 轮次
            assert _auth_for(records, "GET /sse") == "Bearer static"
            assert _auth_for(records, "initialize") == "Bearer static"
        finally:
            await client.disconnect(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_server_exited", [False, True])
async def test_real_streamable_http_client_switches_user_per_turn(monkeypatch, previous_server_exited):
    """真实 StreamableHttpClient：建连/逐轮调用/重连都按当轮用户令牌出网。

    重连段为检视 ④ 回归：SDK 的 connect 与 reconnect 都经 ``_submit`` 提交
    到长期 owner task——首个命令 ``create_task`` 时继承调用者上下文，首次
    connect 恰好正确；owner task 存活期间（@with_reconnect 自愈重连 / TTL
    刷新，owner task 不重建）命令跑在创建时冻结的上下文里，``_submit`` 未
    按命令绑定令牌时握手会拿到首连的陈旧值。

    注 1：重连时探针可能看到一条 ``GET == 旧令牌``——那是**旧连接** GET
    事件流被拆除后 handle_get_stream 的垂死自愈重试（bodyless 请求取旧
    provider 的 last-known），属旧连接自身的收尾，不是新连接握手；本组合
    （FastMCP + 真实 SDK）的 streamable 传输不开启新的 GET 事件流（首连后
    亦无），故握手断言落在 initialize / notifications/initialized 上。
    注 2：覆盖进程中尚无服务器退出、以及上一个服务器已退出两种状态。
    探针服务器必须隔离 SSE 的全局退出标记，否则新会话的 initialize
    响应会被旧标记提前关闭，表现为客户端建连超时。
    """
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", previous_server_exited)
    _enable_real_dynamic_gateway(monkeypatch, "srv-real-stream")
    records: list[dict[str, Any]] = []

    async with _serve(_build_streamable_gateway_app(records)) as base_url:
        client = StreamableHttpClient(
            McpServerConfig(
                server_id="srv-real-stream",
                server_name="srv-real-stream",
                server_path=base_url + "/mcp",
                client_type="streamable-http",
                auth_headers={"Authorization": "Bearer static"},
                auth_query_params={},
            )
        )
        try:
            with _turn_ext({"X-User-Token": "Bearer A"}):
                assert await client.connect(timeout=10) is True
            # 通知 POST 异步落盘：先等握手请求全部落盘再清记录（在途的
            # 旧握手请求会晚到混入后续记录，污染断言）
            await _wait_record(records, "notifications/initialized")
            assert _auth_for(records, "initialize") == "Bearer A"
            assert _auth_for(records, "notifications/initialized") == "Bearer A"

            records.clear()
            with _turn_ext({"X-User-Token": "Bearer B"}):
                await client.call_tool("echo", {"text": "hi"}, timeout=10)
            assert _auth_for(records, "tools/call") == "Bearer B"

            records.clear()
            with _turn_ext({"X-User-Token": "Bearer C"}):
                await client.disconnect(timeout=5)
                assert await client.connect(timeout=10) is True
            await _wait_record(records, "notifications/initialized")
            assert _auth_for(records, "initialize") == "Bearer C"

            records.clear()
            with _turn_ext({"X-User-Token": "Bearer C"}):
                await client.call_tool("echo", {"text": "hi"}, timeout=10)
            assert _auth_for(records, "tools/call") == "Bearer C"

            # owner task 存活期重连（不 disconnect）：握手必须带当轮 D
            records.clear()
            with _turn_ext({"X-User-Token": "Bearer D"}):
                assert await client.reconnect(timeout=10) is True
            await _wait_record(records, "notifications/initialized")
            assert _auth_for(records, "initialize") == "Bearer D"
            assert _auth_for(records, "notifications/initialized") == "Bearer D"

            # 无令牌轮次重连：回退静态快照，不得残留 D
            records.clear()
            assert await client.reconnect(timeout=10) is True
            await _wait_record(records, "notifications/initialized")
            assert _auth_for(records, "initialize") == "Bearer static"
            assert _auth_for(records, "notifications/initialized") == "Bearer static"
        finally:
            await client.disconnect(timeout=5)
    assert AppStatus.should_exit is previous_server_exited
