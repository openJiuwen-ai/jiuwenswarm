"""Public Python objects backed by one protocol-5 session.

The public Computer selects this controller for its whole session. It never
calls v3 readApp/findElement/input methods, and never retries through them.
"""
import os
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from .._identity import FrozenRecord, WeakIdentityMap
from ..errors import ComputerUseError
from ..execution.native import Binding, HostPolicy, Intent
from ..execution.query import resolve_path
from ..execution.transport import ConnectionPort
from ..native_semantics import RELATIONS
from ..observation.app import (
    AppObservation,
    MenuAppObservation,
    NativeAppReader,
    RunningApplication,
    WindowSelectionRequired,
    WorkingWindow,
    window_binding,
    window_summary,
)
from ..observation.desktop_reader import NativeDesktopReader
from ..observation.formatting import json_string
from ..observation.numbering import number_snapshots
from ..observation.pages import ContentPages
from ..observation.reader import captured_children, captured_field
from ..protocol.captures import capture_metadata
from ..protocol.primitives import finite, frozen, reference
from ..protocol.session import desktop_mode
from .actions import action_completed
from .computer import App, computer_for_controller, observation_state
from .connection import Connection
from .desktop_store import DesktopStore
from .diagnostics import local_failure
from .execution import operation
from .images import image_delivery_epoch, image_delivery_required, register_image
from .image_coordinates import ImageCoordinates
from .observation_data import retain_observation_data
from .observations import observation_completed, observations_invalidated, pending_observations_discarded
from .result_presentation import action_result, captured_text
from .window_context import keyboard_context


@dataclass
class AppState:
    selector: str
    running: RunningApplication | None = None
    selection: WorkingWindow | None = None
    observation: AppObservation | None = None
    query_target: Binding | None = None
    query_scope_kind: str | None = None
    directory: dict | None = None
    refs: OrderedDict = field(default_factory=OrderedDict)
    windows: dict = field(default_factory=dict)
    delivered: tuple | None = None
    delivered_coordinates: ImageCoordinates | None = None
    capture_expired: bool = False

    @property
    def application(self):
        return self.running.application if self.running is not None else None

    @property
    def name(self):
        return self.running.name if self.running is not None else ''


