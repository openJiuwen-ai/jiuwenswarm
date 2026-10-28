"""Select the exact native menu ancestry; never resolve it again by title."""
from .graph import UNKNOWN, NativeGraph


class MenuContextQuery(NativeGraph):

    def __init__(self, port, operation, target, remaining):
        super().__init__(port, operation, target, remaining, fields=('role', ), max_nodes=512, max_depth=16)

    async def _node(self, token, *, depth=None):
        return await super()._node(token, depth=0)

    async def resolve(self, anchors=(), *, inspect_submenu=False):
        chain = [self.root.node]
        for _ in range(16):
            current = chain[-1]
            role = await self._field(current, 'role')
            if role == 'AXMenuBar':
                break
            if role not in ('AXMenuItem', 'AXMenuBarItem', 'AXMenu'):
                self.fail('observation_incomplete', 'The selected menu has no complete native menu-bar ancestry.')
            parents = await self._relation(current, 'parent')
            if len(parents) != 1 or parents[0] in chain:
                self.fail('observation_incomplete', 'The selected menu ancestry is missing, ambiguous or cyclic.')
            if current not in await self._relation(parents[0], 'children'):
                self.fail('stale_target', 'The observed menu is no longer a child of its original parent.')
            chain.append(parents[0])
        else:
            self.fail('observation_incomplete', 'Menu ancestry exceeds its preparation budget.')
        within = tuple(reversed(chain[1:]))
        if not set(anchors).issubset(within):
            self.fail('stale_target', 'The menu left an explicitly selected ancestor.')
        return within, await self.has_submenu() if inspect_submenu else False

    async def has_submenu(self):
        roles = [await self._field(child, 'role') for child in await self._relation(self.root.node, 'children')]
        if any(role is UNKNOWN for role in roles):
            self.fail('observation_incomplete', 'The menu branch has unread child roles.')
        return roles.count('AXMenu') == 1
