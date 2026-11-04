"""Bounded semantic path resolution inside an already-open native operation.

This consumes native facts, never display text. Native adapters only collect
objects/relations; matching, wrapper boundaries and uniqueness live here.
"""
import asyncio
from dataclasses import replace

from ..errors import ComputerUseError
from ..native_semantics import RELATIONS, followed_relations, role_name
from ..protocol.element_path import element_path
from .graph import UNKNOWN, NativeGraph


async def resolve_path(port, operation, root, remaining, path, *, timeout_ms=0):
    """Wait only for absence, within the same lease and total call deadline.

    A zero timeout means one lookup, not zero time for native IPC. Each retry
    discards earlier matching facts; ambiguity, partial data and stale scopes
    are never made to look like an element that has merely not appeared yet.
    """
    if type(timeout_ms) is not int or not 0 <= timeout_ms <= 10000:
        raise ValueError('Path wait must be an integer in 0..10000 milliseconds')
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        query = TargetQuery(port, operation, root, remaining)
        try:
            return await query.resolve(path), query.within
        except ComputerUseError as error:
            delay = min(0.1, deadline - asyncio.get_running_loop().time())
            if error.code != 'element_not_found' or error.effect != 'none' or delay <= 0:
                raise
            await asyncio.sleep(min(delay, remaining() / 1000))


class TargetQuery(NativeGraph):

    def __init__(self, port, operation, root, remaining, *, max_nodes=4096, max_depth=40):
        contents = followed_relations(port.platform, getattr(port, 'capabilities', ()), content_only=True)
        super().__init__(port, operation, root, remaining, max_nodes=max_nodes, max_depth=max_depth,
                         fields=('role', 'name', 'description', 'identifier', 'subrole', 'focused', 'modal', 'value'),
                         follow_relations=contents)
        self.content_kinds = tuple(RELATIONS[self.platform][name] for name in contents)
        self.within = ()

    def fail(self, code, detail):
        raise ComputerUseError(
            dict(code=code, effect='none', target='element path', detail=detail,
                 next='read this working window and narrow the path or use an observed element'))

    async def _name(self, token):
        if self.platform == 'windows':
            # UIA Name is the provider's accessible name (including its label).
            # An observed empty Name is not an instruction to infer a new name
            # from HelpText or require optional LabeledBy on every layout node.
            # Failed/unread Name remains UNKNOWN and still prevents ambiguity
            # from being silently treated as a unique match.
            value = await self._field(token, 'name')
            return '' if value is None else value
        for field in ('name', 'description'):
            value = await self._field(token, field)
            if value is UNKNOWN or value:
                return value
        labels = await self._relation(token, 'labelledBy')
        if not labels:
            return ''
        if len(labels) != 1:
            return UNKNOWN
        label = labels[0]
        title = await self._field(label, 'name')
        if title is UNKNOWN or title:
            return title
        value = await self._field(label, 'value')
        return '' if value is None else value

    async def _matches(self, token, condition):
        unknown = False
        for key in ('role', 'identifier', 'name'):
            if key not in condition:
                continue
            value = await self._name(token) if key == 'name' else await self._field(token, key)
            if value is UNKNOWN:
                unknown = True
                continue
            if key == 'role':
                value = role_name(self.platform, value)
            if value != condition[key]:
                return False
        return None if unknown else True

    async def _transparent(self, token, role):
        if role == 'menu':
            return True
        if role not in ('group', 'unknown', 'scrollArea'):
            return False
        values = [
            await self._name(token), await self._field(token, 'identifier'), await self._field(token, 'subrole'), await
            self._field(token, 'focused'), await self._field(token, 'modal')
        ]
        if any(value is not UNKNOWN and bool(value) for value in values):
            return False
        actions = (await self._node(token))['actions']
        if actions['items']:
            return False
        return None if UNKNOWN in values or actions['state'] != 'complete' else True

    async def _contents(self, token):
        children = list(await self._relation(token, 'children'))
        for kind in self.content_kinds:
            children.extend(await self._relation(token, kind, read_missing=True, optional=True))
        # Rows/cells can be shared by several content relations or span several
        # coordinates. They are one native candidate, not repeated matches.
        return tuple(dict.fromkeys(children))

    async def resolve(self, path):
        path = element_path(path)
        if self.root.node is None or self.root.capture is not None:
            raise ValueError('A path query needs an observed accessible root')
        selected = self.root.node
        anchors = [selected]
        for step, condition in enumerate(path):
            candidates = [self.root.node] if step == 0 else await self._contents(selected)
            found, visited, incomplete = [], set(), False
            stack = [(node, (), 0) for node in reversed(candidates)]
            while stack:
                self.remaining()
                token, ancestors, depth = stack.pop()
                if token in ancestors or depth > self.max_depth:
                    self.fail('observation_incomplete', 'Path containment is cyclic or exceeds its budget.')
                if token in visited:
                    continue  # One shared native object is still one candidate.
                if len(visited) >= self.max_nodes:
                    self.fail('observation_incomplete', 'Path containment exceeds its node budget.')
                visited.add(token)
                raw_role = await self._field(token, 'role')
                role = role_name(self.platform, raw_role) if raw_role is not UNKNOWN else 'unknown'
                if token != self.root.node and role in ('window', 'sheet'):
                    continue  # Select independent surfaces via App.window().
                matched = await self._matches(token, condition)
                incomplete |= matched is None
                if matched is True:
                    found.append(token)
                    if len(found) > 1:
                        self.fail('ambiguous_target', f'Path step {step + 1} has more than one native match.')
                if step == 0:
                    descend = condition.get('role') not in ('window', 'sheet', 'menuBar')
                else:
                    descend = False if matched is True else await self._transparent(token, role)
                incomplete |= descend is None
                if descend is True:
                    children = await self._contents(token)
                    stack.extend((child, (*ancestors, token), depth + 1) for child in reversed(children))
            if incomplete:
                self.fail('observation_incomplete',
                          f'Path step {step + 1} cannot establish a unique match from the available facts.')
            if not found:
                self.fail('element_not_found', f'No native element matches path step {step + 1}.')
            selected = found[0]
            if step > 0 and selected in anchors:
                self.fail('observation_incomplete', 'Path steps cannot select an ancestor again.')
            if selected not in anchors:
                anchors.append(selected)
        # The root and explicit path anchors constrain the logical target.
        # Anonymous wrappers and mutable names are deliberately not guards.
        self.within = tuple(anchors[:-1])
        return replace(self.root, node=selected)
