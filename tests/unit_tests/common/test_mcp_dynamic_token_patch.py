from __future__ import annotations

import asyncio
import contextlib
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
    _PROVIDERS,
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


# ---------------------------------------------------------------------------
# 11. 逐轮用户授权：调用作用域（holder）→ provider 出网取用
# ---------------------------------------------------------------------------
#
# HTTP 实际由建连时的后台任务（post_writer）发送——provider 里读 ContextVar
# 拿到的是 connect 时刻快照（首轮 A、次轮 B 仍发 A）。最小实现：call_tool /
# list_tools / connect 的包装作用域在调用者上下文（本轮对话栈）读请求级 ext
# 的 X-User-Token 存进程级 holder（按 server_id 键），provider 出网时取完整值
# 赋给 Authorization（与 X-Actor-Token 平级）；作用域退出即还原，本轮没带
# 令牌不残留上一轮的值。


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


async def _run_flow(provider: DynamicTokenProvider, request: httpx.Request) -> httpx.Request:
    flow = provider.async_auth_flow(request)
    sent = await flow.__anext__()
    await flow.aclose()
    return sent


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


def test_current_user_authorization_rejects_unsafe_values():
    """控制字符 / 非可打印 ASCII / 超长值拒绝注入（防 httpx 发送阶段抛错）。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _MAX_USER_TOKEN_LEN,
        _current_user_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer a\nEvil: 1"}):
        assert _current_user_authorization() == ""  # 内嵌换行拒绝
    with _turn_ext({"X-User-Token": "Bearer tok\r\n"}):
        # 首尾 CRLF 被 strip 剔除，剩余值安全 → 注入
        assert _current_user_authorization() == "Bearer tok"
    with _turn_ext({"X-User-Token": "Bearer token-中文"}):
        assert _current_user_authorization() == ""
    with _turn_ext({"X-User-Token": "  Bearer padded  "}):
        # 首尾空白剔除后注入
        assert _current_user_authorization() == "Bearer padded"
    with _turn_ext({"X-User-Token": "x" * (_MAX_USER_TOKEN_LEN + 1)}):
        assert _current_user_authorization() == ""


# ---------------------------------------------------------------------------
# 11.1 调用作用域：进入写 holder、退出还原、嵌套保存前值
# ---------------------------------------------------------------------------


def test_call_scope_sets_and_restores():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            assert _current_call_authorization("srv-1") == "Bearer A"
        assert "srv-1" not in _CURRENT_CALL_AUTH  # 退出还原，不残留


def test_call_scope_noop_without_token():
    """本轮没带用户令牌（后台轮次/cron）：不写入 holder，不残留旧值。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            pass
    with _turn_ext(None):
        with _call_authorization_scope("srv-1"):
            assert "srv-1" not in _CURRENT_CALL_AUTH


# ---------------------------------------------------------------------------
# 11.2 provider 出网：holder 有值覆盖、无值静态、逐轮换值（评审复现场景）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_flow_overrides_authorization_with_call_token():
    """作用域内：X-User-Token 完整值覆盖静态快照，与 X-Actor-Token 平级并存。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    provider = _make_provider(token="tok-1")

    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        with _call_authorization_scope("srv-1"):
            sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer turn-A"  # 完整值（含 Bearer 前缀）
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"  # 平级动态头并存
    assert sent.headers["X-Static"] == "s"
    assert "qk=qv" in str(sent.url)  # auth_query_params 合并不受影响


@pytest.mark.asyncio
async def test_auth_flow_token_follows_each_turn_then_falls_back():
    """评审两轮复现：A → A，B → B，第三轮不带令牌 → 静态快照（不沿用 B）。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    provider = _make_provider(token="tok-1")

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            sent_a = await _run_flow(provider, _jsonrpc_request(request_id=1))

    with _turn_ext({"X-User-Token": "Bearer B"}):
        with _call_authorization_scope("srv-1"):
            sent_b = await _run_flow(provider, _jsonrpc_request(request_id=2))

    sent_c = await _run_flow(provider, _jsonrpc_request(request_id=3))  # 无作用域/无 ext

    assert sent_a.headers["Authorization"] == "Bearer A"
    assert sent_b.headers["Authorization"] == "Bearer B"
    assert sent_c.headers["Authorization"] == "Bearer static"


