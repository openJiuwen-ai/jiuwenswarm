from __future__ import annotations

import asyncio
from typing import Any
from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.auth import auth_callback
from openjiuwen.core.foundation.tool.auth.auth import ToolAuthConfig

from jiuwenswarm.common.mcp_config import preflight_mcp_server_reachable
from jiuwenswarm.common.mcp_dynamic_token_patch import (
    TOKEN_HEADER_NAME,
    TOKEN_HEADER_PREFIX,
    TOKEN_RESPONSE_FIELD,
    _PROVIDERS,
    _registry_token_url,
    _reset_dynamic_token_state_for_tests,
    _token_form_fields,
    apply_mcp_dynamic_token_patch,
    fetch_dynamic_token,
    is_dynamic_token_server,
    mark_dynamic_token_server,
    on_config_snapshot_reloaded,
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
    """模拟上游下发实例级凭据（client_id=None = 快照未携带该段，清除）。"""
    if client_id is None:
        on_config_snapshot_reloaded({})
        return
    section: dict[str, str] = {"client_id": client_id}
    if client_secret is not None:
        section["client_secret"] = client_secret
    on_config_snapshot_reloaded({"mcp": {"dynamic_token": section}})


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
    # 两者皆空 → None
    on_config_snapshot_reloaded({"mcp": {"dynamic_token": {}}})
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
    on_config_snapshot_reloaded({})  # 快照整份替换且未携带段 → 清除
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


def _make_provider(token: str = "tok-1") -> DynamicTokenProvider:
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    token_url = "http://token-svc.example/token"
    provider = DynamicTokenProvider(
        {"Authorization": "Bearer static", "X-Static": "s"},
        {"qk": "qv"},
        token_url,
        "srv-1",
    )

    async def _fake_fetch() -> str:
        return token

    provider._fetch_token = _fake_fetch  # type: ignore[method-assign]
    return provider


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
    assert isinstance(first.auth_data["auth_provider"], DynamicTokenProvider)
    assert isinstance(second.auth_data["auth_provider"], DynamicTokenProvider)
    # provider 按 server_id 缓存复用（重连不重建 token client）
    assert first.auth_data["auth_provider"] is second.auth_data["auth_provider"]


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

    async def _fake_fetch(token_url, creds, *, client=None, timeout_s=None):
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

    async def _failing_fetch(token_url, creds, *, client=None, timeout_s=None):
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

    async def _must_not_fetch(token_url, creds, *, client=None, timeout_s=None):
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
