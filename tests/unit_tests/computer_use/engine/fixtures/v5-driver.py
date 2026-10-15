"""A protocol peer for transport contracts; performs no desktop operations."""
import json
import base64
from pathlib import Path
import struct
import sys
import time
import zlib
from computer_use.protocol.execution import decode_begin, decode_capture_request, decode_inspect_query, decode_unit_command

mode, journal = sys.argv[1], Path(sys.argv[2])
session = "native-v4-fixture"
binding = None
captures = 0
directories = 0
application_directories = 0
node_reads = 0
finishes = 0
prepared = False
operation_intent = None
key_attempts = 0
inspections = 0
preparations = 0
contracts = {}
launch_observation = mode.startswith('observation-launch')
launch_wrapper = mode.startswith('observation-launch-wrapper')
sdk = mode.startswith('observation-sdk')
menus = mode.startswith('observation-sdk-menu')
loading = mode.startswith('observation-loading')
menu_reads = 0
sdk_value = 'A🙂e\u0301\r\nZ'

def editor_node():
    if mode == 'keyboard-actuator-churn':
        return 'editor-' + str(preparations)
    return 'editor-new' if prepared and mode == 'keyboard-actuator-recreated' else 'editor'

def pixels(width=4, height=2):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
    value = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    return base64.b64encode(value + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x22\x44\x66\xff' * width) * height)) + chunk(b'IEND', b'')).decode()

primitive = dict(id="declared-action", unit="native_action", channel="object", supported="yes", readiness="yes", pending=[],
                 backgroundPreparation="no", action=dict(name="AXPress", description="Press"))
context = dict(foregroundSurface=None, keyboardSurface=None, focusedNode=None, focusAncestors=[],
               editor=None, editorOwner=None, selectionKnown="unknown", geometryCurrent="unknown", windowRoute="unknown")

for line in sys.stdin:
    request = json.loads(line)
    with journal.open("a") as output:
        output.write(json.dumps(request) + "\n")
    if "cancel" in request:
        continue
    method, params = request["method"], request["params"]
    error = None
    if method == "hello":
        request_mode = params['desktopMode']
        capabilities = ["structuredObservations", "cancel", "requestDedup", "captureMetadata", "retainedCaptures", "desktopCoordination"]
        if mode != 'keyboard-actuator-missing-basis':
            capabilities.append('nativeSelectionBasis')
        if mode != "missing-capability":
            capabilities.append("nativeOperations")
        if mode != 'legacy-execution':
            capabilities.append('nativeBoundContracts')
        capabilities.append('nativeUnitReadiness' if mode == 'legacy-readiness' else 'nativeUnitReadinessV2')
        if mode.startswith('nodes-text') and mode != 'nodes-text-missing-capability':
            capabilities.append('nativeCapturedText')
        if mode == 'nodes-table-facts':
            capabilities.extend(('nativeTableFacts', 'nativeAdvertisedFields'))
        if mode == 'nodes-table-unconditional':
            capabilities.append('nativeTableFacts')
        if loading or mode.startswith('nodes-busy'):
            if not mode.endswith('-no-feature'): capabilities.append('nativeBusyFacts')
            if not mode.endswith('-unadvertised'): capabilities.append('nativeAdvertisedFields')
        if mode.startswith('nodes-follow') and mode != 'nodes-follow-missing-capability':
            capabilities.append('nativeFollowRelations')
        if (mode.startswith('launch') or launch_observation) and mode not in ('launch-missing-capability', 'observation-launch-missing-capability'):
            capabilities.append('nativeLaunch')
        if mode.startswith('launch') or launch_observation:
            # Capabilities describe the whole driver, not the current target.
            # Production macOS advertises UI receiver support during launch too.
            capabilities.extend(('nativeVirtualFocus', 'nativeFocusMembers'))
        if mode.startswith(("directory", "desktop", "capture", "pointer", "observation", "launch")):
            capabilities.append("nativeDirectory")
        if mode.startswith(('capture', 'pointer', 'observation', 'desktop')):
            capabilities.append('nativeCapture')
            if mode != 'desktop-legacy-capture': capabilities.append('nativeCaptureKinds')
        if mode.startswith('desktop-input'):
            capabilities.extend(('nativeKeyboardIntents', 'nativeTextInput'))
            if mode != 'desktop-input-missing-capability': capabilities.append('nativeDesktopInput')
        if mode.startswith('relations') and mode != 'relations-missing-capability':
            capabilities.append('nativeRelations')
        if mode.startswith(('action-intents', 'query', 'pointer-element')) and mode != 'action-intents-missing-capability':
            capabilities.append('nativeActionIntents')
        if mode.startswith(('query', 'keyboard', 'pointer')) and mode != 'query-missing-capability':
            capabilities.append('nativeWindowScope')
        if mode.startswith('keyboard') and mode != 'keyboard-missing-capability':
            capabilities.append('nativeKeyboardIntents')
        if mode.startswith('keyboard') and mode != 'keyboard-missing-text-capability':
            capabilities.append('nativeTextInput')
        if mode.startswith('keyboard-virtual') and mode != 'keyboard-virtual-missing-capability':
            capabilities.append('nativeVirtualFocus')
        if mode.startswith('keyboard-member') and mode != 'keyboard-member-missing-capability':
            capabilities.append('nativeFocusMembers')
        if mode.startswith(('keyboard-actuator', 'keyboard-replace')):
            capabilities.append('nativeActuators')
        if (mode.startswith('keyboard-replace') or mode == 'text-value-v5') and mode != 'keyboard-replace-missing-capability':
            capabilities.append('nativeTextReplacement')
        if mode.startswith('scroll') and mode != 'scroll-missing-capability':
            capabilities.append('nativeScrollPattern' if mode == 'scroll-pattern' else 'nativeScroll')
        if mode.startswith('pointer') and mode != 'pointer-missing-capability':
            capabilities.append('nativePointer')
        if mode.startswith('pointer-element') and mode != 'pointer-element-missing-capability':
            capabilities.append('nativeElementPointer')
        if sdk:
            capabilities += ['nativeActionIntents', 'nativeWindowScope', 'nativeKeyboardIntents',
                             'nativeTextInput', 'nativeActuators', 'nativeTextReplacement', 'nativePointer']
            if mode != 'observation-sdk-no-pages': capabilities.append('nativeNodePages')
        if menus: capabilities.append('nativeApplicationRoots')
        if mode == 'observation-isolated': capabilities.append('nativeReadIsolation')
        result = dict(platform="macos" if mode.startswith(("nodes", "directory", "capture", "relations", "action-intents", "query", "keyboard", "pointer", "observation", "scroll")) else "fixture", desktopMode=params["desktopMode"], maxTimeoutMs=30000, capabilities=capabilities)
    elif method == "begin":
        binding = {**params["target"]}
        assert decode_begin(params) == params
        original_intent = params['intent']
        operation_intent = original_intent['kind']
        prepared = False
        key_attempts = 0
        inspections = preparations = 0
        contracts = {}
        if binding.get('surface') is not None and binding['surface'] != 'menu-surface':
            binding['rootWindow'] = binding['rootWindow'] or 'root'
        if binding.get('surface') == 'menu-surface' and params['mode'] == 'write':
            error = dict(code='input_unavailable', effect='none', target='application menu',
                detail='Application menu command context is not connected yet.', next='use an observed window control')
        result = dict(operation="operation", target=binding, remainingMs=25000)
    elif method == "inspect":
        # Use the shared request contract. A lenient peer must not conceal an
        # encoder mismatch that the native endpoint would reject.
        assert decode_inspect_query(params['query']) == params['query']
        declared = dict(primitive)
        if mode.startswith("text-value"):
            declared.update(id="text-value", unit="native_text_edit")
            declared.pop("action", None)
        if mode == "invalid-contract":
            declared.update(unit="key_sequence", channel="keyboard")
        result = dict(kind="inputContext", binding=params['target'], observed="observation", targetLabel="control", identity="yes", withinScope="yes",
                      enabled="yes", blockedBy=[], primitives=[declared], input=context)
        if mode.startswith('scroll') and params['query']['kind'] == 'inputContext':
            selected = params['query'].get('actuator')
            declared.update(id='scroll-page', unit='native_scroll')
            declared.pop('action', None)
            result['primitives'] = [declared] if selected or mode == 'scroll-pattern' else []
            if selected:
                assert selected == dict(node='down', region='node', bar='bar', relation='scrollPage')
                result['actuator'] = selected
                if mode == 'scroll-wrong-echo': result['actuator'] = {**selected, 'region':'another-region'}
        if mode.startswith('pointer'):
            declared.update(id='pointer-route', unit='pointer_sequence', channel='pointer',
                backgroundPreparation='yes')
            declared.pop('action', None)
            declared.update(readiness='yes' if prepared or mode == 'pointer-partial' else 'no',
                            pending=[] if prepared or mode == 'pointer-partial' else ['receiver'])
            result.update(enabled='unknown', input={**context, 'geometryCurrent': 'yes',
                'pointerRoute': 'yes' if prepared or mode == 'pointer-partial' else 'no'})
            if mode == 'pointer-stale':
                error = dict(code='snapshot_expired', effect='none', target='captured window',
                             detail='Window geometry changed.', next='capture the same window again')
            if mode.startswith('pointer-element-object'):
                result.update(nativeRole='AXButton', actionsComplete=True, primitives=[dict(primitive), declared])
                if mode == 'pointer-element-object-no-geometry':
                    declared.update(readiness='unknown', pending=['geometry'])
            if mode == 'pointer-element-partial':
                declared.update(readiness='yes', pending=[])
                result['input']['pointerRoute'] = 'yes'
        if mode.startswith('keyboard'):
            declared.update(id='key-route', unit={'focus': 'prepare_receiver', 'paste': 'clipboard_paste'}.get(operation_intent, 'key_sequence'),
                channel='keyboard', backgroundPreparation='yes')
            declared.pop('action', None)
            ready = prepared or mode in ('keyboard-ready', 'keyboard-partial', 'keyboard-unclean', 'keyboard-window',
                'keyboard-replace-partial', 'keyboard-replace-unknown', 'keyboard-replace-empty', 'keyboard-actuator-partial')
            if mode.startswith('keyboard-rotate'):
                ready = mode != 'keyboard-rotate-preparation'
                declared['id'] = 'key-route-' + str(inspections)
            if mode == 'keyboard-actuator-churn':
                ready = False
            declared.update(readiness='yes' if ready else 'no', pending=[] if ready else ['receiver'])
            result['input'] = {**context, 'keyboardSurface': binding['surface'], 'windowRoute': 'yes',
                'focusedNode': binding['node'] if ready else 'another-control'}
            if mode.startswith('keyboard-actuator') and params['query']['kind'] == 'inputContext':
                selected = params['query'].get('actuator')
                assert params['target'] == binding
                result['input']['focusedNode'] = editor_node() if ready else None
                if selected is None:
                    result['primitives'] = []  # The owner is not itself an editor.
                else:
                    assert selected == dict(node=editor_node(), relation='containedEditor')
                    declared['id'] = 'key-route:' + selected['node']
                    result['actuator'] = selected
                    result['input'].update(editor=selected['node'], editorOwner=binding['node'])
                    if mode == 'keyboard-actuator-wrong-echo':
                        result['actuator'] = {**selected, 'node': 'foreign-editor'}
                    if mode == 'keyboard-actuator-wrong-owner':
                        result['input']['editorOwner'] = 'another-control'
            if mode == 'keyboard-replace-contained' and params['query']['kind'] == 'inputContext':
                selected = params['query'].get('actuator')
                result['input']['focusedNode'] = 'editor' if ready else None
                if selected is None:
                    result['primitives'] = []  # The composite itself has no full-text selection.
                else:
                    assert selected == dict(node='editor', relation='containedEditor')
                    declared['id'] = 'key-route:editor'
                    result['actuator'] = selected
                    result['input'].update(editor='editor', editorOwner=binding['node'],
                        focusedNode='editor' if ready else 'another-control')
        if mode.startswith('query'):
            result.update(nativeRole='AXButton', actionsComplete=True, within=params['query'].get('within', []))
            if mode == 'query-wrong-constraints':
                result['within'] = []
        if mode.startswith('action-intents'):
            result.update(nativeRole='AXButton', actionsComplete=True)
            result['primitives'].append({**declared, 'id': 'context-menu', 'action': dict(name='AXShowMenu', description='Show menu')})
            if mode in ('action-intents-dropdown', 'action-intents-partial'):
                result.update(nativeRole='AXComboBox', actionsComplete=mode != 'action-intents-partial')
                result['primitives'] = result['primitives'][1:]
            if mode == 'action-intents-invalid-role':
                result['nativeRole'] = 42
        if mode.startswith("text-value"):
            result['input'] = {**context, 'focusedNode': 'another-field', 'editor': 'another-editor', 'editorOwner': 'another-field'}
        if params["query"]["kind"] == "nodes":
            node_reads += 1
            if mode.startswith('observation') and not sdk:
                assert binding['node'] is None and params['target'] == {**binding, 'node': params['target']['node']}
                if mode == 'observation-disconnect':
                    sys.exit(0)
            fields = params["query"]["fields"]
            values = {key: value for key, value in dict(AXRole="AXButton", AXTitle="Native fixture").items() if key in fields}
            root = params['target']["node"] if mode != "nodes-wrong-root" else "another-node"
            result = dict(kind="nodes", session=session, surface=binding["surface"], platform="macos",
                observed=dict(id="facts", startedAt=1, completedAt=2), roots=[root],
                nodes=[dict(node=root, requestedFields=fields, values=values,
                    fieldStatus={key: dict(state="unsupported") for key in fields if key not in values},
                    actions=dict(state="complete", items=[]))], requestedRelations=[], relations=[], unread=[], stopReasons=[])
            if loading:
                def fact(token, **properties):
                    properties = {key: value for key, value in properties.items() if key in fields}
                    return dict(node=token, requestedFields=fields, values=properties,
                        fieldStatus={key: dict(state='unsupported') for key in fields if key not in properties},
                        actions=dict(state='complete', items=[]))
                is_busy = node_reads == 1 or mode.endswith(('-persistent', '-refresh-error', '-refresh-disconnect'))
                unknown = mode.endswith(('-initializing', '-unknown', '-already-attempted'))
                body = fact(root + '-body', AXRole='AXGroup', **({} if unknown else dict(AXElementBusy=is_busy)))
                if mode.endswith('-unknown') and 'AXElementBusy' in fields:
                    body['fieldStatus']['AXElementBusy'] = dict(state='timeout', detail='busy unavailable')
                if mode.endswith('-malformed') and 'AXElementBusy' in fields:
                    body['values']['AXElementBusy'] = 'true'
                empty = mode.endswith('-empty')
                result.update(observed=dict(id='facts-' + str(node_reads), startedAt=node_reads * 10, completedAt=node_reads * 10 + 1),
                    nodes=[fact(root, AXRole='AXWindow', AXTitle='Native fixture')] + ([] if empty else [body]),
                    requestedRelations=['AXChildren'], relations=[dict(source=root, property='AXChildren',
                        targets=[] if empty else [root + '-body'], state='complete', certainty='confirmed')])
                if not empty:
                    result['relations'].append(dict(source=root + '-body', property='AXChildren',
                        targets=[root + '-text'] if node_reads > 1 else [], state='complete', certainty='confirmed'))
                    if node_reads > 1:
                        result['nodes'].append(fact(root + '-text', AXRole='AXStaticText', AXValue='Loaded content'))
                        result['relations'].append(dict(source=root + '-text', property='AXChildren', targets=[],
                            state='complete', certainty='confirmed'))
                if node_reads > 1 and mode.endswith('-refresh-error'):
                    error = dict(code='observation_incomplete', effect='none', target='content',
                        detail='refresh provider timeout', next='read later')
                if node_reads > 1 and mode.endswith('-refresh-disconnect'):
                    sys.exit(0)
            if mode.startswith('nodes-follow') and mode != 'nodes-follow-omitted':
                result['requestedRelations'] = params['query'].get('followRelations', [])
                result['relations'] = [dict(source=root, property=property, targets=[],
                    state='unsupported', certainty='unknown') for property in result['requestedRelations']]
            if mode in ('observation-tree-error', 'observation-both-error', 'observation-uncertain'):
                error = dict(code='observation_incomplete', effect='possible' if mode.endswith('uncertain') else 'none',
                    target='tree', detail='Native provider unavailable.', next='use the image')
            if mode == 'observation-wrong-scope':
                result['surface'] = 'unrelated'
            if sdk and not mode.startswith('observation-sdk-pages') and binding['surface'] != 'menu-surface':
                root = params['target']['node']
                suffix = '-second' if params['target']['surface'] in ('second-surface', 'new-surface') else ''
                data = {
                    'node' + suffix: dict(AXRole='AXWindow', AXTitle='Native fixture', AXEnabled=True),
                    'field' + suffix: dict(AXRole='AXTextField', AXTitle='Input', AXValue=sdk_value, AXEnabled=True,
                        AXEditable=True, **{'settable.AXFocused': True, 'settable.AXValue': True}),
                    'button' + suffix: dict(AXRole='AXButton', AXTitle='Apply', AXEnabled=True)}
                values = {key: value for key, value in data[root].items() if key in fields}
                children = ['field' + suffix, 'button' + suffix] if root == 'node' + suffix else []
                result['roots'] = [root]
                result['nodes'] = [dict(node=root, requestedFields=fields, values=values,
                    fieldStatus={key: dict(state='unsupported') for key in fields if key not in values},
                    actions=dict(state='complete', items=[dict(name='AXPress', description='Press'), dict(name='AXShowMenu', description='Show menu')]
                                 if root.startswith('button') else []))]
                result['requestedRelations'] = ['AXChildren', 'AXTitleUIElement']
                result['relations'] = [dict(source=root, property='AXChildren', targets=children, state='complete', certainty='confirmed'),
                    dict(source=root, property='AXTitleUIElement', targets=[], state='complete', certainty='confirmed')]
                result['unread'] = children
                if children and params['query']['maxDepth'] > 0 and params['query']['maxNodes'] >= 3:
                    for token in children:
                        values = {key: value for key, value in data[token].items() if key in fields}
                        result['nodes'].append(dict(node=token, requestedFields=fields, values=values,
                            fieldStatus={key: dict(state='unsupported') for key in fields if key not in values},
                            actions=dict(state='complete', items=[dict(name='AXPress', description='Press'), dict(name='AXShowMenu', description='Show menu')]
                                         if token.startswith('button') else [])))
                        result['relations'].extend([
                            dict(source=token, property='AXChildren', targets=[], state='complete', certainty='confirmed'),
                            dict(source=token, property='AXTitleUIElement', targets=[], state='complete', certainty='confirmed')])
                    result['unread'] = []
            if mode.startswith('observation-sdk-pages'):
                page = params['query'].get('childrenPage')
                start = int(page['cursor'].split(':')[1]) if page and page['cursor'] else 0
                count = page['limit'] if page else 2
                if params['query']['maxDepth'] == 0:
                    count = 0
                end = min(300, start + count, start + params['query']['maxNodes'] - 1)
                children = ['item-' + str(i) for i in range(start, end)] if root == 'node' else []
                result.update(nodes=[], relations=[], unread=[], requestedRelations=['AXChildren'])
                for token in [root, *children]:
                    raw = dict(AXRole='AXGroup' if token == 'node' else 'AXButton',
                               AXTitle='Items' if token == 'node' else 'Item ' + token[5:], AXEnabled=True)
                    values = {key: value for key, value in raw.items() if key in fields}
                    result['nodes'].append(dict(node=token, requestedFields=fields, values=values,
                        fieldStatus={key: dict(state='unsupported') for key in fields if key not in values},
                        actions=dict(state='complete', items=[] if token == 'node' else [dict(name='AXPress', description='Press')])))
                    relation = dict(source=token, property='AXChildren', targets=children if token == root else [],
                                    state='complete', certainty='confirmed')
                    if token == 'node':
                        relation.update(offset=start, total=300, state='partial')
                        if end < 300: relation['cursor'] = 'children:' + str(end)
                        if start and mode.endswith('-wrong-range'):
                            relation['offset'] += 1
                        if start and mode.endswith('-changed'):
                            relation = dict(source=root, property='AXChildren', targets=[], certainty='unknown',
                                state='error', detail='native relation changed; read its source again')
                            result['nodes'] = result['nodes'][:1]
                            children = []
                        if start and mode.endswith('-expired'):
                            error = dict(code='snapshot_expired', effect='none', target='children',
                                detail='Continuation expired.', next='read this element again')
                    result['relations'].append(relation)
                    if start and mode.endswith('-changed'):
                        break
            if mode.startswith("nodes-text"):
                result["nodes"][0]["values"]["AXValue"] = "preview"
                result["nodes"][0]["fieldStatus"]["AXValue"] = dict(state="truncated", textRef="text-capture", totalUnits=20000)
            if mode.startswith('keyboard-actuator') or mode == 'keyboard-replace-contained':
                child = editor_node()
                owner = root == binding['node']
                raw = dict(AXRole='AXComboBox' if owner else 'AXTextField', AXEnabled=True,
                           **{'settable.AXValue': not owner, 'settable.AXFocused': not owner})
                values = {key: value for key, value in raw.items() if key in fields}
                result['nodes'][0].update(values=values,
                    fieldStatus={key: dict(state='absent') for key in fields if key not in values})
                children = [child] if owner else []
                parents = [] if owner else [binding['node']]
                result.update(requestedRelations=['AXChildren', 'AXParent'], unread=children + parents,
                    relations=[dict(source=root, property=key, targets=targets, certainty='confirmed', state='complete')
                               for key, targets in [('AXChildren', children), ('AXParent', parents)]])
            elif mode.startswith('keyboard-replace'):
                raw = dict(AXRole='AXGroup' if mode == 'keyboard-replace-noneditor' else 'AXTextArea',
                    AXEnabled=True, AXEditable=mode != 'keyboard-replace-noneditor',
                    **{'settable.AXFocused': True})
                values = {key: value for key, value in raw.items() if key in fields}
                result['nodes'][0].update(values=values,
                    fieldStatus={key: dict(state='absent') for key in fields if key not in values})
            elif mode.startswith('keyboard'):
                # A ready text unit also needs editor facts, even when this
                # fixture has no composite-actuator capability.
                raw = dict(AXRole='AXButton' if mode == 'keyboard-noneditor' else 'AXTextField',
                    AXEnabled=True, AXEditable=mode != 'keyboard-noneditor' and not (
                        mode == 'keyboard-readonly-after-prepare' and prepared),
                    **{'settable.AXFocused': True})
                values = {key: value for key, value in raw.items() if key in fields}
                result['nodes'][0].update(values=values,
                    fieldStatus={key: dict(state='absent') for key in fields if key not in values})
                # A scalar field is a leaf here. Its complete empty Children
                # is evidence; omitted relation coverage is not equivalent.
                result.update(requestedRelations=['AXChildren'], relations=[dict(source=root,
                    property='AXChildren', targets=[], certainty='confirmed', state='complete')])
            if mode.startswith('scroll'):
                graph = dict(node=dict(AXRole='AXScrollArea'), bar=dict(AXRole='AXScrollBar', AXOrientation='AXVerticalOrientation'),
                             down=dict(AXRole='AXButton', AXSubrole='AXIncrementPage'))
                values = {key:value for key,value in graph[root].items() if key in fields}
                result['nodes'][0].update(values=values, fieldStatus={k:dict(state='absent') for k in fields if k not in values},
                    actions=dict(state='complete', items=[dict(name='AXPress', description='Press')] if root == 'down' else []))
                edges = {'AXChildren': ['down'] if root == 'bar' else [],
                         'AXParent': [] if root == 'node' else ['node' if root == 'bar' else 'bar'],
                         'AXVerticalScrollBar': ['bar'] if root == 'node' else [], 'AXHorizontalScrollBar': []}
                result.update(requestedRelations=list(edges), relations=[dict(source=root, property=k, targets=v,
                    certainty='confirmed', state='complete') for k,v in edges.items()],
                    unread=sorted({n for v in edges.values() for n in v} - {root}))
            if mode.startswith('relations'):
                if root == 'node':
                    result.update(requestedRelations=['AXChildren', 'AXParent'], unread=['child-0', 'outside-parent'], relations=[
                        dict(source=root, property='AXChildren', targets=['child-0'], certainty='confirmed', state='partial',
                             offset=0, total=3, cursor='after-one'),
                        dict(source=root, property='AXParent', targets=['outside-parent'], certainty='confirmed', state='complete', offset=0, total=1)])
                elif 'AXTitle' in fields:
                    result['nodes'][0]['values']['AXTitle'] = 'Related ' + root
            if mode.startswith('query'):
                graph = dict(node=('AXWindow', 'Fixture', ['group']), group=('AXGroup', 'A', ['wrapper']),
                             wrapper=('AXGroup', '', ['button']), button=('AXButton', 'Run', []))
                if mode == 'query-ambiguous':
                    graph['wrapper'][2].append('duplicate')
                    graph['duplicate'] = ('AXButton', 'Run', [])
                result.update(requestedRelations=['AXChildren', 'AXTitleUIElement'], nodes=[], relations=[])
                for token, (role, title, children) in graph.items():
                    values = {key: value for key, value in dict(AXRole=role, AXTitle=title).items() if key in fields}
                    result['nodes'].append(dict(node=token, requestedFields=fields, values=values,
                        fieldStatus={key: dict(state='absent') for key in fields if key not in values}, actions=dict(state='complete', items=[])))
                    for property, targets in (('AXChildren', children), ('AXTitleUIElement', [])):
                        result['relations'].append(dict(source=token, property=property, targets=targets, certainty='confirmed', state='complete'))
            if menus and binding['surface'] == 'menu-surface':
                menu_reads += 1
                graph = {
                    'menubar': ('AXMenuBar', '', ['file', 'edit']),
                    'file': ('AXMenuBarItem', 'File', ['file-menu']),
                    'edit': ('AXMenuBarItem', 'Edit', []),
                    'file-menu': ('AXMenu', 'File', ['open', 'recent']),
                    'open': ('AXMenuItem', 'Open…', []),
                    'recent': ('AXMenuItem', 'Recent', ['recent-menu']),
                    'recent-menu': ('AXMenu', 'Recent', ['document']),
                    'document': ('AXMenuItem', 'Document', [])}
                if mode.endswith('-ambiguous'):
                    graph['edit'] = ('AXMenuBarItem', 'File', [])
                stack, included, unread = [(root, 0)], [], []
                while stack:
                    token, depth = stack.pop()
                    if depth > params['query']['maxDepth'] or len(included) >= params['query']['maxNodes']:
                        unread.append(token)
                        continue
                    included.append(token)
                    stack.extend((child, depth + 1) for child in reversed(graph[token][2]))
                result.update(nodes=[], requestedRelations=['AXChildren', 'AXTitleUIElement'], relations=[], unread=unread)
                for token in included:
                    role, title, children = graph[token]
                    values = {key: value for key, value in dict(AXRole=role, AXTitle=title, AXEnabled=True).items() if key in fields}
                    status = {key: dict(state='unsupported') for key in fields if key not in values}
                    if mode.endswith('-unknown-title') and token == 'edit':
                        values.pop('AXTitle', None); status['AXTitle'] = dict(state='timeout')
                    result['nodes'].append(dict(node=token, requestedFields=fields, values=values, fieldStatus=status,
                        actions=dict(state='complete', items=[dict(name='AXPress', description='Press')] if role == 'AXMenuItem' else [])))
                    result['relations'].extend(dict(source=token, property=key, targets=targets, state='complete', certainty='confirmed')
                        for key, targets in (('AXChildren', children), ('AXTitleUIElement', [])))
        elif params['query']['kind'] == 'relation':
            query = params['query']
            cursor = query['cursor']
            start = {'after-one': 1, 'after-two': 2, None: 0}[cursor]
            total = 3 if query['property'] == 'AXChildren' else 1
            end = min(total, start + query['limit'])
            targets = ['child-' + str(index) for index in range(start, end)] if total == 3 else ['outside-parent']
            relation = dict(source=binding['node'], property=query['property'], targets=targets,
                            certainty='confirmed', state='complete' if start == 0 and end == total else 'partial', offset=start, total=total)
            if end < total:
                relation['cursor'] = {1: 'after-one', 2: 'after-two'}[end]
            result = dict(kind='relation', session=session, surface=binding['surface'], platform='macos',
                          observed=dict(id='relation-page', startedAt=3, completedAt=4), relation=relation)
            if mode == 'relations-stale' and cursor is not None:
                error = dict(code='snapshot_expired', effect='none', target='native relation',
                             detail='The previously observed prefix changed.', next='read the source again')
            if mode == 'relations-wrong-source':
                relation['source'] = 'unrelated'
            if mode == 'relations-wrong-range':
                relation.update(offset=0, state='partial')
            if mode == 'relations-malformed':
                relation.update(state='complete', offset=1)
        elif params['query']['kind'] in ('applications', 'surfaces', 'roots'):
            assert all(binding[key] is None for key in ('instance', 'surface', 'rootWindow', 'node', 'capture'))
            kind = params['query']['kind']
            result = dict(kind=kind, session=session, platform='macos', observed=dict(id='directory', startedAt=1, completedAt=2), stopReasons=[])
            if kind == 'applications':
                application_directories += 1
                result['applications'] = [dict(id='running:app', displayName='Native fixture', isRunning=True,
                    path='/Applications/Fixture.app', identifier='local.fixture')]
                if mode.startswith('observation-sdk-window-selector'):
                    result['scope'] = 'runningWindows'
                    result['applications'][0]['windows'] = [dict(selector='instance', window='root'),
                        dict(selector='second-instance', window='second-root')]
                if mode == 'observation-installed' or launch_observation:
                    result['applications'][0].update(isRunning=False, id='installation-observed')
                if launch_wrapper:
                    result['applications'][0]['matchKeys'] = ['fixture.window-class:editor']
                    threshold = (3 if mode.endswith(('-delayed', '-exited')) else
                                 7 if mode.endswith('-retry') else 2)
                    if application_directories >= threshold:
                        result['applications'].append(dict(id='running:child', displayName='Window owner',
                            isRunning=True, path=None, identifier=None, matchKeys=['fixture.window-class:editor']))
                        if mode.endswith('-conflict'):
                            result['applications'].append({**result['applications'][-1], 'id':'running:other'})
                    if application_directories > 1 and mode.endswith('-partial'):
                        result['stopReasons'] = ['application discovery incomplete']
                    if application_directories > 1 and mode.endswith('-removed'):
                        result['applications'][0]['id'] = 'another-installation'
                if mode == 'observation-partial-applications':
                    result['stopReasons'] = ['application discovery incomplete']
            elif kind == 'roots':
                result.update(application=params['query']['application'], roots=[dict(kind='menuBar', instance='menu-instance',
                    surface='menu-surface', node='menubar')])
                if mode.endswith('-unavailable'):
                    result.update(roots=[], stopReasons=['menu provider timeout'])
                if mode.endswith('-wrong-app'):
                    result['application'] = 'another-app'
            else:
                directories += 1
                result['application'] = params['query']['application'] if mode != 'directory-wrong-app' else 'unrelated'
                result['provider'] = dict(status='alreadyEnabled', attribute=None, writeStatus=None)
                if loading and mode.endswith('-initializing'):
                    result['provider'] = dict(status='requestReturned' if directories == 1 else 'alreadyAttempted',
                        attribute='AXManualAccessibility', writeStatus=0 if directories == 1 else None)
                if loading and mode.endswith('-already-attempted'):
                    result['provider']['status'] = 'alreadyAttempted'
                if loading and mode.endswith('-partial-directory') and directories >= 2:
                    result['stopReasons'] = ['provider directory timeout']
                result['surfaces'] = [dict(window='root', instance='instance', surface='surface', node='node', title='Native fixture',
                    kind='window', parent=None, parentState='none', modal=False, nativeWindow=True, accessibility=True)]
                if mode.startswith('observation-sdk-window-selector'):
                    result['surfaces'].append({**result['surfaces'][0], 'window': 'second-root',
                        'instance': 'second-instance', 'surface': 'second-surface', 'node': 'node-second'})
                    if mode.endswith('-failed-first'):
                        if directories == 1:
                            error = dict(code='observation_incomplete', effect='none', target='directory',
                                detail='Directory unavailable.', next='read again')
                        else:
                            result['surfaces'].pop()
                if menus:
                    if mode.endswith('-windowless'):
                        result['surfaces'] = []
                    if mode.endswith('-root-closed') and menu_reads:
                        result['surfaces'] = []
                if mode == 'observation-sdk-multiple' or mode.startswith('observation-sdk-overview-'):
                    result['surfaces'].append(dict(window='second-root', instance='second-instance', surface='second-surface', node='node-second',
                        title='Second document', kind='window', parent=None, parentState='none', modal=False,
                        nativeWindow=True, accessibility=True))
                if mode == 'observation-sdk-overview-many':
                    result['surfaces'].extend(dict(window=f'root-{i}', instance=f'instance-{i}', surface=f'surface-{i}',
                        node=None, title=f'Window {i}', kind='window', parent=None, parentState='unknown', modal=None,
                        nativeWindow=True, accessibility=False) for i in range(2, 9))
                if mode == 'observation-sdk-overview-recreated' and captures:
                    result['surfaces'][1].update(window='new-auxiliary', instance='new-instance',
                        surface='new-surface')
                if mode == 'observation-sdk-overview-no-ax':
                    for surface in result['surfaces']:
                        surface.update(node=None, accessibility=False, parentState='unknown', modal=None)
                if launch_observation:
                    assert params['query']['application'] in (('running:launched', 'running:child') if launch_wrapper else ('running:launched',))
                    if launch_wrapper and params['query']['application'] == 'running:launched':
                        result['surfaces'] = []
                        if mode.endswith('-exited'):
                            error = dict(code='stale_target', effect='none', target='running:launched',
                                detail='The confirmed wrapper exited.', next='read again')
                    if launch_wrapper and mode.endswith('-root-gone') and directories >= 2:
                        result['surfaces'][0]['window'] = 'another-root'
                    if mode in ('observation-launch-empty', 'observation-launch-partial', 'observation-launch-reuse-empty') or (mode == 'observation-launch-delayed' and directories == 1):
                        result['surfaces'] = []
                    if mode == 'observation-launch-partial':
                        result['stopReasons'] = ['provider directory timeout']
                    if mode == 'observation-launch-multiple':
                        result['surfaces'].append({**result['surfaces'][0], 'window': 'another-root',
                            'instance': 'another-instance', 'surface': 'another-surface', 'node': 'another-node', 'title': 'Other document'})
                    if mode == 'observation-launch-directory-error' or mode == 'observation-launch-directory-once' and directories == 1:
                        error = dict(code='execution_failed', effect='none', target='directory', detail='Directory read failed.', next='read again')
                    if mode == 'observation-launch-root-gone' and directories >= 2:
                        result['surfaces'][0]['window'] = 'another-root'
                if mode.startswith(('capture', 'pointer')):
                    result['surfaces'][0].update(node=None, accessibility=False, parentState='unknown', modal=None)
                if (mode in ('observation-modal', 'observation-churn', 'observation-multiple-modal') and directories >= 2
                        or mode == 'observation-loading-scope-first' and directories >= 2
                        or mode == 'observation-loading-scope-late' and directories >= 3
                        or mode in ('observation-partial-modal', 'observation-modal-closed') and directories == 1):
                    suffix = str(directories) if mode == 'observation-churn' else '2'
                    result['surfaces'].append(dict(window='dialog-' + suffix, instance='dialog-instance-' + suffix,
                        surface='dialog-surface-' + suffix, node='dialog-node-' + suffix, title='Dialog ' + suffix,
                        kind='sheet', parent='root', parentState='known', modal=True, nativeWindow=True, accessibility=True))
                    if mode == 'observation-multiple-modal':
                        result['surfaces'].append({**result['surfaces'][-1], 'window': 'other-dialog',
                            'instance': 'other-dialog-instance', 'surface': 'other-dialog-surface',
                            'node': 'other-dialog-node', 'title': 'Other dialog'})
                if mode == 'observation-partial-modal' and directories > 1:
                    result['stopReasons'] = ['provider timeout']
                if mode in ('observation-root-gone', 'observation-partial-gone', 'observation-root-reused') and directories >= 2:
                    result['surfaces'][0].update(instance='other-instance', surface='other-surface')
                    if mode != 'observation-root-reused':
                        result['surfaces'][0]['window'] = 'other-root'
                    if mode == 'observation-partial-gone':
                        result['stopReasons'] = ['provider timeout']
        if params['query']['kind'] == 'inputContext':
            if mode.startswith('desktop-input'):
                pointer = operation_intent in ('click', 'scroll', 'drag')
                declared = dict(id='desktop-route', unit='pointer_sequence' if pointer else 'key_sequence',
                    channel='pointer' if pointer else 'keyboard', supported='yes', readiness='yes',
                    pending=[], backgroundPreparation='no')
                result.update(targetLabel='Desktop', enabled='unknown', primitives=[declared],
                    input={**context, 'focusedNode': None, 'keyboardSurface': None,
                           'geometryCurrent': 'yes' if pointer else 'unknown'})
            if sdk:
                target = params['target']
                declared = dict(primitive)
                result.update(nativeRole='AXButton' if (target['node'] or '').startswith('button') else 'AXTextField',
                              actionsComplete=True, within=params['query'].get('within', []))
                result['input'] = {**context, 'windowRoute': 'yes', 'keyboardSurface': target['surface'],
                                   'focusedNode': target['node']}
                if target['capture'] is not None:
                    declared.update(id='pointer', unit='pointer_sequence', channel='pointer')
                    declared.pop('action')
                    result['input'].update(pointerRoute='yes', geometryCurrent='yes')
                elif operation_intent in ('key', 'focus', 'insert', 'paste'):
                    declared.update(id='keyboard', unit='clipboard_paste' if operation_intent == 'paste' else 'key_sequence', channel='keyboard')
                    declared.pop('action')
                elif operation_intent == 'replace':
                    declared.update(id='replace', unit='native_text_edit')
                    declared.pop('action')
                elif operation_intent in ('openMenu', 'action'):
                    declared['action'] = dict(name=original_intent['parameters'].get('action', 'AXShowMenu'))
                result['primitives'] = [declared]
            inspections += 1
            # Keep the selection on the peer, just as a native contract does.
            # An execute frame cannot supply/override its own input authority.
            contracts = {p['id']: dict(target=params['target'], actuator=params['query'].get('actuator'),
                                      input=result['input']) for p in result['primitives']}
        if params['query']['kind'] == 'launch':
            declared.update(id='launch-contract', unit='launch_application')
            declared.pop('action', None)
            result = dict(kind='launch', binding=params['target'], observed='launch-observation', targetLabel='Fixture', primitives=[declared])
            contracts = {declared['id']: dict(target=params['target'])}
            if mode == 'launch-stale':
                error = dict(code='stale_target', effect='none', target='installation', detail='Bundle changed.', next='read the directory')
        if params['query']['kind'] == 'capturedText':
            if mode == 'nodes-text-disconnect':
                sys.exit(0)
            query = params['query']
            assert query['textRef'] == 'text-capture' and operation_intent == 'read'
            assert params['target'] == dict(session=session, instance=None, rootWindow=None, surface=None, node=None, capture=None)
            total = 19999 if mode == 'nodes-text-changed' else 20000
            offset, end = query['offset'], min(total, query['offset'] + query['length'])
            if mode == 'nodes-text-over-budget': end += 1
            page = dict(text='x' * (end - offset), offset=offset, total=total, next=end if end < total else None)
            if mode == 'nodes-text-wrong-offset': page.update(offset=offset + 1, next=end + 1)
            if mode == 'nodes-text-bad-page': page['next'] = offset
            result = dict(kind='capturedText', session=session, textRef=query['textRef'], page=page)
            if mode == 'nodes-text-wrong-ref': result['textRef'] = 'another-capture'
            if mode == 'nodes-text-wrong-session': result['session'] = 'another-session'
            if mode == 'nodes-text-expired':
                error = dict(code='snapshot_expired', effect='none', target='text capture',
                             detail='Captured text was evicted.', next='read explicitly for a new capture')
        if params['query']['kind'] == 'inputContext' and 'basis' in params['query']:
            result['basis'] = params['query']['basis']
            if mode == 'keyboard-actuator-wrong-basis': result['basis'] = [{**result['basis'][0], 'observed': 'different'}]
        if params['query']['kind'] == 'inputContext' and 'receiver' in params['query']:
            result['receiver'] = params['query']['receiver']
            if mode == 'keyboard-virtual-wrong-echo': result['receiver'] = {**result['receiver'], 'popup': 'foreign-popup'}
            if mode == 'keyboard-virtual-missing-echo': del result['receiver']
            if mode == 'keyboard-member-wrong-echo': result['receiver'] = {**result['receiver'], 'node': 'foreign-focus'}
            if mode == 'keyboard-member-missing-echo': del result['receiver']
    elif method == 'capture':
        assert decode_capture_request(params) == params
        assert params['target'] == binding and binding['node'] is None and binding['capture'] is None
        captures += 1
        image = dict(id='image-' + str(captures), kind='window', scope=binding['surface'],
                     startedAt=2, completedAt=3, width=4, height=2, png=pixels())
        if mode.endswith('-capture-notes'):
            image['stopReasons'] = ['Initial client repaint is unconfirmed within the capture budget.']
        result = dict(kind='capture', binding=binding, observed=dict(id='observation', startedAt=1, completedAt=4),
            image=image, coordinates=dict(space='window-pixels', origin='top-left', nativeSpace='screen-points', toNative=[2, 0, 0, 2, -200, 80]))
        if mode == 'capture-wrong-window':
            result['binding'] = {**binding, 'rootWindow': 'other-document'}
        if mode == 'capture-wrong-image-scope':
            image['scope'] = 'unrelated'
        if mode == 'capture-bad-png':
            image['width'] = 5
        if mode in ('observation-image-error', 'observation-both-error'):
            error = dict(code='execution_failed', effect='none', target='image',
                detail='Screen recording permission denied.', next='enable screen recording')
        if mode == 'observation-sdk-overview-image-error' and binding['surface'] == 'second-surface':
            error = dict(code='observation_incomplete', effect='none', target='image',
                detail='Second window capture unavailable.', next='read the window again')
        if mode == 'observation-sdk-overview-wait':
            time.sleep(0.2)
        if params['kind'] == 'desktop':
            assert binding == dict(session=session, instance=None, rootWindow=None, surface=None, node=None, capture=None)
            scope = 'desktop-scope-' + str(captures)
            image.update(kind='desktop', scope=scope)
            box = dict(x=0, y=0, width=4, height=2)
            observation = dict(mode=request_mode, canvas=dict(width=4, height=2),
                frontmost=dict(name='Native fixture', selector='running:app'),
                displays=[dict(name='Display 1', rect=box, captured=True)],
                windows=[dict(app='Native fixture', title='Document', selector='running:app', rect=box),
                         dict(app='Native fixture', title='', selector='running:app', rect=dict(x=1,y=0,width=2,height=1))],
                omittedWindows=0)
            result = dict(kind='desktopCapture', binding=binding, observed=result['observed'],
                desktopRef=image['id'], scope=scope, observation=observation, screenshot=image)
            if mode == 'desktop-partial':
                observation['displays'][0]['rect'] = dict(x=0,y=0,width=2,height=2)
                observation['displays'].append(dict(name='Display 2', rect=dict(x=3,y=0,width=1,height=2),
                    captured=False, error='Screen recording unavailable for this display'))
                observation.update(omittedWindows=2, catalogError='Window catalogue is incomplete')
            if mode == 'desktop-no-image':
                observation['displays'][0].update(captured=False, error='Screen recording permission denied')
                result['screenshot'] = None
            if mode == 'desktop-wrong-scope': image['scope'] = 'other'
            if mode == 'desktop-wrong-id': result['desktopRef'] = 'another-image'
            if mode == 'desktop-wrong-binding': result['binding'] = {**binding, 'session': 'other'}
            if mode == 'desktop-wrong-mode': observation['mode'] = 'exclusive' if request_mode == 'shared' else 'shared'
            if mode == 'desktop-bad-png': image['width'] = 3
            if mode == 'desktop-bad-canvas': observation['canvas']['width'] = 5
            if mode == 'desktop-missing-image': result['screenshot'] = None
            if mode == 'desktop-image-time': image['completedAt'] = 6
            if mode == 'desktop-window-kind': result['kind'] = 'capture'
            if mode == 'desktop-delay': time.sleep(0.15)
            if mode == 'desktop-error':
                error = dict(code='desktop_unavailable', effect='none', target='Desktop', detail='Display unavailable', next='observe after unlocking')
    elif method == "execute":
        assert decode_unit_command(params['command']) == params['command']
        contract = contracts[params['command']['primitiveId']]
        assert contract['target'] == params['command']['target']
        result = dict(target=params['command']['target'], dispatch="dispatched", detail="native call returned", preparation=[],
                      confirmation="unavailable", cleanup="complete", errorCode=None, retryReason=None, context=None)
        if mode.startswith('desktop-input'):
            assert binding['capture'] is not None and all(binding[k] is None for k in ('instance', 'rootWindow', 'surface', 'node'))
            assert params['command']['kind'] == ('pointer_sequence' if operation_intent in ('click', 'scroll', 'drag') else 'key_sequence')
            assert not params['command']['foreground']
            if mode in ('desktop-input-partial', 'desktop-input-unknown'):
                result.update(dispatch=mode.rsplit('-', 1)[1], errorCode='execution_unknown', detail='Desktop input interrupted.')
        if sdk:
            if operation_intent == 'replace':
                sdk_value = original_intent['parameters']['text']
            elif operation_intent in ('insert', 'paste'):
                sdk_value += original_intent['parameters']['text']
            if params['command']['kind'] == 'prepare_receiver':
                result.update(dispatch='notDispatched', confirmation='confirmed')
            if mode == 'observation-sdk-uncertain':
                result.update(dispatch='unknown', errorCode='execution_unknown')
        if operation_intent == 'launch':
            assert params['command']['kind'] == 'launch_application'
            result.update(confirmation='confirmed', application=dict(id='running:app', displayName='Fixture',
                isRunning=True, path='/Applications/Fixture.app', identifier='local.fixture'))
            if launch_observation:
                result['application'].update(id='running:launched', displayName='Native fixture')
                result['detail'] = 'The selected application started; window readiness requires a read.'
            if mode in ('launch-already-running', 'observation-launch-reuse', 'observation-launch-reuse-empty'):
                result['dispatch'] = 'notDispatched'
                result['detail'] = 'The selected installation is already running; no launch was sent.'
            if mode in ('launch-uncertain', 'observation-launch-uncertain'):
                result.pop('application')
                result.update(dispatch='unknown', errorCode='execution_failed', confirmation='unavailable', detail='Launch outcome unknown.')
        if mode.startswith('scroll'):
            assert params['command']['kind'] == 'native_scroll' and not params['command']['foreground']
            if mode == 'scroll-pattern':
                assert not contract.get('actuator')
            else:
                assert contract['actuator'] == dict(node='down', region='node', bar='bar', relation='scrollPage')
            if mode == 'scroll-partial': result.update(dispatch='partial', errorCode='input_unavailable', detail='Page button changed after one page.')
        if mode.startswith('pointer') and params['command']['primitiveId'] == 'pointer-route':
            command = params['command']
            assert command['primitiveId'] == 'pointer-route' and command['target'] == binding
            if mode.startswith('pointer-element'):
                assert binding['node'] is not None and binding['capture'] is None
                assert set(original_intent['parameters']) <= {'button', 'count'}
            else:
                assert binding['node'] is None and binding['capture'] is not None
            if command['kind'] == 'prepare_receiver':
                result.update(dispatch='notDispatched', confirmation='confirmed', preparation=['pointer window route'])
                if mode == 'pointer-background-unavailable' and not command['foreground']:
                    result.update(errorCode='input_unavailable', retryReason='preparationUnavailable', confirmation='unavailable')
                else: prepared = True
            else:
                assert command['kind'] == 'pointer_sequence' and not command['foreground']
                assert contract['input']['pointerRoute'] == 'yes' and contract['input']['focusedNode'] is None
                if mode in ('pointer-partial', 'pointer-element-partial'):
                    result.update(dispatch='partial', errorCode='input_unavailable', detail='Window changed during drag.')
        if mode == 'pointer-element-object-unknown' and params['command']['primitiveId'] == primitive['id']:
            assert params['command']['kind'] == 'native_action'
            result.update(dispatch='unknown', errorCode='execution_unknown', detail='Native action outcome unknown.')
        if mode.startswith('keyboard'):
            command = params['command']
            if mode.startswith('keyboard-actuator') or mode == 'keyboard-replace-contained':
                assert command['target'] == binding
                assert command['primitiveId'] == 'key-route:' + contract['actuator']['node']
                assert contract['input']['editorOwner'] == binding['node']
            elif mode.startswith('keyboard-rotate'):
                assert command['primitiveId'].startswith('key-route-')
            else:
                assert command['primitiveId'] == 'key-route'
            if command['kind'] == 'prepare_receiver':
                preparations += 1
                result.update(dispatch='notDispatched', confirmation='confirmed', preparation=['native focus'])
                if mode == 'keyboard-failed-background' and not command['foreground']:
                    result.update(errorCode='input_unavailable', retryReason='preparationUnavailable', confirmation='unavailable')
                else:
                    prepared = True
            else:
                assert command['kind'] == ('clipboard_paste' if operation_intent == 'paste' else 'key_sequence') and not command['foreground']
                key_attempts += 1
                if mode in ('keyboard-rotate-unavailable', 'keyboard-rotate-precondition'):
                    result.update(dispatch='notDispatched', errorCode='input_unavailable',
                        retryReason='routeUnavailable' if mode.endswith('unavailable') else 'preconditionChanged')
                if mode in ('keyboard-partial', 'keyboard-unclean', 'keyboard-actuator-partial'):
                    result.update(dispatch='partial', errorCode='input_unavailable', detail='Receiver changed after modifier down.')
                    if mode == 'keyboard-unclean': result['cleanup'] = 'unknown'
                if mode in ('keyboard-replace-partial', 'keyboard-replace-unknown'):
                    result.update(dispatch='partial' if mode.endswith('partial') else 'unknown',
                        errorCode='input_unavailable', confirmation='notRequested',
                        detail='Selection may have changed; no replacement text sent.')
                if mode == 'keyboard-replace-empty':
                    assert original_intent['parameters']['text'] == ''
                    result.update(dispatch='notDispatched', confirmation='confirmed', detail='Target is already empty.')
        if mode in ("uncertain", "text-value-uncertain", "action-intents-uncertain", "query-uncertain"):
            result.update(dispatch="unknown", errorCode="execution_unknown")
        if mode == 'query-moved':
            result.update(dispatch='notDispatched', errorCode='stale_target', confirmation='notRequested', detail='Target left the selected logical scope.')
        if mode in ("text-value", "text-value-v5"):
            assert params['command']['kind'] == 'native_text_edit'
            assert original_intent['kind'] == 'replace'
            assert set(original_intent['parameters']) == {'text'}
            result.update(confirmation="confirmed", detail="Target value verified; no submission requested.")
        if mode == "invalid-retry":
            result.update(retryReason="routeUnavailable")
    elif method == "finish":
        finishes += 1
        result = dict(cleanup="unknown" if mode in ('capture-cleanup-unknown', 'desktop-cleanup-unknown', 'keyboard-unclean')
            or mode == 'observation-cleanup-unknown' and finishes == 2
            or mode == 'nodes-text-cleanup-unknown' and finishes == 2
            or mode == 'observation-launch-cleanup-unknown' and operation_intent == 'launch' else "complete")
    else:
        raise AssertionError(method)
    print(json.dumps(dict(version=5, session=session, id=request["id"], **({'error': error} if error else {'result': result}))), flush=True)