@pytest.mark.asyncio
async def test_auth_flow_keeps_static_without_scope():
    """作用域外（预热连接、后台 TTL 刷新）：保留条目静态快照头。"""
    provider = _make_provider(token="tok-1")

    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer static"
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_auth_flow_unsafe_ext_records_nothing():
    """ext 值不安全（换行注入）：作用域不写入 → 回退静态头，请求不阻断。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
    )

    provider = _make_provider(token="tok-1")

    with _turn_ext({"X-User-Token": "Bearer a\r\nEvil: 1"}):
        with _call_authorization_scope("srv-1"):
            assert "srv-1" not in _CURRENT_CALL_AUTH
            sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer static"


# ---------------------------------------------------------------------------
# 11.3 SDK 包装安装：call_tool/list_tools/connect 全部挂上作用域
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_call_paths_wrapped_with_scope():
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    apply_mcp_dynamic_token_patch()

    for cls, methods in (
        (StreamableHttpClient, ("call_tool", "list_tools", "connect")),
        (SseClient, ("call_tool", "list_tools", "_do_connect")),
    ):
        for name in methods:
            method = getattr(cls, name)
            assert getattr(method, "_jw_user_token_scoped", False), (cls.__name__, name)
            assert asyncio.iscoroutinefunction(method), (cls.__name__, name)


# ---------------------------------------------------------------------------
# 11.4 preflight：用户授权覆盖仅限双 token 网关（评审问题 2 回归）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_dual_token_gateway_carries_turn_user_token(monkeypatch, _patch_httpx):
    """标记网关（dynamic_token_url + 凭据已下发）：探活带本轮用户令牌 + 动态头。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _fake_fetch(token_url, creds, *, client=None, timeout_s=None):
        return "fresh-tok"

    import jiuwenswarm.common.mcp_dynamic_token_patch as _mod

    monkeypatch.setattr(_mod, "fetch_dynamic_token", _fake_fetch)

    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(
            _gateway_config(), dynamic_token_url="http://kc.example/token"
        )

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer turn-user-tok"  # 覆盖静态快照
    assert headers["X-Actor-Token"] == "Bearer fresh-tok"


@pytest.mark.asyncio
async def test_preflight_plain_gateway_keeps_static_authorization(_patch_httpx):
    """评审问题 2 回归：普通 MCP 服务（无 dynamic_token_url）绝不被覆盖。"""
    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(_gateway_config())

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer static"  # 服务原有凭据保留
    assert "X-Actor-Token" not in headers


@pytest.mark.asyncio
async def test_preflight_marked_gateway_without_creds_keeps_static(_patch_httpx):
    """标记但实例凭据未下发：运行时走原静态策略，探活同样不覆盖。"""
    _dispatch_credentials(None)  # 清除凭据

    with _turn_ext({"X-User-Token": "Bearer turn-user-tok"}):
        ok, _reason = await preflight_mcp_server_reachable(
            _gateway_config(), dynamic_token_url="http://kc.example/token"
        )

    assert ok is True
    headers = _RecordingAsyncClient.requests[0][2]["headers"]
    assert headers["Authorization"] == "Bearer static"
    assert "X-Actor-Token" not in headers


# ---------------------------------------------------------------------------
# 11.6 场景补充：异常还原 / 并发引用计数 / provider 组合语义
# ---------------------------------------------------------------------------


