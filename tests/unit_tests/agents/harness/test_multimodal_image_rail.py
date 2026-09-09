from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails import multimodal_image_rail
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)


def test_multimodal_image_rail_auto_policy_tracks_main_model_probe(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            enable_read_image_multimodal=None,
            model=object(),
        )
    )
    probe_state = {"supported": False}
    monkeypatch.setattr(
        multimodal_image_rail,
        "should_enable_read_image_multimodal",
        lambda candidate: candidate is agent and probe_state["supported"],
    )
    rail = MultimodalImageRail()
    rail.init(agent)

    assert rail._read_image_multimodal_enabled() is False

    probe_state["supported"] = True
    assert rail._read_image_multimodal_enabled() is True


def test_multimodal_image_rail_explicit_policy_skips_probe(
    monkeypatch,
) -> None:
    def fail_if_called(_agent) -> bool:
        raise AssertionError("probe used")

    monkeypatch.setattr(
        multimodal_image_rail,
        "should_enable_read_image_multimodal",
        fail_if_called,
    )

    enabled = MultimodalImageRail(enable_image_multimodal=True)
    disabled = MultimodalImageRail(enable_image_multimodal=False)

    assert enabled._read_image_multimodal_enabled()
    assert not disabled._read_image_multimodal_enabled()
