"""Concrete NativePort over the same supervised JSONL connection as observation.

No UI policy lives here: this module converts typed engine objects and strict
wire DTOs. Native errors keep their effect classification; input is never resent.
"""
import json

from ..client.diagnostics import local_failure
from ..errors import ComputerUseError
from ..protocol.captures import capture_metadata
from ..protocol.primitives import utf16_length
from .lifecycle import CallLifecycle
from .native import (
    ApplicationBinding,
    Binding,
    Channel,
    Cleanup,
    Confirmation,
    Dispatch,
    Facts,
    HostPolicy,
    InputContext,
    Knowledge,
    OpenedOperation,
    Primitive,
    StepResult,
)

_BINDING = dict(session="session", instance="instance", root_window="rootWindow", surface="surface", node="node",
                capture="capture")
_CONTEXT = dict(foreground_surface="foregroundSurface", keyboard_surface="keyboardSurface", focused_node="focusedNode",
                focus_ancestors="focusAncestors", editor="editor", editor_owner="editorOwner",
                selection_known="selectionKnown", geometry_current="geometryCurrent", window_route="windowRoute",
                pointer_route="pointerRoute")


def intent_key(intent):
    return json.dumps(intent, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def encode_fields(value, mapping):
    encoded = {}
    for name, wire in mapping.items():
        item = getattr(value, name)
        encoded[wire] = list(item) if isinstance(item, tuple) else item
    return encoded


def decode_binding(value):
    if 'application' in value:
        return ApplicationBinding(value['session'], value['application'])
    return Binding(**{name: value[wire] for name, wire in _BINDING.items()})


def encode_target(target):
    return (dict(session=target.session, application=target.application)
            if isinstance(target, ApplicationBinding) else encode_fields(target, _BINDING))


def decode_context(value):
    values = {
        name: value.get(wire, 'unknown') if name == 'pointer_route' else value[wire]
        for name, wire in _CONTEXT.items()
    }
    values["focus_ancestors"] = tuple(values["focus_ancestors"])
    values['pointer_hit'] = value.get('pointerHit')
    for name in ("selection_known", "geometry_current", "window_route", "pointer_route"):
        values[name] = Knowledge(values[name])
    return InputContext(**values)


class ConnectionPort:

    def __init__(self, connection, policy: HostPolicy):
        if connection.protocol_version != 5:
            raise ValueError("Native execution requires a protocol v5 connection")
        self.connection, self.policy = connection, policy
        self._operations = {}
        self._continuations = {}
        self.calls = CallLifecycle(self)

    @property
    def platform(self):
        return self.connection.platform

    @property
    def capabilities(self):
        return self.connection.capabilities

    def _check(self, operation):
        expected = self._operations.get(operation)
        if expected is None or self.connection.session_identity != expected[0]:
            raise RuntimeError("The native operation's connection ended; it cannot be resumed on another driver")
        return expected[1]

    async def connect(self, timeout_ms):
        hello = await self.connection.connect(timeout_ms)
        if "nativeDirectory" not in hello["capabilities"]:
            raise RuntimeError("This driver does not provide the native discovery contract")
        return Binding(hello["session"], None, None, None)

    async def directory(self, operation, target, query, timeout_ms):
        self._check(operation)
        result = await self.connection.request(
            "inspect", dict(operation=operation, target=encode_fields(target, _BINDING), query=query),
            dict(timeoutMs=timeout_ms))
        discovery_scope_changed = (result["kind"] != query["kind"] or result["session"] != target.session
                                   or query["kind"] in ('surfaces', 'roots')
                                   and result["application"] != query["application"]
                                   or result['platform'] != self.platform)
        if discovery_scope_changed:
            raise ValueError("Native discovery changed the requested session or application")
        return result

    async def begin(self, intent, target, timeout_ms):
        return await self._begin(dict(kind=intent.kind, parameters=dict(intent.parameters)), "write", target,
                                 timeout_ms)

    async def begin_read(self, target, timeout_ms):
        return await self._begin(dict(kind="read", parameters={}), "read", target, timeout_ms)

    async def _begin(self, intent, mode, target, timeout_ms):
        self.calls.check_begin(intent["kind"], mode, target)
        original = intent_key(intent)
        value = await self.connection.request(
            "begin", dict(intent=json.loads(original), mode=mode, target=encode_target(target)),
            dict(timeoutMs=timeout_ms))
        self._operations[value["operation"]] = (self.connection.session_identity, original)
        while len(self._operations) > 8:
            self._operations.pop(next(iter(self._operations)))
        return OpenedOperation(value["operation"], decode_binding(value["target"]))

    async def inspect(self, operation, target, timeout_ms, *, within=(), actuator=None, receiver=None, basis=()):
        self._check(operation)
        launch = isinstance(target, ApplicationBinding)
        launch_has_selection = launch and (within or actuator is not None or receiver is not None or basis)
        if launch_has_selection:
            raise ValueError('Application launch cannot select an element or editor')
        query = dict(kind='launch') if launch else dict(kind='inputContext', within=list(within))
        if actuator is not None:
            invalid_actuator_owner = target.node is None or target.capture is not None or (
                actuator.relation in ('containedEditor', 'controlPart') and target.node == actuator.node)
            if invalid_actuator_owner:
                raise ValueError('The actuator requires a compatible element owner')
            if actuator.relation == 'controlPart' and 'nativeControlParts' not in self.capabilities:
                raise local_failure('backend does not provide nativeControlParts',
                                    'update the driver before selecting a pointer control member', 'mode_unsupported',
                                    'inspect')
            if actuator.relation == 'scrollPointer':
                if 'nativeElementScroll' not in self.capabilities:
                    raise local_failure('backend does not provide nativeElementScroll',
                                        'update the driver before selecting an element wheel route', 'mode_unsupported',
                                        'inspect')
                if not basis:
                    raise ValueError('An element wheel route requires semantic selection dependencies')
                if actuator.content_path and 'nativeScrollContentPath' not in self.capabilities:
                    raise local_failure('backend does not provide nativeScrollContentPath',
                                        'update the driver before selecting moving scroll content', 'mode_unsupported',
                                        'inspect')
            query['actuator'] = actuator.wire()
        if receiver is not None:
            if target.node is None or target.capture is not None or not basis:
                raise ValueError('A receiver relation requires an element and semantic selection dependencies')
            capability = 'nativeVirtualFocus' if receiver.relation == 'virtualFocus' else 'nativeFocusMembers'
            if capability not in self.capabilities:
                raise local_failure('backend does not provide ' + capability,
                                    'update the driver before selecting a related receiver', 'mode_unsupported',
                                    'inspect')
            query['receiver'] = receiver.wire()
        if basis:
            if 'nativeSelectionBasis' not in self.capabilities:
                raise local_failure('backend does not provide nativeSelectionBasis',
                                    'update the driver before selecting a composed input target', 'mode_unsupported',
                                    'inspect')
            query['basis'] = [entry.wire() for entry in basis]
        value = await self.connection.request("inspect",
                                              dict(operation=operation, target=encode_target(target), query=query),
                                              dict(timeoutMs=timeout_ms))
        if value["kind"] != query['kind']:
            raise ValueError("Native inspection returned a different query kind")
        if tuple(value.get('within', ())) != tuple(within):
            raise ValueError('Native inspection changed the logical target constraints')
        if value.get('basis') != query.get('basis'):
            raise ValueError('Native inspection changed the semantic selection dependencies')
        if value.get('actuator') != query.get('actuator'):
            raise ValueError('Native inspection changed the selected editor')
        if value.get('receiver') != query.get('receiver'):
            raise ValueError('Native inspection changed the selected receiver relationship')
        primitives = tuple(
            Primitive(p["id"], p["unit"], Channel(p["channel"]), Knowledge(p["supported"]), readiness=Knowledge(
                p["readiness"]), pending=tuple(p["pending"]), background_preparation=Knowledge(
                    p["backgroundPreparation"]), action=p.get("action", {}).get("name"), action_description=p.get(
                        "action", {}).get("description"), unavailable_reason=p.get("unavailableReason")) for p in value["primitives"])
        if launch:
            return Facts(decode_binding(value['binding']), value['observed'], value['targetLabel'], Knowledge.YES,
                         Knowledge.YES, Knowledge.YES, (), primitives, platform=self.platform)
        return Facts(decode_binding(value["binding"]), value["observed"], value["targetLabel"],
                     Knowledge(value["identity"]), Knowledge(value["withinScope"]), Knowledge(value["enabled"]),
                     tuple(value["blockedBy"]), primitives, decode_context(value["input"]),
                     platform=self.connection.platform, native_role=value.get("nativeRole"),
                     actions_complete=value.get("actionsComplete", False), actuator=actuator, receiver=receiver,
                     application_menu=value.get("applicationMenu"))

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def nodes(self, operation, target, fields, max_nodes, max_depth, timeout_ms, *, children_page=None,
                    relation_page=None, advertised_fields=None, follow_relations=None):
        self._check(operation)
        query = dict(kind="nodes", fields=list(fields), maxNodes=max_nodes, maxDepth=max_depth)
        if follow_relations is not None:
            if 'nativeFollowRelations' not in self.connection.capabilities:
                raise ValueError('This driver cannot collect additional native relationships')
            query['followRelations'] = list(follow_relations)
        if advertised_fields is not None:
            if 'nativeAdvertisedFields' not in self.connection.capabilities:
                raise ValueError('This driver cannot query advertised native fields')
            query['advertisedFields'] = list(advertised_fields)
        if children_page is not None and relation_page is not None:
            raise ValueError('Select one root relation page')
        page = relation_page if relation_page is not None else children_page
        relation_name = relation_page['property'] if relation_page is not None else self._children_property()
        if relation_page is not None and 'nativeRelationPages' not in self.connection.capabilities:
            raise ValueError('This driver cannot collect native relationship pages')
        expected = None
        if page is not None:
            query['relationPage' if relation_page is not None else 'childrenPage'] = page
            cursor = page['cursor']
            expected = self._continuations.get(cursor) if cursor is not None else None
            if cursor is not None and (expected is None
                                       or expected[:4] != (target.session, target.surface, target.node, relation_name)):
                raise ComputerUseError(
                    dict(code='snapshot_expired', effect='none', target='element content',
                         detail='The continuation is no longer retained for this element and relationship.',
                         next='read the element again and use its reported continuation'))
        result = await self.connection.request(
            "inspect", dict(operation=operation, target=encode_fields(target, _BINDING), query=query),
            dict(timeoutMs=timeout_ms))
        if follow_relations is not None and not set(follow_relations) <= set(result.get('requestedRelations', ())):
            raise ValueError('Native collection omitted requested relationship coverage')
        batch_scope_changed = (result.get("kind") != "nodes" or result["session"] != target.session
                               or result["surface"] != target.surface or result['platform'] != self.connection.platform
                               or result["roots"] != [target.node] or len(result["nodes"]) > max_nodes)
        if batch_scope_changed:
            raise ValueError("Native fact batch changed its requested scope or budget")
        if page is not None:
            relation = next(
                (r for r in result['relations'] if r['source'] == target.node and r['property'] == relation_name), None)
            if relation is None:
                raise ValueError('Native page omitted its requested relation')
            if relation['state'] in ('complete', 'partial') and relation['certainty'] == 'confirmed':
                continuation_changed = (relation.get('offset', 0) != (expected[4] if expected else 0)
                                        or expected is not None and relation.get('total') != expected[5]
                                        or len(relation['targets']) > page['limit'])
                if continuation_changed:
                    raise ValueError('Native page changed its continuation range or budget')
        self._remember_continuations(result, result['relations'])
        return result

    def _children_property(self):
        from ..native_semantics import RELATIONS
        return next(key for key, meaning in RELATIONS[self.platform].items() if meaning == 'children')

    def _remember_continuations(self, result, relations):
        for relation in relations:
            if 'cursor' in relation:
                self._continuations[relation['cursor']] = (result['session'], result['surface'],
                                                           relation['source'], relation['property'],
                                                           relation.get('offset', 0) + len(relation['targets']),
                                                           relation['total'])
        while len(self._continuations) > 512:
            self._continuations.pop(next(iter(self._continuations)))

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def relation(self, operation, target, relation_name, cursor, limit, timeout_ms):
        self._check(operation)
        expected = self._continuations.get(cursor) if cursor is not None else None
        if cursor is not None and (expected is None
                                   or expected[:4] != (target.session, target.surface, target.node, relation_name)):
            raise ValueError('Relation cursor was not observed for this source and scope')
        result = await self.connection.request(
            'inspect',
            dict(operation=operation, target=encode_fields(target, _BINDING),
                 query=dict(kind='relation', property=relation_name, cursor=cursor, limit=limit)),
            dict(timeoutMs=timeout_ms))
        relation_scope_changed = (result.get('kind') != 'relation' or result['session'] != target.session
                                  or result['surface'] != target.surface
                                  or result['platform'] != self.connection.platform)
        if relation_scope_changed:
            raise ValueError('Native relation page changed its requested scope')
        relation = result['relation']
        if (
            relation["source"] != target.node
            or relation["property"] != relation_name
            or len(relation["targets"]) > limit
        ):
            raise ValueError('Native relation page changed its source, property or budget')
        if relation['state'] in ('complete', 'partial'):
            if (relation.get('offset', 0) != (expected[4] if expected else 0)
                    or expected is not None and relation.get('total') != expected[5]):
                raise ValueError('Native relation continuation changed its observed range')
        self._remember_continuations(result, [relation])
        return result

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def captured_text(self, operation, target, text_ref, offset, length, timeout_ms):
        self._check(operation)
        value = await self.connection.request(
            'inspect',
            dict(operation=operation, target=encode_fields(target, _BINDING),
                 query=dict(kind='capturedText', textRef=text_ref, offset=offset, length=length)),
            dict(timeoutMs=timeout_ms))
        if value['kind'] != 'capturedText' or value['session'] != target.session or value['textRef'] != text_ref:
            raise ValueError('Native text page changed its requested capture')
        page = value['page']
        if page['offset'] != offset or utf16_length(page['text']) > length:
            raise ValueError('Native text page changed its requested range or budget')
        return page

    async def capture(self, operation, target, max_dimension, timeout_ms, *, kind='window'):
        self._check(operation)
        value = await self.connection.request(
            'capture',
            dict(operation=operation, target=encode_fields(target, _BINDING), kind=kind, maxDimension=max_dimension),
            dict(timeoutMs=timeout_ms))
        if value['kind'] != ('desktopCapture' if kind == 'desktop' else 'capture') or decode_binding(
                value['binding']) != target:
            raise ValueError('Native capture changed its requested kind or scope')
        if kind == 'desktop':
            dimensions = value['observation']['canvas']
            if value['observation']['mode'] != self.policy.mode:
                raise ValueError('Native desktop capture changed its negotiated mode')
        else:
            dimensions = capture_metadata(value['image'])
        if max(dimensions['width'], dimensions['height']) > max_dimension:
            raise ValueError('Native capture exceeded its requested image budget')
        return value

    async def execute(self, operation, step, command, timeout_ms):
        original = self._check(operation)
        if original != intent_key(dict(kind=command.intent.kind, parameters=dict(command.intent.parameters))):
            raise local_failure('The operation intent is already bound; a step cannot change its parameters',
                                'finish this operation before requesting a different action', 'invalid_request',
                                'execute')
        if (command.intent.kind == 'replace' and command.primitive.unit == 'key_sequence'
                and 'nativeTextReplacement' not in self.capabilities):
            raise local_failure('backend does not provide nativeTextReplacement',
                                'update the driver before selecting a contextual replacement unit', 'mode_unsupported',
                                'execute')
        if (command.primitive.unit == 'pointer_sequence' and command.target.node is not None
                and 'nativeElementPointer' not in self.capabilities):
            raise local_failure('backend does not provide nativeElementPointer',
                                'update the driver before selecting an element pointer unit', 'mode_unsupported',
                                'execute')
        if (command.actuator is not None and command.actuator.relation == 'controlPart'
                and 'nativeControlParts' not in self.capabilities):
            raise local_failure('backend does not provide nativeControlParts',
                                'update the driver before selecting a pointer control member', 'mode_unsupported',
                                'execute')
        if (command.actuator is not None and command.actuator.relation == 'scrollPointer'
                and 'nativeElementScroll' not in self.capabilities):
            raise local_failure('backend does not provide nativeElementScroll',
                                'update the driver before selecting an element wheel route', 'mode_unsupported',
                                'execute')
        unsupported_scroll_path = (command.actuator is not None and command.actuator.relation == 'scrollPointer'
                                   and command.actuator.content_path
                                   and 'nativeScrollContentPath' not in self.capabilities)
        if unsupported_scroll_path:
            raise local_failure('backend does not provide nativeScrollContentPath',
                                'update the driver before selecting moving scroll content', 'mode_unsupported',
                                'execute')
        wire = dict(kind="prepare_receiver" if command.prepare else command.primitive.unit,
                    primitiveId=command.primitive.id, target=encode_target(command.target),
                    foreground=command.foreground)
        value = await self.connection.request("execute", dict(operation=operation, step=step, command=wire),
                                              dict(timeoutMs=timeout_ms))
        return StepResult(decode_binding(value["target"]), Dispatch(value["dispatch"]), value["detail"],
                          tuple(value["preparation"]), Confirmation(value["confirmation"]), Cleanup(value["cleanup"]),
                          value["errorCode"], value["retryReason"], value["context"], value.get('application'))

    async def finish(self, operation):
        self._check(operation)
        value = await self.connection.request("finish", dict(operation=operation), dict(timeoutMs=1000))
        return Cleanup(value["cleanup"])
