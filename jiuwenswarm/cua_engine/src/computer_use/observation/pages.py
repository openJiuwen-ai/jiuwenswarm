"""Public continuation aliases for observed native relationship pages.

Aliases grant no input authority. A page keeps its source binding, property
and native prefix cursor; neither a new query nor a numeric offset can retarget
it. The driver still checks that the original prefix is current when read.
"""
from collections import OrderedDict
from dataclasses import dataclass, replace

from ..errors import ComputerUseError
from ..execution.native import Binding
from ..native_semantics import RELATIONS
from .formatting import json_string


@dataclass(frozen=True)
class ContentPage:
    target: Binding
    property: str
    cursor: str
    offset: int
    total: int


class ContentPages:

    def __init__(self, capacity=512):
        self.capacity = capacity
        self._next = 1
        self._pages = OrderedDict()
        self._aliases = {}

    def remember(self, target, tree):
        for relation in tree.native['relations']:
            if relation['certainty'] != 'confirmed' or 'cursor' not in relation:
                continue
            page = ContentPage(replace(target, node=relation['source'], capture=None), relation['property'],
                               relation['cursor'],
                               relation.get('offset', 0) + len(relation['targets']), relation['total'])
            if page in self._aliases:
                self._pages.move_to_end(self._aliases[page])
                continue
            alias = 'p' + str(self._next)
            self._next += 1
            self._aliases[page] = alias
            self._pages[alias] = page
            while len(self._pages) > self.capacity:
                _, expired = self._pages.popitem(last=False)
                del self._aliases[expired]

    def resolve(self, alias, target):
        page = self._pages.get(alias)
        if page is None or page.target != replace(target, capture=None):
            raise ComputerUseError(
                dict(code='snapshot_expired', effect='none', target='element content',
                     detail='This continuation is expired or belongs to a different element or window.',
                     next='read the intended element again and use a continuation printed in that result'))
        return dict(property=page.property, cursor=page.cursor, limit=128)

    def hints(self, target, tree, *, compact=False, inline_children=()):
        refs = {node: ref for ref, node in tree.refs.items()}
        kinds = RELATIONS[tree.native['platform']]
        lines = []
        inline_children = set(inline_children) if compact else set()
        for relation in tree.native['relations']:
            if relation['certainty'] != 'confirmed' or 'cursor' not in relation:
                continue
            page = ContentPage(replace(target, node=relation['source'], capture=None), relation['property'],
                               relation['cursor'],
                               relation.get('offset', 0) + len(relation['targets']), relation['total'])
            alias = self._aliases.get(page)
            ref = refs[relation['source']]
            label = kinds.get(page.property, page.property)
            if ref in inline_children and label == 'children':
                # A successfully rendered branch already supplies an offset
                # entry, or a first-page menu entry with no consumed prefix.
                continue
            if alias is not None:
                lines.append(f'{ref} {label}, next offset {page.offset} of {page.total}: '
                             f'app.element({json_string(ref)}).read({{"cursor": {json_string(alias)}}})')
            else:
                lines.append(f'{ref} {label}: continuation expired; read this element again.')
        return '' if not lines else '\nContinue reading (live order; changed prefixes expire):\n' + '\n'.join(lines)
