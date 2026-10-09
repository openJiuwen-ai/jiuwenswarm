"""The standing-mandate registry (PR2b): lifecycle, gate verdicts, budget, audit.

Safety behaviors get three-shot repetition per the §13 discipline where the
behavior guards authority (revocation intercepting, a retired state's entries
being tombstoned rather than freed, no-watch refusing).
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import (
    MODES,
    RETIRED_MODES,
    WatchRegistry,
    WatchVerdict,
)


def _write_legacy_entry(reg, doc_id: str, mode: str, **extra) -> None:
    """Put an entry at a retired level straight into the ledger file.

    The registry refuses to *issue* one, which is the point -- but a file written
    by an earlier release still holds them, and that file is what these tests are
    about.
    """
    data = reg._read() if reg._path.is_file() else {
        "version": 1, "global": {"suspended": False}, "watches": {}
    }
    data["watches"][doc_id] = {
        "mode": mode,
        "issued_at": 1_000_000.0,
        "issued_by": "manual",
        "suspended": False,
        "expires_at": None,
        "from": [],
        "budget": {},
        "dispatch_day": "",
        "dispatch_count": 0,
        **extra,
    }
    reg._path.parent.mkdir(parents=True, exist_ok=True)
    reg._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reg._retirements_persisted = False


@pytest.fixture()
def reg(tmp_path):
    clock = {"t": 1_000_000.0}
    # The rolling-window loop brake is disabled here (rate_max=0) so the gate and
    # daily-budget tests exercise exactly what they mean; the brake has its own fixture
    # and tests below.
    r = WatchRegistry(
        tmp_path / "watches.json", now_fn=lambda: clock["t"], rate_max=0
    )
    r.clock = clock  # test handle
    return r


@pytest.fixture()
def reg_rated(tmp_path):
    """A registry with the loop brake on, tight for the test: 3 per 60s."""
    clock = {"t": 1_000_000.0}
    r = WatchRegistry(
        tmp_path / "watches.json", now_fn=lambda: clock["t"],
        rate_max=3, rate_window_seconds=60.0,
    )
    r.clock = clock
    return r


# ---------------------------------------------------------------- issue / gate


def test_no_watch_means_no_dispatch_three_ways(reg):
    # D2 zero-dispatch: the founding behavior, three shots.
    for doc in ("d1", "d2", "d3"):
        v = reg.check(doc)
        assert v == WatchVerdict(False, None, "no_watch")


def test_issue_then_dispatchable_with_mode(reg):
    reg.issue("d1", "apply_scoped")
    v = reg.check("d1")
    assert v.dispatchable and v.mode == "apply_scoped" and v.reason == "ok"
    reg.issue("d2", "apply_scoped")
    assert reg.check("d2").mode == "apply_scoped"


def test_issue_rejects_unknown_mode(reg):
    with pytest.raises(ValueError):
        reg.issue("d1", "off")  # off is a policy value, never a watch mode (M-4)
    with pytest.raises(ValueError):
        reg.issue("d1", "propose")
    with pytest.raises(ValueError):
        # The retired tier cannot be re-granted, by the panel or by anyone else.
        reg.issue("d1", "reply_only")


def test_reissue_is_modify_and_replaces_terms(reg):
    reg.issue("d1", "apply_scoped")
    reg.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 5})
    e = reg.get("d1")
    assert e["mode"] == "apply_scoped"
    assert e["budget"] == {"max_dispatches_per_day": 5}, "再签发换掉旧条款"
    events = [a["event"] for a in reg.audit_tail()]
    assert events == ["grant", "modify"]


# ------------------------------------------------------------------ revocation


def test_revoke_intercepts_three_ways(reg):
    for doc in ("d1", "d2", "d3"):
        reg.issue(doc, "apply_scoped")
        assert reg.is_write_live(doc)
        reg.revoke(doc)
        assert not reg.check(doc).dispatchable
        assert not reg.is_write_live(doc), "撤销必须立即拦截写入(D3 硬急停)"


def test_kill_switch_revokes_everything(reg):
    for doc in ("d1", "d2", "d3"):
        reg.issue(doc, "apply_scoped")
    assert reg.revoke_all() == 3
    for doc in ("d1", "d2", "d3"):
        assert reg.check(doc).reason == "no_watch"
        assert not reg.is_write_live(doc)
        assert reg.get(doc).get("revoked") is True, "急停也留碑"
    assert reg.revoke_all() == 0, "已撤销的条目不再计数"


def test_revocation_keeps_a_flagged_entry(reg):
    """The entry is the tombstone, not the journal alone: a lost audit line must not
    turn a revocation into a re-issue at the next startup."""
    reg.issue("d1", "apply_scoped")
    assert reg.revoke("d1") is True
    entry = reg.get("d1")
    assert entry is not None and entry["revoked"] is True
    assert entry["revoked_at"] == reg.clock["t"]
    assert entry["mode"] == "apply_scoped", "留碑保留原档位供面板显示"
    assert reg.check("d1").reason == "no_watch"
    assert not reg.is_write_live("d1")
    assert reg.revoke("d1") is False, "二次撤销无事发生"


def test_a_manual_grant_replaces_the_tombstone(reg):
    reg.issue("d1", "apply_scoped")
    reg.revoke("d1")
    reg.issue("d1", "apply_scoped", issued_by="manual")
    entry = reg.get("d1")
    assert not entry.get("revoked") and "revoked_at" not in entry
    assert reg.check("d1").dispatchable and reg.check("d1").mode == "apply_scoped"
    assert reg.is_write_live("d1")
    events = [a["event"] for a in reg.audit_tail()]
    assert events == ["grant", "revoke", "grant"], "盖过留碑是新的授予，不是变更"


# ----------------------------------------------------- the pre-write checkpoint


def test_write_liveness_holds_the_entry_to_the_turns_mode(reg):
    """The checkpoint holds the entry to the mode the turn was dispatched under.

    The mid-turn *downgrade* this used to test is gone with the ladder -- there
    is one level, so nothing to downgrade to. What remains is the mismatch
    itself: an entry that no longer reads ``apply_scoped`` (here, a ledger left
    at the retired level) stops an in-flight write.
    """
    reg.issue("d1", "apply_scoped")
    assert reg.is_write_live("d1", mode="apply_scoped")
    _write_legacy_entry(reg, "d1", "reply_only")
    assert not reg.is_write_live("d1", mode="apply_scoped"), "档位对不上必须拦截在途写入"
    assert not reg.is_write_live("d1"), "退役档位视同无授权,连存续都不算"


def test_write_liveness_ends_at_expiry(reg):
    reg.issue("d1", "apply_scoped")
    assert reg.is_write_live("d1")
    reg.clock["t"] += 31 * 24 * 3600
    assert not reg.is_write_live("d1"), "按时钟已过期，即使尚未被 check() 标记"
    assert reg.check("d1").reason == "expired"
    assert reg.get("d1")["expired"] is True
    assert not reg.is_write_live("d1"), "已标记过期同样不存续"


# ------------------------------------------------- the retired suspended state


def test_no_suspension_api_survives():
    """The state left the vocabulary, so the verbs must leave the object.

    A lingering ``suspend`` that only sets a flag nobody reads is worse than no
    method at all: the panel would report success, the ledger would say paused,
    and the dispatcher would keep dispatching.
    """
    for verb in ("suspend", "resume", "suspend_all", "resume_all", "_set_suspended"):
        assert not hasattr(WatchRegistry, verb), verb


def test_a_suspended_entry_becomes_a_tombstone_three_ways(reg):
    """The migration that makes deleting the check safe.

    A ledger written by the previous release still says ``suspended: true``. If
    this release simply stopped reading the flag, every watch the owner had
    paused would dispatch again on the next tick -- authority widening with
    nobody's signature on it. They become tombstones instead: off, with a reason.
    """
    for doc in ("d1", "d2", "d3"):
        _write_legacy_entry(reg, doc, "apply_scoped", suspended=True,
                            budget={"max_edits_per_turn": 4})
        v = reg.check(doc)
        assert not v.dispatchable and v.reason == "no_watch", "暂停过的条目不得变成可派发"
        assert not reg.is_write_live(doc), "在途写入同样拦住"
        entry = reg.get(doc)
        assert entry["revoked"] is True
        assert entry["revoked_reason"] == "suspend_retired"
        assert entry["mode"] == "apply_scoped", "留碑保留原档位供审计显示"


def test_a_suspended_entry_is_not_silently_upgraded_to_on(reg):
    """Off is where "not now" points. The other two readings -- treat the pause as
    over, or keep honouring a state with no UI -- both hand the document back to
    the dispatcher on a decision nobody made."""
    _write_legacy_entry(reg, "d1", "apply_scoped", suspended=True)
    reg.check("d1")
    on_disk = json.loads(reg._path.read_text(encoding="utf-8"))
    assert on_disk["watches"]["d1"]["revoked"] is True, "迁移要落盘"
    assert on_disk["watches"]["d1"]["revoked_reason"] == "suspend_retired"
    lines = [a for a in reg.audit_tail()
             if a["doc_id"] == "d1" and a.get("reason") == "suspend_retired"]
    assert len(lines) == 1 and lines[0]["by"] == "migration", "一次迁移一行审计"
    assert reg.terminated_by_owner("d1"), "留碑后纳管策略不得重新签发"


def test_a_globally_suspended_ledger_retires_every_watch(reg):
    """The global pause is the same fact written once. It retires the same way --
    per document, since that is where the audit view looks."""
    reg.issue("d1", "apply_scoped")
    reg.issue("d2", "apply_scoped")
    data = reg._read()
    data["global"]["suspended"] = True
    reg._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reg._retirements_persisted = False

    assert reg.check("d1").reason == "no_watch"
    assert reg.check("d2").reason == "no_watch"
    for doc in ("d1", "d2"):
        assert reg.get(doc)["revoked_reason"] == "suspend_retired"


def test_the_cleared_global_flag_does_not_eat_the_next_grant(reg):
    """The migration must not outlive the file it migrated.

    Leaving ``global.suspended`` set would retire every *new* grant on the next
    load: the safeguard would consume exactly the watches it exists to protect.
    """
    data = reg._read()
    data["global"]["suspended"] = True
    data["watches"]["d0"] = {"mode": "apply_scoped", "issued_at": 1_000_000.0,
                             "issued_by": "manual", "expires_at": None,
                             "from": [], "budget": {}, "dispatch_day": "",
                             "dispatch_count": 0}
    reg._path.parent.mkdir(parents=True, exist_ok=True)
    reg._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reg._retirements_persisted = False

    assert reg.check("d0").reason == "no_watch"
    reg.issue("d1", "apply_scoped", issued_by="manual")
    assert reg.check("d1").dispatchable, "迁移之后签发的 watch 必须活着"
    assert reg.is_write_live("d1")
    on_disk = json.loads(reg._path.read_text(encoding="utf-8"))
    assert on_disk["global"]["suspended"] is False, "全局挂起标志必须被清掉"


# ---------------------------------------------------------------------- expiry


def test_expiry_flags_the_entry_and_keeps_it(reg):
    """E1: expiry ends the delegation but the entry stays as its own tombstone.

    Deleting on expiry is what let the adoption policy silently re-issue the
    watch at the next startup -- the calendar endpoint became a no-op."""
    reg.issue("d1", "apply_scoped", expires_at=1_000_100.0)
    assert reg.check("d1").dispatchable
    reg.clock["t"] = 1_000_200.0
    v = reg.check("d1")
    assert not v.dispatchable and v.reason == "expired"
    entry = reg.get("d1")
    assert entry is not None and entry.get("expired") is True, "到期留碑不删"
    assert any(a["event"] == "expire" for a in reg.audit_tail()), "到期必须留审计行"


def test_expiry_audit_line_lands_exactly_once(reg):
    reg.issue("d1", "apply_scoped", expires_at=1_000_100.0)
    reg.clock["t"] = 1_000_200.0
    reg.check("d1")
    reg.check("d1")
    reg.check("d1")
    assert sum(1 for a in reg.audit_tail() if a["event"] == "expire") == 1


def test_default_issuance_carries_the_default_term(reg):
    """E1: silence is not permanence -- an unstated term is the default TTL."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import DEFAULT_WATCH_TTL_SECONDS

    reg.issue("d1", "apply_scoped")
    entry = reg.get("d1")
    assert entry["expires_at"] == 1_000_000.0 + DEFAULT_WATCH_TTL_SECONDS
    reg.clock["t"] = 1_000_000.0 + DEFAULT_WATCH_TTL_SECONDS + 1
    assert reg.check("d1").reason == "expired"