def test_call_scope_restores_on_exception():
    """作用域体内抛异常：holder 仍被还原（finally），异常照常传播。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with pytest.raises(RuntimeError, match="boom"):
            with _call_authorization_scope("srv-1"):
                raise RuntimeError("boom")
    assert "srv-1" not in _CURRENT_CALL_AUTH


def test_call_scope_refcount_same_value_survives_inner_exit():
    """同用户并发（同值引用计数）：内层调用退出不影响外层在途调用的可读性。

    回归旧"保存前值/退出还原"语义的缺陷——先进入的调用退出时会把仍在途
    调用的值一起抹掉。
    """
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            with _call_authorization_scope("srv-1"):  # 同用户第二个并发调用
                assert _current_call_authorization("srv-1") == "Bearer A"
            # 内层已退出，外层调用仍在途 → 值仍可读
            assert _current_call_authorization("srv-1") == "Bearer A"
        assert "srv-1" not in _CURRENT_CALL_AUTH  # 全部退出才清空


def test_call_scope_cross_value_concurrent_never_leaks_identity():
    """不同用户的调用在途重叠：无归属值（回退静态快照），绝不串用他人身份。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer user-A"}):
        with _call_authorization_scope("srv-1"):
            with _turn_ext({"X-User-Token": "Bearer user-B"}):
                with _call_authorization_scope("srv-1"):
                    # A、B 两路调用同时在途：无法归属 → 空（静态快照兜底）
                    assert _current_call_authorization("srv-1") == ""
                # B 路退出后，A 路仍在途 → 恢复可读
                assert _current_call_authorization("srv-1") == "Bearer user-A"
            assert _current_call_authorization("srv-1") == "Bearer user-A"
        assert "srv-1" not in _CURRENT_CALL_AUTH


@pytest.mark.asyncio
async def test_auth_flow_actor_failure_with_user_token():
    """取 actor token 失败 + 本轮用户令牌在途：Authorization 带用户令牌、
    X-Actor-Token 置空，请求照常发起（两个头语义互不干扰）。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    token_url = "http://token-svc.example/token"
    provider = DynamicTokenProvider(
        {"Authorization": "Bearer static"}, {}, token_url, "srv-1"
    )

    async def _failing_fetch() -> str:
        raise McpDynamicTokenUnavailableError(token_url, "connection refused")

    provider._fetch_token = _failing_fetch  # type: ignore[method-assign]

    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        with _call_authorization_scope("srv-1"):
            sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer turn-A"  # 用户授权不受影响
    assert sent.headers["X-Actor-Token"] == ""  # 动态头置空降级


@pytest.mark.asyncio
async def test_auth_flow_adds_authorization_when_static_empty():
    """条目没有静态 auth_headers：本轮用户令牌照样补上 Authorization。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    provider = DynamicTokenProvider({}, {}, "http://token-svc.example/token", "srv-1")

    async def _fake_fetch() -> str:
        return "tok-1"

    provider._fetch_token = _fake_fetch  # type: ignore[method-assign]

    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        with _call_authorization_scope("srv-1"):
            sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer turn-A"
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_auth_flow_server_isolation():
    """作用域按网关隔离：srv-1 的在途令牌不外溢到其他网关的 provider。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    _dispatch_credentials("demo_agent1", "demo-agent-secret1")
    provider_other = DynamicTokenProvider(
        {"Authorization": "Bearer static"},
        {},
        "http://token-svc.example/token",
        "srv-other",
    )

    async def _fake_fetch() -> str:
        return "tok-1"

    provider_other._fetch_token = _fake_fetch  # type: ignore[method-assign]

    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        with _call_authorization_scope("srv-1"):
            sent = await _run_flow(provider_other, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer static"  # 未被 srv-1 的值覆盖
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_auth_flow_ambiguous_concurrency_falls_back_to_static():
    """不同用户调用在途重叠时的真实出网：Authorization 回退静态快照。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

    provider = _make_provider(token="tok-1")

    with _turn_ext({"X-User-Token": "Bearer user-A"}):
        with _call_authorization_scope("srv-1"):
            with _turn_ext({"X-User-Token": "Bearer user-B"}):
                with _call_authorization_scope("srv-1"):
                    sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["Authorization"] == "Bearer static"  # 不串身份
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


