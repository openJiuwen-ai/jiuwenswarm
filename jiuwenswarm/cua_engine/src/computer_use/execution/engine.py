"""One intent, one deadline, a finite route plan, and no uncertain replay."""
import asyncio
from dataclasses import replace

from ..errors import ComputerUseError
from ..protocol.element_path import element_path
from .control_part import ControlPartQuery
from .focus_member import FocusMemberQuery
from .menu import MenuContextQuery
from .native import ApplicationBinding, Cleanup, Confirmation, Dispatch, Intent, Knowledge, NativePort, OperationTarget
from .policy import (
    Attempts,
    choose,
    has_independent_text_route,
    has_ready_route,
    needs_pointer_member,
    route_key,
    validate_target,
)
from .query import resolve_path
from .receiver import ReceiverQuery
from .scroll import PageRouteUnavailable, ScrollPointerQuery, ScrollQuery
from .virtual_focus import VirtualFocusQuery


class Engine:

    def __init__(self, native: NativePort):
        self.native = native

    async def execute(self, intent: Intent, target: OperationTarget, timeout_ms=30000, *, path=None,
                      query_timeout_ms=0):
        if self.native.calls.retired or self.native.calls.busy:
            raise ComputerUseError(
                dict(
                    code='execution_unknown' if self.native.calls.retired else 'invalid_request', effect='none',
                    target='desktop', detail='The native session is retired.' if self.native.calls.retired else
                    'Another native call is in progress.', next='close the old driver and observe a new session'
                    if self.native.calls.retired else 'await the active call'))
        with self.native.calls.claim(timeout_ms) as remaining:
            return await self.execute_in_call(intent, target, remaining, path=path, query_timeout_ms=query_timeout_ms)

    async def execute_in_call(self, intent, target, remaining, *, path=None, query_timeout_ms=0):
        """Execute a write stage inside a coordinator's existing call budget."""
        self.native.calls.validate_claim(remaining)
        invalid_query_wait = type(
            query_timeout_ms) is not int or not 0 <= query_timeout_ms <= 10000 or path is None and query_timeout_ms
        if invalid_query_wait:
            raise ValueError('A bounded query wait requires an element path')
        invalid_launch_request = (isinstance(target, ApplicationBinding) != (intent.kind == 'launch')
                                  or intent.kind == 'launch' and (intent.parameters or path is not None))
        if invalid_launch_request:
            raise ValueError('Launch requires an observed installation and has no element path or parameters')
        query_root = None
        if path is not None:
            path = [dict(segment) for segment in element_path(path)]
            invalid_path_root = (
                target.node is None or target.capture is not None or target.instance is None or target.surface is None
            )
            if invalid_path_root:
                raise ValueError('A scoped path requires an observed accessible working-window root')
            query_root = target
            target = replace(target, node=None)
        attempts, steps, preparation = Attempts(), [], []
        dispatched = Dispatch.NOT_DISPATCHED
        result = error = None
        try:
            async with self.native.calls.operation(target, remaining, intent=intent) as lease:
                operation, target = lease.token, lease.target
                within = ()
                if query_root is not None:
                    query_root = replace(target, node=query_root.node)
                    target, within = await resolve_path(self.native, operation, query_root, remaining, path,
                                                        timeout_ms=query_timeout_ms)
                menu_context = None
                pointer_member = None
                scroll_member = None
                while True:
                    scope = {'within': within} if within else {}
                    capabilities = getattr(self.native, 'capabilities', ())
                    facts = None
                    needs_editing_evidence = False
                    if intent.kind == 'click' and pointer_member is not None:
                        # Revalidate the selected member after preparation; its
                        # requested owner and structural basis remain fixed.
                        query = ControlPartQuery(self.native, operation, target, remaining, intent=intent)
                        selected = await query.resolve(pointer_member)
                        if selected is not None:
                            scope['actuator'], scope['basis'] = selected, query.basis
                    else:
                        needs_element_scroll = (intent.kind == 'scroll'
                                                and {'nativeScroll', 'nativeElementScroll'} & set(capabilities)
                                                and target.node is not None and target.capture is None
                                                and not any(k in intent.parameters for k in ('x', 'y')))
                        if needs_element_scroll:
                            selected = None
                            # An addressed page action is preferable when it exactly
                            # represents the request. A proven no-dispatch refusal may
                            # advance the finite plan; uncertain input never gets here.
                            if ('nativeScroll' in capabilities
                                    and not any(route.unit == 'native_scroll' for route in attempts.unavailable)):
                                query = ScrollQuery(self.native, operation, target, remaining)
                                try:
                                    selected = await query.resolve(intent.parameters)
                                except PageRouteUnavailable:
                                    if 'nativeElementScroll' not in capabilities:
                                        raise
                            if selected is None and 'nativeElementScroll' in capabilities:
                                query = ScrollPointerQuery(self.native, operation, target, remaining)
                                selected = await query.resolve(scroll_member)
                            if selected is not None:
                                scope['actuator'], scope['basis'] = selected, query.basis
                        elif intent.kind == 'replace' and 'nativeTextReplacement' in capabilities:
                            facts = await self.native.inspect(operation, target, remaining(), **scope)
                            if not facts.binding.same_target(target):
                                raise ValueError('Native replacement inspection returned a different target')
                            validate_target(facts, self.native.policy)
                            # An object setter addresses exactly that object; it
                            # does not prove a composite owner is the actual editor.
                            # Resolve ownership before selecting even a ready setter.
                            if ('nativeActuators' in capabilities
                                    or not has_independent_text_route(facts, intent, attempts)):
                                if target.node is None:
                                    raise ValueError('Full-text replacement requires a specified text control')
                                query = ReceiverQuery(self.native, operation, target, remaining)
                                receiver = await query.resolve(
                                    require_editable=True, allow_preparation='nativeEditorPreparation' in capabilities,
                                    focused=facts.input.focused_node if 'nativeActuators' in capabilities else None,
                                    allow_internal='nativeActuators' in capabilities)
                                selected = receiver.actuator
                                needs_editing_evidence = receiver.needs_editing_evidence
                                if selected is not None:
                                    if 'nativeActuators' not in capabilities:
                                        raise self._failure('mode_unsupported', facts.target_label,
                                                            'The driver cannot bind the selected internal editor.',
                                                            dispatched, preparation)
                                    scope['actuator'] = selected
                                    scope['basis'] = query.basis
                                    facts = None
                        else:
                            needs_input_context = (
                                intent.kind in ('focus', 'key', 'insert', 'paste')
                                and ('nativeActuators' in capabilities
                                     or intent.kind in ('insert', 'paste') and 'nativeTextInput' in capabilities)
                                and target.node is not None
                                and not (intent.kind in ('insert', 'paste') and intent.parameters.get('text') == ''))
                            if needs_input_context:
                                # Preparation may replace a field editor. Resolve again in
                                # the same operation; the requested target and its anchors
                                # never change. The native contract pins/rechecks this pair.
                                facts = await self.native.inspect(operation, target, remaining(), **scope)
                                if not facts.binding.same_target(target):
                                    raise ValueError('Native receiver inspection returned a different target')
                                validate_target(facts, self.native.policy)
                                query = ReceiverQuery(self.native, operation, target, remaining)
                                receiver = await query.resolve(
                                    require_editable=intent.kind in ('insert', 'paste'),
                                    allow_preparation='nativeEditorPreparation' in capabilities
                                    and intent.kind in ('insert', 'paste'),
                                    focused=facts.input.focused_node if 'nativeActuators' in capabilities else None,
                                    allow_internal='nativeActuators' in capabilities)
                                selected = receiver.actuator
                                needs_editing_evidence = receiver.needs_editing_evidence
                                if selected is not None:
                                    if 'nativeActuators' not in capabilities:
                                        raise self._failure('mode_unsupported', facts.target_label,
                                                            'The driver cannot bind the selected internal editor.',
                                                            dispatched, preparation)
                                    scope['actuator'] = selected
                                    scope['basis'] = query.basis
                                    facts = None
                    if facts is None:
                        facts = await self.native.inspect(operation, target, remaining(), **scope)
                    if not facts.binding.same_target(target):
                        raise ComputerUseError(
                            dict(code="stale_target", effect="possible" if preparation else "none",
                                 target=facts.target_label, detail="Native inspection returned a different target.",
                                 next="read the intended window again"))
                    validate_target(facts, self.native.policy)
                    # Driver-wide UI capabilities also exist during application
                    # launch, whose installation binding has no node or focus.
                    needs_focused_receiver = (
                        intent.kind in ('focus', 'key', 'insert', 'replace', 'paste')
                        and {'nativeVirtualFocus', 'nativeFocusMembers'} & set(capabilities) and target.node is not None
                        and not (intent.kind in ('insert', 'paste') and intent.parameters.get('text') == '')
                        and facts.input.focused_node is not None and not has_ready_route(facts, intent)
                        and not has_independent_text_route(facts, intent, attempts))
                    if needs_focused_receiver:
                        editor = replace(target, node=facts.actuator.node) if facts.actuator else target
                        for capability, query_type in (('nativeFocusMembers', FocusMemberQuery), ('nativeVirtualFocus',
                                                                                                  VirtualFocusQuery)):
                            if capability not in capabilities:
                                continue
                            query = query_type(self.native, operation, editor, remaining)
                            receiver = await query.resolve(facts.input.focused_node)
                            if receiver is None:
                                continue
                            scope['receiver'] = receiver
                            scope['basis'] = scope.get('basis', ()) + query.basis
                            facts = await self.native.inspect(operation, target, remaining(), **scope)
                            if not facts.binding.same_target(target):
                                raise ValueError('Native receiver inspection changed the requested owner')
                            validate_target(facts, self.native.policy)
                            break
                    needs_control_part = (intent.kind == 'click' and 'nativeControlParts' in capabilities
                                          and target.node is not None and target.capture is None
                                          and facts.input.pointer_hit not in (None, target.node)
                                          and needs_pointer_member(facts, intent, attempts))
                    if needs_control_part:
                        query = ControlPartQuery(self.native, operation, target, remaining, intent=intent)
                        selected = await query.resolve(facts.input.pointer_hit)
                        if selected is not None:
                            pointer_member = selected.node
                            facts = await self.native.inspect(operation, target, remaining(), within=within,
                                                              actuator=selected, basis=query.basis)
                            if not facts.binding.same_target(target):
                                raise ValueError('Native control-part inspection changed the requested owner')
                            validate_target(facts, self.native.policy)
                    needs_scroll_content = (intent.kind == 'scroll' and facts.actuator is not None
                                            and facts.actuator.relation == 'scrollPointer'
                                            and facts.input.pointer_hit not in (None, facts.actuator.node)
                                            and needs_pointer_member(facts, intent, attempts))
                    if needs_scroll_content:
                        query = ScrollPointerQuery(self.native, operation, target, remaining)
                        selected = await query.resolve(facts.input.pointer_hit)
                        if selected is not None:
                            scroll_member = selected.node
                            facts = await self.native.inspect(operation, target, remaining(), within=within,
                                                              actuator=selected, basis=query.basis)
                            if not facts.binding.same_target(target):
                                raise ValueError('Native scroll inspection changed the requested element')
                            validate_target(facts, self.native.policy)
                    needs_menu_context = ('nativeMenuActions' in capabilities and facts.platform == 'macos'
                                          and facts.native_role in ('AXMenuBarItem', 'AXMenuItem', 'AXMenu')
                                          and any(p.unit == 'native_action' and p.supported == Knowledge.YES
                                                  for p in facts.primitives))
                    if needs_menu_context:
                        if menu_context is None:
                            query = MenuContextQuery(self.native, operation, target, remaining)
                            if facts.application_menu is False:
                                # A local popup is already scoped to its observed window.
                                within, has_submenu = within, (await query.has_submenu()
                                                               if intent.kind == 'openMenu' else False)
                            else:
                                within, has_submenu = await query.resolve(within,
                                                                          inspect_submenu=intent.kind == 'openMenu')
                            menu_context = within, has_submenu
                            facts = await self.native.inspect(operation, target, remaining(), within=within)
                            if not facts.binding.same_target(target):
                                raise ValueError('Native menu inspection returned a different target')
                            validate_target(facts, self.native.policy)
                        facts = replace(facts, has_submenu=menu_context[1])
                    if intent.kind in ("insert", "paste") and intent.parameters.get("text") == "":
                        result = dict(target=facts.target_label, status="verified",
                                      detail="Empty text; no input or preparation was needed.")
                        break
                    attempts.observe(facts)
                    command = choose(facts, intent, self.native.policy, attempts,
                                     needs_editing_evidence=needs_editing_evidence)
                    route = route_key(command)
                    # The native unit owns the final guard, dispatch barrier and
                    # paired release. Transport errors cannot prove no dispatch.
                    budget = remaining()
                    try:
                        step = await self.native.execute(operation, len(steps) + 1, command, budget)
                    except BaseException as dispatch_error:
                        if isinstance(dispatch_error, ComputerUseError) and dispatch_error.effect == 'none':
                            # A typed admission/validation refusal is distinct from
                            # a lost execute reply. Finish normally; do not replay.
                            raise
                        self.native.calls.retired = True
                        if not command.prepare:
                            dispatched = Dispatch.UNKNOWN
                        else:
                            preparation.append("unknown")
                        raise
                    steps.append(step)
                    preparation.extend(step.preparation)
                    if not step.target.same_target(target):
                        self.native.calls.retired = True
                        dispatched = Dispatch.UNKNOWN
                        raise ValueError("Native step returned a different target")
                    if step.dispatch != Dispatch.NOT_DISPATCHED:
                        dispatched = step.dispatch
                    if step.cleanup != Cleanup.COMPLETE:
                        self.native.calls.retired = True
                        raise ValueError("Native input cleanup was not confirmed")
                    # Preparation and delivery share one structural replan budget.
                    # No requested input or uncertain cleanup may be repeated.
                    if step.dispatch == Dispatch.NOT_DISPATCHED and step.retry_reason == 'preconditionChanged':
                        if attempts.precondition_changes < 1:
                            attempts.precondition_changes += 1
                            attempts.prepared = {p for p in attempts.prepared if p[0] != route}
                            attempts.background_unavailable.discard(route)
                            continue
                    if command.prepare:
                        if step.dispatch != Dispatch.NOT_DISPATCHED:
                            raise ValueError("Preparation unexpectedly dispatched the requested input")
                        attempts.prepared.add((route, command.foreground))
                        if step.error_code is not None:
                            if step.retry_reason == "preparationUnavailable" and not command.foreground:
                                attempts.background_unavailable.add(route)
                            else:
                                raise self._failure(step.error_code, facts.target_label, step.detail, dispatched,
                                                    preparation)
                        elif intent.kind == "focus" and step.confirmation == Confirmation.CONFIRMED:
                            result = dict(target=facts.target_label, status="verified", detail=step.detail)
                            if step.context is not None:
                                result["context"] = dict(step.context)
                            break
                        continue  # Any preparation may change the receiver: inspect anew.
                    if step.dispatch in (Dispatch.UNKNOWN, Dispatch.PARTIAL) or step.error_code is not None:
                        if step.dispatch == Dispatch.NOT_DISPATCHED and step.retry_reason == "routeUnavailable":
                            attempts.unavailable.add(route)
                            continue
                        raise self._failure(step.error_code or "execution_unknown", facts.target_label, step.detail,
                                            dispatched, preparation)
                    if step.dispatch != Dispatch.DISPATCHED:
                        if not (intent.kind in ('replace', 'launch') and step.dispatch == Dispatch.NOT_DISPATCHED
                                and step.confirmation == Confirmation.CONFIRMED):
                            raise ValueError("A completed input unit did not report dispatch")
                    result = dict(target=facts.target_label,
                                  status="verified" if step.confirmation == Confirmation.CONFIRMED else "dispatched",
                                  detail=step.detail)
                    if step.context is not None:
                        result["context"] = dict(step.context)
                    if intent.kind == 'launch':
                        if step.application is None:
                            raise ValueError('Application launch completed without a running-instance receipt')
                        result['application'] = dict(step.application)
                        result['dispatch'] = step.dispatch.value
                    break
        except BaseException as caught:
            if isinstance(caught, asyncio.CancelledError):
                error = caught
            elif isinstance(caught, ComputerUseError):
                error = self._failure(caught.code, caught.target, caught.detail, dispatched, preparation,
                                      minimum_effect=caught.effect, next_step=caught.next)
            else:
                error = self._failure(
                    "execution_unknown" if dispatched != Dispatch.NOT_DISPATCHED else "execution_failed", "desktop",
                    str(caught), dispatched, preparation)
            if error is not caught:
                error.__cause__ = caught
                for note in getattr(caught, '__notes__', ()):
                    error.add_note(note)
        if error is not None:
            raise error
        return result

    @staticmethod
    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    def _failure(code, target, detail, dispatched, preparation, minimum_effect="none", next_step=None):
        effect = (
            "partial"
            if dispatched == Dispatch.PARTIAL
            else "possible"
            if dispatched != Dispatch.NOT_DISPATCHED or preparation
            else "none"
        )
        severity = {"none": 0, "possible": 1, "partial": 2}
        if minimum_effect not in severity or effect not in severity:
            raise ValueError("Unknown native execution effect")
        if severity[minimum_effect] > severity[effect]:
            effect = minimum_effect
        # Keep a specific recovery instruction only when this operation has no
        # possible effects. Preparation and uncertain delivery still require
        # observation before any new attempt.
        recovery = (
            next_step
            if effect == "none" and next_step
            else "read the current target before continuing; do not replay uncertain input"
        )
        return ComputerUseError(dict(code=code, effect=effect, target=target, detail=detail, next=recovery))
