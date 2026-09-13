"""The dispatch gate (PR2b): the watcher × the standing-mandate registry.

The gate is D2's zero-dispatch default made mechanical: no watch, no turn — the
collaborator hears why once, the trigger is consumed (the signed cutline:
observation-time, fail-closed — a later grant covers new events only), and the
mode a dispatched turn carries is the registry's, snapshotted at dispatch (IC-1).
"""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import CloudDocCommentWatcher, WatcherConfig
from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import CloudDocStore
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig
from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

from test_clouddoc_watcher import C, Clock, FakeProvider, DOC, SA


@pytest.fixture
async def gated(tmp_path):
    """The steady-state watcher rig, gated by a real registry."""
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    reg = WatchRegistry(tmp_path / "w.json")
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=reg,
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    return w, prov, store, reg, dispatched


def _mention(cid="c1", content="改这句"):
    # The mention is the summons; C also carries the platform's inert assignee field.
    return C(cid, f"@{SA} {content}", mentioned=(SA,))


async def test_no_watch_never_dispatches_three_ways(gated):
    w, prov, store, reg, dispatched = gated
    for i in (1, 2, 3):
        prov.comments = [_mention(f"c{i}")]
        await w.tick()
    assert dispatched == [], "D2 零派发:未签发 watch 的 @ 永不派发"


async def test_no_watch_posts_unauthorized_reply_once(gated):
    w, prov, store, reg, dispatched = gated
    prov.comments = [_mention("c1")]
    await w.tick()
    replies = [r for r in prov.replies if "值守没有开启" in r[1] or "watch is off" in r[1]]
    assert len(replies) == 1, "②机械回帖:每线程一次"
    await w.tick()
    replies2 = [r for r in prov.replies if "值守没有开启" in r[1] or "watch is off" in r[1]]
    assert len(replies2) == 1, "第二个 tick 不重复回帖(触发键已消耗)"


async def test_a_pre_grant_mention_stays_backlogged_after_grant(gated):
    # The signed cutline: authority is strictly forward-looking. A mention
    # observed before the grant must NOT dispatch after it.
    w, prov, store, reg, dispatched = gated
    prov.comments = [_mention("c1")]
    await w.tick()                      # observed unauthorized -> consumed
    reg.issue(DOC, "apply_scoped")
    await w.tick()
    assert dispatched == [], "授权前的 @ 在授权后不得自动补派(③切割线)"


async def test_a_fresh_mention_after_grant_dispatches_with_mode(gated):
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    prov.comments = [_mention("c9")]
    await w.tick()
    assert len(dispatched) == 1
    assert dispatched[0][2]["clouddoc"]["mode"] == "apply_scoped", (
        "IC-1: watch 档位随授权载荷下发"
    )


async def test_mode_is_snapshotted_not_live(gated):
    # A modification drains: the turn dispatched under the old terms keeps them.
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    prov.comments = [_mention("c1")]
    await w.tick()
    assert dispatched[0][2]["clouddoc"]["mode"] == "apply_scoped"
    reg.revoke(DOC)                     # terms changed after dispatch
    assert dispatched[0][2]["clouddoc"]["mode"] == "apply_scoped", (
        "已派发回合的档位是快照,变更不追溯(D3 drain)"
    )


async def test_a_ledger_left_suspended_never_dispatches_three_ways(gated):
    """The migration seen from the gate.

    Suspension is gone from the state space, and the danger in removing a check
    is that the state it read is still on disk. A file written by the previous
    release, with the watch paused, must dispatch nothing -- the registry turns
    it into a tombstone before the gate ever sees it.
    """
    import json

    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    data = reg._read()
    data["watches"][DOC]["suspended"] = True
    reg._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reg._retirements_persisted = False

    for i in (1, 2, 3):
        prov.comments = [_mention(f"c{i}")]
        await w.tick()
    assert dispatched == [], "存量挂起条目一律不派发(三连测)"
    text = "".join(r[1] for r in prov.replies)
    assert "本轮暂不安排" not in text and "not scheduled this round" not in text, (
        "挂起文案已退役,不得再发出"
    )
    assert "值守没有开启" in text, "隐藏的文档照样回帖说明,只是不派发回合"


