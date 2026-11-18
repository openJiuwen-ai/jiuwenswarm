# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the user-input length gate."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.input_guard import (
    DEFAULT_MESSAGE_EN,
    DEFAULT_MESSAGE_ZH,
    InputBudget,
    InputGuardConfig,
    InputTooLongError,
    estimate_text_tokens,
    evaluate_input_guard,
    parse_input_guard_config,
)
from jiuwenswarm.common.schema.message import ReqMethod


class _Counter:
    """One token per character, with a configurable measurement source."""

    def __init__(self, *, source: str = "native_tokenizer", tools_tokens: int = 0):
        self.measurement_source = source
        self._tools_tokens = tools_tokens

    def count(self, text: str, **_kwargs) -> int:
        return len(text)

    def count_tools(self, tools, **_kwargs) -> int:
        return self._tools_tokens


class _Context:
    def __init__(self, *, window: int = 10_000, counter=None):
        self._window = window
        self._counter = counter

    def token_counter(self):
        return self._counter

    def context_window_tokens(self) -> int:
        return self._window


class _Engine:
    def __init__(self, context):
        self._context = context

    def get_context(self, *, session_id: str):
        return self._context


class _AbilityManager:
    registry_revision = 1

    def __init__(self, cards=None):
        self._cards = cards or []

    def list(self):
        return list(self._cards)

    async def list_tool_info(self):
        return [
            SimpleNamespace(
                name=getattr(card, "name", ""),
                description=getattr(card, "description", "") or "",
                parameters=getattr(card, "input_params", {}),
            )
            for card in self._cards
        ]


class _ReactAgent:
    def __init__(self, *, context, manager):
        self.context_engine = _Engine(context)
        self.ability_manager = manager


class _Instance:
    def __init__(self, *, context, manager, window: int = 10_000):
        self.react_agent = _ReactAgent(context=context, manager=manager)
        self._window = window

    def get_context_usage(self, *, session_id: str):
        return {"context_window_tokens": self._window}


def _adapter(*, window: int = 10_000, counter=None, cards=None):
    # Imported lazily: the adapter module pulls in the full agent stack, which
    # would slow every pure-config test in this file.
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._session_adapters = {}
    adapter._config_cache = {
        "context_engine_config": {"context_window_tokens": window},
    }
    adapter._config_base_cache = {"preferred_language": "zh"}
    adapter._model_request_config = None
    adapter._last_system_prompt = ""
    adapter._input_guard_tools_cache = None
    adapter._input_guard_overhead_cache = {}
    adapter._input_guard_session_model_name = lambda _sid: ""
    adapter._instance = _Instance(
        context=_Context(window=window, counter=counter),
        manager=_AbilityManager(cards),
        window=window,
    )
    return adapter


def _request(*, req_method=ReqMethod.CHAT_SEND, params=None) -> SimpleNamespace:
    return SimpleNamespace(
        req_method=req_method,
        params=params or {},
        session_id="session-1",
        request_id="req-1",
        channel_id="web",
        metadata={},
    )


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_config_defaults_to_disabled():
    config = parse_input_guard_config(None)

    assert config.enabled is False
    assert config.max_input_ratio == 0.95
    assert config.precise_max_input_ratio == 0.99
    assert config.max_input_tokens == 0
    assert config.reserved_output_tokens == 0
    assert config.use_model_max_tokens_as_reserve is False
    assert config.fixed_overhead_tokens == 30_000
    assert config.message_zh == DEFAULT_MESSAGE_ZH
    assert config.message_en == DEFAULT_MESSAGE_EN


@pytest.mark.parametrize("raw", ["true", 1, 1.0, "on", "yes"])
def test_config_parses_truthy_switch(raw):
    assert parse_input_guard_config({"enabled": raw}).enabled is True


@pytest.mark.parametrize("raw", ["false", 0, "off", "no"])
def test_config_parses_falsy_switch(raw):
    assert parse_input_guard_config({"enabled": raw}).enabled is False


@pytest.mark.parametrize("raw", [0, -0.1, 1.1, "abc", None, True])
def test_config_rejects_invalid_ratio(raw):
    assert parse_input_guard_config({"max_input_ratio": raw}).max_input_ratio == 0.95


