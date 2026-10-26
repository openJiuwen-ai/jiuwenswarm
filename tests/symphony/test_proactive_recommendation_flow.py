# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end integration tests for the proactive recommendation engine.

These tests mock out the LLM, WebSocket push, and session scanning to
verify the full Tick 1 → 2 → 3 flow:
  Tick 1: LLM discovers pain point → plans multi-stage path → pushes stage-1
  Tick 2: User accepted → path status="continue" → pushes stage-2
  Tick 3: User rejected → path status="rejected" → path deleted
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures ─────────────────────────────────────────────────────────

SAMPLE_SKILLS = [
    {"name": "auto-test-runner", "description": "监听文件变化自动跑测试", "tags": ["testing"], "installed": False},
    {"name": "test-coverage", "description": "生成测试覆盖率报告", "tags": ["testing"], "installed": False},
    {"name": "ci-pipeline", "description": "CI/CD 流水线配置生成器", "tags": ["ci"], "installed": False},
    {"name": "debug-helper", "description": "智能调试助手", "tags": ["debug"], "installed": False},
]

_PATCH_TARGETS = {
    "situation_report": "jiuwenswarm.agents.harness.common.recommendation.situation_report",
    "actions": "jiuwenswarm.agents.harness.common.recommendation.proactive_actions",
    "utils": "jiuwenswarm.common.utils",
}


def _fake_session_summary() -> MagicMock:
    s = MagicMock()
    s.session_id = "test-session-001"
    s.channel_id = "default"
    s.title = "Test Session"
    s.last_message_at = time.time()
    s.message_count = 5
    s.compressed_history = "[User]: 每次改完代码都要手动跑测试，好麻烦\n[Assistant]: 理解你的痛点。"
    s.delivery_context = {"channel_id": "default", "route_metadata": {}, "session_id": "test-session-001"}
    return s


def _fake_report() -> MagicMock:
    report = MagicMock()
    report.is_empty.return_value = False
    report.most_recent_active_session.return_value = _fake_session_summary()
    report.render_for_llm.return_value = "对话摘要：用户手动跑测试多次"
    report.sessions = [_fake_session_summary()]
    return report


def _write_state(ws_dir: Path, state_data: dict) -> Path:
    """Write recommendation.json into the workspace dir (where the engine reads/writes)."""
    state_path = ws_dir / "recommendation.json"
    state_path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_path


def _read_state(ws_dir: Path) -> dict:
    return json.loads((ws_dir / "recommendation.json").read_text(encoding="utf-8"))


class _MockProactiveAgent:
    """Mock dedicated agent: invoke() returns {"output": <JSON string>}."""

    def __init__(self, tick_responses: list[dict]):
        self._responses = tick_responses
        self._call_index = 0

    async def invoke(self, inputs, session=None):
        if self._call_index < len(self._responses):
            result = self._responses[self._call_index]
        else:
            result = None
        self._call_index += 1
        output = json.dumps(result, ensure_ascii=False) if result else ""
        return {"output": output, "result_type": "answer"}


def _make_proactive_agent(tick_responses: list[dict]):
    return _MockProactiveAgent(tick_responses)


# Capture _trigger_main_agent calls (session_id, channel_id, query, decision)
def _capture_trigger(triggered_list):
    async def _trigger(session_id, channel_id, query, decision, on_delivered=None):
        triggered_list.append({
            "session_id": session_id,
            "channel_id": channel_id,
            "query": query,
            "decision": decision,
        })
        # 真实 trigger_main_agent 是 fire-and-forget：主 agent 跑完才回调 on_delivered
        # 做 Step 7（计数 + save_recommendation_state）。测试 mock 这里同步模拟"后台送达"，
        # 立即回调，让 history/count 断言能验证 Step 7 逻辑。
        if on_delivered is not None:
            try:
                on_delivered()
            except Exception:
                # 测试只关心触发与 history，回调异常不掩盖触发本身
                pass
        return True
    return _trigger


# ── Tick 1: Discover pain point → push recommendation ────────

