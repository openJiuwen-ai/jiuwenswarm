# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FactPremiseObservationRail 单元测试：只测观测，不测行为。

rail 不改任何行为，所以只需断言「该告警时告警、不该告警时不告警、异常不抛」，
不需要测模型行为正确性。

用 monkeypatch 捕获 ``logger.warning`` 而非 caplog——jiuwenswarm 自定义 logging
会给 logger 挂自己的 handler，caplog 的 root handler 捕获不到。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import jiuwenswarm.agents.harness.common.rails.fact_premise_observation_rail as _rail_mod
from jiuwenswarm.agents.harness.common.rails.fact_premise_observation_rail import (
    FactPremiseObservationRail,
)


def _user_ctx(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(parts=[text], source="query"),
        extra={},
        session=None,
    )


def _tool_ctx(tool_name: str, extra: dict, tool_args=None) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(tool_name=tool_name, tool_args=tool_args),
        extra=extra,
        session=None,
    )


def _model_ctx(content: str, extra: dict) -> SimpleNamespace:
    response = SimpleNamespace(tool_calls=None, content=content)
    return SimpleNamespace(
        inputs=SimpleNamespace(response=response), extra=extra, session=None
    )


def _capture(monkeypatch) -> list:
    """捕获 logger.warning 调用，返回 [(fmt, args), ...]。"""
    calls: list = []
    monkeypatch.setattr(
        _rail_mod.logger, "warning", lambda fmt, *args: calls.append((fmt, args))
    )
    return calls