class NativeController:

    def __init__(self, port=None):
        self._states = WeakIdentityMap()
        self._bound_apps = weakref.WeakValueDictionary()
        self._window_catalogues = OrderedDict()
        self.desktop = DesktopStore()
        self.transport = port.connection if port is not None else Connection(self.invalidate)
        self.port = port or ConnectionPort(
            self.transport, HostPolicy(desktop_mode(),
                                       os.environ.get('COMPUTER_USE_FOREGROUND_ASSIST') == '1'))
        self.reader = NativeAppReader(self.port)
        self.desktop_reader = NativeDesktopReader(self.port)
        self.engine = self.reader.engine
        self.pages = ContentPages()

    def register(self, owner, scope):
        self._states.set(owner, scope.get('_nativeState') or AppState(scope['selector']))

    def state(self, owner):
        state = self._states.get(owner)
        if state is None:
            raise local_failure('unknown App', 'obtain an App from this Computer', 'stale_target')
        return state

    def scope(self, owner):
        state = self.state(owner)
        # A subsequent computer.app(name) is a new selection, never permission
        # to silently retarget an already-bound App after its document closes.
        return dict(
            selector=state.selection.root if state.selection is not None else state.application or state.selector)

    def invalidate(self):
        self.desktop.reset()
        self._bound_apps.clear()
        self._window_catalogues.clear()
        observations_invalidated(self)
        # Existing App/Element bindings keep their old session and must fail;
        # clearing their selection would silently turn them into fresh lookups.

    def validate_element(self, target):
        if not isinstance(target, Binding) or target.session != self.transport.session:
            raise local_failure('element belongs to an expired driver session',
                                'observe a new App and obtain a new element', 'stale_target')

    validate_snapshot = validate_element

    def window(self, owner, ref):
        state = self.state(owner)
        if not reference(ref):
            raise local_failure('window is not in this App\'s observed catalogue',
                'read the App and select a decimal reference from Windows', 'stale_target')
        if state.directory is not None and state.directory['session'] != self.transport.session:
            raise local_failure('window catalogue belongs to an expired driver session',
                'observe a new App', 'stale_target')
        # Window aliases are already unique across this Computer's reader. A
        # popup observed through another fixed view of this exact application
        # is explicit selection evidence, not an invitation to rebind owner.
        # A fresh wrapper may explicitly select a known window through the exact
        # running-application identity observed by this Computer. Human names,
        # unknown identities and selectors from another session do not resolve
        # through this cache, and the new wrapper itself stays unbound.
        cached = self._window_catalogues.get((self.transport.session, state.application or state.selector))
        if cached is not None:
            running, directory, windows = cached
        elif state.directory is not None:
            running, directory, windows = state.running, state.directory, state.windows
        else:
            raise local_failure('window is not in this App\'s observed catalogue',
                'read the App or use its exact already-observed running application identity', 'stale_target')
        if ref not in windows:
            raise local_failure('window is not in this application\'s observed catalogue',
                'read the intended App and select a decimal reference from Windows', 'stale_target')
        selected = self.reader.selection_from_directory(running.application, running.name, directory, windows[ref])
        if state.selection == selected:
            return owner
        app = self._bound_apps.get(selected.root)
        if app is None:
            selected_state = AppState(state.selector, selection=selected,
                running=RunningApplication(selected.application, selected.name, selected.root.session),
                directory=directory, windows=dict(windows))
            app = App(self, dict(selector=state.selector, _nativeState=selected_state), _internal=True)
            self._bound_apps[selected.root] = app
        return app

    working_window = window

    async def call(self, method, params, owner=None, options=None):
        if method != 'listApps' or params:
            self.unavailable(method)

        async def read():
            directory = await self.reader.directory.applications()
            return tuple(
                FrozenRecord({key: item[key]
                              for key in ('id', 'displayName', 'isRunning')}) for item in directory['applications'])

        return await operation(read)

    @staticmethod
    def unavailable(method):
        raise local_failure(method + ' is not connected to protocol 5 yet',
                            'use the supported App/Element routes in this session; no legacy fallback was attempted',
                            'mode_unsupported')

    async def read_desktop(self, full=False):

        async def read():
            try:
                observed = await self.desktop_reader.read()
                self.desktop.record(observed)
                state = observation_state(
                    dict(text=observed['text'], details=observed['text'], screenshot=observed['screenshot'],
                         overview=None))
                observation_completed(
                    dict(source=self, scope=observed['scope'], observed=observed['observed'], view='desktop',
                         state=state, refs=frozen({}), full=full))
                return state
            except BaseException:
                self.desktop.reset()
                pending_observations_discarded(self)
                raise

        return await operation(read)

    async def read(self, owner, full=False, menu_path=None, subtree=None):
        if subtree is not None:
            raise ValueError('Use readElement for an already-bound native element')

        async def read():
            state = self.state(owner)

            def remember_scope(scope):
                if isinstance(scope, WorkingWindow):
                    state.selection = scope
                    scope = RunningApplication(scope.application, scope.name, scope.root.session)
                state.running = scope

            try:
                if state.running is not None and state.running.session != self.transport.session:
                    raise local_failure('application belongs to an expired driver session',
                                        'obtain a new App from this Computer', 'stale_target')
                if state.query_target is not None:
                    self.validate_element(state.query_target)
                observed = await self.reader.read(state.selection or state.running or state.selector,
                                                  menu_path=menu_path, on_scope=remember_scope)
            except WindowSelectionRequired as choices:
                return self._choices(owner, state, choices, full)
            except BaseException:
                state.observation = None
                pending_observations_discarded(self)
                raise
            # Decimal aliases describe this result only. Existing Element
            # objects own immutable native snapshots and are not rebound.
            state.refs.clear()
            if isinstance(observed, MenuAppObservation):
                state.selection = observed.selection
                self._catalogue(state, observed.directory, observed.windows)
                state.query_target = observed.menu.target
                state.query_scope_kind = observed.menu.scope_kind
                for part in observed.menu.parts:
                    self._remember(state, part.target, part.tree)
                # Reading a menu changes the query view, never the working
                # window's keyboard target or the last delivered image.
                first = observed.menu.parts[0]
                return self._publish(owner, state, observed.text, first.tree, first.target, full=True, view='menu',
                                     extra_trees=observed.menu.observations[1:])
            state.selection, state.observation = observed.selection, observed
            state.query_target = observed.target
            state.query_scope_kind = 'window'
            self._catalogue(state, observed.directory, observed.windows)
            state.delivered, state.capture_expired = None, False
            state.delivered_coordinates = None
            if observed.tree is not None:
                self._remember(state, observed.target, observed.tree)
            if observed.menus is not None:
                for part in observed.menus.parts:
                    self._remember(state, part.target, part.tree)
            if observed.image is not None:
                self._image(state, observed)
            return self._publish(owner, state, observed.text, observed.tree, observed.target,
                                 observed.image.image if observed.image else None, full=full,
                                 extra_trees=observed.menus.observations if observed.menus else (),
                                 overview=observed.overview)

        return await operation(read)

    def _choices(self, owner, state, choices, full):
        state.running = RunningApplication(choices.application, choices.display_name, choices.directory['session'])
        if choices.selection is not None:
            state.selection = choices.selection
        self._catalogue(state, choices.directory, self.reader.window_aliases(choices.directory))
        state.observation, state.query_target, state.delivered = None, None, None
        state.delivered_coordinates = None
        state.refs.clear()
        lines = [
            'App: ' + json_string(state.name) + '.', 'Select a window with app.window(ref).',
            ('Tree: unavailable; the fixed working window has no unique current view.'
             if state.selection else 'Tree: unavailable; a working window has not been selected.'),
            'Windows: ' + window_summary(state.directory, state.windows, selection=state.selection)
        ]
        lines.append(choices.detail)
        lines.extend('Directory partial: ' + reason for reason in state.directory['stopReasons'])
        if choices.launch is not None:
            lines.append('Launch: ' + choices.launch['detail'])
        if choices.overview is not None:
            lines.append(choices.overview.text)
        result = observation_state(
            dict(text='\n'.join(lines), details='\n'.join(lines), screenshot=None,
                 overview=self._overview_image(choices.overview)))
        retain_observation_data(result, [], [], state.windows,
                                state.selection.root.root_window if state.selection else None)
        observation_completed(
            dict(source=self, owner=owner, scope='catalogue:' + state.application, view='catalogue', state=result,
                 refs=frozen({}), full=True, observed=state.directory['observed']))
        return result

    def _catalogue(self, state, directory, windows):
        state.directory, state.windows = directory, dict(windows)
        key = (directory['session'], state.application)
        self._window_catalogues.pop(key, None)
        self._window_catalogues[key] = (state.running, directory, dict(windows))
        while len(self._window_catalogues) > 32:
            self._window_catalogues.popitem(last=False)

    def _remember(self, state, target, tree):
        if 'nativeRelationPages' in self.transport.capabilities:
            self.pages.remember(target, tree)
        state.refs.update(self._snapshots(target, tree))
        while len(state.refs) > 8192:
            state.refs.popitem(last=False)

    @staticmethod
    def _snapshots(target, tree):
        native_nodes = {node['node']: node for node in tree.native['nodes']}
        child_relations = {
            r['source']: r
            for r in tree.native['relations'] if RELATIONS[tree.native['platform']].get(r['property']) == 'children'
        }
        return {
            ref:
            dict(binding=replace(target, node=node, capture=None), scope_kind=tree.scope_kind,
                 source=dict(ref=ref, observed=tree.native['observed']),
                 text=captured_field(tree, ref, tree.value_fields.get(ref), nodes=native_nodes),
                 children=captured_children(tree, ref, relations=child_relations))
            for ref, node in tree.refs.items()
        }

    def element_snapshot(self, owner, ref):
        state = self.state(owner)
        if not reference(ref) or ref not in state.refs:
            raise local_failure('element reference was not observed in this App',
                                'read the intended App and use one of its element numbers', 'stale_target')
        selected = state.refs[ref]
        self.validate_element(selected['binding'])
        return selected

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    def _publish(self, owner, state, text, tree, target, image=None, *, full=False, view='window', extra_trees=(),
                 overview=None):
        observations = (((target, tree), ) if tree is not None else ()) + extra_trees
        trees = tuple(snapshot for _, snapshot in observations)
        rendered = [snapshot.render(compact=True) for snapshot in trees]
        if 'nativeRelationPages' in self.transport.capabilities:
            for (binding, snapshot), presentation in zip(observations, rendered):
                text += self.pages.hints(binding, snapshot, compact=True,
                                         inline_children=presentation['inlineChildRefs'])
        # Details are from these exact captured facts, not a fresh read and not
        # a retargetable native handle. Keeping the returned Observation keeps
        # its original details even after further reads or session teardown.
        details = [text]
        for snapshot, presentation in zip(trees, rendered):
            details.append('Captured native details ' + json_string(snapshot.native['observed']['id']) + ':\n' +
                           presentation['details'])
        if trees:
            text += '\nDetails: observation.details contains original fields, actions and relations from this read.'
        result = observation_state(
            dict(text=text, details='\n\n'.join(details), screenshot=image, overview=self._overview_image(overview)))
        refs = {ref: node for snapshot in trees for ref, node in snapshot.refs.items()}
        retain_observation_data(result, [root for snapshot in trees for root in snapshot.roots], refs, state.windows,
                                state.selection.root.root_window if state.selection else None)
        event = dict(source=self, owner=owner, scope=target.surface, instance=target.root_window,
                     selector=state.selector, window=self._window_ref(state, target), view=view, state=result,
                     refs=frozen(refs), full=full)
        if tree is not None:
            event['observed'] = tree.native['observed']
        observation_completed(event)
        return result

    @staticmethod
    def _overview_image(overview):
        if overview is None or overview.image is None:
            return None
        # No delivered callback: showing a contact sheet never authorizes
        # coordinates for any tile or the working window's full-size image.
        register_image(overview.image, dict(kind='overview', context=overview.text))
        return overview.image

    @staticmethod
    def _window_ref(state, target):
        native = next((item['window']
                       for item in (state.directory or {}).get('surfaces', []) if item['surface'] == target.surface),
                      None)
        return next((ref for ref, window in state.windows.items() if window == native), None)

    def _image(self, state, observation):
        image = observation.image.image
        weak_state, weak_image = weakref.ref(state), weakref.ref(image)
        weak_controller = weakref.ref(self)
        session, capture = observation.target.session, observation.image.target.capture

        def current():
            owner, pixels, controller = weak_state(), weak_image(), weak_controller()
            return (owner is not None and pixels is not None and controller is not None and not owner.capture_expired
                    and owner.observation is not None and owner.observation.image is not None
                    and owner.observation.image.image is pixels and session == controller.transport.session)

        def delivered(coordinates=None):
            if current():
                weak_state().delivered = (capture, image_delivery_epoch())
                weak_state().delivered_coordinates = coordinates

        selected = next(item for item in observation.source_directory['surfaces']
                        if item['surface'] == observation.target.surface)
        register_image(
            image,
            dict(kind='window', selector=state.selector,
                 context='Window: ' + json_string(selected['title']) + ', App: ' + json_string(state.name) + '.',
                 observed=observation.image.observed, window=self._window_ref(state, observation.target),
                 current=current, delivered=delivered))

    def app_ref(self, owner, coordinates=False):
        state = self.state(owner)
        observed = state.observation
        if observed is None:
            raise local_failure('this App has no current window observation', 'call App.read first', 'stale_target')
        self.validate_element(observed.target)
        if not coordinates:
            return window_binding(observed.target)
        if observed.image is None or state.capture_expired:
            raise local_failure('this App has no current window image', 'read the App and inspect capture availability',
                                'snapshot_expired')
        if image_delivery_required() and state.delivered != (observed.image.target.capture, image_delivery_epoch()):
            raise local_failure('current window image has not been returned to the model',
                                'return the window image, then choose coordinates in the next call',
                                'input_unavailable')
        return observed.image.target

    def keyboard_target(self, owner):
        return dict(appRef=self.app_ref(owner))

    def check_coordinates(self, owner, x, y, operation_name='click'):
        self.app_ref(owner, True)
        coordinates = self.state(owner).delivered_coordinates
        if coordinates is not None:
            x, y = coordinates.source_point((x, y))
        dimensions = capture_metadata(self.state(owner).observation.image.image)
        outside_observed_image = not finite(x) or not finite(
            y) or not 0 <= x < dimensions['width'] or not 0 <= y < dimensions['height']
        if outside_observed_image:
            raise local_failure('coordinates are outside this App\'s observed PNG',
                                'use window-image pixels for ' + operation_name)

    async def act(self, method, params, owner=None):
        if owner is None:
            ref = self.desktop.target(coordinates=method in ('click', 'scroll', 'drag'))
            if params.get('desktopRef') != ref:
                raise local_failure('desktop observation changed', 'read the desktop again', 'stale_target', 'Desktop')
            observed = self.desktop.observed(ref)
            target = Binding(observed['binding']['session'], None, None, None, capture=ref)
            parameter = 'desktopRef'
        else:
            target, parameter = params['appRef'], 'appRef'
        intent = dict(typeText='insert', pressKey='key').get(method, method)
        parameters = {key: value for key, value in params.items() if key != parameter}
        if method in ('click', 'scroll', 'drag'):
            if owner is None:
                parameters = self.desktop.map_parameters(method, parameters)
            else:
                self.app_ref(owner, True)
                coordinates = self.state(owner).delivered_coordinates
                if coordinates is not None:
                    parameters = coordinates.source_parameters(method, parameters)
        return await self._execute(owner, Intent(intent, parameters), target,
                                   app_keyboard=owner is not None and method in ('typeText', 'paste', 'pressKey'))

    async def act_element(self, owner, snapshot, query, intent, parameters):
        if query is None:
            target, path = snapshot['binding'], None
        else:
            target, path = self._query_root(owner), [dict(segment) for segment in query['path']]
        return await self._execute(owner, Intent(intent, parameters), target, path=path,
                                   query_timeout_ms=query['timeoutMs'] if query is not None else 0)

    async def _execute(self, owner, intent, target, *, path=None, query_timeout_ms=0, app_keyboard=False):

        async def perform():
            self.validate_element(target)
            try:
                state = self.state(owner) if app_keyboard else None
                if state is not None and state.selection is not None and target != state.selection.root:
                    # Reconcile a vanished modal only before sending this App
                    # intent. Discovery and execution share one total deadline;
                    # a failure after dispatch is never retried on its parent.
                    with self.port.calls.claim(30000) as remaining:
                        current = await keyboard_context(self.reader, state.selection, state.observation, target,
                                                         remaining)
                        receipt = await self.engine.execute_in_call(intent, current, remaining)
                else:
                    receipt = await self.engine.execute(intent, target, path=path, query_timeout_ms=query_timeout_ms)
            except BaseException as error:
                if owner is None:
                    self.desktop.reset()
                elif isinstance(error,
                                ComputerUseError) and error.code == 'snapshot_expired' and target.capture is not None:
                    state = self.state(owner)
                    if state.observation and state.observation.image and state.observation.image.target == target:
                        state.capture_expired = True
                pending_observations_discarded(self)
                raise
            action_completed(receipt)
            return action_result(receipt)

        return await operation(perform)

    def _query_root(self, owner):
        target = self.state(owner).query_target
        if target is None:
            raise local_failure('this App has no observed query root', 'call App.read first', 'stale_target')
        self.validate_element(target)
        if target.node is None:
            raise local_failure('this window has no accessible query root', 'use its observed window image',
                                'observation_incomplete')
        return target

    async def find(self, owner, path, timeout_ms, enabled):
        target = self._query_root(owner)
        # A read-only lookup can finish before a subsequent read. Writes use
        # actElement -> Engine(path), keeping lookup and dispatch in one lease.
        async with self.port.calls.open(window_binding(target), 30000) as read:
            selected, _ = await resolve_path(self.port, read.token, target, read.remaining,
                                             [dict(segment) for segment in path], timeout_ms=timeout_ms)
            tree = await self.reader.reader.collect(read, node=selected.node,
                                                    scope_kind=self.state(owner).query_scope_kind)
        # A private query is not a displayed observation and must not replace
        # the numbers the caller just saw in App.read/Element.read.
        ref = next(ref for ref, node in tree.refs.items() if node == selected.node)
        snapshot = self._snapshots(selected, tree)[ref]
        snapshot['source']['ref'] = None  # No public display alias was assigned.
        return snapshot

    async def read_element(self, owner, snapshot, query, offset, previous=None, *, cursor=None):
        selected = previous or snapshot
        if not offset and cursor is None and query is not None:
            selected = await self.find(owner, query['path'], query['timeoutMs'], False)
        if selected is None:
            raise local_failure('element continuation needs an observed first page',
                                'read this element before using a continuation offset', 'snapshot_expired')
        target = selected['binding']
        self.validate_element(target)
        if cursor is not None:
            if 'nativeRelationPages' not in self.transport.capabilities:
                raise local_failure('this driver does not support content continuations',
                                    'use supported element reads or update the driver', 'mode_unsupported')
            options = dict(relation_page=self.pages.resolve(cursor, target))
        else:
            continuation = selected.get('children')
            if offset and (continuation is None or continuation.offset != offset):
                raise local_failure('offset does not match this element\'s observed continuation',
                                    'read the element again and use its reported continuation', 'snapshot_expired')
            options = dict(children_page=dict(cursor=continuation.cursor if offset else None, limit=128))
        # All page nodes and their subtrees are acquired in one native batch;
        # the server validates the original relation prefix before continuing.
        tree = await self.reader.reader.read(target, **options, scope_kind=selected.get('scope_kind'))
        tree, = number_snapshots((tree, ))
        state = self.state(owner)
        state.refs.clear()
        self._remember(state, target, tree)
        title = next((item['title']
                      for item in (state.directory or {}).get('surfaces', []) if item['surface'] == target.surface),
                     None)
        context = 'Window: ' + json_string(title) if title is not None else 'Application UI root'
        text = context + ', App: ' + json_string(state.name) + \
            '.\nTree: full (element subtree)\n' + tree.render(compact=True)['text']
        result = self._publish(owner, state, text, tree, target, full=True, view='subtree')
        ref = next(ref for ref, node in tree.refs.items() if node == target.node)
        return result, self.element_snapshot(owner, ref)

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def readText(self, owner, snapshot, options):
        self.validate_snapshot(snapshot['binding'])
        source = snapshot.get('text')
        if source is None:
            raise local_failure('this element has no complete captured text value',
                                'read the element and inspect its available text fields', 'observation_incomplete')
        try:
            page = await self.reader.reader.read_captured_text(source, offset=options.get('offset', 0),
                                                               length=options.get('length', 4096))
        except ValueError as error:
            if source.value is None:
                raise  # A driver contract failure is not invalid user input.
            raise local_failure(str(error), 'continue with the next offset returned by readText') from error
        return captured_text(page, snapshot['source'])


def native_computer(port=None):
    """Host migration entry; model-facing classes and method signatures do not change."""
    return computer_for_controller(NativeController(port))
