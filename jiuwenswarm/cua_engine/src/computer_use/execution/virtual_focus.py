"""Interpret an explicitly related active-descendant popup in common Python.

The native unit gets one selected popup, not permission to search for editors.
Structural evidence is retained; the current active item is sampled afresh at
dispatch so changing suggestions does not invalidate an otherwise sound route.
"""
from ..native_semantics import EDITING_FIELDS, EDITOR_BOUNDARIES, editing_evidence, role_name
from .graph import UNKNOWN, NativeGraph
from .native import VirtualReceiver


class VirtualFocusQuery(NativeGraph):

    def __init__(self, port, operation, editor, remaining):
        super().__init__(port, operation, editor, remaining, max_nodes=64, max_depth=12,
                         fields=('role', 'hasPopup', 'focused', *EDITING_FIELDS))

    async def _role(self, node):
        value = await self._field(node, 'role')
        return UNKNOWN if value is UNKNOWN or value is None else role_name(self.platform, value)

    async def resolve(self, focused):
        editor = self.root.node
        if editor is None or focused is None or focused == editor:
            return None
        await self._node(editor, depth=0)
        role = await self._role(editor)
        if role not in ('textField', 'comboBox') or await self._field(editor, 'hasPopup') is not True:
            return None
        values = await self._control_values(editor)
        if editing_evidence(role, values, platform=self.platform) is not True:
            return None
        windows = await self._relation(editor, 'window')
        if len(windows) != 1:
            return None
        candidates = {}
        for via in ('owns', 'linked'):
            for popup in await self._relation(editor, via, read_missing=True):
                if popup in candidates:
                    continue
                await self._node(popup, depth=0)
                if await self._role(popup) not in ('list', 'outline', 'table'):
                    continue
                if await self._relation(popup, 'window') != windows:
                    continue
                # Focus, active item and its transient ancestry are live unit
                # conditions, not structural snapshot-equality dependencies.
                stable, self.dependencies = self.dependencies, {}
                try:
                    if await self._active(popup, focused, windows):
                        candidates[popup] = VirtualReceiver(popup, via)
                finally:
                    self.dependencies = stable
        if len(candidates) > 1:
            self.fail('ambiguous_target', 'More than one related popup claims the active input item.')
        return next(iter(candidates.values()), None)

    async def _active(self, popup, focused, windows):
        await self._node(focused, depth=0)
        role = await self._role(focused)
        if role is UNKNOWN or role in EDITOR_BOUNDARIES or await self._field(focused, 'focused') is not True:
            return False
        if await self._relation(focused, 'window') != windows:
            return False
        if await self._relation(popup, 'selectedChildren', read_missing=True) != [focused]:
            return False
        current, seen = focused, set()
        for _ in range(self.max_depth):
            if current == popup:
                return True
            if current in seen:
                return False
            seen.add(current)
            parents = await self._relation(current, 'parent', read_missing=True)
            if len(parents) != 1:
                return False
            current = parents[0]
            await self._node(current, depth=0)
        return False
