"""Use host-bound sessions and native rendering without changing agent execution."""
import asyncio
from collections import OrderedDict
from datetime import datetime
from time import perf_counter
from .loop import begin, finish

from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.context_usage import ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiActionInput, UiActionOutput, UiRenderInput
from jiuwenswarm.extensions_v2.webui.resources import complete_before_cancel
from .state import SessionState
from .views import PACKAGE, BAR, PANEL, panel_view, summary_view
from .dashboard import write_dashboard


class ContextObserver:
    def __init__(self, store=None):
        self.sessions = OrderedDict()
        self.lock = asyncio.Lock()
        self.store = store

    def register(self, registrar):
        for name in ('turn.step', 'tool.call'):
            registrar.register_event_handler(name, self.on_loop, {}, 'user')
        registrar.register_event_handler('context.usage.updated', self.on_usage, {}, 'user')
        for event in ('ui.ready', 'ui.click'):
            for slot in (BAR, PANEL):
                registrar.register_event_handler(event, self.on_ui, {'target.slot_id': slot}, 'user')

    async def state(self, sid):
        if sid not in self.sessions:
            self.sessions[sid] = await asyncio.to_thread(self.store.load, sid) if self.store else SessionState()
        state = self.sessions[sid]
        self.sessions.move_to_end(sid)
        while len(self.sessions) > 64:
            self.sessions.popitem(last=False)
        return state

    async def persist(self, sid, state):
        if self.store is not None:
            try:
                await complete_before_cancel(asyncio.to_thread(self.store.save, sid, state))
            except (OSError, ValueError, TypeError):
                state.history_error = True

    async def read(self, ops, state):
        state.error = False
        results = await asyncio.gather(ops.session.usage(breakdown='summary'), ops.session.model(),
                                       ops.session.turns(), return_exceptions=True)
        for key, result in zip(('usage', 'model', 'turns'), results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                state.error = True
            elif key != 'model' or result:
                setattr(state, key, result)
        if not state.error:
            state.updated = datetime.now().astimezone().strftime('%H:%M:%S')
        return not isinstance(results[0], BaseException)

    async def render(self, ops, sid, state):
        result = await ops.ui.render(UiRenderInput(RenderTarget(sid, BAR, PACKAGE, 'summary'), summary_view(state)))
        if state.panel_open:
            result = await self.render_panel(ops, sid, state)
        return result

    async def render_panel(self, ops, sid, state):
        previous = state.resources
        resources = await ops.ui.create_resource_dir(previous=previous.resource_id if previous else '')
        try:
            async with resources:
                await complete_before_cancel(asyncio.to_thread(write_dashboard, resources.path, state))
            source = await ops.ui.asset_url('index.html', resource_id=resources.resource_id)
            output = await ops.ui.render(UiRenderInput(
                RenderTarget(sid, PANEL, PACKAGE, 'context'),
                RenderNode('Iframe', {'src': source, 'title': '上下文驾驶舱', 'minWidth': 300})))
        except BaseException:
            await ops.ui.release_resources(resources.resource_id)
            raise
        if output is not None and output.status == 'delivered':
            state.resources = resources
            if previous is not None:
                await ops.ui.release_resources(previous.resource_id)
        else:
            await ops.ui.release_resources(resources.resource_id)
        return output

    async def read_messages(self, ops, state):
        try:
            messages = await ops.session.messages()
            state.messages = [{'role': str(item.get('role', 'unknown'))[:40],
                               'text': str(item.get('text', ''))[:599] + ('…' if len(str(item.get('text', ''))) > 599 else '')}
                              for item in messages[-12:]]
            state.messages_loaded = True
        except Exception:
            state.error = True

    async def paint_loop(self, ops, sid, state):
        if state.mounted and state.panel_open:
            try:
                await asyncio.wait_for(self.render_panel(ops, sid, state), timeout=2)
            except Exception:
                state.error = True

    async def on_loop(self, event, ops, next_handler):
        context = getattr(ops, 'session_context', None)
        if context is None or not context.session_id:
            return await next_handler(event)
        sid = context.session_id
        row = None
        state = None
        try:
            row = begin(event)
            if row is not None:
                async with self.lock:
                    state = await self.state(sid)
                    state.trace.append(row)
                    state.trace = state.trace[-60:]
                    await self.paint_loop(ops, sid, state)
        except asyncio.CancelledError:
            if row is not None:
                finish(row, None, 0, 'cancelled')
            raise
        except Exception:
            row = None
        started = perf_counter()
        try:
            output = await next_handler(event)
        except BaseException as error:
            if row is not None:
                finish(row, None, perf_counter()-started,
                       'cancelled' if isinstance(error, asyncio.CancelledError) else 'error')
                if not isinstance(error, asyncio.CancelledError):
                    try:
                        async with self.lock:
                            await self.paint_loop(ops, sid, state)
                    except Exception:
                        pass
            raise
        if row is not None:
            try:
                finish(row, output, perf_counter()-started, 'done')
                async with self.lock:
                    await self.paint_loop(ops, sid, state)
            except Exception:
                state.error = True
        return output

    async def on_usage(self, event, ops, next_handler):
        update = event.event_input
        if (not isinstance(update, ContextUsageUpdate) or ops is None or ops.session_context is None
                or ops.session_context.session_id != update.session_id):
            return await next_handler(event)
        async with self.lock:
            state = await self.state(update.session_id)
            usage_ok = await self.read(ops, state)
            if isinstance(update.model, str) and update.model.strip():
                state.model = update.model
            state.observe(update, state.usage if usage_ok else {'context': {}, 'rateLimits': []})
            await self.persist(update.session_id, state)
            if state.mounted:
                if state.panel_open:
                    await self.read_messages(ops, state)
                await self.render(ops, update.session_id, state)
        return await next_handler(event)

    async def on_ui(self, event, ops, next_handler):
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.package_id not in ('', PACKAGE):
            return await next_handler(event)
        target = request.target
        expected = 'summary' if target.slot_id == BAR else 'context'
        if (ops is None or ops.session_context is None or not target.session_id
                or target.session_id != ops.session_context.session_id or target.slot_id not in (BAR, PANEL)
                or target.instance_key not in ('', expected)):
            return UiActionOutput(False, 'invalid')
        action = request.payload.get('action_id')
        if request.event_type != 'ui.ready' and (request.event_type != 'ui.click' or action not in ('open', 'refresh', 'messages')):
            return UiActionOutput(False, 'invalid')
        async with self.lock:
            state = await self.state(target.session_id)
            if request.event_type == 'ui.ready' and target.slot_id == PANEL and not state.panel_open:
                return UiActionOutput(True, 'not_open')
            state.mounted = True
            if target.slot_id == PANEL or action == 'open':
                state.panel_open = True
            await self.read(ops, state)
            if action == 'messages':
                state.expanded = not state.expanded
            if state.panel_open:
                await self.read_messages(ops, state)
            # Keep the clicked source instance alive until its openPanel receipt.
            if action == 'open' or target.slot_id == PANEL:
                output = await self.render_panel(ops, target.session_id, state)
            else:
                output = await self.render(ops, target.session_id, state)
        delivered = output is not None and output.status == 'delivered'
        return UiActionOutput(delivered, output.status if output else 'unavailable')


def create_extension():
    from jiuwenswarm.common.utils import get_user_workspace_dir
    from .storage import HistoryStore
    return ContextObserver(HistoryStore(get_user_workspace_dir() / 'extension_data' / 'context-observer'))
