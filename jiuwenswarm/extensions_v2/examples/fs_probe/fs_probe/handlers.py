# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scan the session workspace and render the result through WebUI slots."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.events.ops_impl.common import OpsError
from openjiuwen.core.events.ops_impl.fs.models import (
    FsAncestorsInput,
    FsAncestorsOutput,
    FsExistsInput,
    FsKind,
    FsListInput,
    FsListOutput,
    FsReadInput,
    FsStatInput,
    FsStatus,
    FsWriteInput,
    FsWriteOutput,
)

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
)
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    RenderStatus,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

from .state import FileCheck, ProbeSnapshot, SessionProbe
from .views import (
    ACTION_EXPAND,
    ACTION_RESCAN,
    ACTION_SELECT,
    ACTION_WRITE,
    CHECKED_FILES,
    INSTANCE_KEY,
    MARKER_RELATIVE,
    PACKAGE_ID,
    SLOT_PANEL,
    panel_tree,
)

PREVIEW_LINES = 400
MAX_LISTED_ENTRIES = 40


class FsProbeHandlers:
    """Keep the latest scan per session and serve panel actions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionProbe] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def handlers(self) -> tuple:
        return (
            (CONTEXT_USAGE_EVENT, self.on_usage, {}),
            (WebUIEvent.READY.value, self.on_ready, {"target.slot_id": SLOT_PANEL}),
            (WebUIEvent.CLICK.value, self.on_click, {"target.slot_id": SLOT_PANEL}),
            (WebUIEvent.CHANGE.value, self.on_change, {"target.slot_id": SLOT_PANEL}),
            (WebUIEvent.SELECT.value, self.on_select, {"target.slot_id": SLOT_PANEL}),
        )

    async def on_usage(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> ContextUsageOutput:
        update = event.event_input
        if not isinstance(update, ContextUsageUpdate):
            return ContextUsageOutput(accepted=False)

        lock = self._locks.setdefault(update.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.setdefault(update.session_id, SessionProbe())
            if state.request_id != update.request_id:
                cwd = ops.session_context.cwd if ops is not None and ops.session_context is not None else None
                state.snapshot = await _scan(ops, cwd or _project_root(ops), _project_root(ops))
                state.request_id = update.request_id
                await _render(ops, update.session_id, state)
        return await _continue(next_handler, event, update.request_id)

    async def on_ready(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or target.instance_key not in ("", INSTANCE_KEY):
            return await next_handler(event)
        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.get(target.session_id)
            if (state is None or state.snapshot is None) and ops is not None:
                cwd = ops.session_context.cwd if ops.session_context is not None else None
                cwd = cwd or _project_root(ops)
                if cwd:
                    state = self._sessions.setdefault(target.session_id, SessionProbe())
                    state.snapshot = await _scan(ops, cwd, _project_root(ops) or cwd)
            snapshot = state.snapshot if state is not None else None
            output = await _render(ops, target.session_id, state)
        receipt = _receipt(output, "rendered" if snapshot is not None else "idle")
        await next_handler(event)
        return receipt

    async def on_click(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        action_id = request.payload.get("action_id") if isinstance(request.payload, Mapping) else None
        if (
            target.package_id != PACKAGE_ID
            or target.instance_key != INSTANCE_KEY
            or action_id not in (ACTION_RESCAN, ACTION_WRITE)
        ):
            return await next_handler(event)

        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.get(target.session_id)
            if state is None or state.snapshot is None:
                return UiActionOutput(accepted=True, status="idle")
            if action_id == ACTION_RESCAN:
                state.snapshot = await _scan(ops, state.snapshot.cwd, state.snapshot.ancestor_stop)
                state.expanded.clear()
                state.children.clear()
            else:
                write = await _write(ops, state.snapshot.cwd, target.session_id)
                snapshot = state.snapshot
                if write.status is FsStatus.OK:
                    snapshot = await _scan(ops, snapshot.cwd, snapshot.ancestor_stop)
                    state.expanded.clear()
                    state.children.clear()
                state.snapshot = replace(snapshot, write=write, refreshed_at=_now())
            output = await _render(ops, target.session_id, state)
        return _receipt(output, "rendered")

    async def on_change(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        payload = request.payload if isinstance(request.payload, Mapping) else {}
        data = payload.get("data")
        key = _tree_key(data.get("key") if isinstance(data, Mapping) else None)
        expanded = data.get("expanded") if isinstance(data, Mapping) else None
        if (
            target.package_id != PACKAGE_ID
            or target.instance_key != INSTANCE_KEY
            or payload.get("action_id") != ACTION_EXPAND
            or not key
            or not isinstance(expanded, bool)
            or ops is None
        ):
            return await next_handler(event)

        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.get(target.session_id)
            if state is None or state.snapshot is None:
                return UiActionOutput(accepted=True, status="idle")
            if expanded:
                listed = await ops.fs.list(
                    FsListInput(
                        path=os.path.join(state.snapshot.cwd, *key.split("/")),
                        include_dirs=True,
                        sort_by="name",
                        max_entries=MAX_LISTED_ENTRIES,
                    )
                )
                if listed.status is FsStatus.OK:
                    state.children[key] = tuple(listed.entries)
                    state.expanded.add(key)
            else:
                state.expanded.discard(key)
                state.expanded.difference_update(
                    item for item in state.expanded if item.startswith(f"{key}/")
                )
                for cached in [item for item in state.children if item == key or item.startswith(f"{key}/")]:
                    state.children.pop(cached, None)
            output = await _render(ops, target.session_id, state)
        return _receipt(output, "rendered")

    async def on_select(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        payload = request.payload if isinstance(request.payload, Mapping) else {}
        data = payload.get("data")
        key = _tree_key(data.get("key") if isinstance(data, Mapping) else None)
        if (
            target.package_id != PACKAGE_ID
            or target.instance_key != INSTANCE_KEY
            or payload.get("action_id") != ACTION_SELECT
            or not key
            or ops is None
        ):
            return await next_handler(event)

        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.get(target.session_id)
            if state is None or state.snapshot is None:
                return UiActionOutput(accepted=True, status="idle")
            path = os.path.join(state.snapshot.cwd, *key.split("/"))
            stat = await ops.fs.stat(FsStatInput(path=path))
            if stat.status is not FsStatus.OK or stat.kind is FsKind.DIRECTORY:
                return UiActionOutput(accepted=True, status="idle")
            preview = await ops.fs.read(FsReadInput(path=path, head=PREVIEW_LINES)) if stat.found else None
            state.snapshot = replace(
                state.snapshot,
                preview=preview,
                preview_stat=stat,
                refreshed_at=_now(),
            )
            output = await _render(ops, target.session_id, state)
        return _receipt(output, "rendered")


async def _scan(ops: EventHandlerOps | None, cwd: str | None, stop_at: str | None) -> ProbeSnapshot:
    if ops is None or not cwd:
        return _unavailable(cwd or "", "session cwd is unavailable", stop_at)
    cwd = os.path.abspath(cwd)
    stop_at = stop_at or cwd
    try:
        listed = await ops.fs.list(
            FsListInput(path=cwd, include_dirs=True, sort_by="name", max_entries=MAX_LISTED_ENTRIES)
        )
        checks: list[FileCheck] = []
        exists_status = FsStatus.OK
        for name in CHECKED_FILES:
            path = os.path.join(cwd, name)
            result = await ops.fs.exists(FsExistsInput(path=path))
            if result.status is not FsStatus.OK:
                exists_status = result.status
            checks.append(FileCheck(name=name, path=path, exists=result.exists))

        preview_path = next((check.path for check in checks if check.exists), None)
        if preview_path is None:
            preview_path = next((entry.path for entry in listed.entries if not entry.is_directory), None)
        preview = None
        preview_stat = None
        if preview_path is not None:
            preview = await ops.fs.read(FsReadInput(path=preview_path, head=PREVIEW_LINES))
            preview_stat = await ops.fs.stat(FsStatInput(path=preview_path))

        ancestors = await ops.fs.ancestors(
            FsAncestorsInput(
                names=("AGENTS.md", "CLAUDE.md"),
                start=cwd,
                stop_at=stop_at,
                max_files=4,
                max_bytes_per_file=4096,
            )
        )
        return ProbeSnapshot(
            cwd=cwd,
            refreshed_at=_now(),
            ancestor_stop=stop_at,
            listed=listed,
            exists_status=exists_status,
            checks=tuple(checks),
            preview=preview,
            preview_stat=preview_stat,
            ancestors=ancestors,
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure must not break the chat round
        return _unavailable(cwd, str(exc) or type(exc).__name__, stop_at)


async def _write(ops: EventHandlerOps | None, cwd: str, session_id: str) -> FsWriteOutput:
    path = os.path.join(cwd, MARKER_RELATIVE)
    if ops is None:
        return _write_failure(path, "session cwd is unavailable")
    content = f"# FS Probe\n\nsession: {session_id}\nwritten: {_now()}\n"
    try:
        return await ops.fs.write(FsWriteInput(path=path, content=content, prepend_newline=False))
    except Exception as exc:  # noqa: BLE001 - surface the failure in the panel
        return _write_failure(path, str(exc) or type(exc).__name__)


async def _render(
    ops: EventHandlerOps | None,
    session_id: str,
    state: SessionProbe | None,
) -> UiRenderOutput | None:
    if ops is None:
        return None
    target = RenderTarget(
        session_id=session_id,
        slot_id=SLOT_PANEL,
        package_id=PACKAGE_ID,
        instance_key=INSTANCE_KEY,
    )
    root = panel_tree(
        state.snapshot if state is not None else None,
        state.expanded if state is not None else frozenset(),
        state.children if state is not None else None,
    )
    return await ops.ui.render(UiRenderInput(target=target, root=root))


def _receipt(output: UiRenderOutput | None, delivered_status: str) -> UiActionOutput:
    if output is None:
        return UiActionOutput(accepted=False, status="unavailable")
    if output.status is RenderStatus.DELIVERED:
        return UiActionOutput(accepted=True, status=delivered_status)
    return UiActionOutput(accepted=False, status=output.status.value)


async def _continue(next_handler: EventNext, event: Event, request_id: str) -> ContextUsageOutput:
    """Let other plugins and the CORE handler observe the same usage event."""

    downstream = await next_handler(event)
    return downstream if isinstance(downstream, ContextUsageOutput) else ContextUsageOutput(
        accepted=True, request_id=request_id
    )


def _unavailable(cwd: str, message: str, stop_at: str | None) -> ProbeSnapshot:
    error = OpsError(kind="unavailable", message=message)
    return ProbeSnapshot(
        cwd=cwd,
        refreshed_at=_now(),
        ancestor_stop=stop_at,
        listed=FsListOutput(status=FsStatus.DENIED, root_path=cwd, error=error),
        exists_status=FsStatus.DENIED,
        checks=(),
        preview=None,
        preview_stat=None,
        ancestors=FsAncestorsOutput(status=FsStatus.DENIED, error=error),
    )


def _write_failure(path: str, message: str) -> FsWriteOutput:
    return FsWriteOutput(
        status=FsStatus.ERROR,
        path=path,
        error=OpsError(kind="fs_error", message=message),
    )


def _project_root(ops: EventHandlerOps | None) -> str | None:
    context = ops.session_context if ops is not None else None
    metadata = context.metadata if context is not None else None
    value = metadata.get("project_root") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, str) and value else None


def _tree_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.replace("\\", "/").strip("/")
    if not key or os.path.isabs(key) or ":" in key:
        return None
    if any(part in ("", ".", "..") for part in key.split("/")):
        return None
    return key


def _now() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")
