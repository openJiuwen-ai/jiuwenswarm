"""Read native menus inside their actual application or working-window scope.

No menu opening, focus, command validation or input occurs here. Application
roots require a declared driver capability; otherwise discovery stays inside
one already selected working window.
"""
from collections import deque
from dataclasses import dataclass, replace

from ..errors import ComputerUseError
from ..execution.graph import NativeGraph
from ..execution.native import Binding
from ..execution.query import TargetQuery
from ..native_semantics import UNKNOWN, role_name
from .formatting import json_string
from .native_facts import SemanticSnapshot


def menu_failure(code, detail):
    return ComputerUseError(
        dict(
            code=code, effect='none', target='menu observation', detail=detail,
            next=('read the parent menu or working window and use an exact observed title '
                  'or element reference; no menu was '
                  'opened')))


class MenuQuery(TargetQuery):

    def fail(self, code, detail):
        raise menu_failure(code, detail)

    async def _node(self, token, *, depth=None):
        # Query only the facts needed to establish this level. Unopened deep
        # submenus must not consume the budget before sibling uniqueness is known.
        return await super()._node(token, depth=0 if depth is None else depth)

    async def _menu_children(self, token, ancestors):
        children = list(dict.fromkeys(await self._relation(token, 'children')))
        for child in children:
            if child in ancestors:
                self.fail('observation_incomplete', 'Menu containment is cyclic.')
            await self._node(child)
            self._check_parent(child, ancestors)
        return children

    async def _role(self, token):
        role = await self._field(token, 'role')
        if role is UNKNOWN or not isinstance(role, str) or not role:
            self.fail('observation_incomplete', 'A menu element has an unread native role.')
        return role_name(self.platform, role)

    async def resolve_menu(self, path):
        current, ancestors = self.root.node, (self.root.node, )
        for index, title in enumerate(path):
            siblings = await self._menu_children(current, ancestors)
            names = []
            for node in siblings:
                if await self._role(node) in ('window', 'sheet'):
                    self.fail('observation_incomplete', 'A menu level crosses an independent window boundary.')
                names.append((node, await self._name(node)))
            matched = [node for node, name in names if name == title]
            if any(name is UNKNOWN for _, name in names):
                self.fail('observation_incomplete',
                          f'Menu level {index + 1} has unread item names; uniqueness is unknown.')
            if len(matched) != 1:
                titles = [name for _, name in names if isinstance(name, str) and name]
                candidates = ', '.join(json_string(name) for name in titles[:30]) or '<none>'
                if len(titles) > 30:
                    candidates += f'; {len(titles) - 30} more (read the parent menu)'
                self.fail('ambiguous_target' if matched else 'element_not_found',
                          (f'Menu level {index + 1}, title {json_string(title)} has {len(matched)} '
                           f'matches. Candidates: {candidates}'))
            selected = matched[0]
            ancestors = (*ancestors, selected)
            role = await self._role(selected)
            current = selected
            if role == 'menu':
                # GTK exposes the named item itself as the submenu container.
                # Its named child menus are siblings of commands, not wrappers.
                continue
            children = await self._menu_children(selected, ancestors)
            roles = [(node, await self._role(node)) for node in children]
            wrappers = [node for node, child_role in roles if child_role == 'menu']
            if role in ('menuItem', 'menuBarItem') and len(children) == 1 and len(wrappers) == 1:
                current = wrappers[0]
                ancestors = (*ancestors, current)
            elif index != len(path) - 1:
                if not children:
                    self.fail('element_not_found', f'{json_string(title)} has no submenu; a command must end the path.')
                self.fail('ambiguous_target' if len(wrappers) > 1 else 'observation_incomplete',
                          (f'{json_string(title)} does not expose one unambiguous native submenu '
                           f'container; read this item first.'))
        return replace(self.root, node=current)


@dataclass(frozen=True)
class MenuSources:
    targets: tuple[Binding, ...]
    gaps: tuple[str, ...] = ()