def test_explicit_permanent_has_no_calendar_endpoint(reg):
    """None stays legal, but only as the owner's explicit word (D3)."""
    reg.issue("d1", "apply_scoped", expires_at=None)
    reg.clock["t"] = 1_000_000.0 + 400 * 24 * 3600  # 400 days later
    assert reg.check("d1").dispatchable, "显式永久无日历终点"


def test_renewal_after_expiry_opens_a_fresh_window(reg):
    """Re-issuance is recertification: the expired flag must not survive it."""
    reg.issue("d1", "apply_scoped", expires_at=1_000_100.0)
    reg.clock["t"] = 1_000_200.0
    assert reg.check("d1").reason == "expired"
    reg.issue("d1", "apply_scoped")
    v = reg.check("d1")
    assert v.dispatchable, v.reason
    assert not reg.get("d1").get("expired")


def test_owner_revocation_is_terminal_for_the_policy(reg):
    """The adoption policy must not resurrect what the owner revoked."""
    reg.issue("d1", "apply_scoped", issued_by="policy")
    reg.revoke("d1")
    assert reg.terminated_by_owner("d1"), "audit 的最后一笔是 revoke"
    reg.issue("d1", "apply_scoped", issued_by="manual")
    assert not reg.terminated_by_owner("d1"), "手工再授权盖过撤销"
    assert not reg.terminated_by_owner("d-never-seen")


