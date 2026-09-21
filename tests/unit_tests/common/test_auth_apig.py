# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import pytest

from jiuwenswarm.common.auth import apig


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def apig_env(monkeypatch):
    _remote(monkeypatch)
    return apig.resolve_apig_config()


def _remote(monkeypatch, *, effective=True, apig_base="https://apig.example.com",
            whitelist=None, blocklist=None, login=None, **apig_paths):
    from jiuwenswarm.common.auth import remote_config

    remote_config.set_config_for_test(remote_config.parse_config({
        "is_effective": effective,
        "huaweiaccount_login": login or {},
        "gateway": {"base_url": apig_base, **apig_paths},
        "models": {"whitelist": whitelist or [], "blocklist": blocklist or []},
    }))
    monkeypatch.setattr(remote_config, "config_url", lambda: "https://config.example.com")


ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.e30.sig"


def test_disabled_without_base_url(monkeypatch):
    _remote(monkeypatch, apig_base="")
    assert apig.resolve_apig_config() is None


def test_base_url_gets_scheme_and_paths_join(monkeypatch):
    _remote(monkeypatch, apig_base="apig.example.com/")
    config = apig.resolve_apig_config()
    assert config.base_url == "https://apig.example.com"
    assert config.models_url == "https://apig.example.com/v1/models"
    assert config.quota_url == "https://apig.example.com/v1/usage"
    assert config.invoke_base_url == "https://apig.example.com/v1"


def test_auth_header_rejects_empty_token():
    with pytest.raises(apig.ApigError) as excinfo:
        apig.build_auth_headers("   ")
    assert excinfo.value.code == "session_expired"


REAL_KEY_INFO = {
    "key": "3b39a29ece66c825878d6920c636acfea984dd04c08fdf0cc60d6a4a6a922046",
    "info": {
        "key_name": "sk-...eng1",
        "spend": 12.5,
        "expires": "2026-10-14T12:46:26.689000+00:00",
        "models": ["GLM-5.2", "GLM-5.1", "openPangu-2.0-Pro", "Kimi-K2.6"],
        "user_id": "lixiaofeng1",
        "max_budget": 100.0,
        "budget_duration": None,
    },
}


def test_parses_litellm_key_info_as_points_one_to_one():
    quota = apig.parse_quota(REAL_KEY_INFO)
    assert (quota.total, quota.used, quota.balance) == (100.0, 12.5, 87.5)
    assert quota.low_balance is False and quota.exhausted is False
    assert quota.reset_period == "" and quota.reset_at == "", "没配周期：额度用完不会自动恢复"


def test_periodic_budget_carries_period_and_next_reset():
    info = {**REAL_KEY_INFO["info"], "budget_duration": "7d", "budget_reset_at": "2026-09-21T00:00:00+00:00"}
    view = apig.parse_quota({"info": info}).public_view()
    assert view["reset_period"] == "7d"
    assert view["reset_at"] == "2026-09-21T00:00:00+00:00"


def test_stale_reset_time_without_a_period_is_ignored():
    info = {"spend": 1.0, "max_budget": 100.0, "budget_duration": None, "budget_reset_at": "2026-09-21T00:00:00+00:00"}
    assert apig.parse_quota({"info": info}).reset_at == ""


def test_unlimited_budget_has_unknown_total_and_is_never_exhausted():
    quota = apig.parse_quota({"info": {"spend": 3.0, "max_budget": None}})
    assert quota.used == 3.0
    assert quota.total == -1.0 and quota.balance == -1.0
    assert quota.exhausted is False and quota.low_balance is False


def test_quota_unknown_is_minus_one_not_zero():
    for payload in ({}, {"info": {}}, {"info": "bad"}, None):
        quota = apig.parse_quota(payload)
        assert (quota.total, quota.balance, quota.used) == (-1.0, -1.0, -1.0)
        assert quota.exhausted is False, "余额未知时不能当成耗尽"


def test_spent_budget_is_exhausted_including_overdraft():
    for spend in (100.0, 100.02, 150):
        quota = apig.parse_quota({"info": {"spend": spend, "max_budget": 100.0}})
        assert quota.exhausted is True
        assert quota.balance <= 0


def test_low_balance_at_ninety_percent_spent():
    assert apig.parse_quota({"info": {"spend": 89.9, "max_budget": 100.0}}).low_balance is False
    assert apig.parse_quota({"info": {"spend": 90.0, "max_budget": 100.0}}).low_balance is True


def test_quota_public_view_hides_key_details():
    view = apig.parse_quota(REAL_KEY_INFO).public_view()
    assert set(view) == {"total", "balance", "used", "low_balance", "exhausted", "reset_period", "reset_at"}
    text = str(view)
    assert "lixiaofeng1" not in text and "3b39a29e" not in text and "sk-" not in text


