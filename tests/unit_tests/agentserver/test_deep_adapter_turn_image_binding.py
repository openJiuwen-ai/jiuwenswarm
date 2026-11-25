from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
    RuntimePromptRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

_IMAGE = {"type": "image", "path": "C:/tmp/a.png", "mime_type": "image/png"}


def _make_request(params: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        params=params or {},
        request_id="req-test",
        session_id="sess-test",
        channel_id="web",
    )


def _make_adapter(rails: bool = True) -> SimpleNamespace:
    adapter = SimpleNamespace()
    if rails:
        adapter._multimodal_image_rail = MultimodalImageRail(enable_image_multimodal=True)
        adapter._runtime_prompt_rail = RuntimePromptRail()
    return adapter


def _call_bind(adapter: SimpleNamespace, request: SimpleNamespace, files):
    return JiuWenSwarmDeepAdapter._bind_turn_image_state(adapter, request, files)


def _call_restore(adapter: SimpleNamespace, tokens):
    return JiuWenSwarmDeepAdapter._restore_turn_image_state(adapter, tokens)


class TestBindTurnImageState:
    def test_attach_only_subscription_binds_nothing(self) -> None:
        adapter = _make_adapter()
        tokens = _call_bind(
            adapter,
            _make_request({"attach_only": True}),
            [_IMAGE],
        )
        assert tokens is None
        assert adapter._multimodal_image_rail._turn_image_files is None

    def test_missing_rails_bind_nothing(self) -> None:
        adapter = _make_adapter(rails=False)
        tokens = _call_bind(adapter, _make_request(), [_IMAGE])
        assert tokens is None

    def test_owning_image_request_binds_both_rails(self) -> None:
        adapter = _make_adapter()
        tokens = _call_bind(adapter, _make_request(), [_IMAGE])

        assert tokens is not None
        assert "multimodal_image" in tokens
        assert "runtime_prompt" in tokens
        assert adapter._multimodal_image_rail._turn_image_files == [_IMAGE]
        assert adapter._runtime_prompt_rail._turn_image_files == [_IMAGE]

    def test_imageless_turn_still_binds_empty(self) -> None:
        adapter = _make_adapter()
        tokens = _call_bind(adapter, _make_request(), [])

        assert tokens is not None
        assert adapter._multimodal_image_rail._turn_image_files == []
        assert adapter._runtime_prompt_rail._turn_image_files == []

    def test_restore_releases_both_rails_to_contextvar_fallback(self) -> None:
        adapter = _make_adapter()
        tokens = _call_bind(adapter, _make_request(), [_IMAGE])
        _call_restore(adapter, tokens)

        assert adapter._multimodal_image_rail._turn_image_files is None
        assert adapter._runtime_prompt_rail._turn_image_files is None
        assert adapter._multimodal_image_rail._turn_binding_token is None

    def test_restore_with_none_tokens_is_noop(self) -> None:
        adapter = _make_adapter()
        _call_restore(adapter, None)
        assert adapter._multimodal_image_rail._turn_image_files is None