@pytest.mark.asyncio
async def test_tick1_recommend_skill():
    """Tick 1: LLM discovers pain point → recommends skill → saves to history."""
    emitted = []

    tick1_llm = {
        "decision": {
            "type": "need_exploration",
            "target": "auto-test-runner",
            "reason": "用户多次手动跑测试，痛点明确",
            "urgency": 0.8,
        },
    }

    dedicated = _make_proactive_agent([tick1_llm])

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [],
            "last_updated": "",
        })

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=0), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=True), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True})
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_capture_trigger(emitted))
            await engine.tick_now()

            # Verify main agent was triggered
            assert len(emitted) == 1, f"Should trigger 1 recommendation, got {len(emitted)}"

            # Verify profile was updated
            state_data = _read_state(ws_path)
            assert len(state_data["recommendation_history"]) == 1


# ── Tick 2: Continue recommending different skill ──────

@pytest.mark.asyncio
async def test_tick2_recommend_different_skill():
    """Tick 2: LLM recommends a different skill after first recommendation was accepted."""
    emitted = []

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [{
                "type": "need_exploration",
                "target": "auto-test-runner",
                "reason": "用户多次手动跑测试",
                "urgency": 0.8,
                "tick_at": time.time() - 3600,
                "session_id": "test-session-001",
            }],
            "last_updated": "",
        })

        tick2_llm = {
            "decision": {
                "type": "need_exploration",
                "target": "test-coverage",
                "reason": "用户已接受 auto-test-runner，继续推荐 test-coverage",
                "urgency": 0.6,
            },
        }

        dedicated = _make_proactive_agent([tick2_llm])

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=0), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=True), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True})
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_capture_trigger(emitted))
            await engine.tick_now()

            # Verify main agent was triggered
            assert len(emitted) == 1, f"Should trigger 1 recommendation, got {len(emitted)}"

            # Verify profile was updated with new recommendation
            state_data = _read_state(ws_path)
            assert len(state_data["recommendation_history"]) == 2


# ── Tick 3: No recommendation when cooldown active ─────

@pytest.mark.asyncio
async def test_tick3_cooldown_blocks_recommendation():
    """Tick 3: When target is in cooldown, no recommendation is emitted."""
    emitted = []

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [{
                "type": "need_exploration",
                "target": "auto-test-runner",
                "reason": "用户多次手动跑测试",
                "urgency": 0.8,
                "tick_at": time.time() - 7200,
                "session_id": "test-session-001",
            }],
            "cooldown_records": {
                "auto-test-runner": time.time() - 3600  # 1 hour ago, still in cooldown
            },
            "last_updated": "",
        })

        tick3_llm = {
            "decision": {
                "type": "skill_recommend",
                "target": "auto-test-runner",
                "reason": "再次推荐",
                "urgency": 0.4,
            },
        }

        dedicated = _make_proactive_agent([tick3_llm])

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=0), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=False), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True})
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_capture_trigger(emitted))
            await engine.tick_now()

            # No recommendation triggered (target in cooldown)
            assert len(emitted) == 0


# ── No recommendation when decision is null ───────

@pytest.mark.asyncio
async def test_no_recommendation_when_decision_null():
    """When LLM decides not to recommend, no recommendation is emitted."""
    emitted = []

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [{
                "type": "need_exploration",
                "target": "auto-test-runner",
                "reason": "痛点",
                "urgency": 0.8,
                "tick_at": time.time() - 7200,
                "session_id": "test-session-001",
            }],
            "last_updated": "",
        })

        tick_llm = {
            "decision": None,  # No recommendation
        }

        dedicated = _make_proactive_agent([tick_llm])

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=0), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=True), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True})
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_capture_trigger(emitted))
            await engine.tick_now()

            # No recommendation triggered (decision was null)
            assert len(emitted) == 0


# ── Daily limit gate ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_limit_blocks_tick():
    """When daily limit is reached, tick should skip without calling the dedicated agent."""
    call_counts = {"analyze": 0}

    class _CountingProactive:
        async def invoke(self, inputs, session=None):
            call_counts["analyze"] += 1
            return {"output": "{}", "result_type": "answer"}

    dedicated = _CountingProactive()

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [],
            "last_updated": "",
        })

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=5), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=True), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({
                "enabled": True,
                "max_recommend_per_day": 5,
            })
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_capture_trigger([]))
            await engine.tick_now()

            assert call_counts["analyze"] == 0, "dedicated agent should not be called when daily limit reached"