@pytest.mark.parametrize("bad", [123, 4.5, ["Bearer x"], {"t": 1}, True, None])
def test_current_user_authorization_rejects_non_string(bad):
    """ext 值非字符串：拒绝注入（str() 会把结构化字符带进请求头）。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization

    with _turn_ext({"X-User-Token": bad}):
        assert _current_user_authorization() == ""


# ---------------------------------------------------------------------------
# 11.7 包装函数级：_wrap_with_call_scope 在调用路径上的行为
# ---------------------------------------------------------------------------


class _DummyClient:
    """模拟 SDK 客户端：记录调用期间 provider 视角能读到的值。"""

    _server_id = "srv-dummy"

    def __init__(self, raise_on_call: BaseException | None = None) -> None:
        self.seen: list[str] = []
        self._raise = raise_on_call

    async def call_tool(self, name, arguments=None):  # noqa: ANN001
        from jiuwenswarm.common.mcp_dynamic_token_patch import (
            _current_call_authorization,
        )

        self.seen.append(_current_call_authorization(self._server_id))
        if self._raise is not None:
            raise self._raise
        return name


@pytest.mark.asyncio
async def test_wrap_with_call_scope_during_and_after_call():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _wrap_with_call_scope,
    )

    _wrap_with_call_scope(_DummyClient, "call_tool")

    with _turn_ext({"X-User-Token": "Bearer T"}):
        client = _DummyClient()
        assert await client.call_tool("t") == "t"
        assert client.seen == ["Bearer T"]  # 调用期间 provider 可读到本轮值
    assert "srv-dummy" not in _CURRENT_CALL_AUTH  # 调用结束还原

    client2 = _DummyClient()
    assert await client2.call_tool("t") == "t"
    assert client2.seen == [""]  # 作用域外（无 ext）→ 空


@pytest.mark.asyncio
async def test_wrap_with_call_scope_restores_on_exception():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _wrap_with_call_scope,
    )

    _wrap_with_call_scope(_DummyClient, "call_tool")

    with _turn_ext({"X-User-Token": "Bearer T"}):
        client = _DummyClient(raise_on_call=RuntimeError("tool exploded"))
        with pytest.raises(RuntimeError, match="tool exploded"):
            await client.call_tool("t")
        assert client.seen == ["Bearer T"]  # 进入时已可读
    assert "srv-dummy" not in _CURRENT_CALL_AUTH  # 异常路径同样还原


def test_wrap_with_call_scope_idempotent():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _wrap_with_call_scope

    _wrap_with_call_scope(_DummyClient, "call_tool")
    wrapped = _DummyClient.__dict__["call_tool"]
    _wrap_with_call_scope(_DummyClient, "call_tool")
    assert _DummyClient.__dict__["call_tool"] is wrapped  # 不二次包装


# ---------------------------------------------------------------------------
# 11.8 请求级 ext 抬升链路（agent_ws_server 入口依赖的原语）
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


@pytest.mark.asyncio
async def test_concurrent_turns_isolated_real_asyncio():
    """真实 asyncio 并发：两轮不同用户同时调用同一网关，各拿各的值。

    （作用域体不含 await 时 enter+read+exit 原子执行，无重叠；若真实调用
    在途重叠，则由 ambiguous_concurrency 测试覆盖——回退静态快照。）
    """
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _call_authorization_scope,
        _current_call_authorization,
    )

    async def tool_call() -> str:
        await asyncio.sleep(0.01)
        with _call_authorization_scope("srv-e2e"):
            return _current_call_authorization("srv-e2e")

    async def turn_call(token: str) -> str:
        from jiuwenswarm.common import request_ext

        tok = request_ext.set_current({"X-User-Token": token})
        try:
            return await tool_call()
        finally:
            request_ext.reset_ext(tok)

    r1, r2 = await asyncio.gather(turn_call("Bearer U1"), turn_call("Bearer U2"))
    assert r1 == "Bearer U1"
    assert r2 == "Bearer U2"


# ---------------------------------------------------------------------------
# 11.9 真实 SDK 类的功能级验证（未连接会话的真实异常路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_client_call_tool_exception_path_restores_holder():
    """真实 StreamableHttpClient（未连接）经包装层调用：异常穿透、holder 还原。

    证明包装真实类方法可用且不吞异常：orig 抛 RuntimeError，作用域 finally
    仍还原 holder。
    """
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
    )

    apply_mcp_dynamic_token_patch()
    client = StreamableHttpClient(
        McpServerConfig(
            server_id="srv-real",
            server_name="real-gw",
            client_type="streamable-http",
            server_path="https://gw.example/mcp",
        )
    )
    assert client._server_id == "srv-real"

    with _turn_ext({"X-User-Token": "Bearer real-tok"}):
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.call_tool("t", {})
    assert "srv-real" not in _CURRENT_CALL_AUTH  # 真实异常路径同样还原


def test_reapply_does_not_double_wrap_after_reset():
    """reset（_PATCHED=False）后再 apply：类方法不二次包装（标记判重）。"""
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    apply_mcp_dynamic_token_patch()
    first = StreamableHttpClient.call_tool
    _reset_dynamic_token_state_for_tests()  # _PATCHED=False，但类包装仍在
    apply_mcp_dynamic_token_patch()
    assert StreamableHttpClient.call_tool is first  # 不叠加新包装层


# ---------------------------------------------------------------------------
# 11.10 作用域边界
# ---------------------------------------------------------------------------


def test_call_scope_empty_server_id_noop():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope(""):
            assert _CURRENT_CALL_AUTH == {}  # 无 server_id → 完全 noop


def test_call_scope_strips_server_id():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("  srv-1  "):
            # provider 侧的 server_id 已 strip（authenticate 构造时 strip）
            assert _current_call_authorization("srv-1") == "Bearer A"


def test_call_scope_external_clear_during_scope_is_safe():
    """作用域在途时 holder 被整体清空（如测试复位）：退出不抛错、保持空。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            _CURRENT_CALL_AUTH.clear()  # 模拟 _reset_dynamic_token_state_for_tests
        # 退出递减找不到条目 → 静默跳过，不恢复出旧值
        assert _CURRENT_CALL_AUTH == {}


