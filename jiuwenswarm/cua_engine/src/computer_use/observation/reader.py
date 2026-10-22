"""The staged v5 native read pipeline, independent of Agent/MCP and UI input."""
from dataclasses import dataclass, replace

from ..errors import ComputerUseError
from ..execution.native import Binding
from ..native_semantics import (
    FIELDS,
    RELATIONS,
    TABLE_FIELDS,
    WINDOWS_ON_DEMAND_FIELDS,
    WINDOWS_READ_V2_FIELDS,
    followed_relations,
)
from ..protocol.primitives import nonempty, safe_integer
from ..protocol.text import text_page
from .native_facts import DisplayReferences, normalize_facts


@dataclass(frozen=True)
class CapturedField:
    session: str
    value: str | None = None
    text_ref: str | None = None
    total_units: int | None = None


@dataclass(frozen=True)
class ChildContinuation:
    cursor: str
    offset: int
    total: int


def captured_children(snapshot, reference, *, relations=None):
    token = snapshot.refs.get(reference)
    if relations is None:
        relations = {
            r['source']: r
            for r in snapshot.native['relations']
            if RELATIONS[snapshot.native['platform']].get(r['property']) == 'children'
        }
    relation = relations.get(token)
    if relation is None or relation['certainty'] != 'confirmed' or 'cursor' not in relation:
        return None
    return ChildContinuation(relation['cursor'],
                             relation.get('offset', 0) + len(relation['targets']), relation['total'])


def captured_field(snapshot, reference, field, *, nodes=None):
    """Retain only the immutable field, not a whole tree for each Element."""
    source = snapshot.text_refs.get((reference, field))
    if source is not None and 'textRef' in source:
        return CapturedField(snapshot.native['session'], text_ref=source['textRef'],
                             total_units=source.get('totalUnits'))
    token = snapshot.refs.get(reference)
    node = nodes.get(token) if nodes is not None else next(
        (node for node in snapshot.native['nodes'] if node['node'] == token), None)
    if node is None or field in node['fieldStatus'] or not isinstance(node['values'].get(field), str):
        return None
    return CapturedField(snapshot.native['session'], value=node['values'][field])


