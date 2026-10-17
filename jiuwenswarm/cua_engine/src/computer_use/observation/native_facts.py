"""Native graph → semantic nodes → existing conservative display projection.

The graph remains separate from its tree view. Extra edges, failed fields and
unknown properties are explicit annotations, never guessed containment/input.
"""
import json
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from ..native_semantics import FIELDS, PROPERTIES, RELATIONS, control_capabilities, role_name
from ..protocol.facts import decode_facts
from ..protocol.primitives import finite, safe_integer
from .accounting import account_fields
from .compact import observation_warnings
from .fact_groups import reference_set, render_properties
from .native_values import display_native_value
from .presentation_policy import editing, known_metadata
from .relations import RelationPresentation, render_relations
from .render import render_tree
from .semantics import compose_controls
from .table import cell_ranges, selection_summaries, uia_cell_range
from .uncompressed import UncompressedFacts, branch_references, literal
from .windows_read import read_summaries


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class DisplayReferences:
    """Session aliases for collection/windows; element views are renumbered."""

    def __init__(self, session, limit=16384):
        self.session = session
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid display reference capacity")
        self._refs, self._next, self._limit = OrderedDict(), 0, limit

    def reference(self, native):
        if native not in self._refs:
            self._refs[native] = str(self._next)
            self._next += 1
            while len(self._refs) > self._limit:
                self._refs.popitem(last=False)
        self._refs.move_to_end(native)
        return self._refs[native]


@dataclass
class SemanticSnapshot:
    native: dict
    roots: list
    refs: dict
    annotations: list
    fields: dict
    text_refs: dict
    scope_kind: str | None = None
    root_contexts: dict = dataclass_field(default_factory=dict)
    # The same source must supply the displayed Value and its continuation.
    # Other native value interfaces remain in fields/native, never substituted.
    value_fields: dict = dataclass_field(default_factory=dict)
    driver_features: frozenset = dataclass_field(default_factory=frozenset)

    def render(self, *, trace=False, compact=False):
        parts, projections, context = [], [], None
        supplements, relation_supplements = {}, {}
        uncompressed = None
        native_nodes = {node['node']: node for node in self.native['nodes']}
        relations = RelationPresentation(self.native, self.refs)
        relation_sources = []
        for root in self.roots:
            members = branch_references(root, self.refs)
            readable_text = set()
            for ref in members:
                key = self.value_fields.get(ref)
                if key is None:
                    continue
                native = native_nodes[self.refs[ref]]
                if not isinstance(native['values'].get(key), str):
                    continue
                if key not in native['fieldStatus'] or 'textRef' in native['fieldStatus'][key]:
                    readable_text.add(ref)
            try:
                rendered = render_tree(
                    compose_controls(root, fact_backed=True),
                    dict(includeBackground=True, recordFields=True, nativeFacts=True, readableTextRefs=readable_text,
                         recordStages=trace, compact=compact))
                residuals, accounting = account_fields(members, self.refs, self.fields, native_nodes, rendered)
                relation_groups, displayed_relations = relations.render(members, rendered)
                rendered['factAccounting'] = accounting
            except (ArithmeticError, LookupError, TypeError, ValueError, AssertionError, RecursionError) as error:
                # Only pure composition/projection/formatting is fallible here.
                # Protocol decoding, identity, I/O, cancellation and resource
                # exhaustion are outside this recovery boundary.
                if uncompressed is None:
                    uncompressed = UncompressedFacts(self.native, self.refs)
                rendered = uncompressed.render(members, error)
                _, displayed_relations = relations.render(members, rendered)
            else:
                # Keep native objects distinct. Only identical metadata from
                # successful roots shares one snapshot-wide supplement; failed
                # roots retain the full, self-contained fallback above.
                for label, items in residuals.items():
                    supplements.setdefault(label, []).extend(items)
                for relation, items in relation_groups.items():
                    relation_supplements.setdefault(relation, []).extend(items)
            relation_sources.extend(displayed_relations)
            related = self.root_contexts.get(root['ref'])
            if related is not None:
                if related != context:
                    parts.append('Related elements (' + '; '.join(
                        (kind if kind.isidentifier() else literal(kind)) + ' of ' + owner
                        for owner, kind in related) + '):')
                parts[-1] += '\n' + '\n'.join('\t' + line for line in rendered['text'].splitlines())
            else:
                parts.append(rendered['text'])
            context = related
            projections.append(rendered)
        primary = parts.copy()
        if supplements:
            parts.append('Additional native facts (grouped by element reference):\n' +
                         '\n'.join('\t' + reference_set(items) + ' ' + label for label, items in supplements.items()))
        if relation_supplements:
            parts.append(('Native relations (source -> ordered targets; total="target-count" means '
                          'the length of that target '
                          'array):\n') + '\n'.join('\t' + line for line in render_relations(relation_supplements)))
        parts.extend(selection_summaries(self.native, self.refs))
        parts.extend(read_summaries(self.native, self.refs))
        if self.annotations:
            parts.append("Native facts:\n" + "\n".join("\t" + line for line in self.annotations))
        details = "\n\n".join(parts)
        if compact:
            primary.extend(selection_summaries(self.native, self.refs))
            primary.extend(read_summaries(self.native, self.refs))
            primary.extend(observation_warnings(self))
        # Annotations can expose a native reference outside the tree (a shared
        # child, unread label, etc.). Keep those bindings available for probing.
        return dict(text="\n\n".join(primary) if compact else details, details=details, refs=dict(self.refs),
                    projections=projections, fields=dict(self.fields), textRefs=dict(self.text_refs),
                    relationSources=relation_sources,
                    inlineChildRefs=[ref for part in projections for ref in part.get('inlineChildRefs', ())])