async def test_a_hidden_document_is_still_polled_and_still_answers(gated):
    """隐藏 is the watch off, and off was never "stop looking".

    The document stays in the watcher's list, so a collaborator who @-mentions
    the agent in it gets an answer saying the watch is off, rather than silence
    that is indistinguishable from the agent being broken. What hiding buys is
    the expensive half: no turn is dispatched, so no model call is made.
    """
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    reg.revoke(DOC)                      # 隐藏
    assert w._docs == [DOC], "隐藏不摘除文档,轮询照旧"

    prov.comments = [_mention("c1")]
    await w.tick()
    assert dispatched == [], "不派发回合,也就不花模型调用"
    assert any("值守没有开启" in r[1] or "watch is off" in r[1] for r in prov.replies), (
        "要说清事实,而不是沉默"
    )


async def test_revoke_then_regrant_does_not_dispatch_the_backlog(gated):
    # The unified backlog law: leaving the backlog takes a human act, and turning
    # the watch back on is not one -- the cutline is observation-time.
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    reg.revoke(DOC)
    prov.comments = [_mention("c1")]
    await w.tick()                      # observed with the watch off
    reg.issue(DOC, "apply_scoped", issued_by="manual")
    await w.tick()
    assert dispatched == [], "重新开启不自动补派关闭期间的积压"


async def test_over_budget_posts_queued_wording_and_counts_persist(gated):
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped", budget={"max_dispatches_per_day": 1})
    prov.comments = [_mention("c1")]
    await w.tick()
    assert len(dispatched) == 1
    prov.comments = [_mention("c1"), _mention("c2")]
    await w.tick()
    assert len(dispatched) == 1, "超预算不派发"
    text = "".join(r[1] for r in prov.replies)
    assert "额度已用完" in text or "budget is used up" in text, (
        "M-cluster-1:超预算对协作者可见(第三文案)"
    )


async def test_ungated_watcher_does_not_dispatch_at_all(tmp_path):
    # registry=None: no mandate can be consulted, so none can be shown, and the
    # trigger is treated exactly like ``no_watch``. This used to dispatch "at the
    # strictest tier" (reply_only) -- but that tier is retired precisely because
    # it was not a weaker mandate, just an unauthorised turn holding tools the
    # agent has anyway. Production wiring always passes a registry.
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(),
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    prov.comments = [_mention("c1")]
    await w.tick()
    assert dispatched == [], "没有授权注册表就不派发,不存在'兜底档派发'"
    # The collaborator still hears why -- posting that reply never needed a mandate.
    assert any("值守没有开启" in r[1] for r in prov.replies), "未开启也要回一句说明"
    assert await store.is_triggered(DOC, f"clouddoc:{DOC}:c1:-"), "拒绝也消耗触发键"


def test_production_wiring_passes_a_registry():
    """The gate exists only if app_gateway wires it: assert at source level, the same
    guard style as the dangling-reference test."""
    import inspect

    from jiuwenswarm.gateway import app_gateway

    src = inspect.getsource(app_gateway)
    assert "WatchRegistry(" in src, "app_gateway 必须构造 WatchRegistry"
    assert "registry=" in src, "app_gateway 必须把 registry 传给 watcher"