@pytest.mark.parametrize("raw", [-1, "abc", None, True])
def test_config_rejects_invalid_limits(raw):
    config = parse_input_guard_config(
        {"max_input_tokens": raw, "reserved_output_tokens": raw}
    )

    assert config.max_input_tokens == 0
    assert config.reserved_output_tokens == 0


def test_config_falls_back_on_bad_template():
    config = parse_input_guard_config(
        {"message_zh": "no placeholders", "message_en": "{bad}"}
    )

    # A static message is a deliberate customization; only an unformattable
    # template falls back to the default.
    assert config.message_zh == "no placeholders"
    assert config.message_en == DEFAULT_MESSAGE_EN


def test_config_ignores_unknown_keys():
    config = parse_input_guard_config({"enabled": True, "unknown": 1})

    assert config.enabled is True


def test_config_parses_precise_ratio_and_reserve_switch():
    config = parse_input_guard_config(
        {"precise_max_input_ratio": 0.99, "use_model_max_tokens_as_reserve": "true"}
    )

    assert config.precise_max_input_ratio == 0.99
    assert config.use_model_max_tokens_as_reserve is True


@pytest.mark.parametrize("raw", [0, 1.1, "bad"])
def test_config_rejects_invalid_precise_ratio(raw):
    config = parse_input_guard_config({"precise_max_input_ratio": raw})

    assert config.precise_max_input_ratio == 0.99


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def test_heuristic_counts_cjk_per_character_and_ascii_per_four():
    text = "中文输入" + "abcd" * 2  # 4 CJK + 8 ASCII

    assert estimate_text_tokens(text) == 4 + (8 + 3) // 4


def test_precise_counter_is_used():
    assert estimate_text_tokens("abcdef", _Counter()) == 6


def test_string_length_fallback_is_replaced_by_cjk_heuristic():
    fallback = _Counter(source="string_length_fallback")
    text = "中" * 400

    # len // 3 would report 133; the gate must count one token per CJK char.
    assert estimate_text_tokens(text, fallback) == 400


def test_counter_failure_falls_back_to_heuristic():
    class _BrokenCounter:
        measurement_source = "native_tokenizer"

        def count(self, _text):
            raise RuntimeError("boom")

    assert estimate_text_tokens("中文", _BrokenCounter()) == 2


def test_zero_returning_counter_falls_back_to_heuristic():
    class _ZeroCounter:
        measurement_source = "native_tokenizer"

        def count(self, _text):
            return 0

    assert estimate_text_tokens("中文", _ZeroCounter()) == 2


def test_empty_text_is_zero():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens(None) == 0


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def test_disabled_config_never_rejects():
    decision = evaluate_input_guard(
        "x" * 100,
        config=InputGuardConfig(enabled=False),
        budget=InputBudget(window=100),
        token_counter=_Counter(),
    )

    assert decision.rejected is False
    assert decision.reason == "disabled"


def test_boundary_equal_passes_and_one_over_rejects():
    budget = InputBudget(window=1000)
    config = InputGuardConfig(
        enabled=True, max_input_ratio=0.95, precise_max_input_ratio=0.95
    )

    assert evaluate_input_guard(
        "x" * 950, config=config, budget=budget, token_counter=_Counter()
    ).rejected is False
    assert evaluate_input_guard(
        "x" * 951, config=config, budget=budget, token_counter=_Counter()
    ).rejected is True


def test_absolute_limit_wins_when_smaller():
    budget = InputBudget(window=1_000_000)
    config = InputGuardConfig(enabled=True, max_input_ratio=0.95, max_input_tokens=100)

    decision = evaluate_input_guard(
        "x" * 101, config=config, budget=budget, token_counter=_Counter()
    )

    assert decision.limit == 100
    assert decision.rejected is True


def test_precise_counter_uses_precise_ratio():
    budget = InputBudget(window=1000)
    config = InputGuardConfig(enabled=True, max_input_ratio=0.95)

    decision = evaluate_input_guard(
        "x" * 990, config=config, budget=budget, token_counter=_Counter()
    )

    assert decision.limit == 990
    assert decision.rejected is False
    assert evaluate_input_guard(
        "x" * 991, config=config, budget=budget, token_counter=_Counter()
    ).rejected is True