def test_fetch_quota_reads_points_from_key_info(apig_env, monkeypatch):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(200, REAL_KEY_INFO))
    quota = apig.fetch_quota(ID_TOKEN, apig_env)
    assert (quota.total, quota.balance) == (100.0, 87.5)


def test_402_raises_quota_exhausted(apig_env, monkeypatch):
    monkeypatch.setattr(
        apig, "requests_request", lambda *a, **k: FakeResponse(402, {"message": "over limit"})
    )
    with pytest.raises(apig.QuotaExhausted) as excinfo:
        apig.fetch_models(ID_TOKEN, apig_env)
    assert excinfo.value.code == apig.QUOTA_EXHAUSTED_CODE
    assert excinfo.value.detail == "over limit"


@pytest.mark.parametrize("payload", [{}, {"error_code": "APIG.0308", "error_msg": "throttled"}, {"error_code": "THROTTLED"}])
def test_throttling_is_rate_limited_not_quota_exhausted(apig_env, monkeypatch, payload):
    status = 200 if payload.get("error_code") == "THROTTLED" else 429
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(status, payload))
    if status == 200:
        apig.fetch_models(ID_TOKEN, apig_env)  # 业务码里的 throttle 不再算额度耗尽
        return
    with pytest.raises(apig.ApigError) as excinfo:
        apig.fetch_models(ID_TOKEN, apig_env)
    assert not isinstance(excinfo.value, apig.QuotaExhausted)
    assert excinfo.value.code == apig.RATE_LIMITED_CODE


@pytest.mark.parametrize(
    "error_code",
    ["APIG.QUOTA_EXCEEDED", "billing.arrears", "insufficient_quota"],
)
def test_business_error_code_also_counts_as_exhausted(apig_env, monkeypatch, error_code):
    monkeypatch.setattr(
        apig,
        "requests_request",
        lambda *a, **k: FakeResponse(200, {"error_code": error_code, "message": "no quota"}),
    )
    with pytest.raises(apig.QuotaExhausted):
        apig.fetch_models(ID_TOKEN, apig_env)


def test_fetch_quota_returns_exhausted_instead_of_raising(apig_env, monkeypatch):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(402, {}))
    quota = apig.fetch_quota(ID_TOKEN, apig_env)
    assert quota.exhausted is True
    assert quota.balance == 0.0


@pytest.mark.parametrize(
    ("status", "payload"),
    [(401, {}), (401, {"error_code": "APIG.0305", "error_msg": "Incorrect authentication information"})],
)
def test_authorizer_rejection_maps_to_session_expired(apig_env, monkeypatch, status, payload):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(status, payload))
    with pytest.raises(apig.ApigError) as excinfo:
        apig.fetch_quota(ID_TOKEN, apig_env)
    assert excinfo.value.code == "session_expired"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {}),
        (403, {"error_code": "APIG.0402", "error_msg": "The IP address is not authorized"}),
        (403, {"error_code": "APIG.0306", "error_msg": "API access denied"}),
        (401, {"error_code": "APIG.0301", "error_msg": "Incorrect IAM authentication information"}),
    ],
)
def test_access_control_rejections_are_not_login_failures(apig_env, monkeypatch, status, payload):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(status, payload))
    with pytest.raises(apig.ApigError) as excinfo:
        apig.fetch_quota(ID_TOKEN, apig_env)
    assert excinfo.value.code == "apig_error"


def test_network_failure_maps_to_unreachable(apig_env, monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(apig, "requests_request", _boom)
    with pytest.raises(apig.ApigError) as excinfo:
        apig.fetch_quota(ID_TOKEN, apig_env)
    assert excinfo.value.code == "apig_unreachable"


@pytest.mark.parametrize(
    "payload",
    [
        {"object": "list", "data": [{"id": "glm-5", "object": "model", "owned_by": "llm-gateway"}]},
        {"data": [{"id": "glm-5"}]},
        {"models": [{"id": "glm-5"}]},
        {"items": [{"id": "glm-5"}]},
        [{"id": "glm-5"}],
    ],
)
def test_model_payload_shapes(apig_env, monkeypatch, payload):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(200, payload))
    items = apig.fetch_models(ID_TOKEN, apig_env)
    assert [item["id"] for item in items] == ["glm-5"]


def test_unknown_model_payload_is_empty_not_error(apig_env, monkeypatch):
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(200, {"ok": True}))
    assert apig.fetch_models(ID_TOKEN, apig_env) == []