# ── 避让：目标 session 正忙时跳过 ──────────────────────────────

@pytest.mark.asyncio
async def test_skip_when_main_agent_busy():
    """trigger_callback 返回 False（session 正忙）时，引擎应跳过、不记冷却/计数。"""
    from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import (
        RecommendationDecision,
    )

    tick_llm = {
        "decision": {
            "type": "task_reminder", "target": "项目周会",
            "reason": "时间临近", "urgency": 0.9,
        },
    }
    dedicated = _make_proactive_agent([tick_llm])

    triggered = []

    async def _busy_trigger(session_id, channel_id, query, decision, on_delivered=None):
        triggered.append((session_id, channel_id, decision))
        return False  # session 正忙（不触发，on_delivered 不该被调）

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {
            "recommendation_history": [], "last_updated": "",
        })

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['situation_report']}.build_situation_report", return_value=_fake_report()), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=0), \
             patch(f"{_PATCH_TARGETS['actions']}._is_cooled_down", return_value=True), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine
            engine = ProactiveEngine({"enabled": True})
            engine.set_proactive_agent(dedicated)
            engine.set_trigger_main_agent_callback(_busy_trigger)
            result = await engine.tick_now()

            # 触发被调用一次但返回 False → 引擎跳过
            assert len(triggered) == 1
            assert result is False

            # 不应记冷却/推荐历史（因为没真正投递）
            state_data = _read_state(ws_path)
            assert len(state_data["recommendation_history"]) == 0


# ── 专用 agent JSON 解析 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_proactive_agent_output_parsed_to_decision():
    """专用 agent invoke 返回的 JSON 字符串应被解析为 decision + profile_delta。"""
    from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import (
        _analyze_and_decide, RecommendationDecision,
    )
    tick_llm = {
        "decision": {
            "type": "skill_recommend", "target": "auto-test-runner",
            "reason": "手动测试繁琐", "urgency": 0.7,
        },
    }
    dedicated = _make_proactive_agent([tick_llm])
    state = MagicMock()

    result = await _analyze_and_decide("report text", state, SAMPLE_SKILLS, dedicated)

    assert result.decision is not None
    assert result.decision.type == "skill_recommend"
    assert result.decision.target == "auto-test-runner"
    assert result.decision.urgency == 0.7


@pytest.mark.asyncio
async def test_proactive_agent_invalid_json_returns_empty():
    """专用 agent 返回非 JSON 时，应安全返回空 AnalysisResult。"""
    from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import (
        _analyze_and_decide,
    )

    class _BadAgent:
        async def invoke(self, inputs, session=None):
            return {"output": "这不是JSON", "result_type": "answer"}

    state = MagicMock()
    result = await _analyze_and_decide("report", state, SAMPLE_SKILLS, _BadAgent())
    assert result.decision is None


# ── Daily limit reached: push "limit reached" notification once per day ──


def _capture_notifications(notified_list):
    """Capture _send_notification_callback calls (channel_id, text)."""
    async def _notify(channel_id, text):
        notified_list.append({"channel_id": channel_id, "text": text})
        return True
    return _notify


@pytest.mark.asyncio
async def test_daily_limit_pushes_notification_once():
    """达到每日上限时，每次 tick 命中上限都推送一次"已达上限"通知。

    旧实现按天去重（每天最多推一次）；新行为改为前端弹窗 + 每次命中都推，
    故同日多次命中上限会多次推送（去重交由前端 toast 的自动消失处理）。
    """
    notified = []

    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {"recommendation_history": [], "last_updated": ""})

        # _today_recommend_count 返回已达上限（max_per_day=1）
        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=1), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True, "max_recommend_per_day": 1})
            engine.set_send_notification_callback(_capture_notifications(notified))

            # 第一次 tick：命中上限 → 推送一次通知
            pushed1 = await engine.tick_now()
            assert pushed1 is False
            assert len(notified) == 1, f"应推送 1 次上限通知，实际 {len(notified)}"
            assert "已达每日上限" in notified[0]["text"]
            assert "1 条" in notified[0]["text"]

            # 第二次 tick：同日仍命中上限 → 再次推送（每次命中都推，前端弹窗负责消失）
            pushed2 = await engine.tick_now()
            assert pushed2 is False
            assert len(notified) == 2, f"同日再次命中上限应再次推送，实际累计 {len(notified)} 次"


