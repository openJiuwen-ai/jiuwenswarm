"""Extension entry point: two right-panel tabs fed by real events and Ops.

Flow (contract §0):

* ``ui.ready`` → the listener is already up (started before any render) →
  refresh the scope → render one ``Iframe`` root per ``instance_key`` with
  ``?sid=<session>`` in ``src``.
* ``team.monitor_event`` → learn ``session → team`` → re-read *everything*
  through ``ops.team.*`` → publish a new revision → wake long polls.

Lifetime note (measured on the real host): the host does not keep the object
returned by ``create_extension()``. It *does* keep the bound methods handed to
``register_event_handler``, so the handler object is the only reliable anchor:
**service owner = handler object, watchdog period = 0.2s, port lifetime =
activation**. Anchoring on the extension instance releases the port ~2ms after
``register`` and leaves the page pointing at a dead port.

The event payload is lossy, so no delta bookkeeping is done: every event fully
rebuilds the snapshot, and HTTP threads only ever read the resulting plain dict.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Mapping
from urllib.parse import quote

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
)

from . import lifecycle
from .http_server import BIND_ADDRESS, ServiceContext
from .sessionmap import SOURCE_EVENT, SOURCE_NONE, SessionMap
from .snapshot import (
    GLOBAL_SCOPE_KEY,
    TEAM_LIFECYCLE_EVENTS,
    build_snapshot,
    now_ms,
)

LOGGER = logging.getLogger(__name__)

PACKAGE_ID = "webui.team-collab-viz"
SLOT = "conversation.panel.right"

TAB_TEAM_INFO = "tcv.team-info"
TAB_TASK_GRAPH = "tcv.task-graph"
TABS: dict[str, str] = {TAB_TEAM_INFO: "团队信息", TAB_TASK_GRAPH: "任务关系"}
TAB_PAGES: dict[str, str] = {TAB_TEAM_INFO: "team-info.html", TAB_TASK_GRAPH: "task-graph.html"}

STALE_OPS_ERROR = "ops_error"
STALE_NO_SESSION_MAP = "no_session_map"
STALE_COLD_START = "cold_start"


def _attr(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing host object."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _enum_value(value: Any) -> str:
    """Normalise a (possibly enum) host value into its string form."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else ("" if raw is None else str(raw))


