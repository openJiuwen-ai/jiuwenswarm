"""Session discovery and pure working-window selection for the v5 read chain.

No launch, focus, screenshot, or legacy readApp bootstrap occurs here. A native-
only window remains a valid visual target even when it has no AX root.
"""
from ..errors import ComputerUseError
from ..execution.native import Binding


def _refuse(code, detail, target="working window"):
    raise ComputerUseError(
        dict(code=code, effect="none", target=target, detail=detail,
             next="inspect the directory and explicitly select an observed " + target))


def _running_match(directory, installation):
    """Declared catalogue/window hints select an identity; they never merge it.

    Keys are opaque native facts, not a guessed executable or name. A shared
    key cannot distinguish launch recipes, and several processes remain a
    choice for the caller even if their window classes are equal.
    """
    if installation['isRunning']:
        return dict(installation)
    keys = set(installation.get('matchKeys', []))
    running = [
        item for item in directory['applications'] if item['isRunning'] and keys.intersection(item.get('matchKeys', []))
    ]
    if not running:
        return dict(installation)
    if directory['stopReasons']:
        _refuse('observation_incomplete', 'Application discovery is partial; select an exact running application id.',
                'application')
    observed_keys = keys.intersection(key for item in running for key in item.get('matchKeys', []))
    recipes = [
        item for item in directory['applications']
        if not item['isRunning'] and observed_keys.intersection(item.get('matchKeys', []))
    ]
    if len(running) != 1 or len(recipes) != 1:
        _refuse(
            'ambiguous_target', 'Declared window identity does not identify one running application and installation; '
            'select an exact observed running application id.', 'application')
    return dict(running[0])


def select_application(directory, selector):
    """Resolve observed facts in Python; selection never starts an application."""
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("A nonempty application selector is required")
    entries = directory['applications']
    exact = [item for item in entries if item['id'] == selector]
    if exact:
        return _running_match(directory, exact[0])
    windows = [item for item in entries if selected_window(item, selector) is not None]
    if len(windows) == 1:
        return dict(windows[0])
    matches = []
    for item in entries:
        if selector in (item['identifier'], item['path']):
            matches.append(item)
        elif item['displayName'].casefold() == selector.strip().casefold():
            matches.append(item)
    if directory['stopReasons']:
        _refuse('observation_incomplete', 'Application discovery is partial; use an exact observed application id.',
                'application')
    if len(matches) != 1:
        if not matches and directory.get('scope') == 'runningWindows':
            _refuse(
                'element_not_found', 'No observed running window matches ' + selector +
                '. This driver does not discover or launch installed applications; '
                'read the desktop or listApps() and select a running application/window.', 'application')
        _refuse('ambiguous_target' if matches else 'element_not_found',
                'Application selector has no unique match: ' + selector, 'application')
    return _running_match(directory, matches[0])


def selected_window(application, selector):
    """Resolve only the exact native window selector, never a title or ordinal."""
    matches = [item['window'] for item in application.get('windows', []) if item['selector'] == selector]
    if len(matches) > 1:
        _refuse('ambiguous_target', 'The observed window selector has conflicting identities.')
    return matches[0] if matches else None


def select_surface(directory, window=None, *, follow_modal=True):
    """Select a fixed root and a confirmed modal view, preserving unknown owners."""
    entries = {item["window"]: item for item in directory["surfaces"]}
    if window is not None:
        if window not in entries:
            _refuse("stale_target", "The selected working window is not in this observation.")
        root = window
    else:
        if any(item['parentState'] == 'unknown' for item in entries.values()):
            _refuse('observation_incomplete',
                    'Some windows have unknown ownership; select an explicit observed window.')
        for key in entries:
            seen, current = set(), key
            while current is not None:
                if current not in entries or current in seen:
                    _refuse("observation_incomplete",
                            "Window ownership is incomplete or cyclic; select an explicit observed window.")
                seen.add(current)
                current = entries[current]["parent"]
        candidates = [key for key, item in entries.items() if item["parent"] not in entries and item["kind"] != "menu"]
        if len(candidates) != 1:
            code = "ambiguous_target" if len(candidates) > 1 else "observation_incomplete"
            _refuse(code, ('Select one working window using a reference from Windows; the directory '
                           'has no unique '
                           'root.'))
        if directory["stopReasons"]:
            _refuse("observation_incomplete",
                    "An incomplete directory cannot prove that the only observed root is the only window.")
        root = candidates[0]
    view, visited = root, {root}
    if follow_modal:
        while True:
            children = [key for key, item in entries.items() if item["parent"] == view and item["modal"] is True]
            if len(children) > 1:
                _refuse("ambiguous_target", "Several confirmed modal children require an explicit view selection.")
            if not children:
                break
            view = children[0]
            if view in visited:
                _refuse("observation_incomplete", "The native window ownership relationships contain a cycle.")
            visited.add(view)
    selected = entries[view]
    return Binding(directory["session"], entries[root]["instance"], root, selected["surface"], selected["node"])


class NativeDirectory:

    def __init__(self, port):
        self.port = port

    async def applications(self, *, timeout_ms=30000):
        return await self._query(dict(kind="applications"), timeout_ms)

    async def surfaces(self, application, *, timeout_ms=30000):
        if not isinstance(application, str) or not application:
            raise ValueError("An observed running-application id is required")
        return await self._query(dict(kind="surfaces", application=application), timeout_ms)

    async def _query(self, query, timeout_ms):
        async with self.port.calls.open(None, timeout_ms) as read:
            return await self.collect(read, query)

    async def collect(self, read, query):
        """Directory facts never expand a window-scoped operation to a session."""
        if read.target != Binding(read.target.session, None, None, None):
            raise ValueError('Directory collection requires a session read operation')
        result = await self.port.directory(read.token, read.target, query, read.remaining())
        read.remaining()
        return result
