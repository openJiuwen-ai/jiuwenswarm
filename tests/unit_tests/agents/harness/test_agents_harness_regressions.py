# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 3 small test modules)."""

from __future__ import annotations

import pytest
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    complete_multimodal_model_configured,
    multimodal_model_enabled,
)
from types import SimpleNamespace
from jiuwenswarm.agents.harness.common.rails import multimodal_image_rail
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from openjiuwen.harness.subagent_runtime.control import SubagentControl
from openjiuwen.harness.subagent_runtime.models import (
    ResumeResult,
    SubagentStatus,
    SubagentStatusKind,
)
from openjiuwen.harness.subagent_runtime.status import StatusChannel
from openjiuwen.harness.subagent_runtime.status_events import (
    build_subagent_updated_payload,
)
from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
    CompatibleSubagentControl,
    install_subagent_control_compat_patch,
)


# --- from test_multimodal_config.py ---

def _vision_config(*, provider: str = "OpenAI") -> dict:
    return {
        "models": {
            "vision": {
                "model_client_config": {
                    "api_base": "https://vision.example/v1",
                    "api_key": "secret",
                    "model_name": "vision-model",
                    "client_provider": provider,
                }
            }
        }
    }

def test_complete_multimodal_config_requires_all_four_fields() -> None:
    assert complete_multimodal_model_configured(_vision_config(), "vision") is True
    assert (
        complete_multimodal_model_configured(_vision_config(provider=""), "vision")
        is False
    )

def test_multimodal_enabled_prefers_explicit_switch_and_preserves_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _vision_config()
    monkeypatch.delenv("VISION_ENABLED", raising=False)
    assert multimodal_model_enabled(config, "vision") is True

    monkeypatch.setenv("VISION_ENABLED", "false")
    assert multimodal_model_enabled(config, "vision") is False

    monkeypatch.setenv("VISION_ENABLED", "")
    assert multimodal_model_enabled(config, "vision") is False

    monkeypatch.setenv("VISION_ENABLED", "true")
    assert multimodal_model_enabled(config, "vision") is True

# --- from test_multimodal_image_rail.py ---

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

# --- from test_subagent_compat.py ---

class _RestoredInstance:
    def __init__(self) -> None:
        self.status = StatusChannel()

    def agent_status(self) -> SubagentStatus:
        return self.status.current()

@pytest.mark.asyncio
async def test_restored_subagent_is_reported_idle_and_accepts_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed instance has no queued turn and must be externally idle."""
    restored = _RestoredInstance()
    control = object.__new__(CompatibleSubagentControl)
    setattr(control, "_manager", SimpleNamespace(find=lambda subagent_id: restored))

    async def _resume(_self: SubagentControl, subagent_id: str) -> ResumeResult:
        assert subagent_id == "sub-1"
        return ResumeResult(status=SubagentStatus.pending_init(), restored=True)

    monkeypatch.setattr(SubagentControl, "resume", _resume)

    result = await control.resume("sub-1")

    assert result.status.kind is SubagentStatusKind.COMPLETED
    payload = build_subagent_updated_payload(
        subagent_id="sub-1",
        subagent_type="explore_agent",
        display_name="Explorer",
        role="Explore code",
        parent_session_id="parent-1",
        task_description="Inspect the repository",
        created_at_ms=1.0,
        updated_at_ms=2.0,
        closed_at_ms=None,
        status=restored.agent_status(),
        revision=restored.status.version(),
    )
    assert payload["status"] == "idle"
    assert payload["can_send_input"] is True
    assert payload["needs_resume"] is False

def test_install_patch_rebinds_control_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """New parent sessions must be created with the compatible control."""
    from openjiuwen.harness.tools.subagent import _control_registry

    monkeypatch.setattr(_control_registry, "SubagentControl", SubagentControl)

    install_subagent_control_compat_patch()

    assert _control_registry.SubagentControl is CompatibleSubagentControl

