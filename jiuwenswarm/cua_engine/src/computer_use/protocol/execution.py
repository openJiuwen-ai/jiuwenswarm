"""Strict wire DTOs for protocol v5 execution; no platform or policy imports."""
import json
import math

from .action_context import decode_action_context
from .captures import capture_metadata, decode_capture, decode_stamp
from .desktop import decode_desktop_observation
from .directory import _application, decode_directory
from .facts import decode_facts, decode_relation_page
from .schema import array, boolean, enum, identity, number, obj, string, union, unknown
from .text import decode_text_page

knowledge = enum("yes", "no", "unknown")
nullable_id = identity.nullable()
actuator = union(
    obj(dict(node=identity, relation=enum("containedEditor", "controlPart"))),
    obj(dict(node=identity, region=identity, bar=identity, relation=enum("scrollPage"))),
    obj(
        dict(node=identity, region=identity, relation=enum("scrollPointer"), contentPath=array(identity, min=2,
                                                                                               max=40).optional())))


def validate_actuator_path(selected):
    path = selected.get('contentPath') if selected is not None else None
    if path is not None and (path[0] != selected['node'] or len(set(path)) != len(path)):
        raise ValueError('protocol: a scroll content path must start at its hit and contain distinct nodes')


binding = obj(
    dict(session=identity, instance=nullable_id, rootWindow=nullable_id, surface=nullable_id, node=nullable_id,
         capture=nullable_id))
application_binding = obj(dict(session=identity, application=identity))
operation_target = union(binding, application_binding)
context = obj(
    dict(foregroundSurface=nullable_id, keyboardSurface=nullable_id, focusedNode=nullable_id,
         focusAncestors=array(identity, max=128), editor=nullable_id, editorOwner=nullable_id, selectionKnown=knowledge,
         geometryCurrent=knowledge, windowRoute=knowledge, pointerRoute=knowledge.optional(),
         pointerHit=nullable_id.optional()))
intent_kinds = ("click", "focus", "insert", "replace", "paste", "key", "openMenu", "action", "scroll", "drag", "launch")
intents = enum(*intent_kinds)
pending_conditions = ('receiver', 'selection', 'geometry', 'foreground', 'enabled')
primitive = obj(
    dict(
        id=identity, unit=enum("native_action", "native_text_edit", "key_sequence", "clipboard_paste",
                               "pointer_sequence", "native_scroll", "prepare_receiver",
                               "launch_application"), channel=enum("object", "editor", "keyboard", "pointer"),
        supported=knowledge, readiness=knowledge, pending=array(enum(*pending_conditions), max=5),
        backgroundPreparation=knowledge, action=obj(dict(name=identity, description=string().optional())).optional(),
        unavailableReason=identity.optional()))

cleanup = enum("complete", "failed", "unknown")
# These are wire-contract meanings, not a fallback policy. Both decoded driver
# declarations and in-process NativePort implementations obey the same rules.
unit_contracts = {
    "native_action": ({"object"}, {"click", "openMenu", "action"}),
    "native_text_edit": ({"object", "editor"}, {"insert", "replace"}),
    "key_sequence": ({"keyboard"}, {"insert", "replace", "key"}),
    "clipboard_paste": ({"keyboard"}, {"paste"}),
    "pointer_sequence": ({"pointer"}, {"click", "openMenu", "scroll", "drag"}),
    "native_scroll": ({"object"}, {"scroll"}),
    # A preparation-only declaration can establish context before an editor
    # exposes its editing capability. It never authorizes content dispatch.
    "prepare_receiver": ({"object", "editor", "keyboard"}, {"focus", "insert", "replace", "paste"}),
    "launch_application": ({"object"}, {"launch"}),
}

_intent = obj(dict(kind=enum('read', *intent_kinds), parameters=unknown()))
_begin = obj(dict(mode=enum('read', 'write'), target=operation_target, intent=_intent))
_unit_command = obj(
    dict(kind=enum(*unit_contracts), primitiveId=identity, target=operation_target, foreground=boolean()))
_capture_request = obj(
    dict(operation=identity, target=binding, kind=enum('window', 'desktop'),
         maxDimension=number(integer=True, min=1, max=4096)))


def decode_capture_request(value):
    result = _capture_request.parse(value)
    target = result['target']
    if target['node'] is not None or target['capture'] is not None:
        raise ValueError('protocol: capture needs a read scope, not an element or old image')
    window = result['kind'] == 'window'
    if any((target[key] is not None) != window for key in ('instance', 'rootWindow', 'surface')):
        raise ValueError('protocol: capture kind and target scope disagree')
    return result