def test_call_scope_cross_server_nesting():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer T"}):
        with _call_authorization_scope("srv-1"):
            with _call_authorization_scope("srv-2"):
                assert _current_call_authorization("srv-1") == "Bearer T"
                assert _current_call_authorization("srv-2") == "Bearer T"
            assert _current_call_authorization("srv-1") == "Bearer T"
            assert _current_call_authorization("srv-2") == ""
        assert _CURRENT_CALL_AUTH == {}


def test_call_scope_nested_inner_exception_outer_survives():
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    with _turn_ext({"X-User-Token": "Bearer A"}):
        with _call_authorization_scope("srv-1"):
            with pytest.raises(ValueError, match="inner"):
                with _call_authorization_scope("srv-1"):
                    raise ValueError("inner")
            # 内层异常退出只递减自己的计数，外层仍可读
            assert _current_call_authorization("srv-1") == "Bearer A"
        assert _CURRENT_CALL_AUTH == {}


def test_current_call_authorization_empty_sid():
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_call_authorization

    assert _current_call_authorization("") == ""


# ---------------------------------------------------------------------------
# 11.11 ext 值边界（strip / 控制字符 / 长度边界）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["   ", "Bearer\ttok", "Bearer tok\x7f", "a\x00b"])
def test_current_user_authorization_rejects_whitespace_and_control_chars(bad):
    from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization

    with _turn_ext({"X-User-Token": bad}):
        assert _current_user_authorization() == ""