@pytest.mark.asyncio
async def test_daily_limit_no_callback_no_crash():
    """达到上限但未注册 notification 回调时，不应抛异常。"""
    with tempfile.TemporaryDirectory() as ws:
        ws_path = Path(ws)
        _write_state(ws_path, {"recommendation_history": [], "last_updated": ""})

        with patch(f"{_PATCH_TARGETS['utils']}.get_agent_workspace_dir", return_value=ws_path), \
             patch(f"{_PATCH_TARGETS['actions']}._today_recommend_count", return_value=5), \
             patch(f"{_PATCH_TARGETS['actions']}._get_all_skills", return_value=(set(), SAMPLE_SKILLS)):

            from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

            engine = ProactiveEngine({"enabled": True, "max_recommend_per_day": 5})
            # 故意不调 set_send_notification_callback
            pushed = await engine.tick_now()
            assert pushed is False




# ── 两种触发统一为"最活跃会话"：选法修复回归 ──────────────────────

@pytest.mark.asyncio
async def test_find_session_for_channel_no_history_required():
    """推送目标选择不再要求 compressed_history 非空——刚发消息、assistant
    还没回完的会话（历史空）仍可作为"最活跃会话"推送候选。

    回归保护：推送本身只需 session_id，历史是 LLM 决策素材（render_for_llm/is_empty），
    不该用历史空过滤推送候选。
    """
    from jiuwenswarm.agents.harness.common.recommendation.situation_report import (
        SessionSummary, SituationReport,
    )
    # 两个会话：一个历史完整、一个历史空但 last_message_at 更新（刚发消息）
    full = SessionSummary(
        session_id="sess_old", channel_id="web", last_message_at=1000.0,
        compressed_history="[User]: hi\n[Assistant]: hello",
    )
    fresh = SessionSummary(
        session_id="sess_current", channel_id="web", last_message_at=2000.0,
        compressed_history="",  # 刚发消息，assistant 没回完 → 历史空
    )
    report = SituationReport(sessions=[full, fresh])

    # find_session_for_channel 应选 last_message_at 最新的 fresh（即便历史空）
    chosen = report.find_session_for_channel("web")
    assert chosen is not None
    assert chosen.session_id == "sess_current"

    # most_recent_active_session 同理
    chosen2 = report.most_recent_active_session()
    assert chosen2 is not None
    assert chosen2.session_id == "sess_current"


@pytest.mark.asyncio
async def test_render_for_llm_still_skips_empty_history():
    """LLM 决策素材职责不变：render_for_llm 仍只渲染 compressed_history 非空的
    会话（历史空的不给 LLM 看决策素材），与推送目标选择解耦。"""
    from jiuwenswarm.agents.harness.common.recommendation.situation_report import (
        SessionSummary, SituationReport,
    )
    full = SessionSummary(
        session_id="sess_old", channel_id="web", last_message_at=1000.0,
        compressed_history="[User]: hi\n[Assistant]: hello",
    )
    fresh = SessionSummary(
        session_id="sess_current", channel_id="web", last_message_at=2000.0,
        compressed_history="",
    )
    report = SituationReport(sessions=[full, fresh])

    rendered = report.render_for_llm()
    # 只含历史完整的 sess_old，不含历史空的 sess_current
    assert "sess_old" in rendered or "hi" in rendered
    assert "sess_current" not in rendered


def test_is_empty_still_true_when_all_history_empty():
    """is_empty 守卫不变：所有会话 history 都空 → report 为空 → tick 跳过
    （LLM 无决策素材，本就该跳过）。"""
    from jiuwenswarm.agents.harness.common.recommendation.situation_report import (
        SessionSummary, SituationReport,
    )
    report = SituationReport(sessions=[
        SessionSummary(session_id="a", last_message_at=1000.0, compressed_history=""),
        SessionSummary(session_id="b", last_message_at=2000.0, compressed_history=""),
    ])
    assert report.is_empty() is True