class NativeReader:

    def __init__(self, port):
        self.port = port
        self._references = None

    async def read(self, target, *, fields=None, max_nodes=1000, max_depth=None, timeout_ms=30000, children_page=None,
                   relation_page=None, scope_kind=None):
        self.validate(target, max_nodes, max_depth)
        async with self.port.calls.open(target, timeout_ms) as read:
            return await self.collect(read, fields=fields, max_nodes=max_nodes, max_depth=max_depth,
                                      children_page=children_page, relation_page=relation_page, scope_kind=scope_kind)

    @staticmethod
    def validate(target, max_nodes, max_depth):
        if target.node is None:
            raise ComputerUseError(
                dict(
                    code="observation_incomplete", effect="none", target="native observation",
                    detail=('This surface has no accessible root; its window identity remains '
                            'available for visual '
                            'observation.'),
                    next="use a supported window capture route; do not infer that the window has no content"))
        invalid_read_budget = (type(max_nodes) is not int or not 1 <= max_nodes <= 2000
                               or max_depth is not None and (type(max_depth) is not int or not 0 <= max_depth <= 2000))
        if invalid_read_budget:
            raise ValueError("Invalid native read budget")

    async def collect(self, read, *, node=None, fields=None, max_nodes=1000, max_depth=None, children_page=None,
                      relation_page=None, scope_kind=None):
        """Collect inside an existing node or window read operation."""
        if read.target.capture is not None or node is not None and read.target.node not in (None, node):
            raise ValueError('Node collection cannot change an existing node or image scope')
        target = replace(read.target, node=node if node is not None else read.target.node)
        self.validate(target, max_nodes, max_depth)
        platform = self.port.connection.platform
        features = getattr(self.port.connection, 'capabilities', ())
        if max_depth is None:
            # A path through N distinct collected objects has at most N-1
            # edges. Native collectors are iterative and deduplicate identity;
            # the node/time budgets already bound deep layout hierarchies.
            max_depth = max_nodes - 1 if 'nativeNodeDepthBudget' in features else 40
        elif max_depth > 40 and 'nativeNodeDepthBudget' not in features:
            raise ValueError('This driver supports an explicit depth of at most 40')
        options = {} if children_page is None else dict(children_page=children_page)
        if relation_page is not None:
            options['relation_page'] = relation_page
        # A root's children page and its independent content relationships
        # are separate read dimensions. Keep the same bounded content profile
        # for App.read and Element.read, including continued children pages.
        if fields is None:
            relations = followed_relations(platform, features)
            if relations:
                options['follow_relations'] = list(relations)
        if fields is None:
            # Popup ownership is operation-specific receiver evidence. Query
            # it for a selected editor, not every node of a display read (or
            # older drivers which do not implement nativeVirtualFocus).
            fields = [key for key, semantic in FIELDS[platform].items() if semantic != 'hasPopup']
            if platform == 'windows' and 'nativeUiaReadV2' not in features:
                fields = [key for key in fields if key not in WINDOWS_READ_V2_FIELDS]
            if platform == 'windows':
                fields = [key for key in fields if key not in WINDOWS_ON_DEMAND_FIELDS]
                if 'nativeWindowReadiness' not in features:
                    fields = [key for key in fields if key != 'Win32.ClientRectangle']
            fields += ["AXDocument"] if platform == "macos" else []
            if platform == 'linux' and 'nativeTableFacts' not in features:
                fields = [key for key in fields if key != 'visible']
            if not {'nativeBusyFacts', 'nativeAdvertisedFields'} <= set(features):
                fields = [key for key in fields if FIELDS[platform].get(key) != 'busy']
            else:
                options['advertised_fields'] = [key for key in fields if FIELDS[platform].get(key) == 'busy']
            if platform == 'macos' and 'nativeActuators' not in features:
                editing_fields = {'editable', 'readOnly', 'focusWritable', 'valueWritable', 'textWritable'}
                fields = [key for key in fields if FIELDS[platform].get(key) not in editing_fields]
            if 'nativeScroll' not in getattr(self.port.connection, 'capabilities', ()):
                fields = [key for key in fields if FIELDS[platform].get(key) != 'orientation']
            if not {'nativeTableFacts', 'nativeAdvertisedFields'} <= set(features):
                fields = [key for key in fields if FIELDS[platform].get(key) not in TABLE_FIELDS]
            else:
                # Native providers may return successful zero defaults for
                # properties they do not advertise. Query declarations, not
                # an application/role allowlist. Explicit profiles stay raw.
                advertised = [key for key in fields if FIELDS[platform].get(key) in TABLE_FIELDS]
                if advertised:
                    options.setdefault('advertised_fields', []).extend(advertised)
        batch = await self.port.nodes(read.token, target, fields, max_nodes, max_depth, read.remaining(), **options)
        if self._references is None or self._references.session != target.session:
            self._references = DisplayReferences(target.session)
        result = normalize_facts(batch, self._references, driver_features=getattr(self.port.connection, 'capabilities',
                                                                                  ()), scope_kind=scope_kind)
        read.remaining()
        return result

    async def read_relation(self, target, relation_name, *, cursor=None, limit=128, timeout_ms=30000):
        """Read native relation identities; do not reinterpret them as children.

        A returned target can be inspected with an explicit Binding in the same
        scope. Continuations refer to observed prefixes, never guessed offsets.
        """
        if target.node is None or target.capture is not None:
            raise ValueError('Native relation reads require an observed node binding')
        nonempty(relation_name, 'native relation property')
        if cursor is not None:
            nonempty(cursor, 'native relation cursor')
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError('Native relation page limit must be an integer in 1..2000')
        async with self.port.calls.open(target, timeout_ms) as read:
            return await self.port.relation(read.token, read.target, relation_name, cursor, limit, read.remaining())

    async def read_text(self, snapshot, reference, field, *, offset=0, length=4096, timeout_ms=30000):
        source = captured_field(snapshot, reference, field)
        if source is None:
            raise ValueError("This observed field has no immutable full-text capture")
        return await self.read_captured_text(source, offset=offset, length=length, timeout_ms=timeout_ms)

    async def read_captured_text(self, source, *, offset=0, length=4096, timeout_ms=30000):
        invalid_text_budget = (not safe_integer(offset) or offset < 0 or not safe_integer(length)
                               or not 2 <= length <= 16384 or type(timeout_ms) is not int
                               or not 1 <= timeout_ms <= 120000)
        if invalid_text_budget:
            raise ValueError("Invalid captured text range or budget")
        session = source.session
        # A capture outlives its live element, but never its driver session.
        # Reading it needs no AX/window binding and must not restart a driver.
        if self.port.connection.session is None or self.port.connection.session != session:
            raise RuntimeError("The text capture's driver session ended")
        if source.value is not None:
            with self.port.calls.claim(timeout_ms):
                return text_page(source.value, offset, length)
        if source.text_ref is None:
            raise ValueError("This observed field has no immutable full-text capture")
        target = Binding(session, None, None, None)
        async with self.port.calls.open(target, timeout_ms) as read:
            page = await self.port.captured_text(read.token, read.target, source.text_ref, offset, length,
                                                 read.remaining())
            if page["offset"] != offset or source.total_units is not None and page["total"] != source.total_units:
                raise ValueError("Native text page changed its immutable capture range")
            return page