def test_heuristic_counter_uses_conservative_ratio():
    budget = InputBudget(window=1000)
    config = InputGuardConfig(enabled=True, max_input_ratio=0.95)

    decision = evaluate_input_guard(
        "测" * 950, config=config, budget=budget, token_counter=None
    )

    assert decision.limit == 950
    assert decision.rejected is False
    assert evaluate_input_guard(
        "测" * 951, config=config, budget=budget, token_counter=None
    ).rejected is True


def test_tiktoken_fallback_uses_conservative_ratio():
    budget = InputBudget(window=1000)
    config = InputGuardConfig(enabled=True, max_input_ratio=0.95)
    fallback = _Counter(source="tiktoken_fallback")

    decision = evaluate_input_guard(
        "测" * 951, config=config, budget=budget, token_counter=fallback
    )

    assert decision.limit == 950
    assert decision.rejected is True


def test_family_tokenizer_fallback_uses_conservative_ratio():
    budget = InputBudget(window=1000)
    config = InputGuardConfig(enabled=True, max_input_ratio=0.95)
    fallback = _Counter(source="family_tokenizer_fallback")

    decision = evaluate_input_guard(
        "测" * 951, config=config, budget=budget, token_counter=fallback
    )

    assert decision.limit == 950
    assert decision.rejected is True


def test_fixed_costs_reduce_usable_budget():
    budget = InputBudget(window=1000, system_tokens=100, tools_tokens=100, reserved_tokens=100)
    config = InputGuardConfig(
        enabled=True, max_input_ratio=0.95, precise_max_input_ratio=0.95
    )

    decision = evaluate_input_guard(
        "x" * 700, config=config, budget=budget, token_counter=_Counter()
    )

    assert decision.usable == 700
    assert decision.limit == 665
    assert decision.rejected is True


def test_budget_exhausted_does_not_blame_the_input():
    budget = InputBudget(window=100, system_tokens=100)
    config = InputGuardConfig(enabled=True)

    decision = evaluate_input_guard(
        "x" * 1000, config=config, budget=budget, token_counter=_Counter()
    )

    assert decision.rejected is False
    assert decision.reason == "budget_exhausted"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_message_formats_numbers_and_language():
    config = InputGuardConfig()

    zh = config.message("zh", tokens=312000, limit=180000)
    en = config.message("en", tokens=312000, limit=180000)

    assert "312,000" in zh and "180,000" in zh
    assert "312,000" in en and "180,000" in en
    assert zh != en


# ---------------------------------------------------------------------------
# Adapter wiring
# ---------------------------------------------------------------------------


def _enable_adapter(adapter, **overrides) -> None:
    config = dict(adapter._config_cache)
    config["input_guard_config"] = {
        "enabled": True,
        "max_input_ratio": 0.95,
        **overrides,
    }
    adapter._config_cache = config


async def test_adapter_rejects_oversized_input_with_formatted_message():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(_request(), "x" * 1000)

    error = exc_info.value
    assert error.tokens == 1000
    assert error.limit == 990
    assert "1,000" in str(error)
    assert "990" in str(error)
    assert "已拦截" in str(error)


async def test_adapter_passes_input_within_limit():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(_request(), "x" * 900)


async def test_adapter_is_noop_when_disabled():
    adapter = _adapter(window=1000, counter=_Counter())
    adapter._config_cache = {"input_guard_config": {"enabled": False}}

    await adapter.check_user_input_guard(_request(), "x" * 10_000)


async def test_adapter_skips_non_chat_methods():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(
        _request(req_method=ReqMethod.CHAT_CANCEL), "x" * 10_000
    )


async def test_adapter_skips_non_text_queries():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(_request(), {"type": "interactive"})
    await adapter.check_user_input_guard(_request(), "   ")


async def test_adapter_skips_permission_and_client_tool_resume():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(
        _request(params={"source": "permission_interrupt", "answers": [{"id": "1"}]}),
        "x" * 10_000,
    )
    await adapter.check_user_input_guard(
        _request(params={"source": "client_tool_interrupt"}),
        "x" * 10_000,
    )