class WindowMenus(NativeGraph):
    """A bounded read discovery; unlike input lookup it need not prove uniqueness."""

    def __init__(self, port, operation, root, remaining, *, max_nodes=1000, max_depth=40):
        super().__init__(port, operation, root, remaining, fields=('role', ), max_nodes=max_nodes, max_depth=max_depth)

    def fail(self, code, detail):
        raise menu_failure(code, detail)

    async def collect(self):
        if self.root.node is None or self.root.capture is not None:
            self.fail('observation_incomplete', 'The working window has no accessible root for menu discovery.')
        pending, visited, candidates, gaps = deque([(self.root.node, (), 0)]), set(), [], []
        while pending:
            self.remaining()
            token, ancestors, depth = pending.popleft()
            try:
                if token in ancestors or depth > self.max_depth:
                    self.fail('observation_incomplete',
                              'Window-menu containment is cyclic or exceeds its depth budget.')
                await self._node(token, depth=0)
                if ancestors:
                    self._check_parent(token, ancestors)
                if token in visited:
                    continue
                visited.add(token)
                raw = await self._field(token, 'role')
                if raw is UNKNOWN or not isinstance(raw, str) or not raw:
                    self.fail('observation_incomplete', 'A window branch has an unread native role.')
                role = role_name(self.platform, raw)
                if token != self.root.node and role in ('window', 'sheet'):
                    continue
                if role == 'menuBar':
                    candidates.append(replace(self.root, node=token))
                    continue
                edge = self.edges.get((token, self.relations.get('children')), {})
                # Virtual containers can contain millions of children. Do not
                # let an unfinishable branch consume all other menu discovery.
                # It remains explicitly unread, not an inferred menu-free area.
                branch_exceeds_budget = (edge.get('state') == 'partial' and edge.get('certainty') == 'confirmed'
                                         and edge.get('offset', 0) == 0 and edge.get('cursor') is not None
                                         and edge.get('total', 0) > self.max_nodes - len(self.nodes))
                if branch_exceeds_budget:
                    gaps.append(
                        'A window branch has more children than the menu-discovery budget; its subtree is unread.')
                    continue
                else:
                    children = await self._relation(token, 'children')
                pending.extend((child, (*ancestors, token), depth + 1) for child in children)
            except ComputerUseError as error:
                # A disappearing descendant cannot invalidate other confirmed
                # menu branches. The working root is rechecked after collection;
                # an expired root/session or a selected menu still fails closed.
                unavailable_child = error.code == 'stale_target' and token != self.root.node
                if error.effect != 'none' or not (error.code == 'observation_incomplete' or unavailable_child):
                    raise
                gaps.append(
                    'A window branch became unavailable during discovery.' if unavailable_child else error.detail)
                if len(self.nodes) >= self.max_nodes:
                    break
        if not candidates:
            self.fail('observation_incomplete', '; '.join(dict.fromkeys(gaps))
                      or 'The working window exposes no readable native menu bar.')
        return MenuSources(tuple(candidates), tuple(dict.fromkeys(gaps)))


@dataclass(frozen=True)
class MenuObservation:
    target: Binding
    tree: SemanticSnapshot
    directory: dict | None
    path: tuple[str, ...]


@dataclass(frozen=True)
class MenuRead:
    """Separate native observations and their common query scope.

    A read may describe several menus; it does not choose an action target or
    merge objects just because their titles or commands happen to be equal.
    """
    target: Binding
    parts: tuple[MenuObservation, ...]
    discovery_gaps: tuple[str, ...] = ()

    @property
    def scope_kind(self):
        return self.parts[0].tree.scope_kind

    @property
    def observations(self):
        return tuple((part.target, part.tree) for part in self.parts)

    def render(self, *, compact=False):
        if len(self.parts) == 1 and not self.discovery_gaps:
            return self.parts[0].tree.render(compact=compact)['text']
        lines = [(f'Menu sources: {len(self.parts)} observed. Native elements remain separate; '
                  f'use an observed element number for actions.')]
        if self.discovery_gaps:
            lines.append('Menu discovery incomplete: ' + ' '.join(self.discovery_gaps) +
                         ' Other sources may exist; read the working window or an observed element to continue.')
        for index, part in enumerate(self.parts, 1):
            lines.extend((f'Menu source {index}:', part.tree.render(compact=compact)['text']))
        return '\n'.join(lines)


