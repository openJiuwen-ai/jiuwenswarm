"""Resolve scrolling from raw facts while retaining the requested element.

An exact page button or the nearest area plus its actual native hit becomes a
bounded selection contract. No convenient point or alternate scroller is chosen.
"""
import math

from ..errors import ComputerUseError
from ..native_semantics import role_name
from .graph import UNKNOWN, NativeGraph
from .native import ScrollActuator, ScrollPointerActuator


class PageRouteUnavailable(ComputerUseError):
    """Page-specific facts do not establish an addressed page action.

    The original target/nearest-region lookup failed separately if uncertain.
    A caller may try an independently observed wheel route only before input.
    """


class _ScrollRegionQuery(NativeGraph):

    async def _node(self, token, *, depth=None):
        # Scroll resolution needs the ancestor/bar path, not the document body.
        return await super()._node(token, depth=0)

    async def _one(self, token, relation):
        values = tuple(
            dict.fromkeys(await self._relation(token, relation, read_missing=relation
                                               in ('verticalScrollBar', 'horizontalScrollBar'))))
        if len(values) > 1:
            self.fail('ambiguous_target', 'More than one native object declares the ' + relation + ' relationship.')
        return values[0] if values else None

    async def _enabled(self, token):
        enabled = await self._field(token, 'enabled')
        if self.platform == 'linux' and enabled is not True:
            sensitive = await self._field(token, 'sensitive')
            enabled = False if enabled is False and sensitive is False else sensitive
        if enabled is False:
            self.fail('input_unavailable', 'The selected scrolling control is disabled.')

    async def nearest_region(self):
        current, visited = self.root.node, set()
        region = None
        while current is not None:
            if current in visited or len(visited) >= self.max_depth:
                self.fail('observation_incomplete', 'Scrolling ancestors are cyclic or exceed the query budget.')
            visited.add(current)
            role = await self._field(current, 'role')
            if role is UNKNOWN or role is None:
                self.fail('observation_incomplete', 'The nearest scrolling region cannot be identified.')
            role = role_name(self.platform, role)
            if role in ('scrollArea', 'scrollBar'):
                region = current
                break
            if role in ('window', 'sheet', 'application'):
                return None
            current = await self._one(current, 'parent')
        return region


class ScrollQuery(_ScrollRegionQuery):

    def __init__(self, port, operation, target, remaining, *, max_nodes=256, max_depth=40):
        super().__init__(port, operation, target, remaining, fields=('role', 'subrole', 'orientation', 'enabled'),
                         max_nodes=max_nodes, max_depth=max_depth)

    async def _backlink(self, token, expected):
        seen = {token}
        for _ in range(self.max_depth):
            parent = await self._one(token, 'parent')
            if parent is None or parent == expected:
                return  # No reverse edge does not erase the standard forward link.
            if parent in seen or await self._field(parent, 'role') not in ('AXGroup', 'AXUnknown'):
                self.fail('observation_incomplete',
                          'The selected native scrolling relationship has a conflicting parent.')
            seen.add(parent)
            token = parent
        self.fail('observation_incomplete', 'Scrolling parent validation exceeded its depth budget.')

    async def resolve(self, parameters):
        if not set(parameters) <= {'direction', 'pages'} or parameters.get('direction') not in ('up', 'down', 'left',
                                                                                                'right'):
            raise ValueError('Element scroll requires a direction and optional positive pages')
        pages = parameters.get('pages', 1)
        if type(pages) not in (int, float) or not 0 < pages <= 9_007_199_254_740_991 or not math.isfinite(pages):
            raise ValueError('Scroll pages must be finite and positive')
        # Discrete native page buttons cannot preserve fractional-page intent.
        if pages != int(pages) or self.platform != 'macos':
            return None
        region = await self.nearest_region()
        if region is None:
            return None
        role = await self._field(region, 'role')
        await self._enabled(region)
        try:
            return await self._page(region, role, parameters)
        except ComputerUseError as error:
            if error.effect != 'none' or error.code not in ('observation_incomplete', 'ambiguous_target',
                                                            'input_unavailable'):
                raise
            # Only this page-candidate stage is optional. Identity/modal/deadline
            # errors and failures finding the original nearest region propagate.
            raise PageRouteUnavailable(
                dict(code=error.code, effect=error.effect, target=error.target, detail=error.detail, next=error.next),
                cause=error) from error

    async def _page(self, region, role, parameters):
        vertical = parameters['direction'] in ('up', 'down')
        bar = region if role == 'AXScrollBar' else await self._one(
            region, 'verticalScrollBar' if vertical else 'horizontalScrollBar')
        if bar is None:
            return None
        if await self._field(bar, 'role') != 'AXScrollBar':
            self.fail('observation_incomplete', 'The declared scroll-bar relationship does not identify a scroll bar.')
        axis = await self._field(bar, 'orientation')
        invalid_orientation = (axis is UNKNOWN or region == bar and axis is None or axis is not None
                               and axis != ('AXVerticalOrientation' if vertical else 'AXHorizontalOrientation'))
        if invalid_orientation:
            self.fail('observation_incomplete',
                      'The scroll-bar orientation is unavailable or conflicts with the request.')
        await self._enabled(bar)
        subrole = 'AXIncrementPage' if parameters['direction'] in ('down', 'right') else 'AXDecrementPage'
        candidates = []
        for child in dict.fromkeys(await self._relation(bar, 'children')):
            child_role = await self._field(child, 'role')
            if child_role is UNKNOWN or child_role is None:
                self.fail('observation_incomplete', 'An unread scroll-bar child may be a page button.')
            if child_role != 'AXButton':
                continue
            child_subrole = await self._field(child, 'subrole')
            if child_subrole is UNKNOWN:
                self.fail('observation_incomplete', 'A scroll-bar button has an unknown subrole.')
            if child_subrole == subrole:
                candidates.append(child)
        if len(candidates) > 1:
            self.fail('ambiguous_target', 'The selected scroll bar contains multiple matching page buttons.')
        if not candidates:
            return None
        button = candidates[0]
        await self._enabled(button)
        actions = await self._actions(button)
        if actions['state'] != 'complete':
            self.fail('observation_incomplete', 'The page button has incomplete native action declarations.')
        if sum(a['name'] == 'AXPress' for a in actions['items']) != 1:
            return None
        # Forward links select the route. Explicit backward conflicts are not
        # ignored; a missing redundant parent remains usable by the live unit.
        for node, expected in ((bar, region), (button, bar)):
            if node == expected:
                continue
            await self._backlink(node, expected)
        return ScrollActuator(button, region, bar)