def normalize_facts(value, references, *, driver_features=(), scope_kind=None):
    if scope_kind not in (None, 'window', 'applicationMenu'):
        raise ValueError('Unknown observation scope kind')
    batch = decode_facts(value)
    if references.session != batch["session"]:
        raise ValueError("Native display references belong to another session")
    platform = batch["platform"]
    fields = FIELDS[platform]
    nodes, annotations, sources, text_refs, value_fields = {}, [], {}, {}, {}
    refs = {references.reference(node["node"]): node["node"] for node in batch["nodes"]}
    refs.update({references.reference(node): node for node in batch["unread"]})
    aliases = {token: ref for ref, token in refs.items()}
    grouped_annotations = {}
    flags = {"enabled", "selected", "expanded", "focused", "modal", "disclosing", "busy", "visible", "showing"}
    textual = {"name", "description", "identifier", "help", "url", "roleDescription", "placeholder", "selectedText"}
    for source in batch["nodes"]:
        token, raw = source["node"], source["values"]
        ref = references.reference(token)
        node = dict(ref=ref, role="unknown", children=[],
                    capabilities=control_capabilities(platform, source, driver_features, scope_kind=scope_kind))
        nodes[token] = node
        # Text and numeric Value are independent native interfaces. Prefer the
        # platform's readable text property for the display value, regardless
        # of wire object order. Accounting retains any different numeric fact.
        value_properties = (('RangeValue.Value', 'Text.DocumentRange',
                             'Value.Value') if platform == 'windows' else (PROPERTIES[platform].get('value'), ))
        entries = [(key, item) for key, item in raw.items() if key not in value_properties]
        entries.extend((key, raw[key]) for key in value_properties if key in raw)
        for key, item in entries:
            field = fields.get(key)
            recognized = True
            projections = None
            if field in ("role", "subrole") and isinstance(item, str):
                node[field] = role_name(platform, item)
                if node[field] == "unknown":
                    recognized = False
                    grouped_annotations.setdefault((key, encoded(item)), []).append(ref)
                    node["preserveGroup"] = True
            elif field in textual and isinstance(item, str):
                node[field] = item
            elif field in flags and isinstance(item, bool):
                node[field] = item
            elif platform == 'windows' and field == 'toggleState' and item in ('On', 'Off', 'Indeterminate'):
                # UIA Toggle and Value patterns are independent. An empty text
                # value must not erase a control's separate on/off state.
                node['toggleState'] = {'On': 'on', 'Off': 'off', 'Indeterminate': 'mixed'}[item]
            elif platform == 'windows' and field == 'expandCollapseState' and item in ('Expanded', 'Collapsed'):
                node['expanded'] = item == 'Expanded'
                projections = [dict(field='expanded', normalized=node['expanded'])]
            else:
                scalar_value = field == "value" and (isinstance(item, str) or isinstance(item, bool) or finite(item))
                if scalar_value:
                    node[field] = item if isinstance(item, str) else encoded(item)
                    value_fields[ref] = key
                    if type(item) in (int, float):
                        node["valueIsNumber"] = True
                    else:
                        node.pop("valueIsNumber", None)
                # Keep conversion lazy: earlier native value variants take precedence.
                # pylint: disable-next=not-use-assignment-expressions
                elif field == 'value' and (display := display_native_value(platform, item, aliases)) is not None:
                    node[field] = display
                    # The readable projection cannot communicate the native type
                    # tag/epoch or opaque object token by itself. Keep that carrier
                    # in same-capture details as well as the useful main-tree value.
                    grouped_annotations.setdefault((key, encoded(item)), []).append(ref)
                # Keep conversion lazy: earlier native value variants take precedence.
                # pylint: disable-next=not-use-assignment-expressions
                elif platform == "linux" and field == "cellPosition" and (ranges := cell_ranges(source,
                                                                                                item)) is not None:
                    node.update(ranges)
                    projections = [dict(field=name, normalized=deepcopy(value)) for name, value in ranges.items()]
                # Keep conversion lazy: earlier native value variants take precedence.
                # pylint: disable-next=not-use-assignment-expressions
                elif platform == 'windows' and (position := uia_cell_range(source, key)) is not None:
                    name, value = position
                    node[name] = value
                    projections = [dict(field=name, normalized=deepcopy(value))]
                else:
                    valid_native_range = field in ("selectionRange", "rowRange", "columnRange") and isinstance(
                        item, dict) and set(item) == {"location", "length"} and all(
                            safe_integer(item[k]) and item[k] >= 0
                            for k in item) and safe_integer(item["location"] + item["length"])
                    if valid_native_range:
                        node[field] = dict(item)
                    elif field in ("disclosureLevel", "rowCount", "columnCount", "index",
                                   "selectedChildCount") and safe_integer(item) and item >= 0:
                        node[field] = item
                    else:
                        recognized = False
                        if not known_metadata(field, item):
                            node["preserveGroup"] = True
                        grouped_annotations.setdefault((key, encoded(item)), []).append(ref)
            if projections is not None:
                sources[(ref, key)] = dict(node=token, field=None, representation="projection", projections=projections)
            else:
                sources[(ref, key)] = dict(node=token, field=field if recognized else None,
                                           representation="field" if recognized else "annotation",
                                           **({'normalized': deepcopy(node[field])} if recognized else {}))
        for key, status in source["fieldStatus"].items():
            label = status['state']
            if status.get("detail"):
                label += " (" + encoded(status["detail"]) + ")"
            if "textRef" in status:
                text_refs[(ref, key)] = dict(status)
                label += "; captured text available"
                label += (" via this element's readText()"
                          if value_fields.get(ref) == key else "; no public continuation for this field")
            if 'totalUnits' in status:
                label += '; ' + str(status['totalUnits']) + ' UTF-16 units'
            grouped_annotations.setdefault((key, label), []).append(ref)
            if status["state"] in ("error", "timeout", "truncated"):
                # Protect unknown content from folding without misreporting a
                # property failure as missing Children or suggesting a rewalk.
                # A failed incidental range/level on non-editable static UI
                # is retained by reference in Native facts. It is not evidence
                # of an independent control or of missing containment.
                incidental = (fields.get(key) in ('selectionRange', 'disclosureLevel') and not editing(node)
                              and node['role'] not in ('row', 'cell', 'outline')
                              and not any(node.get(k) for k in ('selected', 'focused', 'selectedText')))
                if not incidental:
                    node["preserveGroup"] = True
        actions = source["actions"]
        node["actions"] = [{key: action[key]
                            for key in ("name", "description") if key in action} for action in actions["items"]]
        # The complete native catalogue (including descriptionStatus, detail,
        # count and coverage) is emitted once per distinct record by accounting.
        # Repeating its diagnostics here would add one line per action per node.
        if actions["state"] != "complete":
            node["actionsUnavailable"] = True
            count = f" ({len(actions['items'])}/{actions['total']})" if "total" in actions else ""
            summary = actions['state'] + count + (': ' + encoded(actions['detail']) if 'detail' in actions else '')
            grouped_annotations.setdefault(('actions', summary), []).append(ref)

    children = {token: [] for token in nodes}
    # Build a canonical containment view and composition hints. Only the final
    # projected hierarchy can determine which raw edges it actually expresses.
    extra_edges = []
    for relation in batch["relations"]:
        token = relation["source"]
        source = nodes[token]
        targets, prop = relation["targets"], relation["property"]
        kind = RELATIONS[platform].get(prop, prop)
        target_refs = [references.reference(t) for t in targets]
        complete = relation["state"] == "complete"
        confirmed = relation["certainty"] == "confirmed"
        # An explicit no-value is neither a failed query nor an unqueried edge.
        if relation['state'] == 'absent' or relation['state'] == 'unsupported' and kind != 'children':
            continue
        if kind == "children" and confirmed:
            children[token].extend(targets)
            if 'nativeNodePages' in driver_features and not complete and 'total' in relation:
                source['childCount'] = relation['total']
                source['childrenStart'] = relation.get('offset', 0)
                source['childrenEnd'] = source['childrenStart'] + len(targets)
                if 'cursor' in relation:
                    source['nextChildOffset'] = source['childrenEnd']
            if not complete or any(t not in nodes for t in targets):
                source["partialReason"] = "native children partly collected; see Relation details"
        else:
            confirmed_title = kind == "labelledBy" and confirmed and complete and len(
                targets) == 1 and targets[0] in nodes
            if confirmed_title:
                source["titleRef"] = target_refs[0]
                extra_edges.append((relation, kind, target_refs))
            elif kind == "labels" and confirmed:
                source["labelTargets"] = target_refs
                source["incompleteLabelRelation"] = not complete or any(t not in nodes for t in targets)
                extra_edges.append((relation, kind, target_refs))
            else:
                confirmed_disclosure = kind == "disclosedBy" and confirmed and complete and len(
                    targets) == 1 and targets[0] in nodes
                if confirmed_disclosure:
                    source["disclosedBy"] = target_refs[0]
                    extra_edges.append((relation, kind, target_refs))
                else:
                    extra_edges.append((relation, kind, target_refs))
        if not complete or not confirmed:
            source["preserveGroup"] = True

    placed, active = set(), set()

    def visit(token):
        # The native graph is bounded by object count, not Python stack depth.
        # Construct containment iteratively so a deep layout cannot silently
        # become disconnected roots. Projection has its own lossless fallback.
        placed.add(token)
        active.add(token)
        pending = [(token, iter(children[token]))]
        while pending:
            parent, edges = pending[-1]
            child = next(edges, None)
            if child is None:
                active.remove(parent)
                pending.pop()
                continue
            node = nodes[parent]
            child_ref = references.reference(child)
            if child in active or child in placed or child not in nodes:
                reason = "cycle" if child in active else "shared" if child in placed else "unread"
                annotations.append(f"{node['ref']} contains {child_ref} ({reason})")
                node["preserveGroup"] = True
            else:
                node['children'].append(nodes[child])
                placed.add(child)
                active.add(child)
                pending.append((child, iter(children[child])))
        return nodes[token]

    roots, root_contexts = [], {}
    related_contexts = {}
    for relation, kind, target_refs in extra_edges:
        if relation['certainty'] == 'confirmed' and kind not in ('parent', 'window'):
            for target in relation['targets']:
                if target != relation['source']:
                    entry = (references.reference(relation['source']), kind)
                    if entry not in related_contexts.setdefault(target, []):
                        related_contexts[target].append(entry)
    for token in [*batch["roots"], *nodes]:
        if token not in placed:
            if token not in batch["roots"]:
                if token in related_contexts:
                    root_contexts[references.reference(token)] = tuple(related_contexts[token])
                else:
                    annotations.append(
                        f"{references.reference(token)} has no expanded containment path from the requested roots")
            roots.append(visit(token))
    annotations.extend(render_properties(grouped_annotations))
    if batch["unread"]:
        annotations.append("Uncollected: " + ", ".join(references.reference(t) for t in batch["unread"]))
    annotations.extend("Collection: " + encoded(reason) for reason in batch["stopReasons"])
    return SemanticSnapshot(batch, roots, refs, annotations, sources, text_refs, scope_kind, root_contexts,
                            value_fields, frozenset(driver_features))
