from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    reset_current_multimodal_image_files,
    set_current_multimodal_image_files,
)
from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail as runtime_module
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail

_IMAGE = {"type": "image", "path": "C:/tmp/a.png", "mime_type": "image/png"}
_MEDIA_SECTION = "runtime.current_media"


@pytest.fixture(autouse=True)
def _isolated_runtime_state(tmp_path, monkeypatch):
    state_path = tmp_path / "session.yaml"
    state_path.write_text("model: GLM-5.3\n", encoding="utf-8")
    monkeypatch.setattr(runtime_module, "get_runtime_state_path", lambda session_id: state_path)
    monkeypatch.setattr(RuntimePromptRail, "_configured_model_names", staticmethod(lambda: []))


def _rail(language: str = "cn") -> tuple[RuntimePromptRail, PromptAttachmentManager]:
    manager = PromptAttachmentManager()
    rail = RuntimePromptRail(language=language)
    rail.init(SimpleNamespace(prompt_attachment_manager=manager, system_prompt_builder=None))
    rail.set_session_id("turn-image-session")
    rail.set_model_name("GLM-5.3")
    return rail, manager


def _context() -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=SimpleNamespace(config=SimpleNamespace(model_name="GLM-5.3")),
        session=SimpleNamespace(get_session_id=lambda: "turn-image-session"),
    )


async def _sections(manager: PromptAttachmentManager) -> set[str]:
    items = await manager.collect_for_session("turn-image-session")
    return {item.section for item in items}


class TestCurrentMediaGate:
    @pytest.mark.asyncio
    async def test_turn_images_suppress_the_no_image_notice(self) -> None:
        rail, manager = _rail()
        token = rail.bind_turn_image_files([_IMAGE])
        try:
            await rail._refresh_dynamic_attachments(_context())
            assert _MEDIA_SECTION not in await _sections(manager)
        finally:
            rail.restore_turn_image_files(token)

    @pytest.mark.asyncio
    async def test_imageless_turn_emits_the_no_image_notice(self) -> None:
        rail, manager = _rail()
        await rail._refresh_dynamic_attachments(_context())
        assert _MEDIA_SECTION in await _sections(manager)

    @pytest.mark.asyncio
    async def test_explicit_empty_binding_beats_a_stale_contextvar(self) -> None:
        rail, manager = _rail()
        context_token = set_current_multimodal_image_files([_IMAGE])
        try:
            token = rail.bind_turn_image_files([])
            await rail._refresh_dynamic_attachments(_context())
            assert _MEDIA_SECTION in await _sections(manager)

            rail.restore_turn_image_files(token)
        finally:
            reset_current_multimodal_image_files(context_token)

    @pytest.mark.asyncio
    async def test_restore_reopens_the_contextvar_fallback(self) -> None:
        rail, manager = _rail()
        context_token = set_current_multimodal_image_files([_IMAGE])
        try:
            token = rail.bind_turn_image_files([_IMAGE])
            rail.restore_turn_image_files(token)

            assert rail._turn_image_files is None
            await rail._refresh_dynamic_attachments(_context())
            assert _MEDIA_SECTION not in await _sections(manager)
        finally:
            reset_current_multimodal_image_files(context_token)


class TestForkCopiesTurnImageState:
    def test_child_rail_inherits_the_turn_state_without_its_token(self) -> None:
        rail = RuntimePromptRail()
        token = rail.bind_turn_image_files([_IMAGE])

        child = rail.fork_for_agent()

        assert child is not rail
        assert child._current_turn_image_files() == [_IMAGE]
        assert child._turn_binding_token is None
        child.restore_turn_image_files(None)
        assert rail._turn_image_files == [_IMAGE]
        rail.restore_turn_image_files(token)

    def test_unbound_rail_forks_unbound(self) -> None:
        rail = RuntimePromptRail()
        assert rail.fork_for_agent()._turn_image_files is None