# ---------------------------------------------------------------------- budget


def test_budget_over_cap_stops_dispatch_but_not_writes(reg):
    reg.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 2})
    assert reg.check("d1").dispatchable
    reg.note_dispatch("d1")
    reg.note_dispatch("d1")
    v = reg.check("d1")
    assert not v.dispatchable and v.reason == "over_budget"
    # Budget bounds volume of NEW dispatches; it is not a revocation, so the
    # in-flight write checkpoint stays live.
    assert reg.is_write_live("d1")


def test_budget_refreshes_on_a_new_day_for_new_events_only(reg):
    reg.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 1})
    reg.note_dispatch("d1")
    assert reg.check("d1").reason == "over_budget"
    reg.clock["t"] += 24 * 3600
    assert reg.check("d1").dispatchable, "预算按天刷新"


def test_budget_survives_restart(reg, tmp_path):
    reg.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 1})
    reg.note_dispatch("d1")
    fresh = WatchRegistry(tmp_path / "watches.json", now_fn=lambda: reg.clock["t"])
    assert fresh.check("d1").reason == "over_budget", "重启不清预算计数"


def test_no_budget_means_no_cap(reg):
    reg.issue("d1", "apply_scoped")  # D9: optional, default no budget
    for _ in range(50):
        reg.note_dispatch("d1")
    assert reg.check("d1").dispatchable


