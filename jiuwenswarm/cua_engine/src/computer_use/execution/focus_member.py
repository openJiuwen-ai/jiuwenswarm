"""Interpret a fixed reported focus without treating all descendants as input.

Only passive control content, or logical items of a collection, can stand for
that control. Independent editors/actions remain boundaries. This lookup never
searches for a different focused item or changes the requested control.
"""
from ..native_semantics import (
    EDITING_FIELDS,
    EDITOR_ROLES,
    LAYOUT_ROLES,
    POINTER_CONTROL_OWNERS,
    UNKNOWN,
    control_member_role,
    editing_evidence,
    role_name,
)
from .graph import NativeGraph
from .native import FocusMember


class FocusMemberQuery(NativeGraph):

    def __init__(self, port, operation, editor, remaining):
        super().__init__(port, operation, editor, remaining, max_nodes=32, max_depth=8,
                         fields=('role', *EDITING_FIELDS))

    async def _role(self, token):
        raw = await self._field(token, 'role')
        return UNKNOWN if raw is UNKNOWN or raw is None else role_name(self.platform, raw)

    async def resolve(self, focused):
        owner = self.root.node
        if owner is None or focused is None or focused == owner:
            return None
        await self._node(owner, depth=0)
        role = await self._role(owner)
        collections = {'list', 'outline', 'table'}
        if role not in EDITOR_ROLES | POINTER_CONTROL_OWNERS | collections:
            return None
        values = await self._control_values(owner)
        if values['enabled'] is False or role in EDITOR_ROLES and editing_evidence(role, values,
                                                                                   platform=self.platform) is not True:
            return None
        logical_items = role in collections
        current, seen = focused, set()
        for _ in range(self.max_depth):
            if current in seen:
                return None
            seen.add(current)
            await self._node(current, depth=0)
            member_role = await self._role(current)
            if member_role is UNKNOWN:
                return None
            # Collection items are a meaningful keyboard-navigation scope,
            # unlike independent controls embedded in document content.
            item = logical_items and member_role in ('row', 'cell')
            kind = control_member_role(member_role)
            not_focus_member = not item and (kind is None or kind == 'layout' and member_role not in LAYOUT_ROLES)
            if not_focus_member:
                return None
            values = await self._control_values(current)
            if values['enabled'] is False or values['editable'] is True:
                return None
            # A selectable/focusable collection item is expected. Other parts
            # must not declare a separate editor or an independent action.
            if not item:
                if any(values[key] is True or values[key] is UNKNOWN
                       for key in ('editable', 'focusWritable', 'valueWritable', 'textWritable')):
                    return None
                actions = await self._actions(current)
                if actions['state'] != 'complete' or actions['items']:
                    return None
            parents = await self._relation(current, 'parent', read_missing=True)
            if len(parents) != 1:
                return None
            if parents[0] == owner:
                return FocusMember(focused)
            current = parents[0]
        return None