async def test_adapter_skips_automation_and_internal_turns():
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)

    for params in (
        {"automation": {"kind": "heartbeat"}},
        {"source": "cron"},
        {"log_as_user": False},
        {"attach_goal": True},
    ):
        await adapter.check_user_input_guard(_request(params=params), "x" * 10_000)


async def test_adapter_budget_exhausted_does_not_reject(caplog):
    adapter = _adapter(window=1000, counter=_Counter())
    # system prompt is counted through the counter; make it fill the window.
    adapter._last_system_prompt = "s" * 1000
    _enable_adapter(adapter)

    with caplog.at_level("WARNING"):
        await adapter.check_user_input_guard(_request(), "x" * 100)

    assert "budget exhausted" in caplog.text


async def test_adapter_tools_tokens_counted_once_per_registry_revision():
    calls = {"tools": 0}

    class _CountingCounter(_Counter):
        def count_tools(self, tools, **_kwargs):
            calls["tools"] += 1
            return 100

    card = SimpleNamespace(name="read_file", description="read a file", input_params={})
    adapter = _adapter(window=10_000, counter=_CountingCounter(), cards=[card])
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(_request(), "x" * 10)
    await adapter.check_user_input_guard(_request(), "x" * 10)

    assert calls["tools"] == 1


async def test_adapter_skips_when_window_unresolved(monkeypatch):
    adapter = _adapter(window=1000, counter=_Counter())
    _enable_adapter(adapter)
    adapter._config_base_cache = {}
    adapter._resolve_model_name = lambda: ""
    adapter._model_request_config = None
    adapter._get_agent_system_prompt = lambda: ""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.resolve_context_window_tokens",
        lambda *args, **kwargs: 0,
    )

    await adapter.check_user_input_guard(_request(), "x" * 10_000)


