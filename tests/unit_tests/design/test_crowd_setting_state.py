"""Crowd state belongs to the setting: establish once, inherit, event-replace."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.clip_policy import (
    _crowd_lock_for_setting,
)
from jiuwenswarm.server.runtime.designer.script_analysis import _normalize_shot_crowd


def test_first_shot_of_setting_establishes_crowd() -> None:
    shot = {
        "crowd_spec": {
            "present": True,
            "density": "a dozen patrons",
            "placement": "background",
        },
    }
    lock = _crowd_lock_for_setting(shot, is_first_of_set=True, prev_crowd=None)
    assert lock["present"] is True
    assert lock["density"] == "a dozen patrons"
    assert lock["disposition"] == "background_hold"


def test_undecided_shot_inherits_setting_state() -> None:
    prev = {
        "present": True,
        "density": "a dozen patrons",
        "disposition": "background_hold",
        "rule": "r",
        "note": "lunch crowd",
    }
    lock = _crowd_lock_for_setting({}, is_first_of_set=False, prev_crowd=prev)
    assert lock["present"] is True
    assert lock["density"] == "a dozen patrons"
    assert lock["disposition"] == "background_hold"
    assert lock["note"] == "lunch crowd"


def test_mid_setting_declaration_replaces_state() -> None:
    prev = {"present": True, "density": "a dozen", "disposition": "background_hold"}
    shot = {"crowd_spec": {"present": False, "disposition": "exited"}}
    lock = _crowd_lock_for_setting(shot, is_first_of_set=False, prev_crowd=prev)
    assert lock["present"] is False
    assert lock["disposition"] == "exited"
    # The old density does not bleed into the emptied setting.
    assert lock["density"] == ""


def test_nothing_declared_stays_unspecified() -> None:
    lock = _crowd_lock_for_setting({}, is_first_of_set=True, prev_crowd=None)
    assert lock["present"] is None
    assert lock["disposition"] == ""


def test_normalize_shot_crowd_drops_continue() -> None:
    out = _normalize_shot_crowd({"present": True, "density": "sparse", "continue": True})
    assert out.get("present") is True
    assert "continue" not in out


def test_crowd_lock_key_also_counts_as_declaration() -> None:
    # Downstream storyboard nodes restate the lock as crowd_lock; it is the
    # same declaration channel and must not be mistaken for "undecided".
    shot = {"crowd_lock": {"present": True, "density": "dense", "disposition": "in_frame"}}
    prev = {"present": False, "density": "", "disposition": "empty"}
    lock = _crowd_lock_for_setting(shot, is_first_of_set=False, prev_crowd=prev)
    assert lock["present"] is True
    assert lock["density"] == "dense"
