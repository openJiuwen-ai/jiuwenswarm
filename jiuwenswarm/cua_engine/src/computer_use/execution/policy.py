"""Pure, platform-independent route selection. No application-name exceptions."""
from dataclasses import dataclass, field, replace
from typing import NoReturn

from ..errors import ComputerUseError
from ..native_semantics import action_for_intent
from ..protocol.execution import unit_contracts
from .native import (
    ApplicationBinding,
    Channel,
    Facts,
    HostPolicy,
    Intent,
    Knowledge,
    OperationTarget,
    SelectedActuator,
    SelectedReceiver,
    UnitCommand,
)


def refuse(code, facts, detail, next_step="read the target and choose a supported operation") -> NoReturn:
    raise ComputerUseError(dict(code=code, effect="none", target=facts.target_label, detail=detail, next=next_step))


@dataclass
class Attempts:
    unavailable: set['RouteKey'] = field(default_factory=set)
    background_unavailable: set['RouteKey'] = field(default_factory=set)
    prepared: set[tuple['RouteKey', bool]] = field(default_factory=set)
    precondition_changes: int = 0
    actuators: set[tuple[SelectedActuator | None, SelectedReceiver | None]] = field(default_factory=set)

    def observe(self, facts):
        # Preparation can replace a field editor, but an endless stream of new
        # native objects cannot keep granting fresh preparation attempts.
        self.actuators.add((facts.actuator, facts.receiver))
        if len(self.actuators) > 2:
            refuse('input_unavailable', facts, 'The selected receiver kept changing during this operation.',
                   'read the original target after its editor settles')


@dataclass(frozen=True)
class RouteKey:
    target: OperationTarget
    intent: str
    unit: str
    channel: Channel
    actuator: SelectedActuator | None
    receiver: SelectedReceiver | None


def route_key(command: UnitCommand):
    """One semantic route for this intent; opaque contract IDs are not routes.

    The actual primitive/action stays pinned by the native contract. Changes to
    its ID, localized description or support flags cannot reset the attempt
    allowance for the same unit/channel and selected receiver.
    """
    return RouteKey(command.target, command.intent.kind, command.primitive.unit, command.primitive.channel,
                    command.actuator, command.receiver)


def _candidates(facts: Facts, intent: Intent, *, include_unavailable=False):
    candidates = [
        p for p in facts.primitives
        if intent.kind in unit_contracts[p.unit][1] and (p.unit != 'native_action' or intent.kind == 'action')
    ]
    if intent.kind == "action":
        name = intent.parameters.get("action")
        exact = [p for p in candidates if p.action == name]
        matches = exact or [p for p in candidates if p.action_description == name]
        if len(matches) > 1:
            refuse("ambiguous_target", facts, "More than one native action matches this name.")
        return [p for p in matches if include_unavailable or p.supported == Knowledge.YES]
    # Coordinate requests mean pointer events, even when a semantic action exists.
    requires_pointer_event = (
        any(key in intent.parameters for key in ("x", "fromX"))
        or (intent.kind == "click" and
            (intent.parameters.get("button", "left") != "left" or intent.parameters.get("count", 1) != 1)))
    if requires_pointer_event:
        candidates = [p for p in candidates if p.channel == Channel.POINTER]
    elif intent.kind in ("click", "openMenu"):
        actions = [p for p in facts.primitives if p.unit == "native_action" and p.action is not None]
        name = action_for_intent(facts.platform, facts.native_role, [p.action for p in actions], facts.actions_complete,
                                 intent.kind, has_submenu=facts.has_submenu)
        matches = [p for p in actions if p.action == name]
        if len(matches) > 1:
            refuse("ambiguous_target", facts, "More than one native primitive declares the selected action.")
        # Keep the declared primitive unchanged. Python resolves semantics;
        # the driver receives its opaque contract ID and the original intent.
        candidates.extend(p for p in matches if p not in candidates)
    if intent.kind == "paste":
        candidates = [p for p in candidates if p.unit in ("clipboard_paste", "prepare_receiver")]
    if intent.kind == "key":
        candidates = [p for p in candidates if p.unit == "key_sequence"]
    # Independent object edits remain first. Contextual text setters can report
    # success without driving the editor's ordinary input/commit callbacks. If
    # a keyboard contract is already ready, use that editing path instead; do
    # not prepare an unavailable keyboard ahead of a usable contextual edit.
    # choose() still requires editing evidence before permitting keyboard input.
    order = {Channel.OBJECT: 0, Channel.EDITOR: 2, Channel.KEYBOARD: 3, Channel.POINTER: 4}

    def priority(primitive):
        channel_priority = order.get(primitive.channel)
        if channel_priority is None:
            raise ValueError("Unknown native input channel")
        ready_text_keys = (intent.kind in ('insert', 'replace') and primitive.unit == 'key_sequence'
                           and primitive.channel == Channel.KEYBOARD and primitive.readiness == Knowledge.YES)
        return (1 if ready_text_keys else channel_priority, primitive.id)

    return sorted((p for p in candidates if include_unavailable or p.supported == Knowledge.YES), key=priority)