def decode_begin(value):
    """Bind the original payload once, before observing or preparing a route."""
    result = _begin.parse(value)
    intent, mode = result['intent'], result['mode']
    if not isinstance(intent['parameters'], dict):
        raise ValueError('protocol: native intent parameters must be an object')
    try:
        json.dumps(intent['parameters'], allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError('protocol: native intent parameters must be JSON values') from error
    if (mode == 'read') != (intent['kind'] == 'read'):
        raise ValueError('protocol: native intent and operation mode disagree')
    if intent['kind'] in ('read', 'launch', 'focus') and intent['parameters']:
        raise ValueError('protocol: this operation intent has no parameters')
    if ('application' in result['target']) != (intent['kind'] == 'launch'):
        raise ValueError('protocol: an installation target permits only application launch')
    return result


def decode_unit_command(value):
    """Only the selected contract crosses the write boundary.

    InputContext is an observation, never client-supplied dispatch authority.
    Parameters are validated by the chosen native unit before preparation.
    """
    result = _unit_command.parse(value)
    installation = 'application' in result['target']
    if installation != (result['kind'] == 'launch_application'):
        raise ValueError('protocol: an installation target permits only application launch')
    if installation and result['foreground']:
        raise ValueError('protocol: application launch has no activation or arbitrary parameters')
    return result


# Request shapes are checked as well as responses. In particular an empty
# constraint list is data, not an omitted optional field in one adapter.
receiver = union(obj(dict(relation=enum('virtualFocus'), popup=identity, via=enum('owns', 'linked'))),
                 obj(dict(relation=enum('containedFocus'), node=identity)))
selection_basis = array(
    obj(
        dict(observed=identity, node=identity, fields=array(identity, max=32), actions=boolean(),
             relations=array(identity, max=16))), min=1, max=512)


def decode_selection_basis(value):
    result = selection_basis.parse(value)
    seen = set()
    for entry in result:
        key = entry['observed'], entry['node']
        if key in seen or not (entry['fields'] or entry['actions'] or entry['relations']):
            raise ValueError('protocol: duplicate or empty selection dependency')
        seen.add(key)
        if any(len(entry[k]) != len(set(entry[k])) for k in ('fields', 'relations')):
            raise ValueError('protocol: duplicate selection dependency property')
    return result


_inspect_queries = {
    "launch":
    obj(dict(kind=enum("launch"))),
    "applications":
    obj(dict(kind=enum("applications"))),
    "surfaces":
    obj(dict(kind=enum("surfaces"), application=identity, includeAccessibility=boolean().optional())),
    "roots":
    obj(dict(kind=enum("roots"), application=identity)),
    "inputContext":
    obj(
        dict(kind=enum("inputContext"), within=array(identity, max=16), actuator=actuator.optional(),
             receiver=receiver.optional(), basis=selection_basis.optional())),
    "nodes":
    obj(
        dict(
            kind=enum("nodes"), fields=array(identity, min=1,
                                             max=128), advertisedFields=array(identity, min=1, max=128).optional(),
            followRelations=array(identity, min=1, max=16).optional(), maxNodes=number(integer=True, min=1, max=2000),
            maxDepth=number(integer=True, min=0, max=2000),
            childrenPage=obj(dict(cursor=nullable_id, limit=number(integer=True, min=1, max=1999))).optional(),
            relationPage=obj(dict(property=identity, cursor=nullable_id, limit=number(integer=True, min=1,
                                                                                      max=1999))).optional())),
    "relation":
    obj(dict(kind=enum("relation"), property=identity, cursor=nullable_id, limit=number(integer=True, min=1,
                                                                                        max=2000))),
    "capturedText":
    obj(
        dict(kind=enum("capturedText"), textRef=identity, offset=number(integer=True, min=0, max=9_007_199_254_740_991),
             length=number(integer=True, min=2, max=16_384))),
}


def decode_inspect_query(value):
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value["kind"] not in _inspect_queries:
        raise ValueError("protocol: unknown native inspection query")
    result = _inspect_queries[value["kind"]].parse(value)
    if 'advertisedFields' in result and (len(set(result['advertisedFields'])) != len(result['advertisedFields'])
                                         or not set(result['advertisedFields']) <= set(result['fields'])):
        raise ValueError('protocol: advertised fields must be a distinct subset of the requested fields')
    if 'childrenPage' in result and 'relationPage' in result:
        raise ValueError('protocol: select one root relation page')
    page = result.get('relationPage', result.get('childrenPage'))
    if page is not None and (result['maxDepth'] < 1 or page['limit'] >= result['maxNodes']):
        raise ValueError('protocol: a relation page needs depth and space for its root and targets')
    if 'basis' in result:
        decode_selection_basis(result['basis'])
        if 'actuator' not in result and 'receiver' not in result:
            raise ValueError('protocol: selection dependencies require a selected actuator or receiver')
    selected = result.get('actuator')
    validate_actuator_path(selected)
    missing_receiver_dependencies = 'receiver' in result and ('basis' not in result or selected is not None
                                                              and selected['relation'] != 'containedEditor')
    if missing_receiver_dependencies:
        raise ValueError('protocol: a related receiver requires selection dependencies')
    if selected is not None and selected['relation'] == 'scrollPointer' and not result.get('basis'):
        raise ValueError('protocol: an element scroll pointer requires selection dependencies')
    if selected is not None and selected['relation'] == 'scrollPage' and selected['node'] in (selected['region'],
                                                                                              selected['bar']):
        raise ValueError('protocol: a page button must be distinct from its region and bar')
    for key in ("within", "fields", "followRelations"):
        if key in result and len(set(result[key])) != len(result[key]):
            raise ValueError("protocol: duplicate native query " + key)
    return result


# Explicit native/observation contract fields stay aligned with their caller.
# pylint: disable-next=huawei-too-many-arguments
def validate_primitive_contract(unit, channel, supported, readiness, pending, action):
    channels, _ = unit_contracts[unit]
    if supported not in ('yes', 'no', 'unknown') or readiness not in ('yes', 'no', 'unknown'):
        raise ValueError("protocol: invalid native support or readiness evidence")
    if channel not in channels:
        raise ValueError("protocol: native unit cannot implement the declared channel")
    if len(set(pending)) != len(pending) or not set(pending) <= set(pending_conditions):
        raise ValueError("protocol: invalid or duplicate native readiness condition")
    inconsistent_readiness = readiness == 'yes' and pending or supported == 'yes' and readiness != 'yes' and not pending
    if inconsistent_readiness:
        raise ValueError("protocol: native readiness and pending conditions disagree")
    if (unit == 'native_action') != (action is not None):
        raise ValueError("protocol: inconsistent native action declaration")
    if unit == 'launch_application' and pending:
        raise ValueError("protocol: application launch cannot acquire UI input requirements")


def _validate_primitive(p):
    validate_primitive_contract(p['unit'], p['channel'], p['supported'], p['readiness'], p['pending'], p.get('action'))


_schemas = {
    "begin":
    obj(dict(operation=identity, target=operation_target, remainingMs=number(integer=True, min=0, max=120000))),
    "inspect":
    obj(
        dict(kind=enum("inputContext"), binding=binding, observed=identity, targetLabel=identity, identity=knowledge,
             withinScope=knowledge, enabled=knowledge, blockedBy=array(identity,
                                                                       max=128), primitives=array(primitive, max=256),
             input=context, nativeRole=nullable_id.optional(), actionsComplete=boolean().optional(),
             applicationMenu=boolean().optional(), within=array(identity, max=16).optional(),
             actuator=actuator.nullable().optional(), basis=selection_basis.optional(), receiver=receiver.optional())),
    "execute":
    obj(
        dict(target=operation_target, dispatch=enum("notDispatched", "dispatched", "partial",
                                                    "unknown"), detail=identity, preparation=array(identity, max=128),
             confirmation=enum("notRequested", "confirmed", "contradicted", "unavailable"), cleanup=cleanup,
             errorCode=nullable_id, retryReason=enum("routeUnavailable", "preparationUnavailable",
                                                     "preconditionChanged").nullable(), context=unknown(),
             application=_application.optional())),
    "finish":
    obj(dict(cleanup=cleanup)),
    "capture":
    obj(
        dict(
            kind=enum("capture"), binding=binding, observed=unknown(), image=unknown(), coordinates=obj(
                dict(space=enum("window-pixels"), origin=enum("top-left"),
                     nativeSpace=enum("screen-points", "screen-pixels"), toNative=array(number(), min=6, max=6))))),
}

_launch_facts = obj(
    dict(kind=enum('launch'), binding=application_binding, observed=identity, targetLabel=identity,
         primitives=array(primitive, min=1, max=1)))
_captured_text = obj(dict(kind=enum('capturedText'), session=identity, textRef=identity, page=unknown()))
_desktop_capture = obj(
    dict(kind=enum('desktopCapture'), binding=binding, desktopRef=identity, scope=identity, observed=unknown(),
         observation=unknown(), screenshot=unknown()))


def decode_execution_result(method, value):
    if method == 'capture' and isinstance(value, dict) and value.get('kind') == 'desktopCapture':
        result = _desktop_capture.parse(value)
        if any(result['binding'][key] is not None for key in ('instance', 'rootWindow', 'surface', 'node', 'capture')):
            raise ValueError('protocol: desktop capture requires a session read scope')
        decoded = decode_desktop_observation(
            {key: result[key]
             for key in ('desktopRef', 'scope', 'observed', 'observation', 'screenshot')})
        if decoded['screenshot'] is not None and capture_metadata(decoded['screenshot'])['id'] != decoded['desktopRef']:
            raise ValueError('protocol: desktop image and coordinate mapping have different identities')
        return {**result, **decoded}
    if method == 'inspect' and isinstance(value, dict) and value.get('kind') == 'capturedText':
        result = _captured_text.parse(value)
        result['page'] = decode_text_page(result['page'])
        return result
    if method == 'inspect' and isinstance(value, dict) and value.get('kind') == 'launch':
        result = _launch_facts.parse(value)
        p = result['primitives'][0]
        _validate_primitive(p)
        if p['unit'] != 'launch_application':
            raise ValueError('protocol: inconsistent application launch contract')
        return result
    if method == "inspect" and isinstance(value, dict) and value.get("kind") in ("applications", "surfaces", "roots"):
        return decode_directory(value)
    if method == "inspect" and isinstance(value, dict) and value.get("kind") == "nodes":
        return decode_facts(value)
    if method == 'inspect' and isinstance(value, dict) and value.get('kind') == 'relation':
        return decode_relation_page(value)
    result = _schemas[method].parse(value)
    bound = result.get("target") or result.get("binding")
    invalid_discovery_binding = (bound is not None and 'application' not in bound and bound["surface"] is None
                                 and any(bound[key] is not None for key in ("instance", "rootWindow", "node")))
    if invalid_discovery_binding:
        raise ValueError("protocol: unbound discovery cannot contain window or element identities")
    if method == 'execute' and 'application' in result:
        if 'application' not in bound or not result['application']['isRunning']:
            raise ValueError('protocol: only an installation operation can return a running application')
    if method == "capture":
        if (any(bound[key] is None for key in ('instance', 'rootWindow', 'surface'))
                or any(bound[key] is not None for key in ('node', 'capture'))):
            raise ValueError("protocol: window capture requires a window binding, not an element or old image")
        result['observed'] = decode_stamp(result['observed'])
        result['image'] = decode_capture(result['image'], 'window', bound['surface'], result['observed'])
        a, b, c, d, _, _ = result['coordinates']['toNative']
        determinant = a * d - b * c
        if not math.isfinite(determinant) or determinant == 0:
            raise ValueError('protocol: image coordinate transform is not finite and invertible')
    if method == "inspect":
        if 'basis' in result:
            decode_selection_basis(result['basis'])
            if result.get('actuator') is None and result.get('receiver') is None:
                raise ValueError('protocol: selection dependencies require an actuator or receiver')
        within = result.get('within', [])
        if len(set(within)) != len(within) or result['binding']['node'] in within:
            raise ValueError('protocol: target ancestry must have distinct proper ancestors')
        selected = result.get('actuator')
        validate_actuator_path(selected)
        if 'receiver' in result:
            editor = selected['node'] if selected is not None else bound['node']
            invalid_receiver_selection = (editor is None or bound['capture'] is not None or not result.get('basis') or
                                          (result['receiver'].get('popup') or result['receiver'].get('node')) == editor
                                          or selected is not None and selected['relation'] != 'containedEditor')
            if invalid_receiver_selection:
                raise ValueError('protocol: invalid receiver relation selection')
        if selected is not None:
            if result['binding']['node'] is None or result['binding']['capture'] is not None:
                raise ValueError('protocol: an actuator requires an element target')
            if selected['relation'] in ('containedEditor',
                                        'controlPart') and selected['node'] == result['binding']['node']:
                raise ValueError('protocol: a selected member requires a distinct requested owner')
            if selected['relation'] == 'containedEditor' and (result['input']['editor'] != selected['node']
                                                              or result['input']['editorOwner']
                                                              != result['binding']['node']):
                raise ValueError('protocol: the confirmed editor must match the selected actuator and requested owner')
            if selected['relation'] == 'scrollPage' and (selected['node'] in (selected['region'], selected['bar'])
                                                         or any(p['unit'] != 'native_scroll'
                                                                for p in result['primitives'])):
                raise ValueError('protocol: invalid native scroll actuator contract')
            if selected['relation'] in ('controlPart', 'scrollPointer') and any(p['unit'] != 'pointer_sequence'
                                                                                for p in result['primitives']):
                raise ValueError('protocol: a selected pointer member only supports its pointer contract')
            if selected['relation'] == 'scrollPointer' and not result.get('basis'):
                raise ValueError('protocol: an element scroll pointer requires selection dependencies')
        identifiers = [p["id"] for p in result["primitives"]]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("protocol: duplicate native primitive id")
        for p in result["primitives"]:
            _validate_primitive(p)
    if method == "execute":
        if result["context"] is not None:
            result["context"] = decode_action_context(result["context"])
        if result["retryReason"] is not None and (result["dispatch"] != "notDispatched"
                                                  or result["cleanup"] != "complete"):
            raise ValueError("protocol: uncertain input cannot authorize another route")
    return result