def _root_adapter(*, window=100_000, max_tokens=20_000, fixed_overhead=30_000):
    """An adapter without a live instance, as the first message of a session."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._session_adapters = {}
    adapter._instance = None
    adapter._config_cache = {
        "input_guard_config": {
            "enabled": True,
            "fixed_overhead_tokens": fixed_overhead,
        },
        "context_engine_config": {},
    }
    adapter._config_base_cache = {
        "preferred_language": "zh",
        "models": {
            "defaults": [
                {
                    "is_default": True,
                    "alias": "m-alias",
                    "model_id": "mdl_m",
                    "model_client_config": {"model_name": "m"},
                    "model_config_obj": {
                        "context_window": window,
                        "max_tokens": max_tokens,
                    },
                }
            ]
        },
    }
    adapter._model_request_config = None
    adapter._last_system_prompt = ""
    adapter._input_guard_tools_cache = None
    adapter._input_guard_overhead_cache = {}
    return adapter


async def test_static_budget_guards_first_message_without_live_instance():
    adapter = _root_adapter(window=100_000, max_tokens=20_000, fixed_overhead=30_000)

    # usable = 100000 - 30000 - 0 = 70000; limit = 66500
    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_name": "m"}), "测" * 66_501
        )

    assert exc_info.value.limit == 66_500
    assert exc_info.value.window == 100_000
    assert exc_info.value.system_tokens == 30_000
    assert exc_info.value.reserved_tokens == 0

    await adapter.check_user_input_guard(_request(params={"model_name": "m"}), "测" * 66_500)


async def test_static_budget_honours_absolute_limit():
    adapter = _root_adapter(window=1_000_000, max_tokens=0, fixed_overhead=0)
    adapter._config_cache["input_guard_config"]["max_input_tokens"] = 5000

    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_name": "m"}), "测" * 5001
        )

    assert exc_info.value.limit == 5000


async def test_static_budget_resolves_model_selection_id():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)
    adapter._config_base_cache["models"]["defaults"].append(
        {
            "model_id": "mdl_other",
            "model_client_config": {"model_name": "other"},
            "model_config_obj": {"context_window": 5_000, "max_tokens": 0},
        }
    )

    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_selection": {"type": "model", "id": "mdl_m"}}),
            "测" * 66_501,
        )

    assert exc_info.value.window == 100_000
    assert exc_info.value.reserved_tokens == 0


async def test_static_budget_searches_all_model_sections():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)
    adapter._config_cache["input_guard_config"]["use_model_max_tokens_as_reserve"] = True
    adapter._config_base_cache["models"]["agentos"] = [
        {
            "model_id": "mdl_agent",
            "model_client_config": {"model_name": "agent-model"},
            "model_config_obj": {"context_window": 50_000, "max_tokens": 10_000},
        }
    ]

    # usable = 50000 - 30000 - 10000 = 10000; limit = 9500
    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_selection": {"type": "model", "id": "mdl_agent"}}),
            "测" * 9_501,
        )

    assert exc_info.value.window == 50_000
    assert exc_info.value.reserved_tokens == 10_000


async def test_static_budget_reserves_nothing_by_default():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)

    # usable = 100000 - 30000 - 0 = 70000; limit = 66500
    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_name": "m"}), "测" * 66_501
        )

    assert exc_info.value.reserved_tokens == 0
    assert exc_info.value.limit == 66_500


async def test_static_budget_opt_in_model_max_tokens_reserve():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)
    adapter._config_cache["input_guard_config"]["use_model_max_tokens_as_reserve"] = True

    # usable = 100000 - 30000 - 20000 = 50000; limit = 47500
    with pytest.raises(InputTooLongError) as exc_info:
        await adapter.check_user_input_guard(
            _request(params={"model_name": "m"}), "测" * 47_501
        )

    assert exc_info.value.reserved_tokens == 20_000
    assert exc_info.value.limit == 47_500


async def test_static_budget_explicit_reserve_wins():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)
    adapter._config_cache["input_guard_config"]["reserved_output_tokens"] = 5_000
    adapter._config_cache["input_guard_config"]["use_model_max_tokens_as_reserve"] = True

    budget, _counter, _model = await adapter._input_guard_budget(
        _request(params={"model_name": "m"}), adapter._input_guard_config()
    )

    assert budget.reserved_tokens == 5_000


async def test_model_group_selection_id_is_not_treated_as_model_id():
    adapter = _root_adapter(window=100_000, max_tokens=20_000)
    adapter._input_guard_session_model_name = lambda _sid: ""
    adapter._config_base_cache["models"]["agentos"] = [
        {
            "model_id": "grp_1",
            "model_client_config": {"model_name": "group-model"},
            "model_config_obj": {"context_window": 5_000, "max_tokens": 0},
        }
    ]

    budget, _counter, model = await adapter._input_guard_budget(
        _request(
            params={
                "model_selection": {
                    "type": "model_group",
                    "id": "grp_1",
                    "name": "m-alias",
                }
            }
        ),
        adapter._input_guard_config(),
    )

    # A group id/name must not match a concrete model; with no concrete model
    # the product default window applies.
    assert model == ""
    assert budget.window == 262_144


async def test_window_uses_session_adapter_override():
    adapter = _adapter(window=10_000, counter=_Counter())
    adapter._input_guard_session_model_name = lambda _sid: "unlisted-model"
    adapter._config_cache = {
        "input_guard_config": {"enabled": True},
        "context_engine_config": {},
    }
    adapter._selected_model_context_window_tokens = 1_048_576

    budget, _counter, _model = await adapter._input_guard_budget(
        _request(), adapter._input_guard_config()
    )

    assert budget.window == 1_048_576


async def test_window_override_ignored_when_live_model_differs():
    adapter = _adapter(window=10_000, counter=_Counter())
    adapter._input_guard_session_model_name = lambda _sid: "new-model"
    adapter._config_cache = {
        "input_guard_config": {"enabled": True},
        "context_engine_config": {},
    }
    adapter._model_request_config = SimpleNamespace(model_name="old-model")
    adapter._selected_model_context_window_tokens = 131_072

    budget, _counter, model = await adapter._input_guard_budget(
        _request(), adapter._input_guard_config()
    )

    # The cached adapter still belongs to the previous model; its captured
    # window must not be applied to the new one.
    assert model == "new-model"
    assert budget.window == 262_144


async def test_overhead_cache_feeds_static_path():
    adapter = _adapter(window=10_000, counter=_Counter())
    adapter._last_system_prompt = "x" * 1000
    adapter._input_guard_session_model_name = lambda _sid: "m"
    adapter._config_base_cache = {
        "preferred_language": "zh",
        "models": {
            "defaults": [
                {
                    "is_default": True,
                    "model_id": "mdl_m",
                    "model_client_config": {"model_name": "m"},
                    "model_config_obj": {"context_window": 10_000},
                }
            ]
        },
    }
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(_request(), "x" * 10)
    assert adapter._input_guard_overhead_cache.get("m") == 1000

    adapter._instance = None
    budget, _counter, model = await adapter._input_guard_budget(
        _request(), adapter._input_guard_config()
    )

    assert model == "m"
    assert budget.system_tokens == 1000
    assert budget.tools_tokens == 0


async def test_overhead_cache_keeps_largest_measurement():
    adapter = _adapter(window=10_000, counter=_Counter())
    adapter._last_system_prompt = "x" * 1000
    adapter._input_guard_session_model_name = lambda _sid: "m"
    adapter._config_base_cache = {
        "preferred_language": "zh",
        "models": {
            "defaults": [
                {
                    "is_default": True,
                    "model_id": "mdl_m",
                    "model_client_config": {"model_name": "m"},
                    "model_config_obj": {"context_window": 10_000},
                }
            ]
        },
    }
    _enable_adapter(adapter)

    await adapter.check_user_input_guard(_request(), "x" * 10)
    assert adapter._input_guard_overhead_cache["m"] == 1000

    # A transiently smaller prompt measurement must not replace the larger one.
    adapter._last_system_prompt = "x" * 400
    await adapter.check_user_input_guard(_request(), "x" * 10)
    assert adapter._input_guard_overhead_cache["m"] == 1000


async def test_empty_list_tool_info_does_not_fall_back_to_cards():
    class _EmptyListManager(_AbilityManager):
        async def list_tool_info(self):
            return []

    card = SimpleNamespace(name="read_file", description="read a file", input_params={})
    adapter = _adapter(window=10_000, counter=_Counter(tools_tokens=100))
    adapter._instance.react_agent.ability_manager = _EmptyListManager([card])
    adapter._input_guard_tools_cache = None

    tokens = await adapter._input_guard_tools_tokens(
        adapter._instance.react_agent, _Counter(tools_tokens=100)
    )

    assert tokens == 0


# ---------------------------------------------------------------------------
# Facade wiring
# ---------------------------------------------------------------------------


async def test_facade_helper_invokes_adapter_and_propagates():
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _check_user_input_guard,
    )

    calls = []

    class _CheckingAdapter:
        async def check_user_input_guard(self, request, query):
            calls.append((request, query))
            raise InputTooLongError(
                "too long",
                tokens=10,
                limit=5,
                window=5,
                system_tokens=0,
                tools_tokens=0,
                reserved_tokens=0,
            )

    request = _request()
    with pytest.raises(InputTooLongError):
        await _check_user_input_guard(_CheckingAdapter(), request, "query")

    assert calls == [(request, "query")]


async def test_facade_helper_supports_sync_gate():
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _check_user_input_guard,
    )

    calls = []

    class _SyncAdapter:
        def check_user_input_guard(self, request, query):
            calls.append((request, query))
            raise InputTooLongError(
                "too long",
                tokens=10,
                limit=5,
                window=5,
                system_tokens=0,
                tools_tokens=0,
                reserved_tokens=0,
            )

    with pytest.raises(InputTooLongError):
        await _check_user_input_guard(_SyncAdapter(), _request(), "query")

    assert len(calls) == 1


async def test_facade_helper_is_noop_for_adapters_without_the_gate():
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _check_user_input_guard,
    )

    class _PlainAdapter:
        pass

    await _check_user_input_guard(_PlainAdapter(), _request(), "query")


async def test_facade_helper_swallows_unexpected_errors(caplog):
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _check_user_input_guard,
    )

    class _BrokenAdapter:
        async def check_user_input_guard(self, _request, _query):
            raise RuntimeError("boom")

    with caplog.at_level("WARNING"):
        await _check_user_input_guard(_BrokenAdapter(), _request(), "query")

    assert "input guard check failed" in caplog.text