class NativeMenus:

    def __init__(self, directory, reader):
        self.directory, self.reader = directory, reader
        self.port = reader.port

    async def collect(self, application, remaining, *, context=None, path=(), summary=False, source_directory=None,
                      fields=None, max_nodes=1000, max_depth=40):
        # Menu discovery/path matching keeps its own bounded search policy;
        # normal document observation must not broaden this input/query route.
        max_depth = 40 if max_depth is None else max_depth
        if 'nativeApplicationRoots' not in self.port.connection.capabilities:
            invalid_window_context = (context is None or context.node is None or context.capture is not None
                                      or context.instance is None or context.root_window is None
                                      or context.surface is None)
            if invalid_window_context:
                raise menu_failure('observation_incomplete',
                                   'Window-menu discovery needs an accessible, fixed working window.')
            async with self.port.calls.operation(replace(context, node=None), remaining) as read:
                sources = await WindowMenus(self.port, read.token, context, read.remaining, max_nodes=max_nodes,
                                            max_depth=max_depth).collect()
                # Menu path resolution is a separate bounded phase; a large
                # document must not consume the budget for known menu sources.
                query = MenuQuery(self.port, read.token, context, read.remaining, max_nodes=max_nodes,
                                  max_depth=max_depth)
                selected, missing = [], []
                for target in sources.targets:
                    query.root = target
                    try:
                        selected.append(await query.resolve_menu(path) if path else target)
                    except ComputerUseError as error:
                        if error.code != 'element_not_found' or error.effect != 'none':
                            raise
                        missing.append(error)
                if not selected:
                    raise menu_failure(
                        'observation_incomplete' if sources.gaps else 'element_not_found',
                        f'No match in {len(sources.targets)} observed menu sources. ' +
                        ('Other sources may remain unread. ' if sources.gaps else '') + missing[0].detail)
                # Each source gets its own timestamp, raw facts, references and
                # truncation. The total node budget does not multiply by roots.
                quotient, extra = divmod(max_nodes, len(selected))
                parts = []
                for index, target in enumerate(selected):
                    tree = await self.reader.collect(read, node=target.node, fields=fields,
                                                     max_nodes=quotient + (index < extra),
                                                     max_depth=1 if summary else max_depth, scope_kind='window')
                    parts.append(MenuObservation(target, tree, source_directory, tuple(path)))
            remaining()
            return MenuRead(parts[0].target if len(parts) == 1 and not sources.gaps else context, tuple(parts),
                            sources.gaps)
        async with self.port.calls.operation(None, remaining) as read:
            if context is not None and read.target.session != context.session:
                raise menu_failure('stale_target', 'The menu context belongs to a previous driver session.')
            roots = await self.directory.collect(read, dict(kind='roots', application=application))
        candidates = [item for item in roots['roots'] if item['kind'] == 'menuBar']
        if roots['stopReasons']:
            raise menu_failure('observation_incomplete', '; '.join(roots['stopReasons']))
        if len(candidates) != 1:
            raise menu_failure('ambiguous_target' if candidates else 'observation_incomplete',
                               f'The application exposes {len(candidates)} observed menu-bar roots.')
        root = candidates[0]
        target = Binding(roots['session'], context.instance if context else root['instance'],
                         context.root_window if context else None, root['surface'], root['node'])
        async with self.port.calls.operation(replace(target, node=None), remaining) as read:
            selected = (await MenuQuery(self.port, read.token, target, read.remaining, max_nodes=max_nodes,
                                        max_depth=max_depth).resolve_menu(path) if path else target)
            # The roots directory establishes this independent application scope.
            tree = await self.reader.collect(read, node=selected.node, fields=fields, max_nodes=max_nodes,
                                             max_depth=1 if summary else max_depth, scope_kind='applicationMenu')
        remaining()
        return MenuRead(selected, (MenuObservation(selected, tree, roots, tuple(path)), ))
