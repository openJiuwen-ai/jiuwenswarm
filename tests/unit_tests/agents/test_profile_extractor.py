# -*- coding: utf-8 -*-
"""profile_extractor 单元测试（推荐状态持久化）。

此前零测试。重点覆盖：

- 加载容错：文件缺失/空文件/残缺 JSON（撕裂产物）/非 dict 载荷/字段类型异常
  → 返回空状态（记录撕裂清零路径，说明原子写的必要性）；
- 原子写契约：保存必须"同目录临时文件 + os.replace 原子替换"——
  ① 替换前正式文件不得被原地写入新内容；② 保存失败（replace 中断）时
  此前落盘的好状态必须完好无损。write_text 直接截断写在写一半崩溃/并发
  时会留下残缺 JSON，下次加载失败返回空态并被后续保存固化——反馈缓冲/
  策略梯度/推荐历史全部永久清零。
"""

from __future__ import annotations

import os

from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
    RecommendationState,
    load_recommendation_state,
    save_recommendation_state,
)


def _g(gid: str, rule: str) -> dict:
    return {"gradient_id": gid, "rule": rule, "category": "tone"}


# ---------- RecommendationState ----------


def test_state_add_recommendation_caps_at_20() -> None:
    state = RecommendationState()
    for i in range(25):
        state.add_recommendation({"id": f"r{i}"})
    assert len(state.recommendation_history) == 20
    assert state.recommendation_history[0]["id"] == "r5"  # 留最新


def test_state_touch_sets_utc_timestamp() -> None:
    state = RecommendationState()
    state.touch()
    assert state.last_updated
    assert "+" in state.last_updated or state.last_updated.endswith("Z")


# ---------- 加载容错 ----------


def test_load_missing_file_returns_empty_state(tmp_path) -> None:
    assert load_recommendation_state(tmp_path / "absent.json") == RecommendationState()


def test_load_empty_file_returns_empty_state(tmp_path) -> None:
    f = tmp_path / "recommendation.json"
    f.write_text("", encoding="utf-8")
    assert load_recommendation_state(f) == RecommendationState()


def test_load_torn_json_returns_empty_state(tmp_path) -> None:
    """残缺 JSON（截断写的撕裂产物）→ 空状态——这是原子写要堵死的清零路径。"""
    f = tmp_path / "recommendation.json"
    f.write_text('{"strategy_gradients": [{"gradient_id": "g1", "rule"', encoding="utf-8")
    assert load_recommendation_state(f).strategy_gradients == []


def test_load_non_dict_payload_returns_empty_state(tmp_path) -> None:
    f = tmp_path / "recommendation.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_recommendation_state(f) == RecommendationState()


def test_load_field_type_anomaly_falls_back_to_empty_list(tmp_path) -> None:
    f = tmp_path / "recommendation.json"
    f.write_text(
        '{"recommendation_history": "not-a-list", "feedback_buffer": null, "strategy_gradients": [1, 2]}',
        encoding="utf-8",
    )
    state = load_recommendation_state(f)
    assert state.recommendation_history == []
    assert state.feedback_buffer == []
    assert state.strategy_gradients == [1, 2]


def test_round_trip(tmp_path) -> None:
    f = tmp_path / "recommendation.json"
    state = RecommendationState(
        recommendation_history=[{"id": "r1", "type": "skill"}],
        feedback_buffer=[{"rec_id": "r1", "feedback_type": "explicit_like"}],
        strategy_gradients=[_g("g1", "保持简洁")],
    )
    state.touch()
    save_recommendation_state(state, f)
    reloaded = load_recommendation_state(f)
    assert reloaded.recommendation_history == state.recommendation_history
    assert reloaded.feedback_buffer == state.feedback_buffer
    assert reloaded.strategy_gradients == state.strategy_gradients
    assert reloaded.last_updated == state.last_updated


# ---------- 原子写契约 ----------


def test_save_replaces_atomically_via_temp_file(tmp_path, monkeypatch) -> None:
    """新内容只能经临时文件 + os.replace 落地；替换发生时正式文件仍是旧内容。"""
    f = tmp_path / "recommendation.json"
    old = RecommendationState(strategy_gradients=[_g("g_old", "旧规则")])
    save_recommendation_state(old, f)  # 预置一份好状态（未打桩，走真实路径）

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spying_replace(src, dst):
        calls.append((str(src), str(dst)))
        # 替换前一刻：正式文件必须仍是旧内容（新内容绝不能原地写入）
        assert "旧规则" in f.read_text(encoding="utf-8")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spying_replace)

    new = RecommendationState(strategy_gradients=[_g("g_new", "新规则")])
    save_recommendation_state(new, f)

    assert calls, "save 未使用 os.replace 原子替换（直接 write_text 原地写入会撕裂文件）"
    assert calls[0][1] == str(f)
    assert calls[0][0] != str(f)  # 源必须是临时文件
    # 保存成功后无临时文件残留
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != f.name]
    assert leftovers == []
    assert load_recommendation_state(f).strategy_gradients[0]["rule"] == "新规则"


def test_interrupted_save_preserves_previous_state(tmp_path, monkeypatch) -> None:
    """保存中途失败（模拟写一半崩溃/磁盘满）：此前落盘的好状态必须完好。"""
    f = tmp_path / "recommendation.json"
    good = RecommendationState(strategy_gradients=[_g("g1", "辛苦学来的规则")])
    save_recommendation_state(good, f)  # 预置好状态

    def failing_replace(src, dst):
        raise OSError("simulated crash mid-save")

    monkeypatch.setattr(os, "replace", failing_replace)

    # 保存失败按契约只记日志不抛出
    save_recommendation_state(RecommendationState(strategy_gradients=[]), f)

    reloaded = load_recommendation_state(f)
    assert reloaded.strategy_gradients == good.strategy_gradients  # 旧状态完好
    # 失败后无临时文件残留
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != f.name]
    assert leftovers == []


def test_concurrent_saves_never_produce_torn_file(tmp_path) -> None:
    """并发保存冒烟：最终文件要么是 A 要么是 B，任何时刻都可解析（不撕裂）。"""
    import threading

    f = tmp_path / "recommendation.json"
    state_a = RecommendationState(strategy_gradients=[_g("ga", "A")])
    state_b = RecommendationState(strategy_gradients=[_g("gb", "B")])

    save_recommendation_state(state_a, f)

    def worker(state):
        for _ in range(30):
            save_recommendation_state(state, f)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (state_a, state_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = load_recommendation_state(f)
    rules = [g["rule"] for g in final.strategy_gradients]
    assert rules in (["A"], ["B"])  # 完整一方，绝不残缺
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != f.name]
    assert leftovers == []  # 并发后临时文件也无残留
