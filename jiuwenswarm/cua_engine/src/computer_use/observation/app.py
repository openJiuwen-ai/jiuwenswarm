"""Python coordination of discovery, fixed-window observation and projection.

Used by the public v5 NativeController for a fixed working-window session.
Native operations collect facts; they neither select a new working window nor
combine an old tree with a newly discovered dialog on behalf of Python.
"""
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from ..errors import ComputerUseError
from ..execution.engine import Engine
from ..execution.native import ApplicationBinding, Binding, Dispatch, Intent
from ..protocol.captures import capture_metadata
from .capture import NativeImages, WindowImage
from .components import ObservationGap, collect_component, observation_gap
from .directory import NativeDirectory, select_application, select_surface, selected_window
from .formatting import json_string
from .loading import busy_nodes, initialization_requested, window_content_unexposed
from .menus import MenuRead, NativeMenus
from .native_facts import DisplayReferences, SemanticSnapshot, reference_set
from .numbering import number_snapshots
from .overview import NativeOverview, WindowOverview
from .reader import NativeReader


def window_binding(binding):
    return replace(binding, node=None, capture=None)


@dataclass(frozen=True)
class RunningApplication:
    """An observed instance, before its working window has been selected."""
    application: str
    name: str
    session: str


@dataclass(frozen=True)
class SelectedApplicationWindow(RunningApplication):
    """An exact directory choice, retained even before its first read succeeds."""
    window: str


@dataclass(frozen=True)
class StartingApplication(RunningApplication):
    """A dispatched launch before a declared running application is selected.

    The direct process receipt is retained separately from the eventual window
    owner. Reading this scope can only observe; it never repeats the launch.
    """
    installation: str
    launch: Mapping


@dataclass(frozen=True)
class WorkingWindow:
    application: str
    name: str
    root: Binding


@dataclass(frozen=True)
class AppObservation:
    selection: WorkingWindow
    target: Binding
    applications: dict | None
    source_directory: dict
    directory: dict
    tree: SemanticSnapshot | None
    image: WindowImage | None
    text: str
    refs: Mapping[str, Binding]
    windows: Mapping[str, str]
    gaps: tuple[ObservationGap, ...]
    scope_confirmed: bool
    launch: Mapping | None = None
    menus: MenuRead | None = None
    overview: WindowOverview | None = None


@dataclass(frozen=True)
class MenuAppObservation:
    application: str
    name: str
    selection: WorkingWindow | None
    menu: MenuRead
    directory: dict
    windows: Mapping[str, str]
    text: str
    launch: Mapping | None = None


class WindowSelectionRequired(ComputerUseError):
    """Preserve the catalogue and any fixed root when a view needs a choice."""

    def __init__(self, error, application, name, directory, launch=None, overview=None, selection=None):
        super().__init__({key: getattr(error, key) for key in ('code', 'effect', 'target', 'detail', 'next')})
        self.application, self.display_name, self.directory, self.launch = application, name, directory, launch
        self.overview = overview
        self.selection = selection


def failure(code, detail):
    return ComputerUseError(
        dict(code=code, effect='none', target='working window', detail=detail,
             next='read the window directory again; explicitly select a working window if needed'))


def window_summary(directory, windows, *, selection=None, target=None, scope_confirmed=False):
    aliases = {window: alias for alias, window in windows.items()}
    entries = []
    for item in directory['surfaces']:
        flags = []
        if selection is not None and item['window'] == selection.root.root_window:
            flags.append('root')
        if target is not None and item['surface'] == target.surface:
            flags.append('current' if scope_confirmed else 'observed')
        if item['modal'] is True:
            flags.append('modal')
        if item['parentState'] == 'known':
            flags.append('parent=' + aliases.get(item['parent'], json_string(item['parent'])))
        elif item['parentState'] == 'unknown':
            flags.append('parent unknown')
        entries.append(aliases[item['window']] + ' ' + json_string(item['title']) +
                       (' (' + ', '.join(flags) + ')' if flags else ''))
    return '; '.join(entries)


