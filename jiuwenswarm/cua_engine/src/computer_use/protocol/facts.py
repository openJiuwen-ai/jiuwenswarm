"""Native fact batches: a bounded graph, not an already-compressed UI tree.

Values retain native property names/types. Absence, unsupported fields, failed
queries and previews are explicit; cycles and shared children are legal data.
"""
from copy import deepcopy

from .primitives import finite, nonempty, safe_integer
from .schema import array, count, enum, identity, obj, string, unknown

_status = obj(
    dict(state=enum("absent", "unsupported", "error", "timeout", "truncated"), detail=string(max=4096).optional(),
         textRef=identity.optional(), totalUnits=count.optional()))
_actions = obj(
    dict(
        state=enum("complete", "partial", "unavailable"),
        items=array(obj(dict(name=identity, description=string().optional(), descriptionStatus=_status.optional())),
                    max=128), total=count.optional(), detail=string(max=4096).optional()))
_node = obj(
    dict(node=identity, requestedFields=array(identity, max=128), values=unknown(), fieldStatus=unknown(),
         actions=_actions))
_relation = obj(
    dict(source=identity, property=identity, targets=array(identity, max=8192),
         certainty=enum("confirmed", "inferred", "unknown",
                        "conflicting"), state=enum("complete", "absent", "partial", "unsupported", "error",
                                                   "timeout"), offset=count.optional(), total=count.optional(),
         cursor=identity.optional(), detail=string(max=4096).optional()))
_batch = obj(
    dict(kind=enum("nodes"), session=identity, surface=identity, platform=enum("macos", "windows", "linux"),
         observed=obj(dict(id=identity, startedAt=count, completedAt=count)), roots=array(identity, min=1, max=64),
         nodes=array(_node, min=1, max=8192), requestedRelations=array(identity, max=32),
         relations=array(_relation, max=16384), unread=array(identity, max=8192), stopReasons=array(identity, max=128)))
_relation_page = obj(
    dict(kind=enum('relation'), session=identity, surface=identity, platform=enum('macos', 'windows', 'linux'),
         observed=obj(dict(id=identity, startedAt=count, completedAt=count)), relation=_relation))


def _validate_relation(relation):
    offset, targets = relation.get('offset', 0), relation['targets']
    end = offset + len(targets)
    if not safe_integer(end) or relation.get('total', end) < end:
        raise ValueError('protocol: native relation count contradicts its range')
    if (offset or 'cursor' in relation) and 'total' not in relation:
        raise ValueError('protocol: native relation continuation needs its observed total')
    complete_has_remainder = relation['state'] == 'complete' and (offset or 'cursor' in relation
                                                                  or relation.get('total', end) != end)
    if complete_has_remainder:
        raise ValueError('protocol: complete native relation cannot have an unread remainder')
    absent_has_targets = relation['state'] == 'absent' and (end or relation.get('total', 0) != 0
                                                            or 'cursor' in relation)
    if absent_has_targets:
        raise ValueError('protocol: absent native relation cannot contain targets')
    if 'cursor' in relation and (relation['state'] != 'partial' or end >= relation['total']):
        raise ValueError('protocol: relation cursor must point to an unread suffix')


def decode_relation_page(value):
    result = deepcopy(_relation_page.parse(value))
    if result['observed']['completedAt'] < result['observed']['startedAt']:
        raise ValueError('protocol: native collection interval is reversed')
    _validate_relation(result['relation'])
    return result


def _values(value, depth=0, budget=None):
    """Reject foreign objects and unbounded nesting even for in-process peers."""
    if budget is None:
        budget = [32768]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        raise ValueError("protocol: native field value exceeds structural budget")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 1_048_576:
            raise ValueError("protocol: native text requires an explicit preview")
        return
    if finite(value):
        return
    if isinstance(value, dict):
        for key, child in value.items():
            nonempty(key, "native property name")
            _values(child, depth + 1, budget)
        return
    if isinstance(value, list):
        for child in value:
            _values(child, depth + 1, budget)
        return
    raise ValueError("protocol: unsupported native field value")