class TestAttachAndSendInputsBinding:
    @staticmethod
    def _make_fake_stream() -> Any:
        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self, abort_active_round: bool = False) -> None:
                return None

        return _Stream()

    @staticmethod
    def _make_send_adapter(order: list, send_result: bool = False) -> SimpleNamespace:
        mm = MultimodalImageRail(enable_image_multimodal=True)
        rp = RuntimePromptRail()
        original_mm_bind = mm.bind_turn_image_files
        original_rp_bind = rp.bind_turn_image_files

        def mm_bind(files):
            order.append("bind_mm")
            return original_mm_bind(files)

        def rp_bind(files):
            order.append("bind_rp")
            return original_rp_bind(files)

        mm.bind_turn_image_files = mm_bind
        rp.bind_turn_image_files = rp_bind

        async def send(request, *, send=None):
            order.append("send_input")
            return send_result

        adapter = SimpleNamespace(
            _instance=SimpleNamespace(
                attach_output=AsyncMock(
                    return_value=TestAttachAndSendInputsBinding._make_fake_stream(),
                ),
            ),
            _enable_auto_permission=False,
            _permission_dispatch=SimpleNamespace(release=Mock()),
            _resolve_input_dispatch_mode=lambda params: None,
            _should_refresh_cron_web_context=lambda request, send_without_output: False,
            _send_input_with_permission_resume_guard=send,
            _permission_inputs_for_dispatch=lambda request, inputs, mode: inputs,
            _multimodal_image_rail=mm,
            _runtime_prompt_rail=rp,
        )
        adapter._bind_turn_image_state = (
            JiuWenSwarmDeepAdapter._bind_turn_image_state.__get__(adapter)
        )
        adapter._restore_turn_image_state = (
            JiuWenSwarmDeepAdapter._restore_turn_image_state.__get__(adapter)
        )
        return adapter

    @pytest.mark.asyncio
    async def test_owner_path_binds_before_send_input(self) -> None:
        order: list = []
        adapter = self._make_send_adapter(order)

        tokens_out: dict = {}
        stream, dispatched = await JiuWenSwarmDeepAdapter._attach_and_send_inputs(
            adapter, _make_request(), {}, send_without_output=False,
            turn_image_files=[_IMAGE],
            image_binding_tokens_out=tokens_out,
        )

        assert stream is not None
        assert dispatched is False
        assert tokens_out
        assert order == ["bind_mm", "bind_rp", "send_input"]
        assert adapter._multimodal_image_rail._turn_image_files == [_IMAGE]
        assert adapter._runtime_prompt_rail._turn_image_files == [_IMAGE]

        JiuWenSwarmDeepAdapter._restore_turn_image_state(adapter, dict(tokens_out))
        assert adapter._multimodal_image_rail._turn_image_files is None

    @pytest.mark.asyncio
    async def test_lease_owning_send_without_output_binds(self) -> None:
        order: list = []
        adapter = self._make_send_adapter(order)

        tokens_out: dict = {}
        stream, _dispatched = await JiuWenSwarmDeepAdapter._attach_and_send_inputs(
            adapter, _make_request(), {}, send_without_output=True,
            turn_image_files=[_IMAGE],
            image_binding_tokens_out=tokens_out,
        )

        assert stream is not None
        assert order == ["bind_mm", "bind_rp", "send_input"]
        assert tokens_out
        assert adapter._multimodal_image_rail._turn_image_files == [_IMAGE]

    @pytest.mark.asyncio
    async def test_busy_session_injects_without_taking_the_binding(self) -> None:
        order: list = []
        adapter = self._make_send_adapter(order, send_result=True)

        adapter._instance.attach_output = AsyncMock(return_value=None)

        tokens_out: dict = {}
        stream, dispatched = await JiuWenSwarmDeepAdapter._attach_and_send_inputs(
            adapter, _make_request(), {}, send_without_output=True,
            turn_image_files=[_IMAGE],
            image_binding_tokens_out=tokens_out,
        )

        assert stream is None
        assert dispatched is True
        assert order == ["send_input"]
        assert not tokens_out
        assert adapter._multimodal_image_rail._turn_image_files is None
        assert adapter._runtime_prompt_rail._turn_image_files is None

    @pytest.mark.asyncio
    async def test_send_failure_rolls_back_binding(self) -> None:
        order: list = []
        adapter = self._make_send_adapter(order)

        async def failing_send(request, *, send=None):
            order.append("send_input")
            raise RuntimeError("send failed")

        adapter._send_input_with_permission_resume_guard = failing_send

        with pytest.raises(RuntimeError, match="send failed"):
            await JiuWenSwarmDeepAdapter._attach_and_send_inputs(
                adapter, _make_request(), {}, send_without_output=False,
                turn_image_files=[_IMAGE],
            )

        assert order == ["bind_mm", "bind_rp", "send_input"]
        assert adapter._multimodal_image_rail._turn_image_files is None
        assert adapter._runtime_prompt_rail._turn_image_files is None
        assert adapter._multimodal_image_rail._turn_binding_token is None