class _Facts:
    """Per-team lifecycle and last-event facts (plain scalars only)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lifecycle: dict[str, str] = {}
        self._last_event_at: dict[str, int] = {}

    def note_event(self, team_name: str, event_type: str, at: int | None) -> None:
        """Record one monitor event for ``team_name``."""
        with self._lock:
            self._last_event_at[team_name] = at if isinstance(at, int) else now_ms()
            if event_type in TEAM_LIFECYCLE_EVENTS:
                self._lifecycle[team_name] = event_type

    def of(self, team_name: str) -> tuple[str | None, bool, int | None]:
        """Return ``(last_lifecycle_event, has_lifecycle_fact, last_event_at)``."""
        with self._lock:
            return (
                self._lifecycle.get(team_name),
                team_name in self._lifecycle,
                self._last_event_at.get(team_name),
            )


class TeamVizHandlers:
    """Owns the local service and both handlers; this object is the lifetime anchor."""

    def __init__(self) -> None:
        """Start listening before anything can render (contract §5.5 R1)."""
        self._context = ServiceContext(PACKAGE_ID)
        self._sessions = SessionMap()
        self._facts = _Facts()
        self._last_success: dict[str, int] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()
        self._service: lifecycle.LocalService | None = None
        try:
            self._service = lifecycle.start_service(self, self._context)
        except OSError as error:
            # R6: no listener means no render; the failure stays observable.
            self._context.note_bind_error(repr(error))
            LOGGER.error("team-collab-viz: local service unavailable: %r", error)

    # ------------------------------------------------------------- wiring

    def handlers(self) -> tuple[tuple[str, Any, dict[str, str]], ...]:
        """Return the ``(event_type, handler, match_pattern)`` triples to register."""
        return (
            ("ui.ready", self.on_ui_ready, {"target.slot_id": SLOT}),
            ("team.monitor_event", self.on_team_monitor_event, {}),
        )

    def mark_registered(self) -> None:
        """Record that the host accepted this activation's handlers."""
        self._context.handler_registered = True

    @property
    def port(self) -> int:
        """Port the listener is bound to (0 when unavailable)."""
        return self._context.port

    def close(self, reason: str = "closed") -> None:
        """Release the listener (used by tests and by an explicit teardown)."""
        if self._service is not None:
            self._service.close(reason)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _session_id(ops: EventHandlerOps | None) -> str:
        if ops is None:
            return ""
        context = getattr(ops, "session_context", None)
        session_id = getattr(context, "session_id", "") if context is not None else ""
        return session_id if isinstance(session_id, str) else ""

    @staticmethod
    def _scope(session_id: str) -> tuple[str, str]:
        if session_id:
            return "session", session_id
        return "global", GLOBAL_SCOPE_KEY

    def _lock_for(self, key: str) -> asyncio.Lock:
        with self._locks_guard:
            lock = self._refresh_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._refresh_locks[key] = lock
            return lock

    def _publish(
        self,
        kind: str,
        key: str,
        *,
        team: Any,
        members: Any,
        tasks: Any,
        dependencies: Any,
        source: str,
        stale: bool,
        reason: str | None,
        facts: tuple[str | None, bool, int | None],
        last_success_at: int | None,
    ) -> None:
        lifecycle_event, has_lifecycle_fact, last_event_at = facts
        snapshot = build_snapshot(
            scope_kind=kind,
            scope_key=key,
            team=team,
            members=members,
            tasks=tasks,
            dependencies=dependencies,
            session_map_source=source,
            last_event_at=last_event_at,
            last_success_at=last_success_at,
            stale=stale,
            stale_reason=reason,
            lifecycle_event=lifecycle_event,
            has_lifecycle_fact=has_lifecycle_fact,
        )
        revision = self._context.snapshots.publish(key, snapshot)
        LOGGER.info(
            "team-collab-viz: snapshot published scope=%s revision=%s stale=%s team=%s",
            kind,
            revision,
            stale,
            getattr(team, "team_name", None) if team is not None else None,
        )

    def _publish_degraded(self, kind: str, key: str, source: str, reason: str, team_name: str | None) -> None:
        """Keep the last good data and flag it stale; never fabricate a team.

        §5.2.1: a failed re-read must preserve the previous successful snapshot —
        ``team/members/tasks/graph/limits`` stay byte-identical and only
        ``stale``/``stale_reason``/``served_at`` move (§5.2.1 D1–D4). The cached
        payload is not rewritten either (D2), and the revision only bumps on the
        first ``false → true`` transition (D5). The §2.8 empty payload is used
        solely when this scope has never been read successfully.
        """
        facts = self._facts.of(team_name) if team_name else (None, False, None)
        revision = self._context.snapshots.mark_stale(key, reason)
        if revision is not None:
            LOGGER.info(
                "team-collab-viz: snapshot marked stale scope=%s revision=%s reason=%s",
                kind,
                revision,
                reason,
            )
            return
        self._publish(
            kind,
            key,
            team=None,
            members=(),
            tasks=(),
            dependencies={},
            source=source,
            stale=True,
            reason=reason,
            facts=facts,
            last_success_at=None,
        )

    # -------------------------------------------------------------- refresh

    async def _refresh(self, ops: EventHandlerOps, session_id: str) -> None:
        """Rebuild the snapshot for one scope; every failure degrades, never raises."""
        kind, key = self._scope(session_id)
        async with self._lock_for(key):
            try:
                await self._refresh_locked(ops, session_id, kind, key)
            except Exception as error:  # noqa: BLE001 - the tab must never blank out
                LOGGER.warning("team-collab-viz: refresh failed for %s: %r", key, error)
                previous = self._last_success.get(key)
                reason = STALE_OPS_ERROR if previous is not None else STALE_COLD_START
                self._publish_degraded(kind, key, SOURCE_NONE, reason, None)

    async def _refresh_locked(self, ops: EventHandlerOps, session_id: str, kind: str, key: str) -> None:
        team_name, source = await self._sessions.resolve(ops, session_id)
        if not team_name:
            self._publish(
                kind,
                key,
                team=None,
                members=(),
                tasks=(),
                dependencies={},
                source=SOURCE_NONE,
                stale=True,
                reason=STALE_NO_SESSION_MAP,
                facts=(None, False, None),
                last_success_at=self._last_success.get(key),
            )
            return
        if source == SOURCE_EVENT and session_id:
            await self._sessions.persist(ops, session_id, team_name)

        read_session = self._sessions.session_of(team_name) or session_id
        try:
            info = await ops.team.info(team_name=team_name)
            if info is None:
                raise LookupError(f"team '{team_name}' not readable")
            members = await ops.team.members(team_name=team_name)
            tasks = await self._read_tasks(ops, team_name, read_session)
            dependencies = await self._read_dependencies(ops, team_name, read_session, tasks)
        except Exception as error:  # noqa: BLE001 - keep the last good snapshot
            LOGGER.warning("team-collab-viz: ops read failed for team %s: %r", team_name, error)
            previous = self._last_success.get(key)
            reason = STALE_OPS_ERROR if previous is not None else STALE_COLD_START
            self._publish_degraded(kind, key, source, reason, team_name)
            return

        last_success_at = now_ms()
        self._last_success[key] = last_success_at
        self._publish(
            kind,
            key,
            team=info,
            members=members,
            tasks=tasks,
            dependencies=dependencies,
            source=source,
            stale=False,
            reason=None,
            facts=self._facts.of(team_name),
            last_success_at=last_success_at,
        )

    @staticmethod
    async def _read_tasks(ops: EventHandlerOps, team_name: str, session_id: str) -> list[Any]:
        if session_id:
            return list(await ops.team.tasks(team_name=team_name, session_id=session_id))
        return list(await ops.team.tasks(team_name=team_name))

    @staticmethod
    async def _read_dependencies(
        ops: EventHandlerOps,
        team_name: str,
        session_id: str,
        tasks: list[Any],
    ) -> dict[str, list[Any]]:
        """One ``task_dependencies`` call per task (there is no batch Ops)."""
        collected: dict[str, list[Any]] = {}
        for task in tasks:
            task_id = _attr(task, "task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            try:
                if session_id:
                    rows = await ops.team.task_dependencies(
                        task_id,
                        team_name=team_name,
                        session_id=session_id,
                    )
                else:
                    rows = await ops.team.task_dependencies(task_id, team_name=team_name)
            except Exception as error:  # noqa: BLE001 - one bad edge must not kill the read
                LOGGER.debug("team-collab-viz: dependencies failed for %s: %r", task_id, error)
                rows = []
            collected[task_id] = list(rows)
        return collected

    # --------------------------------------------------------------- render

    async def on_ui_ready(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        """Publish the two ``Iframe`` instances for this session."""
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.slot_id != SLOT:
            return await next_handler(event)
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or not target.session_id or ops is None:
            return await next_handler(event)
        if request.event_type != "ui.ready":
            return await next_handler(event)
        if self._service is None or self._service.closed or not self._context.port:
            return UiActionOutput(accepted=False, status="render_failed")

        session_id = self._session_id(ops) or target.session_id
        await self._refresh(ops, session_id)

        instances = [target.instance_key] if target.instance_key in TABS else list(TABS)
        for instance in instances:
            src = self._iframe_src(self._context.port, instance, session_id)
            try:
                output = await ops.ui.render(
                    UiRenderInput(
                        target=RenderTarget(target.session_id, SLOT, PACKAGE_ID, instance),
                        root=RenderNode("Iframe", {"src": src, "title": TABS[instance]}),
                    )
                )
            except Exception as error:  # noqa: BLE001 - report instead of breaking the chain
                LOGGER.error("team-collab-viz: render failed for %s: %r", instance, error)
                return UiActionOutput(accepted=False, status="render_failed")
            if output is None or getattr(output, "status", None) != "delivered":
                LOGGER.warning(
                    "team-collab-viz: render rejected instance=%s status=%s",
                    instance,
                    getattr(output, "status", None),
                )
                return UiActionOutput(accepted=False, status="render_failed")
        return UiActionOutput(accepted=True, status="rendered")

    @staticmethod
    def _iframe_src(port: int, instance: str, session_id: str) -> str:
        page = TAB_PAGES[instance]
        return f"http://{BIND_ADDRESS}:{int(port)}/web/{page}?sid={quote(session_id, safe='')}"

    # --------------------------------------------------------------- events

    async def on_team_monitor_event(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> Any:
        """Learn the session→team pair, then rebuild the snapshot in full."""
        try:
            request = event.event_input
            monitor = _attr(request, "monitor_event")
            team_name = _attr(request, "team_name") or _attr(monitor, "team_name")
            event_type = _enum_value(_attr(monitor, "event_type"))
            at = _attr(monitor, "timestamp")
            if isinstance(team_name, str) and team_name:
                self._facts.note_event(team_name, event_type, at)
                if ops is not None:
                    self._context.note_event(at)
                    session_id = self._session_id(ops)
                    if session_id and self._sessions.learn(session_id, team_name):
                        await self._sessions.persist(ops, session_id, team_name)
                    await self._refresh(ops, session_id)
        except Exception as error:  # noqa: BLE001 - never pollute the team chain
            LOGGER.warning("team-collab-viz: monitor event handling failed: %r", error)
        return await next_handler(event)


class TeamCollabVizExtension:
    """Host-facing factory object; it holds the handler (the real lifetime anchor)."""

    def __init__(self) -> None:
        """Create the handler object, which owns the local listener."""
        self._handlers = TeamVizHandlers()

    def register(self, registrar: Any) -> None:
        """Hand the bound methods to the host; they keep the handler alive."""
        for event_type, handler, match_pattern in self._handlers.handlers():
            registrar.register_event_handler(event_type, handler, match_pattern, "user")
        self._handlers.mark_registered()


def create_extension() -> TeamCollabVizExtension:
    """Factory called by the host on (re)load."""
    return TeamCollabVizExtension()
