# -*- coding: utf-8 -*-
"""feedback_collector 单元测试（反馈采集/缓冲/消费）。

此前零测试。反馈缓冲是梯度学习的唯一入口，语义规则多且相互纠缠：
显式/隐式去重（各留第一条）、FIFO 容量 20、history 未写入时的时序竞态
兜底（用调用方元数据照样记录）、consume 的"取出即清空并落盘"。

测试通过 monkeypatch profile_extractor._default_state_path 注入临时
文件驱动全链路（record → load/append/save → reload 验证持久化）。
"""

from __future__ import annotations

import jiuwenswarm.agents.harness.common.recommendation.profile_extractor as pe
from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
    RecMeta,
    consume_feedback_buffer,
    find_latest_recommendation,
    record_explicit_feedback,
    record_implicit_feedback,
)
from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
    RecommendationState,
    load_recommendation_state,
)


def _use_state_file(tmp_path, monkeypatch):
    """所有无 path 参数的入口统一指向临时文件。返回该文件路径。"""
    state_file = tmp_path / "recommendation.json"
    monkeypatch.setattr(pe, "_default_state_path", lambda: state_file)
    return state_file


def _seed(state_file, history=None, buffer=None) -> None:
    state = RecommendationState(
        recommendation_history=history or [],
        feedback_buffer=buffer or [],
    )
    pe.save_recommendation_state(state, state_file)


# ---------- record_feedback ----------


def test_record_explicit_feedback_persists_to_buffer(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    record_explicit_feedback("rec_1", "explicit_like", RecMeta(rec_type="skill", rec_target="translate", rec_content="翻译技能"))
    state = load_recommendation_state(f)
    assert len(state.feedback_buffer) == 1
    fb = state.feedback_buffer[0]
    assert fb["rec_id"] == "rec_1"
    assert fb["feedback_type"] == "explicit_like"
    # history 未写入 → 用调用方元数据兜底（时序竞态不丢反馈）
    assert fb["rec_type"] == "skill"
    assert fb["rec_target"] == "translate"
    assert fb["rec_content"] == "翻译技能"
    assert fb["user_reply"] == ""


def test_record_feedback_uses_history_when_available(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[{"id": "rec_1", "type": "skill", "target": "calendar", "content": "日程提醒"}])
    record_explicit_feedback("rec_1", "explicit_dislike")  # 无 meta
    fb = load_recommendation_state(f).feedback_buffer[0]
    assert fb["rec_type"] == "skill"
    assert fb["rec_target"] == "calendar"
    assert fb["rec_content"] == "日程提醒"


def test_record_feedback_history_meta_fallback_chain(tmp_path, monkeypatch) -> None:
    """history 命中但字段为空 → 回退调用方 meta。"""
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[{"id": "rec_1"}])  # history 记录字段全空
    record_explicit_feedback("rec_1", "explicit_like", RecMeta(rec_content="meta 内容"))
    fb = load_recommendation_state(f).feedback_buffer[0]
    assert fb["rec_content"] == "meta 内容"