@pytest.mark.asyncio
async def test_warns_on_zero_retrieval_year_assertion(monkeypatch):
    """零检索 + 带年份的事件断言 → 告警，且告警是结构化 JSON。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    assert "2028" in ctx.extra["_fpo_years"]

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2028年世界杯冠军是巴西队。", ctx.extra))

    assert len(calls) == 1
    assert "fact_premise_observation" in calls[0][0]
    payload = calls[0][1][0]
    assert '"retrieved": false' in payload
    assert "2028" in payload


@pytest.mark.asyncio
async def test_english_question_warns(monkeypatch):
    """英文路径：年份 + 英文断言词（won）也应告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("Who won the 2028 World Cup?")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    assert "2028" in ctx.extra["_fpo_years"]

    calls = _capture(monkeypatch)
    await rail.after_model_call(
        _model_ctx("Brazil won the 2028 World Cup.", ctx.extra)
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_warn_when_retrieved(monkeypatch):
    """本轮检索过 → 不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)

    await rail.after_tool_call(_tool_ctx("web_flash", ctx.extra))
    assert ctx.extra["_fpo_retrieved"] is True

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2028年世界杯冠军是巴西队。", ctx.extra))
    assert not calls


@pytest.mark.asyncio
async def test_deepresearch_execute_marks_retrieved():
    """deepresearch 真名（deepresearch_execute）也算检索。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    await rail.after_tool_call(_tool_ctx("deepresearch_execute", ctx.extra))
    assert ctx.extra["_fpo_retrieved"] is True


@pytest.mark.asyncio
async def test_invoke_tool_proxy_marks_retrieved():
    """invoke_tool 代理 deferred 检索工具 → 解包真实工具名并标记检索。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    await rail.after_tool_call(
        _tool_ctx("invoke_tool", ctx.extra, {"tool_name": "web_flash"})
    )
    assert ctx.extra["_fpo_retrieved"] is True


@pytest.mark.asyncio
async def test_task_tool_browser_agent_marks_retrieved():
    """task_tool 派 browser_agent 子代理 → 也算检索。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    await rail.after_tool_call(
        _tool_ctx("task_tool", ctx.extra, {"subagent_type": "browser_agent"})
    )
    assert ctx.extra["_fpo_retrieved"] is True


@pytest.mark.asyncio
async def test_no_observe_without_year_anchor(monkeypatch):
    """用户消息无年份/届次词 → 不开启观测，后续不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("帮我写一份周报")
    await rail.on_user_message(ctx)
    assert ctx.extra.get("_fpo_observing") is not True

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("好的，周报写好了。", ctx.extra))
    assert not calls


@pytest.mark.asyncio
async def test_envelope_timestamp_does_not_trigger():
    """信封里的 timestamp 年份不应触发观测，只有 content 里的年份才算。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx(
        '你收到一条消息：\n{"source": "officeclaw", "timestamp": "2026-11-26 14:30:00", '
        '"content": "帮我写个周报", "type": "user input", "preferred_response_language": "zh"}'
    )
    await rail.on_user_message(ctx)
    assert ctx.extra.get("_fpo_observing") is not True


@pytest.mark.asyncio
async def test_envelope_content_year_triggers():
    """信封 content 里的年份触发观测，锚点是 content 的年份而非 timestamp 的。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx(
        '你收到一条消息：\n{"source": "officeclaw", "timestamp": "2026-11-26 14:30:00", '
        '"content": "2028年世界杯冠军是谁？", "type": "user input", '
        '"preferred_response_language": "zh"}'
    )
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    assert "2028" in ctx.extra["_fpo_years"]
    assert "2026" not in ctx.extra["_fpo_years"]


@pytest.mark.asyncio
async def test_past_year_does_not_trigger():
    """已定论的过去年份（2015）不触发观测——零检索不一定是问题。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2015年奥斯卡最佳影片是谁？")
    await rail.on_user_message(ctx)
    assert ctx.extra.get("_fpo_observing") is not True


@pytest.mark.asyncio
async def test_relative_year_with_past_year_answer_does_not_warn(monkeypatch):
    """相对年份触发 + 答案含已定论过去年份 → 输出侧窗口过滤，不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("今年谁夺冠")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    assert ctx.extra["_fpo_years"] == []

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2015年夺冠的是巴西队。", ctx.extra))
    assert not calls


@pytest.mark.asyncio
async def test_followup_without_year_keeps_observation():
    """同一 invoke 内无年份的 follow-up 消息不清空原观测窗口（避免漏报）。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True

    # 同一 ctx 的 follow-up 消息（无年份词）：不清空，保留原观测窗口
    ctx.inputs.parts = ["帮我写个周报"]
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    assert ctx.extra["_fpo_years"] == ["2028"]


@pytest.mark.asyncio
async def test_no_warn_on_refusal(monkeypatch):
    """正确否认（无法确认/不存在）是期望行为，不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)

    calls = _capture(monkeypatch)
    await rail.after_model_call(
        _model_ctx("无法确认2028年世界杯冠军，因为2028年没有世界杯。", ctx.extra)
    )
    assert not calls


@pytest.mark.asyncio
async def test_no_warn_without_assertion_word(monkeypatch):
    """答案里有年份但没有事件断言词（泛泛提到年份）→ 不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2028年距离现在还有两年。", ctx.extra))
    assert not calls


@pytest.mark.asyncio
async def test_edition_word_triggers_observation():
    """届次词（上届）也算时间锚点。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("上届冠军是谁")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True


@pytest.mark.asyncio
async def test_fail_open_on_bad_inputs():
    """各种坏输入都不该抛异常（fail-open）。"""
    rail = FactPremiseObservationRail()
    await rail.on_user_message(SimpleNamespace(inputs=None, extra=None, session=None))
    await rail.on_user_message(
        SimpleNamespace(inputs=SimpleNamespace(parts=None), extra=None, session=None)
    )
    await rail.after_tool_call(SimpleNamespace(inputs=None, extra=None, session=None))
    await rail.after_model_call(SimpleNamespace(inputs=None, extra=None, session=None))
    await rail.after_model_call(
        SimpleNamespace(inputs=SimpleNamespace(response=None), extra=None, session=None)
    )


@pytest.mark.asyncio
async def test_state_is_isolated_per_invoke():
    """状态放 ctx.extra（invoke 作用域），两个 ctx 互不污染。"""
    rail = FactPremiseObservationRail()
    ctx_a = _user_ctx("2028年世界杯冠军是谁？")
    ctx_b = _user_ctx("帮我写个 PPT")
    await rail.on_user_message(ctx_a)
    await rail.on_user_message(ctx_b)
    assert ctx_a.extra.get("_fpo_observing") is True
    assert ctx_b.extra.get("_fpo_observing") is not True


@pytest.mark.asyncio
async def test_json_contains_structured_fields(monkeypatch):
    """告警 JSON 含 rail / session_id / retrieved / year_anchors / answer_claim 字段。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2028年世界杯冠军是巴西队。", ctx.extra))

    assert calls
    payload = calls[0][1][0]
    for key in ("rail", "session_id", "retrieved", "year_anchors", "answer_claim"):
        assert re.search(rf'"{key}"', payload), f"缺字段 {key}"


@pytest.mark.asyncio
async def test_tools_search_does_not_mark_retrieved():
    """tools_search 是元工具（查 schema），不应标记为已检索。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    await rail.after_tool_call(_tool_ctx("tools_search", ctx.extra))
    assert ctx.extra["_fpo_retrieved"] is False


@pytest.mark.asyncio
async def test_tools_search_then_direct_answer_still_warns(monkeypatch):
    """查 schema（tools_search）后直接作答带年份断言 → 仍告警（tools_search 不是真检索）。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    await rail.after_tool_call(_tool_ctx("tools_search", ctx.extra))
    assert ctx.extra["_fpo_retrieved"] is False  # tools_search 不算检索

    calls = _capture(monkeypatch)
    await rail.after_model_call(_model_ctx("2028年世界杯冠军是巴西队。", ctx.extra))
    assert len(calls) == 1  # 零真检索 + 带年份断言 → 仍告警


@pytest.mark.asyncio
async def test_steering_does_not_clear_observation():
    """steering 消息（source != query）不清掉原观测窗口。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True
    ctx2 = SimpleNamespace(
        inputs=SimpleNamespace(parts=["请用英语回答"], source="steering"),
        extra=ctx.extra,
        session=None,
    )
    await rail.on_user_message(ctx2)
    assert ctx.extra["_fpo_observing"] is True  # 窗口仍在


@pytest.mark.asyncio
async def test_long_id_year_not_extracted():
    """长编号里的四位子串不应被误认为年份。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("请核对订单 12028123 的名称")
    await rail.on_user_message(ctx)
    assert ctx.extra.get("_fpo_observing") is not True


@pytest.mark.asyncio
async def test_negated_assertion_does_not_alert(monkeypatch):
    """「不会举办」是否定式，不是虚假断言，不告警。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("2028年会举办男子足球世界杯吗？")
    await rail.on_user_message(ctx)
    calls = _capture(monkeypatch)
    await rail.after_model_call(
        _model_ctx("2028年不会举办男子足球世界杯。", ctx.extra)
    )
    assert not calls


@pytest.mark.asyncio
async def test_english_relative_year_triggers():
    """英文相对年份词（this year）也能触发观测。"""
    rail = FactPremiseObservationRail()
    ctx = _user_ctx("Who won the championship this year?")
    await rail.on_user_message(ctx)
    assert ctx.extra["_fpo_observing"] is True


@pytest.mark.asyncio
async def test_window_opened_logged(monkeypatch):
    """开观测窗口时打 info 日志（窗口数 = 分母）。"""
    rail = FactPremiseObservationRail()
    info_calls = []
    monkeypatch.setattr(
        _rail_mod.logger, "info",
        lambda fmt, *args: info_calls.append((fmt, args)),
    )
    ctx = _user_ctx("2028年世界杯冠军是谁？")
    await rail.on_user_message(ctx)
    assert any("window_opened" in c[1][0] for c in info_calls)
