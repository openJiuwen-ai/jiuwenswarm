"""模型调用错误人性化的回归测试。

背景：配置页「测试连接」此前把 ``str(exc)`` 原样回传前端，DeepSeek 账户欠费时
用户看到的是 ``[181001] model call failed ... APIStatusError: Error code: 402 -
{'error': {'message': 'Insufficient Balance ...'}}`` 这类内部包装文本，既读不懂
也不知道该做什么。修复后错误出口应返回可执行的中文提示。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.model_error_messages import (
    humanize_model_error,
    is_non_retryable_model_error,
    user_facing_model_error,
)

BALANCE_ERROR = (
    "[181001] model call failed, reason: openAI API async invoke error: "
    "APIStatusError: Error code: 402 - {'error': {'message': "
    "'Insufficient Balance (request_id: 8541f2ec-b586-45e9-ac5a-160b708f752d)', "
    "'type': 'insufficient_balance_error', 'param': None, "
    "'code': 'invalid_request_error'}}"
)
DEEPSEEK_BASE = "https://api.deepseek.com/v1"


class TestHumanizeModelError:
    """错误文本 → 用户提示的映射。"""

    def test_insufficient_balance_is_actionable(self):
        message = humanize_model_error(BALANCE_ERROR, api_base=DEEPSEEK_BASE)
        assert message is not None
        assert "余额不足" in message
        assert "充值" in message
        assert "重新测试" in message
        assert "platform.deepseek.com/top_up" in message
        assert "181001" not in message
        assert "APIStatusError" not in message
        assert "Insufficient Balance" not in message

    def test_balance_hint_falls_back_to_host_for_unknown_provider(self):
        message = humanize_model_error(
            "Error code: 402 Insufficient Balance",
            api_base="https://my-llm.example.com/v1",
        )
        assert message is not None
        assert "my-llm.example.com" in message

    def test_balance_marked_as_non_retryable(self):
        assert is_non_retryable_model_error(BALANCE_ERROR) is True

    @pytest.mark.parametrize(
        ("raw", "keyword"),
        [
            ("APIStatusError: Error code: 401 - Invalid API key", "API Key"),
            ("model_not_found: the model does not exist", "模型名称"),
            ("Error code: 429 - rate limit reached", "稍后"),
            ("Connection refused by upstream", "无法连接"),
            ("Error code: 503 - service unavailable", "暂时不可用"),
        ],
    )
    def test_other_categories_are_humanized(self, raw: str, keyword: str):
        message = humanize_model_error(raw)
        assert message is not None
        assert keyword in message

    def test_nested_cause_is_detected(self):
        inner = RuntimeError("Insufficient Balance")
        outer = RuntimeError("model call failed")
        outer.__cause__ = inner
        assert is_non_retryable_model_error(outer) is True

    def test_unknown_short_error_is_kept(self):
        assert humanize_model_error("some odd failure") is None
        assert user_facing_model_error("some odd failure") == "some odd failure"

    def test_unrelated_timeout_is_not_treated_as_network_error(self):
        assert humanize_model_error("process CLI execution timed out") is None

    def test_internal_wrapper_is_compressed(self):
        message = user_facing_model_error("[181001] model call failed: " + "x" * 400)
        assert "181001" not in message
        assert "请检查" in message

    def test_empty_error_uses_fallback(self):
        assert user_facing_model_error(None, fallback="unavailable") == "unavailable"


class TestProbeRetryPolicy:
    """永久性错误不该为了换 max_tokens 再失败一次。"""

    @staticmethod
    def _fake_model(error: Exception):
        calls: list[int] = []

        class FakeModel:
            def invoke(self, **kwargs):
                calls.append(kwargs["max_tokens"])

                async def _fail():
                    raise error

                return _fail()

        return FakeModel(), calls

    def test_balance_error_aborts_after_first_attempt(self):
        from jiuwenswarm.common.model_config_validation import probe_model_connection

        model, calls = self._fake_model(RuntimeError(BALANCE_ERROR))
        with pytest.raises(RuntimeError):
            asyncio.run(probe_model_connection(model, token_limits=(3, 16)))
        assert calls == [3]

    def test_transient_error_still_uses_second_token_limit(self):
        from jiuwenswarm.common.model_config_validation import probe_model_connection

        model, calls = self._fake_model(ValueError("Empty response from model"))
        with pytest.raises(ValueError):
            asyncio.run(probe_model_connection(model, token_limits=(3, 16)))
        assert calls == [3, 16]


class TestCliErrorSummary:
    """CLI 摘要同样要给人话，但不能误伤非模型错误。"""

    def test_balance_error_is_humanized(self):
        from jiuwenswarm.channels.process_cli.render import _human_error_summary

        summary = _human_error_summary(BALANCE_ERROR)
        assert "余额不足" in summary
        assert "181001" not in summary

    def test_unrelated_error_keeps_original_translation(self):
        from jiuwenswarm.channels.process_cli.render import _human_error_summary

        assert "进程式 CLI 执行超时" in _human_error_summary(
            "process CLI execution timed out"
        )


class TestValidateModelHandler:
    """端到端：配置页「测试连接」收到的 error 字段就是友好提示。"""

    def test_probe_failure_returns_actionable_error(self, monkeypatch):
        import jiuwenswarm.common.config_panel.models_handlers as handlers
        from openjiuwen.core.foundation.llm import ProviderType

        async def fake_probe(*_args, **_kwargs):
            raise RuntimeError(BALANCE_ERROR)

        class FakeChannel:
            def __init__(self):
                self.calls: list[dict] = []

            async def send_response(self, ws, req_id, **kwargs):
                self.calls.append(kwargs)

        provider = next(
            (p.value for p in ProviderType if "openai" in str(p.value).lower()),
            next(iter(ProviderType)).value,
        )
        monkeypatch.setattr(handlers, "probe_model_connection", fake_probe)
        monkeypatch.setattr(handlers, "Model", lambda **_kwargs: object())
        monkeypatch.setattr(
            handlers,
            "ModelRequestConfig",
            lambda **_kwargs: SimpleNamespace(model_dump=lambda: {}),
        )
        monkeypatch.setattr(
            handlers, "build_reasoning_model_request_kwargs", lambda **_kwargs: {}
        )
        monkeypatch.setattr(
            handlers, "build_probe_model_client_config", lambda *_a, **_kw: {}
        )
        monkeypatch.setattr(
            handlers, "resolve_persisted_mcc_for_validate", lambda *_a, **_kw: {}
        )
        monkeypatch.setattr(
            handlers, "resolve_validate_endpoint_profile", lambda *_a, **_kw: "deepseek"
        )
        monkeypatch.setattr(
            handlers, "resolve_model_config_obj_for_validate", lambda *_a, **_kw: {}
        )

        channel = FakeChannel()
        params = {
            "api_base": DEEPSEEK_BASE,
            "api_key": "unit-test-placeholder",
            "model": "deepseek-flash",
            "model_provider": handlers.normalize_provider_value(provider),
        }
        asyncio.run(
            handlers.config_validate_model_handler(channel, object(), "req-1", params)
        )

        assert len(channel.calls) == 1
        response = channel.calls[0]
        assert response["ok"] is False
        assert response["code"] == "LLM_ERROR"
        assert "余额不足" in response["error"]
        assert "充值" in response["error"]
        assert "platform.deepseek.com/top_up" in response["error"]
        assert "181001" not in response["error"]
        assert "APIStatusError" not in response["error"]