class NativeAppReader:

    def __init__(self, port):
        self.port = port
        self.directory = NativeDirectory(port)
        self.reader = NativeReader(port)
        self.images = NativeImages(port)
        self.overviews = NativeOverview(port)
        self.engine = Engine(port)
        self.menus = NativeMenus(self.directory, self.reader)
        self._window_references = None

    def window_aliases(self, directory):
        if self._window_references is None or self._window_references.session != directory['session']:
            self._window_references = DisplayReferences(directory['session'])
        return {self._window_references.reference(item['window']): item['window'] for item in directory['surfaces']}

    async def read(self, selector, *, window=None, fields=None, max_nodes=1000, max_depth=None, max_dimension=2048,
                   timeout_ms=30000, allow_launch=True, menu_path=None, on_scope=None):
        """Observe a selected app, launching an observed installation if allowed.

        Launch is a separate write stage inside this call's remaining budget.
        Rereading a WorkingWindow never relaunches or resolves its name again.
        Publish each established scope before later reads can fail or cancel.
        The owner retains identity independently of observation completeness.
        """
        if not isinstance(selector, (str, RunningApplication, WorkingWindow)) or isinstance(
                selector, str) and not selector.strip():
            raise ValueError('Expected an application selector or an observed application/window')
        if on_scope is not None and not callable(on_scope):
            raise ValueError('The scope observer must be callable')
        invalid_window_option = not isinstance(
            allow_launch, bool) or window is not None and (not isinstance(window, str) or not window)
        if invalid_window_option:
            raise ValueError('Invalid launch option or observed window id')
        if isinstance(selector, WorkingWindow) and window is not None:
            raise ValueError('A bound WorkingWindow cannot be retargeted; select a new working window explicitly')
        if isinstance(selector, SelectedApplicationWindow):
            if window is not None and window != selector.window:
                raise ValueError('An exact window choice cannot be retargeted')
            window = selector.window
        invalid_observation_budget = (type(max_nodes) is not int or not 1 <= max_nodes <= 2000
                                      or max_depth is not None and
                                      (type(max_depth) is not int or not 0 <= max_depth <= 2000)
                                      or type(max_dimension) is not int or not 1 <= max_dimension <= 4096)
        if invalid_observation_budget:
            raise ValueError('Invalid app observation budget')
        invalid_menu_path = menu_path is not None and (not isinstance(menu_path,
                                                                      (list, tuple)) or not 1 <= len(menu_path) <= 8
                                                       or any(not isinstance(title, str) or not title.strip()
                                                              for title in menu_path))
        if invalid_menu_path:
            raise ValueError('A menu path contains one to eight exact observed titles')
        launch = None
        try:
            with self.port.calls.claim(timeout_ms) as remaining:
                applications = None
                if isinstance(selector, WorkingWindow):
                    selection, directory = await self.discover(selector, remaining)
                    application = dict(id=selection.application, displayName=selection.name)
                else:
                    if isinstance(selector, StartingApplication):
                        application, directory = await self._startup_directory(selector.launch['application'],
                                                                               selector.session, remaining,
                                                                               launched=True,
                                                                               installation=selector.installation,
                                                                               on_scope=on_scope)
                    else:
                        async with self.port.calls.operation(None, remaining) as read:
                            if isinstance(selector, RunningApplication):
                                if read.target.session != selector.session:
                                    raise failure('stale_target',
                                                  'The selected application belongs to a previous driver session.')
                                application = dict(id=selector.application, displayName=selector.name, isRunning=True)
                            else:
                                applications = await self.directory.collect(read, dict(kind='applications'))
                                application = select_application(applications, selector)
                                chosen_window = selected_window(application, selector)
                                if chosen_window is not None:
                                    if window is not None and window != chosen_window:
                                        raise failure('invalid_request',
                                                      'A window selector cannot select a different working window.')
                                    window = chosen_window
                            if application['isRunning']:
                                if on_scope is not None:
                                    scope = RunningApplication(application['id'], application['displayName'],
                                                               read.target.session)
                                    if window is not None:
                                        scope = SelectedApplicationWindow(scope.application, scope.name, scope.session,
                                                                          window)
                                    on_scope(scope)
                                directory = await self.directory.collect(
                                    read, dict(kind='surfaces', application=application['id']))
                            else:
                                directory = None
                    if directory is None:
                        if window is not None:
                            raise failure('stale_target',
                                          'An installed, nonrunning application cannot contain the requested window.')
                        if not allow_launch:
                            raise failure('observation_incomplete',
                                          'The selected application is not running; launch is disabled for this read.')
                        installation = application['id'] if application.get('matchKeys') else None
                        receipt = await self.engine.execute_in_call(
                            Intent('launch'), ApplicationBinding(applications['session'], application['id']), remaining)
                        launch = MappingProxyType(
                            {**receipt, 'application': MappingProxyType(dict(receipt['application']))})
                        application = launch['application']
                        if launch['dispatch'] != Dispatch.DISPATCHED.value:
                            installation = None
                        if on_scope is not None:
                            scope = RunningApplication(application['id'], application['displayName'],
                                                       applications['session'])
                            if installation is not None:
                                scope = StartingApplication(scope.application, scope.name, scope.session, installation,
                                                            launch)
                            on_scope(scope)
                        application, directory = await self._startup_directory(
                            application, applications['session'], remaining,
                            launched=launch['dispatch'] == Dispatch.DISPATCHED.value, installation=installation,
                            on_scope=on_scope)
                    selection = None
                    if directory['surfaces'] or 'nativeApplicationRoots' not in self.port.connection.capabilities:
                        try:
                            selection = self.selection_from_directory(
                                application['id'], application['displayName'], directory, window)
                        except ComputerUseError as error:
                            if window is None and directory['surfaces'] and error.code in ('ambiguous_target',
                                                                                           'observation_incomplete'):
                                raise await self._window_choices(error, application['id'], application['displayName'],
                                                                 directory, remaining) from error
                            raise
                    elif window is not None:
                        raise failure('stale_target', 'The selected working window is not in the current directory.')
                if selection is not None and on_scope is not None:
                    on_scope(selection)
                if menu_path is not None or selection is None:
                    return await self._menu_observation(application, selection, directory, menu_path or (), launch,
                                                        remaining, fields=fields, max_nodes=max_nodes,
                                                        max_depth=max_depth)
                return await self._observe(selection, directory, applications, launch, remaining, fields=fields,
                                           max_nodes=max_nodes, max_depth=max_depth, max_dimension=max_dimension)
        except BaseException as error:
            if launch is not None:
                annotated = self._after_launch(error, launch)
                if annotated is not error:
                    raise annotated from error
            raise

    async def _observe(self, selection, directory, applications, launch, remaining, **limits):
        try:
            target = select_surface(directory, selection.root.root_window)
        except ComputerUseError as error:
            if error.code not in ('ambiguous_target', 'observation_incomplete'):
                raise
            raise await self._window_choices(error, selection.application, selection.name, directory, remaining,
                                             selection=selection) from error
        gaps = [
            ObservationGap('applications', 'observation_incomplete', reason,
                           'use the exact observed application identity') for reason in applications['stopReasons']
        ] if applications is not None else []
        initializing = initialization_requested(directory, launch)
        refresh_tree = False
        component_gaps = []
        for attempt in range(2):
            if refresh_tree:
                # Same view: retain independently timed images and menus. A
                # local refresh failure does not erase an earlier valid tree.
                refreshed, _, refresh_gaps = await self._components(target, remaining, include_image=False, **limits)
                if refreshed is not None:
                    tree = refreshed
                    component_gaps = [gap for gap in component_gaps if gap.component not in ('tree', 'view')]
                elif tree is not None:
                    refresh_gaps.append(
                        ObservationGap(
                            'tree', 'observation_incomplete',
                            'The content refresh failed; the earlier tree and its capture interval are retained.',
                            'read again if newer content is needed'))
                component_gaps.extend(refresh_gaps)
            else:
                captured_directory = directory
                tree, image, component_gaps = await self._components(target, remaining, **limits)
                menus = None
                if 'nativeApplicationRoots' in self.port.connection.capabilities:
                    menus, gap = await self._component(
                        'menus', lambda: self.menus.collect(selection.application, remaining, context=selection.root,
                                                            summary=True))
                    if gap:
                        component_gaps.append(gap)
                overview = await self.overviews.collect(captured_directory, self.window_aliases(captured_directory),
                                                        remaining, current=image, root=selection.root.root_window)
            # Every component is collected before this recheck. If a dialog
            # changes the current view, discard the whole attempt together.
            _, directory = await self.discover(
                selection, remaining, accessibility='nativeReadIsolation' not in self.port.connection.capabilities)
            scope_confirmed = self._complete_scope(directory)
            try:
                current = select_surface(directory, selection.root.root_window)
            except ComputerUseError as error:
                if error.code not in ('ambiguous_target', 'observation_incomplete'):
                    raise
                scope_confirmed = False
                current = None
                gaps.append(self._gap('scope', error))
            if (tree is None and current is not None and window_binding(current) == window_binding(target)):
                # Worker replacement can expire the root object while the
                # Win32 window and its independently captured pixels survive.
                target = current
            if current != target:
                if scope_confirmed and attempt == 0:
                    # Both components belong to the old view. Discard them
                    # together; never reuse its tree with the new image.
                    target = current
                    continue
                gaps.append(
                    ObservationGap(
                        'scope', 'observation_incomplete',
                        'The observed view changed or could not be confirmed after collection; '
                        'the returned components retain their original view.',
                        'read again before acting on the current view'))
                scope_confirmed = False
            else:
                needs_initial_content_refresh = (attempt == 0 and target.node is not None and
                                                 (initializing or busy_nodes(tree) or limits['fields'] is None
                                                  and limits['max_depth'] is None and window_content_unexposed(tree))
                                                 and remaining() >= 400)
                if needs_initial_content_refresh:
                    # One refresh allowance is shared with view-change recovery.
                    # Never extend the deadline, repeat initialization or activate.
                    await asyncio.sleep(0.2)
                    refresh_tree = True
                    continue
            gaps.extend(component_gaps)
            break
        gaps.extend(self._coverage(directory))
        if limits['fields'] is None and limits['max_depth'] is None and window_content_unexposed(tree):
            gaps.append(
                ObservationGap(
                    "content",
                    "observation_incomplete",
                    "UIA exposed no identifiable client-area content after a bounded refresh. "
                    "This does not establish that the window is empty or ready; "
                    "content may be loading, suspended or inaccessible.",
                    "inspect the window screenshot; read again after an explicit user-visible operation if needed",
                )
            )
        # Number only the final, confirmed result. Window and menu rows share
        # one display sequence; discarded attempts never affect its numbers.
        parts = (tree, ) if tree is not None else ()
        if menus is not None:
            parts += tuple(part.tree for part in menus.parts)
        numbered = iter(number_snapshots(parts))
        if tree is not None:
            tree = next(numbered)
        if menus is not None:
            menus = replace(menus, parts=tuple(replace(part, tree=next(numbered)) for part in menus.parts))
        busy = busy_nodes(tree)
        if busy:
            busy = set(busy)
            references = reference_set(ref for ref, node in tree.refs.items() if node in busy)
            gaps.append(
                ObservationGap(
                    'content', 'observation_incomplete',
                    'The provider reports elements ' + references + ' are busy; their contents may still change.',
                    'use the available regions; read again when the loading region is needed'))
        rendered = tree.render(compact=True) if tree is not None else None
        # Allocate a complete result only after all checks and rendering.
        # Each alias retains its observed window, not the latest App view.
        refs = {
            ref: replace(target, node=node, capture=None)
            for ref, node in (rendered['refs'].items() if rendered else ())
        }
        windows = self.window_aliases(directory)
        text = self._text(selection, target, captured_directory, directory, windows, rendered, image, gaps,
                          scope_confirmed, launch, image_precedes_tree=refresh_tree and refreshed is not None)
        if menus is not None:
            text += '\nApplication menu (separate from the working window):\n' + menus.render(compact=True)
            for part in menus.parts:
                refs.update({ref: replace(part.target, node=node) for ref, node in part.tree.refs.items()})
        if overview is not None:
            if {item['window']
                    for item in captured_directory['surfaces']} != {item['window']
                                                                    for item in directory['surfaces']}:
                overview = replace(
                    overview,
                    text=overview.text + "\nWindow catalogue changed after these thumbnails; "
                    "some pictured windows may have closed and new windows may not be pictured. "
                    "Use the current Windows list.",
                )
            text += '\n' + overview.text
        remaining()
        return AppObservation(selection=selection, target=target, applications=applications,
                              source_directory=captured_directory, directory=directory, tree=tree, image=image,
                              text=text, refs=MappingProxyType(refs), windows=MappingProxyType(windows),
                              gaps=tuple(gaps), scope_confirmed=scope_confirmed, launch=launch, menus=menus,
                              overview=overview)

    async def _window_choices(self, error, application, name, directory, remaining, *, selection=None):
        overview = await self.overviews.collect(directory, self.window_aliases(directory), remaining,
                                                root=selection.root.root_window if selection else None,
                                                include_single=True)
        # Capturing the overview can create system sharing UI and change AX
        # objects, even though we dispatched no input. Publish choices from a
        # post-capture catalogue, as normal window observations already do.
        async with self.port.calls.operation(None, remaining) as read:
            if read.target.session != directory['session']:
                raise failure('stale_target', 'The application belongs to a previous driver session.')
            current = await self.directory.collect(read, dict(kind='surfaces', application=application))
        if selection is not None:
            self.selection_from_directory(application, name, current, selection.root.root_window, previous=selection)
        if directory['surfaces'] != current['surfaces']:
            if overview is not None:
                overview = replace(
                    overview,
                    text=overview.text + "\nWindow catalogue changed after these thumbnails; "
                    "some pictured windows may have closed and new windows may not be pictured. "
                    "Use the current Windows list.",
                )
            error = failure(
                error.code,
                'The window catalogue changed during observation; select an entry from the current Windows list.')
        return WindowSelectionRequired(error, application, name, current, overview=overview, selection=selection)

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def _menu_observation(self, application, selection, directory, path, launch, remaining, **limits):
        context = selection.root if selection else None
        if selection is not None and 'nativeApplicationRoots' not in self.port.connection.capabilities:
            # A local menu is a subtree of the chosen working window, not a
            # global root or a menu borrowed from the current modal/document.
            context = select_surface(directory, selection.root.root_window, follow_modal=False)
        menu = await self.menus.collect(application['id'], remaining, context=context, source_directory=directory,
                                        path=path, summary=not path, **limits)
        menu = replace(
            menu, parts=tuple(
                replace(part, tree=tree)
                for part, tree in zip(menu.parts, number_snapshots(part.tree for part in menu.parts))))
        if selection is not None:
            _, directory = await self.discover(selection, remaining)
        else:
            async with self.port.calls.operation(None, remaining) as read:
                if read.target.session != menu.target.session:
                    raise failure('stale_target', 'The menu belongs to a previous driver session.')
                directory = await self.directory.collect(read, dict(kind='surfaces', application=application['id']))
        windows = self.window_aliases(directory)
        scope = 'application menu' if menu.scope_kind == 'applicationMenu' else 'working-window menu'
        lines = [
            'App: ' + json_string(application['displayName']) + '.', 'Tree: full (' + scope + ' only)',
            'Menu: ' + (' > '.join(json_string(title) for title in path) if path else 'menu bar')
        ]
        if selection is not None:
            lines.append('Working window: ' +
                         next(ref for ref, value in windows.items() if value == selection.root.root_window))
        else:
            lines.append('No working window is bound; this is an application UI root.')
        if len(directory['surfaces']) > 1:
            lines.append('Windows: ' + window_summary(directory, windows, selection=selection))
        if launch:
            lines.append('Launch: ' + launch['detail'])
        lines.extend('Directory partial: ' + reason for reason in directory['stopReasons'])
        lines.append(menu.render(compact=True))
        remaining()
        return MenuAppObservation(application['id'], application['displayName'], selection, menu, directory,
                                  MappingProxyType(windows), '\n'.join(lines), launch)

    async def discover(self, selection, remaining, *, accessibility=True):
        async with self.port.calls.operation(None, remaining) as read:
            if read.target.session != selection.root.session:
                raise failure('stale_target', 'The selected application belongs to a previous driver session.')
            query = dict(kind='surfaces', application=selection.application)
            if not accessibility:
                query['includeAccessibility'] = False
            directory = await self.directory.collect(read, query)
        return self.selection_from_directory(
            selection.application, selection.name, directory, selection.root.root_window,
            previous=selection), directory

    async def _startup_directory(self, application, session, remaining, *, launched, installation=None, on_scope=None):
        # A launch receipt identifies the process started by the OS, which may
        # only be a wrapper. Before selecting any working window, the same pure
        # catalogue selection used before launch can resolve declared association.
        # Never infer ancestry, merge process identities, or launch again here.
        for attempt in range(5):
            stale = None
            async with self.port.calls.operation(None, remaining) as read:
                if read.target.session != session:
                    raise failure('stale_target', 'The launched application belongs to a previous driver session.')
                if installation is not None:
                    applications = await self.directory.collect(read, dict(kind='applications'))
                    if applications['stopReasons']:
                        raise failure('observation_incomplete',
                                      ('Startup application discovery is partial; inspect the application '
                                       'directory before choosing an '
                                       'instance.'))
                    selected = select_application(applications, installation)
                    if selected['isRunning']:
                        application, installation = selected, None
                        if on_scope is not None:
                            on_scope(RunningApplication(application['id'], application['displayName'], session))
                try:
                    directory = await self.directory.collect(read, dict(kind='surfaces', application=application['id']))
                except ComputerUseError as error:
                    # A confirmed wrapper may exit before the child publishes a
                    # window. Only an unresolved start can re-observe its recipe;
                    # an already selected process/window is never substituted.
                    if installation is None or error.code != 'stale_target' or error.effect != 'none':
                        raise
                    stale = error
            if stale is not None:
                if attempt == 4:
                    raise stale
            else:
                startup_finished = not launched or directory['surfaces'] or directory['stopReasons'] or attempt == 4
                if startup_finished:
                    return application, directory
            await asyncio.sleep(min(0.1 * 2**attempt, remaining() / 1000))

    @staticmethod
    def selection_from_directory(application, name, directory, root_id, *, previous=None):
        if root_id is not None and not any(item['window'] == root_id for item in directory['surfaces']):
            raise failure(
                'observation_incomplete' if directory['stopReasons'] else 'stale_target',
                'The fixed working window is missing from the current directory; another document was not selected.')
        root = window_binding(select_surface(directory, root_id, follow_modal=False))
        if previous is not None and previous.root != root:
            raise failure('stale_target', 'The fixed working-window identity changed.')
        return previous or WorkingWindow(application, name, root)

    @staticmethod
    def _after_launch(error, launch):
        app = launch['application']
        detail = launch['detail'] + ' Running application: ' + app['id'] + '.'
        if not isinstance(error, Exception):
            error.add_note(detail + ' The following observation was cancelled; launch was not undone.')
            error.launch = launch
            return error
        effect = error.effect if isinstance(error, ComputerUseError) else 'none'
        if launch['dispatch'] != Dispatch.NOT_DISPATCHED.value and effect == 'none':
            effect = 'possible'
        launch_failure = ComputerUseError(
            dict(
                code=error.code if isinstance(error, ComputerUseError) else 'execution_failed', effect=effect,
                target=error.target if isinstance(error, ComputerUseError) else app['displayName'], detail=detail +
                ' Observation failed: ' + (error.detail if isinstance(error, ComputerUseError) else str(error)),
                next='read the same App again; inspect listApps() before requesting another launch'), cause=error)
        launch_failure.launch = launch
        for note in getattr(error, '__notes__', ()):
            launch_failure.add_note(note)
        if isinstance(error, WindowSelectionRequired):
            return WindowSelectionRequired(launch_failure, error.application, error.display_name, error.directory,
                                           launch, error.overview, error.selection)
        return launch_failure

    async def _components(self, target, remaining, *, include_image=True, **limits):
        if 'nativeReadIsolation' in self.port.connection.capabilities:
            return await self._isolated_components(target, remaining, include_image=include_image, **limits)
        tree = image = None
        gaps = []
        try:
            async with self.port.calls.operation(window_binding(target), remaining) as read:
                # A partial provider result can leave time for capture and the
                # final directory check. Connection timeout/cancellation still
                # terminates the pipeline rather than starting a new peer.
                tree, gap = await self._component(
                    'tree', lambda: self.reader.collect(read.bounded(max(
                        1,
                        remaining() // 3)), node=target.node, fields=limits['fields'], max_nodes=limits['max_nodes'],
                                                        max_depth=limits['max_depth'], scope_kind='window'))
                if gap:
                    gaps.append(gap)
                if include_image and 'nativeCapture' not in self.port.capabilities:
                    gaps.append(
                        ObservationGap('image', 'mode_unsupported', 'This driver does not declare window capture.',
                                       'use a driver with nativeCapture support'))
                elif include_image:
                    image, gap = await self._component(
                        'image', lambda: self.images.collect(read.bounded(max(1,
                                                                              remaining() // 2)), max_dimension=limits[
                                                                                  'max_dimension']))
                    if gap:
                        gaps.append(gap)
                    if image is not None:
                        gaps.extend(
                            ObservationGap('image', 'observation_incomplete', reason,
                                           'use clearly visible content; read again if required content is missing')
                            for reason in capture_metadata(image.image).get('stopReasons', ()))
        except ComputerUseError as error:
            # Failure to begin an already observed surface can be explained by
            # the final directory. Unknown admission or cleanup is never local.
            if self.port.calls.retired or error.effect != 'none' or error.code not in ('stale_target',
                                                                                       'observation_incomplete'):
                raise
            gaps.append(self._gap('view', error))
        return tree, image, gaps

    async def _isolated_components(self, target, remaining, *, include_image, **limits):
        # A terminated read worker expires its operation. End that lease before
        # opening an independent, window-only lease for pixels. No action is
        # retried and no node handle is reconstructed in Python.
        async def tree_read():
            async with self.port.calls.operation(window_binding(target), remaining) as read:
                return await self.reader.collect(read.bounded(max(1,
                                                                  remaining() // 3)), node=target.node,
                                                 fields=limits['fields'], max_nodes=limits['max_nodes'],
                                                 max_depth=limits['max_depth'], scope_kind='window')

        tree, gap = await self._component('tree', tree_read)
        gaps = [gap] if gap else []
        image = None
        if include_image:

            async def image_read():
                async with self.port.calls.operation(window_binding(target), remaining) as read:
                    return await self.images.collect(read.bounded(max(1,
                                                                      remaining() // 2)),
                                                     max_dimension=limits['max_dimension'])

            image, gap = await self._component('image', image_read)
            if gap:
                gaps.append(gap)
            if image is not None:
                gaps.extend(
                    ObservationGap('image', 'observation_incomplete', reason,
                                   'use clearly visible content; read again if required content is missing')
                    for reason in capture_metadata(image.image).get('stopReasons', ()))
        return tree, image, gaps

    async def _component(self, name, collect):
        return await collect_component(self.port, name, collect)

    @staticmethod
    def _gap(component, error):
        return observation_gap(component, error)

    @staticmethod
    def _complete_scope(directory):
        # A partial catalogue cannot prove that an old modal disappeared.
        entries = {item['window']: item for item in directory['surfaces']}
        if directory['stopReasons'] or any(item['parentState'] == 'unknown' for item in entries.values()):
            return False
        for window in entries:
            seen = set()
            while window is not None:
                if window not in entries or window in seen:
                    return False
                seen.add(window)
                window = entries[window]['parent']
        return True

    @staticmethod
    def _coverage(directory):
        gaps = [
            ObservationGap('directory', 'observation_incomplete', reason,
                           'inspect the directory again if the required window is missing')
            for reason in directory['stopReasons']
        ]
        if any(item['parentState'] == 'unknown' for item in directory['surfaces']):
            gaps.append(
                ObservationGap('directory', 'observation_incomplete', 'Some window parent relationships are unknown.',
                               'select an observed window explicitly when needed'))
        return gaps

    @staticmethod
    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    def _text(selection, target, captured_directory, directory, windows, rendered, image, gaps, scope_confirmed, launch,
              *, image_precedes_tree=False):
        selected = next(item for item in captured_directory['surfaces'] if item['surface'] == target.surface)
        lines = [f'Window: {json_string(selected["title"])}, App: {json_string(selection.name)}.']
        if launch is not None:
            lines.append('Launch: ' + launch['detail'])
        if len(directory['surfaces']) > 1:
            lines.append(
                'Windows: ' +
                window_summary(directory, windows, selection=selection, target=target, scope_confirmed=scope_confirmed))
        lines.append('Tree: full' if rendered is not None else 'Tree: unavailable')
        if image is not None:
            metadata = capture_metadata(image.image)
            lines.append(f'Screenshot: window {metadata["width"]}x{metadata["height"]} px; origin=top-left')
            if image_precedes_tree:
                lines.append('The screenshot was captured before the refreshed tree; their contents may differ.')
        else:
            lines.append('Screenshot: unavailable')
        if not scope_confirmed:
            lines.append('Current view is not confirmed; components describe the observed view.')
        if rendered is not None:
            lines.append(rendered['text'])
        lines.extend(f'{gap.component}: {gap.code}: {json_string(gap.detail)}; next: {json_string(gap.next)}'
                     for gap in gaps)
        return '\n'.join(lines)