# --------------------------------------------------------------------- storage


def test_registry_is_cross_process_readable(reg, tmp_path):
    reg.issue("d1", "apply_scoped")
    # A second process (IC-3), on the same clock: the default term is thirty days
    # from the issuing clock, and a real wall clock would read that as expired.
    other = WatchRegistry(tmp_path / "watches.json", now_fn=lambda: reg.clock["t"])
    assert other.get("d1")["mode"] == "apply_scoped"
    assert other.is_write_live("d1")
    reg.revoke("d1")
    assert not other.is_write_live("d1"), "撤销必须跨进程立即可见"


def test_corrupt_file_treated_as_empty_not_crash(reg, tmp_path):
    (tmp_path / "watches.json").write_text("{not json", encoding="utf-8")
    assert reg.check("d1").reason == "no_watch"


def test_audit_covers_full_lifecycle(reg):
    reg.issue("d1", "apply_scoped")
    reg.issue("d1", "apply_scoped")
    reg.revoke("d1")
    events = [a["event"] for a in reg.audit_tail()]
    assert events == ["grant", "modify", "revoke"], "D3:授予/变更/撤销各一行审计"


def test_modes_constant_is_one_rung():
    # The ladder is retired: a watch is binary, off or apply_scoped. ``reply_only``
    # delegated "run an unattended turn", not "reply" -- replying never needed a
    # mandate -- so the strictest fallback is not dispatching, not a narrower tier.
    assert MODES == ("apply_scoped",)
    assert RETIRED_MODES == ("reply_only",)


# ------------------------------------------------------- panel term semantics