def decode_facts(value):
    result = deepcopy(_batch.parse(value))
    stamp = result["observed"]
    if stamp["completedAt"] < stamp["startedAt"]:
        raise ValueError("protocol: native collection interval is reversed")
    nodes, unread = {node["node"] for node in result["nodes"]}, set(result["unread"])
    if len(nodes) != len(result["nodes"]) or len(unread) != len(result["unread"]) or nodes & unread:
        raise ValueError("protocol: duplicate native node identity")
    if len(set(result["roots"])) != len(result["roots"]) or not set(result["roots"]) <= nodes:
        raise ValueError("protocol: native roots must refer to distinct collected nodes")
    requested_relations = set(result["requestedRelations"])
    if len(requested_relations) != len(result["requestedRelations"]):
        raise ValueError("protocol: duplicate native relation request")
    for node in result["nodes"]:
        requested = set(node["requestedFields"])
        if len(requested) != len(node["requestedFields"]) or not isinstance(node["values"], dict) or not isinstance(
                node["fieldStatus"], dict):
            raise ValueError("protocol: invalid native property sets")
        values, statuses = node["values"], node["fieldStatus"]
        if set(values) | set(statuses) != requested:
            raise ValueError("protocol: every requested field needs a value or an explicit status")
        _values(values)
        # A typed AXValue may refer to another native object, rather than hold
        # editor text. Its token follows the same declared-identity boundary as
        # relationship targets; it never grants containment or input authority.
        for value in values.values():
            if (result['platform'] == 'macos' and isinstance(value, dict) and value.get('nativeType') == 'AXUIElement'):
                if set(value) != {'nativeType', 'node'
                                  } or not isinstance(value['node'], str) or value['node'] not in nodes | unread:
                    raise ValueError('protocol: native value references an undeclared identity')
        for name, raw in statuses.items():
            status = _status.parse(raw)
            if status["state"] == "truncated":
                if not isinstance(values.get(name), str) or not ("textRef" in status or status.get("detail")):
                    raise ValueError("protocol: text previews need a text reference or an explicit capture limitation")
            elif name in values:
                raise ValueError("protocol: failed or absent native field cannot also have a value")
            statuses[name] = status
        actions = node["actions"]
        total = actions.get("total", len(actions["items"]))
        if total < len(actions["items"]) or actions["state"] == "complete" and total != len(actions["items"]):
            raise ValueError("protocol: native action coverage contradicts its items")
        for action in actions["items"]:
            if "descriptionStatus" not in action:
                if "description" not in action:
                    raise ValueError("protocol: native action description needs a value or explicit status")
                continue
            status = action["descriptionStatus"]
            if status["state"] == "truncated":
                if "description" not in action or not (status.get("textRef") or status.get("detail")):
                    raise ValueError("protocol: truncated action description needs a preview and coverage")
            elif "description" in action:
                raise ValueError("protocol: failed action description cannot also have a value")
    edge_count = 0
    seen_relations = {node: set() for node in nodes}
    for relation in result["relations"]:
        edge_count += len(relation["targets"])
        if edge_count > 32768:
            raise ValueError("protocol: native relation edge budget exceeded")
        if relation["source"] not in nodes or not set(relation["targets"]) <= nodes | unread:
            raise ValueError("protocol: native relation references undeclared identities")
        seen = seen_relations[relation["source"]]
        if relation["property"] not in requested_relations or relation["property"] in seen:
            raise ValueError("protocol: undeclared or duplicate native relation")
        seen.add(relation["property"])
        _validate_relation(relation)
    if any(seen != requested_relations for seen in seen_relations.values()):
        raise ValueError("protocol: every requested relation needs an explicit result for each collected node")
    # Schema unknown() is deliberately non-coercing; detach its nested payload
    # so downstream semantic normalization cannot change the native evidence.
    return result
