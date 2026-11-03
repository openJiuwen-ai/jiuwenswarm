"""Interpret one native pointer hit inside a control; never choose coordinates.

The OS supplies the exact hit object. Passive members, or internal layout
members with only matching context-menu support, can stand for a control on
a confirmed path. Independent controls remain boundaries, even when disabled
or geometrically inside its rectangle.
"""
from copy import deepcopy

from ..native_semantics import (
    LAYOUT_ROLES,
    POINTER_CONTROL_OWNERS,
    UNKNOWN,
    action_for_intent,
    control_member_role,
    role_name,
)
from .graph import NativeGraph
from .native import Actuator


class ControlPartQuery(NativeGraph):

    def __init__(self, port, operation, target, remaining, *, max_nodes=64, max_depth=8, intent=None):
        self.intent = intent
        super().__init__(port, operation, target, remaining, max_nodes=max_nodes, max_depth=max_depth,
                         fields=('role', 'editable', 'focusWritable', 'valueWritable', 'textWritable'))

    async def resolve(self, hit):
        owner = self.root.node
        if owner is None or hit is None or hit == owner:
            return None
        # Probe the owner before expanding anything. Once its control role is
        # established, NativeGraph can batch the small member subtree instead
        # of requiring a separate RPC for every wrapper and label.
        await self._node(owner, depth=0)
        role = await self._field(owner, 'role')
        if role is UNKNOWN:
            return None
        owner_role = role_name(self.platform, role)
        owner_actions = None
        if owner_role not in POINTER_CONTROL_OWNERS:
            if owner_role not in LAYOUT_ROLES:
                return None
            owner_actions = await self._actions(owner)
            if not action_for_intent(self.platform, role, [a['name'] for a in owner_actions['items']],
                                     owner_actions['state'] == 'complete', 'click'):
                return None
        # Right click remains a pointer event. Context-menu/scroll support on
        # internal layout nodes is not an independent primary control action.
        support_actions = set()
        if self.platform == 'macos' and self.intent is not None and self.intent.parameters.get('button') == 'right':
            owner_actions = owner_actions or await self._actions(owner)
            if owner_actions['state'] == 'complete' and any(a['name'] == 'AXShowMenu' for a in owner_actions['items']):
                support_actions = {'AXShowMenu', 'AXScrollToVisible'}

        async def visit(token, ancestors):
            if token in ancestors or len(ancestors) > self.max_depth:
                self.fail('observation_incomplete', 'Control composition is cyclic or exceeds its depth budget.')
            raw_role = await self._field(token, 'role')
            if raw_role is UNKNOWN:
                return False
            role = role_name(self.platform, raw_role)
            kind = control_member_role(role)
            # A cell is a logical item, not a transparent button wrapper.
            if kind is None or kind == 'layout' and role not in LAYOUT_ROLES:
                return False
            actions = await self._actions(token)
            allowed_actions = support_actions if kind == 'layout' else set()
            if actions['state'] != 'complete' or any(a['name'] not in allowed_actions for a in actions['items']):
                return False
            for field in ('editable', 'focusWritable', 'valueWritable', 'textWritable'):
                value = await self._field(token, field)
                if value is True or value is UNKNOWN:
                    return False
            children = await self._relation(token, 'children')
            if kind != 'layout' and children:
                return False
            found = token == hit
            if not found and kind == 'layout':
                found = await descend(children, ancestors + (token, ))
            if found:
                self._check_parent(token, ancestors)
            return found

        async def descend(children, ancestors):
            for child in children:
                previous = deepcopy(self.dependencies)
                if await visit(child, ancestors):
                    return True
                # The identity of the supplied hit is already fixed. An
                # unrelated sibling's role/action state is not a dependency.
                self.dependencies = previous
            return False

        if await descend(await self._relation(owner, 'children'), (owner, )):
            return Actuator(hit, 'controlPart')
        return None