@pytest.mark.asyncio
async def test_watch_set_keeps_silence_and_forever_apart(reg):
    """E1 at the RPC seam: omission issues the default term, permanent=true is
    the owner's explicit word, and an explicit timestamp is honored verbatim.
    Over JSON a missing field and null both arrive as None -- the flag is what
    keeps the two meanings apart."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import CloudDocPanel
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import DEFAULT_WATCH_TTL_SECONDS

    import types

    panel = object.__new__(CloudDocPanel)
    panel._registry = lambda: reg
    # watch_set refuses an id nobody adopted; this rig only exercises the term
    # semantics, so every id counts as adopted.
    panel._reg = types.SimpleNamespace(find_doc=lambda _doc_id: object())

    out = await panel.watch_set("d-def", "apply_scoped")
    assert out["entry"]["expires_at"] == 1_000_000.0 + DEFAULT_WATCH_TTL_SECONDS

    out = await panel.watch_set("d-perm", "apply_scoped", permanent=True)
    assert out["entry"]["expires_at"] is None

    out = await panel.watch_set("d-ts", "apply_scoped", expires_at=1_000_500.0)
    assert out["entry"]["expires_at"] == 1_000_500.0


# ------------------------------------------------------------ audit view (E1)


def test_usage_summary_counts_dispatches_and_denials(reg):
    reg.issue("d1", "apply_scoped")
    reg.note_dispatch("d1")
    reg.note_dispatch("d1")
    reg.note_denied("d1", "over_budget")
    reg.note_denied("d1", "no_watch")
    reg.note_denied("d1", "over_budget")
    u = reg.usage_summary("d1")
    assert u["grants"] == 1 and u["dispatches"] == 2
    assert u["denials"] == {"over_budget": 2, "no_watch": 1}
    assert u["last_grant_at"] == 1_000_000.0
    # another doc's lines never bleed in
    reg.issue("d2", "apply_scoped")
    assert reg.usage_summary("d2")["dispatches"] == 0


@pytest.mark.asyncio
async def test_watch_usage_reads_granted_minus_used(reg, tmp_path, monkeypatch):
    """The panel view: the registry's grant beside the ledger's writes, and the
    friction hint when denials pile up."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts as rc
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import CloudDocPanel

    monkeypatch.setattr(rc, "get_receipts_path", lambda: tmp_path / "r.json")
    store = rc.ReceiptStore(tmp_path / "r.json")
    rid = store.begin("d1", [{"old": "旧", "new": "新", "for_comment_ids": []}],
                      highlight=False, executor="panel", source="apply_for_comment")
    store.commit(rid, revision_after="rev1")

    reg.issue("d1", "apply_scoped")
    reg.note_dispatch("d1")
    for _ in range(3):
        reg.note_denied("d1", "over_budget")

    panel = object.__new__(CloudDocPanel)
    panel._registry = lambda: reg
    out = await panel.watch_usage("d1")
    assert out["ok"] and out["granted"]["mode"] == "apply_scoped"
    assert out["used"]["write_batches"] == 1
    assert out["used"]["dispatches"] == 1
    assert out["used"]["executors"] == ["panel"]
    assert "frequent_denials" in out["hints"]
    assert "idle_wide_grant" not in out["hints"], "有写入不算闲置"


