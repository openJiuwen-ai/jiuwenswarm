# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the pinned SDK's lazy Team tool-document scan off the Worker loop."""

from __future__ import annotations

import functools
import logging
import threading
import time
import weakref
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.background_work import BackgroundWorkBusy, BoundedBackgroundWork

logger = logging.getLogger(__name__)

_FIRST_READ_WAIT = 0.05
_ERROR_RETRY_DELAY = 30.0
_BUSY_RETRY_DELAY = 1.0
_reads = BoundedBackgroundWork(max_workers=2, name="team-tool-document-read")
_lock = threading.RLock()
_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@dataclass
class _ReadState:
    future: Future | None = None
    retry_at: float = 0.0
    warned: bool = False


def install_team_workspace_read_guard() -> None:
    """Adapt only synchronous document reads; SDK async owners stay on-loop.

    agent-core's first get_tool_md/get_tool_param scans and parses all tool
    documents synchronously. Read into an isolated cache instead. A short
    first wait preserves fast customized descriptions; a slow read uses the
    SDK's bundled descriptions until its complete snapshot can be published.
    """
    from openjiuwen.agent_teams.team_workspace.workspace_cache import WorkspaceCache

    with _lock:
        original = WorkspaceCache._load_tools
        if getattr(original, "jiuwen_background_read", False):
            return
        original_invalidate = WorkspaceCache.invalidate

        @functools.wraps(original)
        def load_tools(cache: Any) -> None:
            wait = False
            warning = False
            with _lock:
                if cache._tools_loaded:
                    return
                state = _states.setdefault(cache, _ReadState())
                if state.future is None and time.monotonic() >= state.retry_at:
                    store, team_name, language = cache._store, cache._team_name, cache._lang

                    def read():
                        snapshot = WorkspaceCache(store, team_name, language=language)
                        original(snapshot)
                        return snapshot._tool_md_values, snapshot._tool_params

                    try:
                        state.future = _reads.submit(read)
                        wait = True
                    except BackgroundWorkBusy:
                        state.retry_at = time.monotonic() + _BUSY_RETRY_DELAY
                future = state.future
            if future is not None and wait:
                try:
                    future.result(timeout=_FIRST_READ_WAIT)
                except Exception:
                    # Publish or report the error below. Never fall back to a
                    # synchronous scan on the business event loop.
                    pass
            with _lock:
                if _states.get(cache) is not state or cache._tools_loaded:
                    return  # Invalidated or primed while the read was in flight.
                if future is not None and future.done():
                    try:
                        descriptions, parameters = future.result()
                    except Exception as exc:
                        state.future = None
                        state.retry_at = time.monotonic() + _ERROR_RETRY_DELAY
                        if not state.warned:
                            logger.warning("[TeamWorkspace] tool document read failed (%s); "
                                           "using bundled descriptions", type(exc).__name__)
                            state.warned = True
                        return
                    # The assembler can prime newer values during a slow read.
                    # Its values win over the isolated disk snapshot.
                    cache._tool_md_values = {**descriptions, **cache._tool_md_values}
                    cache._tool_params = {**parameters, **cache._tool_params}
                    cache._tools_loaded = True
                    _states.pop(cache, None)
                    return
                if not state.warned:
                    state.warned = True
                    warning = True
            if warning:
                logger.warning("[TeamWorkspace] tool documents are still loading; "
                               "using bundled descriptions")

        @functools.wraps(original_invalidate)
        def invalidate(cache: Any) -> None:
            with _lock:
                state = _states.pop(cache, None)
                if state is not None and state.future is not None:
                    # Running IO retains its bounded slot. No completion
                    # callback can publish this obsolete snapshot afterwards.
                    state.future.cancel()
                original_invalidate(cache)

        load_tools.jiuwen_background_read = True
        WorkspaceCache._load_tools = load_tools
        WorkspaceCache.invalidate = invalidate


def preload_bundled_team_descriptions() -> None:
    """Warm immutable SDK fallback files on the backend preparation thread."""
    from openjiuwen.agent_teams.tools import locales

    for language in ("cn", "en"):
        for key in locales._desc_index(language):
            locales._load_desc(key, language)
        fragments = locales._DESCS_DIR / language / "fragments"
        for path in fragments.glob("*.md"):
            locales._load_fragment(path.stem, language)
