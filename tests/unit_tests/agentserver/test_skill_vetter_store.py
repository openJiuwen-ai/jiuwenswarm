from jiuwenswarm.server.runtime.skill.skill_vetter.store import (
    get_vet_approval,
    get_vet_report,
    set_vet_approval,
    set_vet_report,
)


def test_report_roundtrip_in_state():
    state = {}
    report = {"grade": "high", "content_hash": "abc", "findings": []}
    set_vet_report(state, report)
    assert get_vet_report(state, "abc") == report
    assert get_vet_report(state, "missing") is None


def test_approval_roundtrip_keyed_by_hash():
    state = {}
    set_vet_approval(state, "abc", approved_by="u1")
    got = get_vet_approval(state, "abc")
    assert got is not None
    assert got["approved_by"] == "u1"
    assert "approved_at" in got
    # different hash -> no approval
    assert get_vet_approval(state, "xyz") is None


def test_store_is_isolated_from_other_state_keys():
    state = {"skill_configs": {"x": {"enabled": True}}}
    set_vet_report(state, {"grade": "low", "content_hash": "h", "findings": []})
    assert state["skill_configs"] == {"x": {"enabled": True}}
