# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Carry a stable OpenCode Go session header on every Team member model call."""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.opencode_go import (
    OPENCODE_GO_SESSION_HEADER,
    derive_session_id,
    is_opencode_go_client_config,
)

logger = logging.getLogger(__name__)


class OpenCodeSessionRail(DeepAgentRail):
    """Install the member-scoped ``x-opencode-session`` header before a model call.

    Recipes built by the adapter already carry a session header; this rail is
    the safety net for members whose model was installed by another path. It
    is a no-op for non-OpenCode Go models and for models that already carry a
    user-supplied session header.
    """

    # Before runtime model identity (5); after model planning (90).
    priority = 4

    def __init__(self, session_id: str, member_identity: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.member_identity = member_identity
        self._signature: tuple[int, str] | None = None

    def _scope(self) -> str:
        return f"member:{self.session_id}:{self.member_identity}"

    async def before_model_call(self, ctx: Any) -> None:
        agent = getattr(ctx, "agent", None)
        if agent is None:
            return
        llm = agent.get_llm()
        model_client_config = getattr(llm, "model_client_config", None)
        if model_client_config is None or not hasattr(model_client_config, "model_copy"):
            return
        if not is_opencode_go_client_config(model_client_config.model_dump()):
            return

        header_value = derive_session_id(self._scope())
        signature = (id(llm), header_value)
        if self._signature == signature:
            return

        custom_headers = dict(getattr(model_client_config, "custom_headers", None) or {})
        if any(
            str(key).strip().lower() == OPENCODE_GO_SESSION_HEADER
            for key in custom_headers
        ):
            return
        custom_headers[OPENCODE_GO_SESSION_HEADER] = header_value
        updated_config = model_client_config.model_copy(
            update={"custom_headers": custom_headers}
        )
        updated_llm = Model(
            model_client_config=updated_config,
            model_config=llm.model_config,
        )
        self._signature = (id(updated_llm), header_value)
        agent.set_llm(updated_llm)