@pytest.mark.asyncio
async def test_set_mode_persists_and_get_conf_reports_it(reg, tmp_path):
    """D21's switch: an explicit act that lands in the config and on the audit
    journal; the UI only ever offers mandate and direct."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import CloudDocPanel

    cfg = tmp_path / "config.yaml"
    cfg.write_text("clouddoc:\n  enabled: true\n", encoding="utf-8")
    panel = object.__new__(CloudDocPanel)
    panel._config_path = cfg
    panel._registry = lambda: reg

    out = await panel.set_mode("direct")
    assert out["ok"] and "mode: direct" in cfg.read_text()
    assert panel._current_mode() == "direct"
    assert any(a["event"] == "mode" and a.get("value") == "direct" for a in reg.audit_tail())

    assert not (await panel.set_mode("recorded"))["ok"], "隐藏档不可从 UI 设置"
    out = await panel.set_mode("mandate")
    assert out["ok"] and panel._current_mode() == "mandate"


# ------------------------------------------------------ the rolling-window loop brake

def test_the_rate_brake_stops_a_burst_within_the_window(reg_rated):
    reg_rated.issue("d1", "apply_scoped")  # no daily budget at all
    assert reg_rated.check("d1").dispatchable
    for _ in range(3):
        reg_rated.note_dispatch("d1")
    v = reg_rated.check("d1")
    assert not v.dispatchable and v.reason == "rate_limited"
    # It is a pause, not a revocation: in-flight writes still checkpoint live.
    assert reg_rated.is_write_live("d1")


def test_the_rate_brake_recovers_after_the_window_passes(reg_rated):
    reg_rated.issue("d1", "apply_scoped")
    for _ in range(3):
        reg_rated.note_dispatch("d1")
    assert reg_rated.check("d1").reason == "rate_limited"
    reg_rated.clock["t"] += 61.0  # slide past the window
    assert reg_rated.check("d1").dispatchable, "窗口滑过后自行恢复"


def test_the_rate_brake_is_per_document(reg_rated):
    reg_rated.issue("d1", "apply_scoped")
    reg_rated.issue("d2", "apply_scoped")
    for _ in range(3):
        reg_rated.note_dispatch("d1")
    assert reg_rated.check("d1").reason == "rate_limited"
    assert reg_rated.check("d2").dispatchable, "一篇的回环不牵连另一篇"


def test_the_daily_budget_trips_before_the_rate_brake_when_tighter(reg_rated):
    # Both caps exist; check() reports the budget first, which is the tighter one here.
    reg_rated.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 2})
    reg_rated.note_dispatch("d1")
    reg_rated.note_dispatch("d1")
    assert reg_rated.check("d1").reason == "over_budget"


# ------------------------------------------------- the retired tier's tombstones


def test_legacy_reply_only_entry_is_a_tombstone_not_an_upgrade(reg):
    """A ledger written before the tier retired must read as **off**.

    Upgrading it to apply_scoped would hand the agent write authority over a
    document whose owner signed only for the narrower thing -- a silent widening.
    Honouring it is impossible (no contract, no toolset, no dispatch path). So it
    becomes a tombstone and the owner re-grants deliberately.
    """
    _write_legacy_entry(reg, "d1", "reply_only")
    v = reg.check("d1")
    assert v == WatchVerdict(False, None, "no_watch"), "退役档位视同无授权"
    entry = reg.get("d1")
    assert entry["mode"] == "reply_only", "绝不静默升档成 apply_scoped"
    assert entry["revoked"] is True and entry["revoked_reason"] == "retired_tier"
    assert not reg.is_write_live("d1")


def test_retired_tier_migration_is_persisted_with_one_audit_line(reg):
    _write_legacy_entry(reg, "d1", "reply_only")
    reg.check("d1")
    reg.check("d1")
    reg.snapshot()
    on_disk = json.loads(reg._path.read_text(encoding="utf-8"))
    assert on_disk["watches"]["d1"]["revoked"] is True, "迁移要落盘,不只在内存里"
    assert on_disk["watches"]["d1"]["revoked_reason"] == "retired_tier"
    lines = [a for a in reg.audit_tail()
             if a["event"] == "revoke" and a.get("reason") == "retired_tier"]
    assert len(lines) == 1, "一次迁移一行审计,不是每次读一行"


def test_retired_tombstone_is_not_resurrected_by_policy(reg):
    _write_legacy_entry(reg, "d1", "reply_only")
    assert reg.check("d1").reason == "no_watch"
    assert reg.terminated_by_owner("d1"), "迁移留碑后,纳管策略不得重新签发"
    # only a deliberate manual grant brings the document back
    reg.issue("d1", "apply_scoped", issued_by="manual")
    assert reg.check("d1").dispatchable


def test_a_suspended_or_expired_legacy_entry_still_reads_as_no_watch(reg):
    _write_legacy_entry(reg, "d1", "reply_only", suspended=True)
    _write_legacy_entry(reg, "d2", "reply_only", expired=True)
    _write_legacy_entry(reg, "d3", "some_future_tier")
    for doc in ("d1", "d2", "d3"):
        assert reg.check(doc).reason == "no_watch", "认不出的档位一律落到不派发"
        assert not reg.is_write_live(doc)


@pytest.mark.asyncio
async def test_watch_set_refuses_the_retired_tier_without_rounding_up(reg):
    """A stale client naming ``reply_only`` gets a refusal, not an upgrade.

    Rounding an unrecognised level to the one that survives would grant write
    authority on a click that asked for less.
    """
    import types

    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import CloudDocPanel

    panel = object.__new__(CloudDocPanel)
    panel._registry = lambda: reg
    panel._reg = types.SimpleNamespace(find_doc=lambda _doc_id: object())

    out = await panel.watch_set("d1", "reply_only")
    assert out["ok"] is False and "已退役" in out["detail"]
    assert reg.get("d1") is None, "被拒的档位不得落成任何 watch"

    out = await panel.watch_set("d1", "propose")
    assert out["ok"] is False and "未知档位" in out["detail"]
    assert reg.get("d1") is None


def test_the_migration_audits_even_when_a_write_runs_first(reg):
    """The tombstone and its audit line must not depend on a read coming first.

    ``issue`` for another document persists the migration as a side effect of
    loading. If only the read path audited, the owner would find the watch off
    with nothing in the journal saying why.
    """
    _write_legacy_entry(reg, "d1", "reply_only")
    reg.issue("d2", "apply_scoped")
    lines = [a for a in reg.audit_tail()
             if a["doc_id"] == "d1" and a.get("reason") == "retired_tier"]
    assert len(lines) == 1, "写路径先跑也要留下退役审计行"
    assert reg.check("d1").reason == "no_watch"


# ------------------------------------------------- the per-document lineage


def test_audit_for_returns_only_this_documents_lines_newest_first(reg):
    """The panel's authority lineage. ``audit_tail`` is the deployment's tail and
    answers a different question -- opened per document it shows mostly other
    documents' lines, and this document's may have scrolled off it entirely."""
    reg.issue("d1", "apply_scoped")
    reg.issue("d2", "apply_scoped")
    reg.note_dispatch("d1")
    reg.note_denied("d2", "no_watch")
    reg.revoke("d1", reason="document_removed")

    lines = reg.audit_for("d1")
    assert [x["event"] for x in lines] == ["revoke", "dispatch", "grant"], "最新在前"
    assert all(x["doc_id"] == "d1" for x in lines), "别的文档的行不得渗进来"
    assert lines[0]["reason"] == "document_removed"


def test_audit_for_never_claims_a_global_line_for_a_document(reg):
    """A line with no ``doc_id`` belongs to the deployment, not to whatever
    document the reader happens to have open. Attributing one would invent an
    association the journal never made."""
    reg.issue("d1", "apply_scoped")
    reg._audit("mode", None, value="direct")
    reg._audit("suspend_all", None, by="an_old_release")

    lines = reg.audit_for("d1")
    assert [x["event"] for x in lines] == ["grant"]
    assert all(x.get("doc_id") for x in lines)


def test_audit_for_is_empty_and_quiet_without_a_journal(reg):
    assert reg.audit_for("never-seen") == []


# --------------------------------------------------------------- is_on


def test_is_on_is_the_standing_fact_not_the_gates_transient_refusals(reg_rated):
    """Bulk "turn on" asks this, and it must not read a spent budget as off:
    re-issuing over a live grant restarts the thirty-day term, so a momentary
    refusal would turn "leave these alone" into "renew these"."""
    reg = reg_rated
    reg.issue("d1", "apply_scoped", budget={"max_dispatches_per_day": 1})
    reg.note_dispatch("d1")
    assert reg.check("d1").reason == "over_budget"
    assert reg.is_on("d1") is True, "额度用尽只是当下不派发,不是关着"

    reg.revoke("d1")
    assert reg.is_on("d1") is False
    reg.issue("d2", "apply_scoped", expires_at=1.0)
    assert reg.is_on("d2") is False, "到期视同关"
    assert reg.is_on("ghost") is False


# ------------------------- review: audit lines land after the state write, under its lock


def test_no_audit_line_for_a_state_write_that_failed(reg, monkeypatch):
    from jiuwenswarm.extensions.co_scribe.backend.host.authority import watch_registry as wr

    reg.issue("d1", "apply_scoped")
    before = reg.audit_tail(100)
    assert [e["event"] for e in before][-1] == "grant"

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(wr.json, "dumps", broken)
    with pytest.raises(OSError):
        reg.revoke("d1")
    monkeypatch.undo()
    assert reg.audit_tail(100) == before, "状态没写成，日志不得声称撤销发生过"
    assert reg.check("d1").allowed if hasattr(reg.check("d1"), "allowed") else True


def test_a_denial_outside_a_mutation_still_lands(reg):
    reg.issue("d1", "apply_scoped")
    reg.note_denied("d1", "over_budget")
    events = [e["event"] for e in reg.audit_tail(100)]
    assert events[-1] == "deny"