class ScrollPointerQuery(_ScrollRegionQuery):
    """Qualify the OS hit, never search for a convenient point or inner scroller.

    The nearest region gives the viewport. A completely observed content path
    may share it: a document can receive wheel events while also being editable.
    Nested scrollers and independent value controls remain separate boundaries.
    Raw facts used here are pinned and rechecked by the driver's input contract;
    containment qualifies dispatch, not proof that the application will scroll.
    """
    _CONTENT_ROLES = frozenset(('textArea', 'textField', 'table', 'outline', 'list', 'browser', 'row', 'column', 'cell',
                                'webArea', 'document'))
    _LAYOUT_ROLES = frozenset(('group', 'staticText', 'heading', 'image'))
    # These describe clicks/context menus or editing confirmation, not wheel
    # consumption. Increment, decrement, scrolling and unknown custom actions
    # need their own boundary, including when declared by a text field's parent.
    _NON_WHEEL_ACTIONS = {
        'macos': frozenset(('AXPress', 'AXShowMenu', 'AXConfirm', 'AXCancel')),
        'linux': frozenset(('click', 'press', 'activate', 'show menu', 'edit', 'expand or contract', 'toggle')),
    }
    _COLLECTION_ROLES = frozenset(('table', 'outline', 'list', 'browser'))
    _ITEM_ROLES = frozenset(('row', 'column', 'cell'))

    def __init__(self, port, operation, target, remaining, *, max_nodes=80, max_depth=40):
        super().__init__(
            port, operation, target, remaining,
            fields=('role', 'enabled', 'sensitive', 'editable', 'focusWritable', 'valueWritable', 'textWritable'),
            max_nodes=max_nodes, max_depth=max_depth)

    async def resolve(self, hit=None):
        if self.platform not in self._NON_WHEEL_ACTIONS:
            return None
        region = await self.nearest_region()
        # Some providers expose a scrolling widget as an ordinary container.
        # An explicitly requested container supplies its own wheel area; this
        # does not invent a scrolling ancestor for an arbitrary descendant.
        addressed_container = (region is None and self.platform == 'linux' and role_name(
            self.platform, await self._field(self.root.node, 'role')) in self._COLLECTION_ROLES | {'group'})
        if addressed_container:
            region = self.root.node
        # A scroll bar has no content viewport; do not interpret its narrow
        # rectangle as a page. It retains the separate native-page route.
        if region is None or not addressed_container and role_name(self.platform, await self._field(
                region, 'role')) != 'scrollArea':
            return None
        await self._enabled(region)
        if hit is None or hit == region:
            return ScrollPointerActuator(region, region)
        current, visited, path = hit, set(), []
        while current != region:
            if current in visited or len(visited) >= self.max_depth:
                self.fail('observation_incomplete', 'The wheel hit path is cyclic or exceeds the query budget.')
            visited.add(current)
            role = role_name(self.platform, await self._field(current, 'role'))
            path.append((current, role))
            if role not in self._CONTENT_ROLES and role not in self._LAYOUT_ROLES:
                return None
            actions = await self._actions(current)
            if (actions['state'] != 'complete' or any(
                (action['name'].casefold() if self.platform == 'linux' else action['name']
                 ) not in self._NON_WHEEL_ACTIONS[self.platform] for action in actions['items'])):
                return None
            if role in self._LAYOUT_ROLES:
                # A generic wrapper may conceal an independent editor. In a
                # declared document/collection, however, writable value/focus
                # or selection is normal and says nothing about wheel routing.
                for field in ('editable', 'focusWritable', 'valueWritable', 'textWritable'):
                    value = await self._field(current, field)
                    if value is True or value is UNKNOWN:
                        return None
            current = await self._one(current, 'parent')
            if current is None:
                return None
        path.append((region, role_name(self.platform, await self._field(region, 'role'))))
        # Only a proven item path may change identities while its collection
        # remains fixed. Drivers compare new objects with these exact raw facts;
        # they do not classify rows, editors or independent controls themselves.
        content_path = ()
        if 'nativeScrollContentPath' in getattr(self.port, 'capabilities', ()):
            for index, (_, role) in enumerate(path):
                if role in self._COLLECTION_ROLES:
                    prefix = path[:index]
                    if (any(r in self._ITEM_ROLES for _, r in prefix)
                            and all(r in self._ITEM_ROLES | self._LAYOUT_ROLES for _, r in prefix)):
                        content_path = tuple(node for node, _ in path[:index + 1])
                    break
        return ScrollPointerActuator(hit, region, content_path)