def test_record_implicit_feedback_stores_raw_reply(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    record_implicit_feedback("rec_1", "不需要，谢谢")
    fb = load_recommendation_state(f).feedback_buffer[0]
    assert fb["feedback_type"] == "implicit"
    assert fb["user_reply"] == "不需要，谢谢"


def test_dedup_same_category_keeps_first(tmp_path, monkeypatch) -> None:
    """同一条推荐：显式只留第一条（二次点赞/改点踩都丢弃）。"""
    f = _use_state_file(tmp_path, monkeypatch)
    record_explicit_feedback("rec_1", "explicit_like")
    record_explicit_feedback("rec_1", "explicit_like")
    record_explicit_feedback("rec_1", "explicit_dislike")
    state = load_recommendation_state(f)
    assert len(state.feedback_buffer) == 1
    assert state.feedback_buffer[0]["feedback_type"] == "explicit_like"


def test_dedup_implicit_keeps_first_reply_only(tmp_path, monkeypatch) -> None:
    """隐式只采紧跟的第一条回复，后续回复丢弃。"""
    f = _use_state_file(tmp_path, monkeypatch)
    record_implicit_feedback("rec_1", "第一条回复")
    record_implicit_feedback("rec_1", "又聊了两句别的")
    state = load_recommendation_state(f)
    assert len(state.feedback_buffer) == 1
    assert state.feedback_buffer[0]["user_reply"] == "第一条回复"


def test_explicit_and_implicit_both_kept(tmp_path, monkeypatch) -> None:
    """显式与隐式互补不互斥：同一条推荐两类信号都保留。"""
    f = _use_state_file(tmp_path, monkeypatch)
    record_explicit_feedback("rec_1", "explicit_like")
    record_implicit_feedback("rec_1", "补充：其实更想要文档总结")
    state = load_recommendation_state(f)
    assert len(state.feedback_buffer) == 2


def test_dedup_is_per_rec_id(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    record_explicit_feedback("rec_1", "explicit_like")
    record_explicit_feedback("rec_2", "explicit_like")
    assert len(load_recommendation_state(f).feedback_buffer) == 2


def test_buffer_fifo_caps_at_20(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    for i in range(23):
        record_explicit_feedback(f"rec_{i}", "explicit_like")
    state = load_recommendation_state(f)
    assert len(state.feedback_buffer) == 20
    assert state.feedback_buffer[0]["rec_id"] == "rec_3"  # 留最新
    assert state.feedback_buffer[-1]["rec_id"] == "rec_22"


# ---------- consume_feedback_buffer ----------


def test_consume_returns_records_and_clears_persistently(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    record_explicit_feedback("rec_1", "explicit_like")
    record_implicit_feedback("rec_1", "回复")

    state = load_recommendation_state(f)
    feedbacks = consume_feedback_buffer(state)
    assert len(feedbacks) == 2

    # 清空已落盘：重新加载缓冲为空
    assert load_recommendation_state(f).feedback_buffer == []


def test_consume_empty_buffer_returns_empty_list(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f)
    state = load_recommendation_state(f)
    assert consume_feedback_buffer(state) == []
    # 空缓冲不触发保存（load 后文件仍是原始空态）
    assert load_recommendation_state(f) == RecommendationState()


# ---------- find_latest_recommendation ----------


def test_find_latest_returns_newest_for_session(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[
        {"id": "r1", "session_id": "s1", "tick_at": 100.0},
        {"id": "r2", "session_id": "s2", "tick_at": 200.0},
        {"id": "r3", "session_id": "s1", "tick_at": 300.0},
    ])
    rec = find_latest_recommendation("s1")
    assert rec is not None and rec["id"] == "r3"


def test_find_latest_no_match_returns_none(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[{"id": "r1", "session_id": "s1", "tick_at": 100.0}])
    assert find_latest_recommendation("s_other") is None


def test_find_latest_age_cutoff(tmp_path, monkeypatch) -> None:
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[{"id": "r1", "session_id": "s1", "tick_at": 1.0}])
    # max_age 很小 → 该推荐已过期
    assert find_latest_recommendation("s1", max_age_seconds=0.001) is None
    # 默认不做过期检查
    assert find_latest_recommendation("s1") is not None


def test_find_latest_skips_records_with_bad_tick_at(tmp_path, monkeypatch) -> None:
    """脏数据（缺 tick_at / 非数值）跳过而非崩，继续找更早的有效记录。"""
    f = _use_state_file(tmp_path, monkeypatch)
    _seed(f, history=[
        {"id": "r_bad", "session_id": "s1"},            # 缺 tick_at
        {"id": "r_bad2", "session_id": "s1", "tick_at": "abc"},  # 非数值
        {"id": "r_ok", "session_id": "s1", "tick_at": 50.0},
    ])
    rec = find_latest_recommendation("s1")
    assert rec is not None and rec["id"] == "r_ok"
