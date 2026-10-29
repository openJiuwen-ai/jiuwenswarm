# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-local normalization before SDK input and prompt construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gateway.common.mode_matrix import is_team_mode
from gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.runtime.session.model import TEAM_EXECUTION_CONTEXT_METADATA_KEY


def _nonempty_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class RequestContext:
    """An envelope snapshot; normalization never mutates the caller's mappings."""

    params: dict[str, Any]
    query: Any
    metadata: dict[str, Any]
    request_channel: str
    channel: str
    language: str
    trusted_dirs: list[str]
    skills: list[str] | None
    project_dir: str | None
    cwd: str | None
    request_debug: bool

    @classmethod
    def from_request(
        cls,
        request: Any,
        config: dict[str, Any],
        *,
        strip_debug: Callable[[str], tuple[str, bool]],
    ) -> RequestContext:
        params = dict(request.params) if isinstance(request.params, dict) else {}
        query = params.get("query")
        if query is None or query == "":
            query = params.get("content", "")
        if query is None or query == "":
            message = params.get("message")
            if message is not None:
                query = message
        raw_mode = params.get("mode")
        mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
        cross_session = isinstance(params.get(SESSION_MESSAGE_INTERNAL_KEY), dict)
        request_debug = False
        if (
            not cross_session
            and not (params.get("team") or is_team_mode(mode))
            and isinstance(query, str)
        ):
            query, request_debug = strip_debug(query)
        request_channel = str(request.channel_id or "").strip()
        channel = request_channel or (
            request.session_id.split("_")[0] if request.session_id else "web"
        )
        raw_dirs = params.get("trusted_dirs")
        trusted_dirs = (
            [
                value.strip()
                for value in raw_dirs
                if isinstance(value, str) and value.strip()
            ]
            if isinstance(raw_dirs, list)
            else []
        )
        raw_skills = params.get("skills")
        skills = (
            (
                [
                    value.strip()
                    for value in raw_skills
                    if isinstance(value, str) and value.strip()
                ]
                or None
            )
            if isinstance(raw_skills, list)
            else None
        )
        metadata = dict(request.metadata) if isinstance(request.metadata, dict) else {}
        if isinstance(params.get("metadata"), dict):
            metadata.update(params["metadata"])
        metadata.pop(TEAM_EXECUTION_CONTEXT_METADATA_KEY, None)
        return cls(
            params=params,
            query=query,
            metadata=metadata,
            request_channel=request_channel,
            channel=channel,
            language=config.get("preferred_language", "zh"),
            trusted_dirs=trusted_dirs,
            skills=skills,
            project_dir=_nonempty_text(params.get("project_dir"))
            or _nonempty_text(metadata.get("project_dir")),
            cwd=_nonempty_text(params.get("cwd"))
            or _nonempty_text(metadata.get("cwd")),
            request_debug=request_debug,
        )