def test_a_retired_or_unknown_mode_gets_no_tools_three_ways():
    """IC-1 triple test, after the ladder retired.

    The old test asserted the reply_only family held no write tool. The family is
    gone: a mode that is not ``apply_scoped`` now gets the **empty set**, which is
    strictly stricter -- no write tool and no read tool either. Falling back to
    some other family would be a fallback to authority nobody granted.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        unattended_allowlist_for,
    )

    for _ in range(3):
        assert unattended_allowlist_for("reply_only") == frozenset(), (
            "退役档位不是更严的工具集,是没有工具集"
        )


def test_unknown_or_missing_mode_resolves_to_the_empty_set():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        unattended_allowlist_for,
    )

    for mode in (None, "", "reply_only", "apply_for_everything"):
        assert unattended_allowlist_for(mode) == frozenset(), mode
    assert unattended_allowlist_for("apply_scoped"), "唯一还在的档位仍然有工具"


def test_adoption_policy_issues_watch_but_never_overwrites(tmp_path):
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    class _FakeConns:
        pass

    reg = WatchRegistry(tmp_path / "w.json")
    # Simulate the policy hook contract directly (the connections method is thin):
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.connections import CloudDocConnections

    conns = CloudDocConnections.__new__(CloudDocConnections)
    conns._watch_registry = reg
    conns.auto_watch_policy = "apply_scoped"
    conns.policy_issue(["d1", "d2"])
    assert reg.get("d1")["issued_by"] == "policy"
    assert reg.get("d2")["mode"] == "apply_scoped"
    # A manual grant outranks the policy: re-adoption must not reset terms.
    reg.issue("d1", "apply_scoped", issued_by="manual")
    conns.policy_issue(["d1"])
    assert reg.get("d1")["mode"] == "apply_scoped", "策略不得覆盖已有 watch"
    # A revoked entry is the owner's tombstone: the policy leaves it alone even
    # when the journal is not consulted at all.
    reg.revoke("d2")
    conns.policy_issue(["d2"])
    assert reg.get("d2").get("revoked") is True and reg.check("d2").reason == "no_watch"
    # off / invalid issue nothing.
    conns.auto_watch_policy = "off"
    conns.policy_issue(["d3"])
    conns.auto_watch_policy = "garbage"
    conns.policy_issue(["d4"])
    assert reg.get("d3") is None and reg.get("d4") is None


async def test_a_refused_dispatch_logs_the_gate_decision(gated, caplog):
    """The gate's verdict was invisible outside the audit journal; one INFO line per
    refusal names the document, the comment and the reason."""
    import logging

    w, prov, store, reg, dispatched = gated
    prov.comments = [_mention("c1")]
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher"):
        await w.tick()
    lines = [r.getMessage() for r in caplog.records if "gate " in r.getMessage()]
    assert any(f"gate {DOC}/c1 denied: no_watch" in ln for ln in lines), lines


async def test_a_rate_limited_pause_logs_without_consuming(tmp_path, caplog):
    """The brake's pause leaves no audit line by design; the log line is its only
    trace, and it carries the brake's own setting."""
    import logging

    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    reg = WatchRegistry(tmp_path / "w.json", rate_max=1, rate_window_seconds=60.0)
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=reg,
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    reg.issue(DOC, "apply_scoped")
    prov.comments = [_mention("c1"), _mention("c2")]
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher"):
        await w.tick()
    assert len(dispatched) == 1, "第一条派发后刹车生效"
    lines = [r.getMessage() for r in caplog.records if "paused: rate_limited" in r.getMessage()]
    assert any(f"gate {DOC}/c2 paused: rate_limited (1/60s)" in ln for ln in lines), lines
    assert not await store.is_triggered(DOC, f"clouddoc:{DOC}:c2:-"), "暂停不消耗触发键"
    assert not any("值守没有开启" in r[1] for r in prov.replies), "限速刹车不回帖"


async def test_a_refused_dispatch_lands_a_denial_audit_line(gated):
    """E1's friction signal: the audit view can only surface repeated refusals
    if each one leaves a line -- silence here and the signal never exists."""
    w, prov, store, reg, dispatched = gated
    prov.comments = [_mention("c1")]
    await w.tick()
    denies = [a for a in reg.audit_tail() if a["event"] == "deny"]
    assert len(denies) == 1 and denies[0]["reason"] == "no_watch"
    assert reg.usage_summary(DOC)["denials"] == {"no_watch": 1}


async def test_direct_mode_has_no_unattended_path_at_all(gated, monkeypatch):
    """D21: off mandate, the watcher dispatches nothing and consumes nothing --
    mentions stay in the document exactly as their authors left them, so a
    later switch back to mandate finds an untouched backlog (with the signed
    cutline still applying from the new grant's timestamp)."""
    w, prov, store, reg, dispatched = gated
    reg.issue(DOC, "apply_scoped")
    import jiuwenswarm.common.config as cfgmod
    real_get = cfgmod.get_config
    monkeypatch.setattr(
        cfgmod, "get_config",
        lambda: {**(real_get() or {}), "clouddoc": {**((real_get() or {}).get("clouddoc") or {}), "mode": "direct"}},
    )
    prov.comments = [_mention("c1")]
    out = await w.tick()
    assert dispatched == [] and out["dispatched"] == 0
    assert prov.replies == [], "直连档不发机械回帖"


def test_a_retired_policy_value_lands_on_off_never_on_apply(tmp_path, caplog):
    """A config file outlives the release that documented it.

    ``auto_watch_on_adopt: reply_only`` is still out there. It must issue
    **nothing** -- reading an unrecognised level as the one surviving level would
    auto-grant write authority across every adopted document on next startup.
    """
    import logging

    from jiuwenswarm.extensions.co_scribe.backend.host.authority.connections import CloudDocConnections
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    reg = WatchRegistry(tmp_path / "w.json")
    conns = CloudDocConnections.__new__(CloudDocConnections)
    conns._watch_registry = reg
    conns.auto_watch_policy = "reply_only"
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.extensions.co_scribe.backend.host.authority.connections"):
        conns.policy_issue(["d1", "d2"])
    assert reg.get("d1") is None and reg.get("d2") is None, "退役档位的策略值一律按 off"
    assert any("已退役" in r.getMessage() for r in caplog.records), "按 off 处理要告警"