def test_current_user_authorization_max_length_boundary():
    """长度 == 上限 → 接受；仅超 1 个字符 → 拒绝。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _MAX_USER_TOKEN_LEN,
        _current_user_authorization,
    )

    exact = "x" * _MAX_USER_TOKEN_LEN
    with _turn_ext({"X-User-Token": exact}):
        assert _current_user_authorization() == exact


# ---------------------------------------------------------------------------
# 11.12 provider 头合并语义（httpx 大小写不敏感覆盖，不产生重复头）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_flow_overrides_lowercase_authorization_key():
    """静态头键为小写 authorization：覆盖后仅一个 authorization 头且为新值。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import _call_authorization_scope

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

    with _turn_ext({"X-User-Token": "Bearer turn-A"}):
        with _call_authorization_scope("srv-1"):
            sent = await _run_flow(provider, _jsonrpc_request(request_id=1))

    assert sent.headers["authorization"] == "Bearer turn-A"  # 大小写不敏感覆盖
    assert sent.headers.get_list("authorization") == ["Bearer turn-A"]  # 无重复头
    assert sent.headers["X-Actor-Token"] == "Bearer tok-1"


# ---------------------------------------------------------------------------
# 11.13 并发压力与确定性重叠
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_scope_stress_many_concurrent_same_value():
    """50 路同用户并发调用同一网关：全程可读，全部退出后清空。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    async def one_call() -> str:
        await asyncio.sleep(0.001)
        with _call_authorization_scope("srv-stress"):
            await asyncio.sleep(0.001)
            return _current_call_authorization("srv-stress")

    with _turn_ext({"X-User-Token": "Bearer mass"}):
        results = await asyncio.gather(*(one_call() for _ in range(50)))

    assert results == ["Bearer mass"] * 50
    assert "srv-stress" not in _CURRENT_CALL_AUTH


@pytest.mark.asyncio
async def test_call_scope_deterministic_overlap_ambiguous():
    """确定性构造两路不同用户重叠：重叠期内都读不到（回退静态），退出清空。"""
    from jiuwenswarm.common.mcp_dynamic_token_patch import (
        _CURRENT_CALL_AUTH,
        _call_authorization_scope,
        _current_call_authorization,
    )

    entered = asyncio.Barrier(2)
    read_values: list[tuple[str, str]] = []  # (自己带入的值, 读到的值)

    async def one_call(token: str) -> None:
        from jiuwenswarm.common import request_ext

        tok = request_ext.set_current({"X-User-Token": token})
        try:
            with _call_authorization_scope("srv-overlap"):
                await entered.wait()  # 确保两路都在途
                read_values.append((token, _current_call_authorization("srv-overlap")))
        finally:
            request_ext.reset_ext(tok)

    await asyncio.gather(one_call("Bearer A"), one_call("Bearer B"))

    # 不变量：每路读到的要么是空（重叠期无归属）、要么是自己的值——
    # 绝不读到对方用户的令牌。barrier 放开后两路串行执行（读+退出之间
    # 无 await），先执行者看到两路在途读空，后执行者看到仅剩自己。
    for owner, seen in read_values:
        assert seen in ("", owner), (owner, seen)
    assert "" in {seen for _owner, seen in read_values}  # 至少一路经历重叠期
    assert "srv-overlap" not in _CURRENT_CALL_AUTH  # 全部退出后清空


# ---------------------------------------------------------------------------
# 11.14 preflight 组合（用户令牌 + actor 取 token 失败）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_dual_token_actor_failure_with_user_token(monkeypatch, _patch_httpx):
    """标记网关：actor 取 token 失败 → X-Actor-Token 置空，用户令牌照常覆盖。"""
    _dispatch_credentials("demo_agent1", "demo-agent-secret1")

    async def _failing_fetch(token_url, creds, *, client=None, timeout_s=None):
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