def test_auth_header_is_sent(apig_env, monkeypatch):
    seen: dict = {}

    def _capture(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return FakeResponse(200, {"data": []})

    monkeypatch.setattr(apig, "requests_request", _capture)
    apig.fetch_models(ID_TOKEN, apig_env)
    assert seen["method"] == "GET"
    assert seen["url"] == "https://apig.example.com/v1/models"
    assert seen["headers"]["Authorization"] == f"Bearer {ID_TOKEN}"
    assert "X-User-Profile" not in seen["headers"]
    assert "X-Sdk-Date" not in seen["headers"], "不再签名"

_SDK = "[181001] model call failed, reason: openAI API async stream error: "


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (_SDK + "AuthenticationError: Error code: 401 - {'error_msg': 'Incorrect authentication information: "
         "frontend authorizer', 'error_code': 'APIG.0305', 'request_id': '36969153b5ab7c47180fd155f1481012'}",
         apig.LOGIN_REQUIRED_CODE),
        ("Error code: 401 - {'error_code': 'APIG.0307'}", apig.LOGIN_REQUIRED_CODE),
        (_SDK + "InternalServerError: Error code: 502 - {'error_msg': 'Backend unavailable', 'error_code': "
         "'APIG.0202', 'request_id': 'b3844ae0aacba00b6e5187c8ee14982f'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 504 - {'error_code': 'APIG.0203', 'error_msg': 'Backend timeout'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 404 - {'error_code': 'APIG.0101'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 404 - {'detail': 'Not Found'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 401 - {'error_code': 'APIG.0301'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 405 - {'error_code': 'APIG.0501', 'error_msg': 'App quota exhausted'}", apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 429 - {'error_code': 'APIG.0308'}", apig.RATE_LIMITED_CODE),
        # 模型网关（llm_gw）
        (_SDK + "Error code: 402 - {'detail': {'error': {'message': 'Insufficient quota: balance is exhausted.', "
         "'type': 'quota_exceeded', 'code': 'insufficient_quota'}}}", apig.QUOTA_EXHAUSTED_CODE),
        (_SDK + "Error code: 401 - {'detail': 'X-User-Profile missing required field(s): principal_id'}",
         apig.SERVICE_UNAVAILABLE_CODE),
        (_SDK + "Error code: 502 - {'detail': {'error': {'message': 'Upstream provider error: boom'}}}",
         apig.SERVICE_UNAVAILABLE_CODE),
        ("Error code: 429 - Too Many Requests", apig.RATE_LIMITED_CODE),
        ("账号已欠费", apig.QUOTA_EXHAUSTED_CODE),
        # LiteLLM
        (_SDK + "BadRequestError: Error code: 400 - {'error': {'message': 'Budget has been exceeded! "
         "Current cost: 100.12, Max budget: 100.0', 'type': 'budget_exceeded', 'param': None, 'code': '400'}}",
         apig.QUOTA_EXHAUSTED_CODE),
        (_SDK + "AuthenticationError: Error code: 401 - {'error': {'message': 'Authentication Error, "
         "Invalid proxy server token passed.', 'type': 'auth_error', 'code': '401'}}", apig.SERVICE_UNAVAILABLE_CODE),
        (_SDK + "AuthenticationError: Error code: 401 - {'error': {'message': 'Authentication Error - Expired Key.'}}",
         apig.SERVICE_UNAVAILABLE_CODE),
        (_SDK + "Error code: 401 - {'error': {'message': 'key not allowed to access model. This key can only "
         "access models=[...]. Tried to access GLM-9'}}", apig.SERVICE_UNAVAILABLE_CODE),
        (_SDK + "RateLimitError: Error code: 429 - {'error': {'message': 'Max parallel request limit reached.'}}",
         apig.RATE_LIMITED_CODE),
    ],
)
def test_free_model_errors_are_classified(text, code):
    assert apig.classify_model_error(text, login_model=True) == code


def test_user_configured_models_are_never_classified():
    for text in ("Error code: 429 - Too Many Requests", "Error code: 401 - {'error_code': 'APIG.0305'}"):
        assert apig.classify_model_error(text, login_model=False) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "connection reset by peer",
        "model returned an empty response",
        "tool failed, request_id=7f429ab0c1d2",
    ],
)
def test_unrelated_error_text_is_not_classified(text):
    assert apig.classify_model_error(text, login_model=True) is None


def test_litellm_nested_error_message_is_surfaced(apig_env, monkeypatch):
    body = {"error": {"message": "Authentication Error, Invalid proxy server token passed.", "type": "auth_error"}}
    monkeypatch.setattr(apig, "requests_request", lambda *a, **k: FakeResponse(401, body))
    with pytest.raises(apig.ApigError) as excinfo:
        apig.fetch_models(ID_TOKEN, apig_env)
    assert "Invalid proxy server token" in str(excinfo.value)
