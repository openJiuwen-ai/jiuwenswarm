"""Connector-held messages use the host's queued-turn marker."""

from jiuwenswarm.extensions.sdk.queued_turn import mark_queued_new_turn
from jiuwenswarm.gateway.im_pipeline.im_session_input import IM_QUEUED_NEW_TURN_KEY


def test_queued_turn_marker_matches_im_pipeline():
    metadata = {}
    mark_queued_new_turn(metadata)
    assert metadata == {IM_QUEUED_NEW_TURN_KEY: True}