def validate_target(facts: Facts, policy: HostPolicy):
    installation = isinstance(facts.binding, ApplicationBinding)
    if not installation and facts.binding.surface is None and not facts.binding.is_desktop:
        refuse("invalid_request", facts,
               "Session discovery has no input target; select a working window before writing.")
    if facts.identity == Knowledge.NO or facts.within_scope == Knowledge.NO:
        refuse("stale_target", facts, "The original target identity or working-window boundary changed.")
    if facts.identity != Knowledge.YES or facts.within_scope != Knowledge.YES:
        refuse("input_unavailable", facts, "Current target identity or input scope cannot be confirmed.")
    if facts.blocked_by:
        # Native surface identities are diagnostic facts, not public window or
        # element references. A fresh read supplies the actionable references.
        refuse(
            "blocked_by_modal", facts, "A modal dialog blocks the requested target.",
            "call app.read() to observe the dialog, then use the window or element references returned by that read")
    requires_exclusive_desktop = (
        not installation
        and facts.binding.instance is None
        and facts.binding.root_window is None
        and policy.mode != "exclusive"
    )
    if requires_exclusive_desktop:
        refuse("mode_unsupported", facts, "Desktop-wide input requires exclusive mode.")


def has_independent_text_route(facts: Facts, intent: Intent, attempts: Attempts | None = None):
    """An addressed replacement must not acquire unrelated editor dependencies."""
    return any(p.unit == 'native_text_edit' and p.channel == Channel.OBJECT and not any(
        condition in p.pending
        for condition in ('receiver', 'selection', 'geometry')) and (attempts is None or route_key(
            UnitCommand(p, facts.binding, intent, facts.actuator, receiver=facts.receiver)) not in attempts.unavailable)
               for p in _candidates(facts, intent))


def has_ready_route(facts: Facts, intent: Intent):
    """A usable native contract needs no speculative receiver enrichment."""
    return any(p.readiness == Knowledge.YES for p in _candidates(facts, intent))


def needs_pointer_member(facts: Facts, intent: Intent, attempts: Attempts):
    """Resolve a hit member only when this intent has no usable direct route."""
    candidates = []
    for primitive in _candidates(facts, intent):
        if route_key(UnitCommand(primitive, facts.binding, intent, facts.actuator)) in attempts.unavailable:
            continue
        if 'enabled' not in primitive.pending:
            candidates.append(primitive)
    return (any(p.unit == 'pointer_sequence' and 'geometry' in p.pending for p in candidates)
            and not any('geometry' not in p.pending for p in candidates))


def choose(facts: Facts, intent: Intent, policy: HostPolicy, attempts: Attempts, *,
           needs_editing_evidence=False) -> UnitCommand:
    validate_target(facts, policy)

    preparation = []
    for primitive in _candidates(facts, intent):
        if needs_editing_evidence and primitive.unit not in ('prepare_receiver', 'native_text_edit'):
            continue  # A ready keyboard route is not evidence that this control is editable.
        if intent.kind == 'paste' and not needs_editing_evidence and primitive.unit == 'prepare_receiver':
            continue  # Known paste targets can prepare their keyboard route directly.
        command = UnitCommand(primitive, facts.binding, intent, facts.actuator, receiver=facts.receiver)
        key = route_key(command)
        if key in attempts.unavailable:
            continue
        # Native contracts own context-dependent readiness. Do not infer it
        # again from diagnostic enabled/focus fields in a different snapshot.
        if 'enabled' in primitive.pending:
            continue
        if 'geometry' in primitive.pending:
            continue  # Never repair stale coordinates by silently taking a new screenshot.
        if primitive.readiness == Knowledge.YES:
            if primitive.unit == "prepare_receiver" and (key, False) in attempts.prepared:
                continue
            return replace(command, prepare=primitive.unit == "prepare_receiver")
        preparation.append((command, 'foreground' in primitive.pending))

    # Try a usable alternate background route before activating any window.
    for command, needs_foreground in preparation:
        primitive, key = command.primitive, route_key(command)
        failed = key in attempts.background_unavailable
        can_prepare_in_background = (not needs_foreground and not failed
                                     and primitive.background_preparation != Knowledge.NO
                                     and (key, False) not in attempts.prepared)
        if can_prepare_in_background:
            return replace(command, prepare=True)
    for command, needs_foreground in preparation:
        primitive, key = command.primitive, route_key(command)
        background_unavailable = (needs_foreground or primitive.background_preparation == Knowledge.NO
                                  or key in attempts.background_unavailable)
        if (background_unavailable and policy.foreground_allowed and (key, True) not in attempts.prepared):
            return replace(command, prepare=True, foreground=True)
    if needs_editing_evidence:
        refuse("input_unavailable", facts,
               "The selected control has no confirmed text-editing capability or remaining preparation route.",
               "read the intended control or its window; focus alone does not authorize text input")
    reasons = list(dict.fromkeys(p.unavailable_reason for p in _candidates(facts, intent, include_unavailable=True)
                                if p.unavailable_reason))
    detail = "No confirmed route can perform this intent in the current desktop mode."
    if reasons:
        detail += " " + " ".join(reasons)
    refuse("input_unavailable", facts, detail,
           "read the current target; foreground assistance or an independent desktop may be required")
