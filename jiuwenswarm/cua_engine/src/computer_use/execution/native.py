"""The typed boundary between the Python engine and native execution units.

Facts are observations, never authorizations. Native units must revalidate their
own target and route immediately before dispatch even after Python selects them.
"""
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from ..protocol.execution import validate_primitive_contract

if TYPE_CHECKING:
    from .lifecycle import CallLifecycle


class Knowledge(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Channel(str, Enum):
    OBJECT = "object"
    EDITOR = "editor"
    KEYBOARD = "keyboard"
    POINTER = "pointer"


class Dispatch(str, Enum):
    NOT_DISPATCHED = "notDispatched"
    DISPATCHED = "dispatched"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class Confirmation(str, Enum):
    NOT_REQUESTED = "notRequested"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    UNAVAILABLE = "unavailable"


class Cleanup(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"
    UNKNOWN = "unknown"


INTENTS = frozenset(
    ("click", "focus", "insert", "replace", "paste", "key", "openMenu", "action", "scroll", "drag", "launch"))


@dataclass(frozen=True)
class Intent:
    kind: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in INTENTS:
            raise ValueError(f"Unknown execution intent: {self.kind}")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class Binding:
    session: str
    instance: str | None
    root_window: str | None
    surface: str | None
    node: str | None = None
    capture: str | None = None

    @property
    def is_desktop(self) -> bool:
        return (self.capture is not None and self.instance is None and self.root_window is None and self.surface is None
                and self.node is None)

    def same_target(self, other: "Binding") -> bool:
        # A fresh image may show the same window, but its coordinate mapping is
        # a different input binding. An operation must not silently adopt it.
        return self == other

    def canonical(self, value: "Binding") -> "Binding":
        bound_identity_changed = (not isinstance(value, Binding) or value.session != self.session
                                  or value.instance != self.instance or value.surface != self.surface
                                  or value.node != self.node or value.capture != self.capture
                                  or (self.root_window is not None or self.surface is None)
                                  and value.root_window != self.root_window)
        if bound_identity_changed:
            raise ValueError("Native begin changed an already bound target")
        return value


@dataclass(frozen=True)
class ApplicationBinding:
    """One observed installation, not permission to send desktop input."""
    session: str
    application: str

    def same_target(self, other):
        return self == other

    def canonical(self, value):
        if value != self:
            raise ValueError('Native begin changed the selected application installation')
        return value


OperationTarget = Binding | ApplicationBinding


@dataclass(frozen=True)
class OpenedOperation:
    token: str
    target: OperationTarget


@dataclass(frozen=True)
class SelectionDependency:
    """Names of native observations used by a semantic selection, not values."""
    observed: str
    node: str
    fields: tuple[str, ...] = ()
    actions: bool = False
    relations: tuple[str, ...] = ()

    def wire(self):
        return dict(observed=self.observed, node=self.node, fields=list(self.fields), actions=self.actions,
                    relations=list(self.relations))


@dataclass(frozen=True)
class Actuator:
    """A selected native member, never a replacement for the requested owner."""
    node: str
    relation: str = "containedEditor"

    def __post_init__(self):
        from ..protocol.execution import actuator
        actuator.parse(dict(node=self.node, relation=self.relation))

    def wire(self):
        return dict(node=self.node, relation=self.relation)


@dataclass(frozen=True)
class ScrollActuator:
    """An exact native page button and its observed scrolling relationships."""
    node: str
    region: str
    bar: str
    relation: str = field(default='scrollPage', init=False)

    def __post_init__(self):
        from ..protocol.execution import actuator
        actuator.parse(self.wire())
        if self.node in (self.region, self.bar):
            raise ValueError('A page button must be distinct from its scrolling region and bar')

    def wire(self):
        return dict(node=self.node, region=self.region, bar=self.bar, relation=self.relation)


@dataclass(frozen=True)
class ScrollPointerActuator:
    """The nearest scroll area and its current, separately confirmed hit object."""
    node: str
    region: str
    content_path: tuple[str, ...] = ()
    relation: str = field(default='scrollPointer', init=False)

    def __post_init__(self):
        from ..protocol.execution import actuator
        if not isinstance(self.content_path, tuple):
            raise ValueError('A scroll content path must be immutable')
        actuator.parse(self.wire())
        if (self.content_path
                and (self.content_path[0] != self.node or len(set(self.content_path)) != len(self.content_path))):
            raise ValueError('A scroll content path must start at its hit and contain distinct nodes')

    def wire(self):
        result = dict(node=self.node, region=self.region, relation=self.relation)
        if self.content_path:
            result['contentPath'] = list(self.content_path)
        return result


SelectedActuator = Actuator | ScrollActuator | ScrollPointerActuator


@dataclass(frozen=True)
class VirtualReceiver:
    """One selected editor's popup; active-item changes remain live facts."""
    popup: str
    via: str
    relation: str = field(default='virtualFocus', init=False)

    def __post_init__(self):
        from ..protocol.execution import receiver
        receiver.parse(self.wire())

    def wire(self):
        return dict(relation=self.relation, popup=self.popup, via=self.via)


@dataclass(frozen=True)
class FocusMember:
    """One current focus object proved to be a part of the selected control."""
    node: str
    relation: str = field(default='containedFocus', init=False)

    def __post_init__(self):
        from ..protocol.execution import receiver
        receiver.parse(self.wire())

    def wire(self):
        return dict(relation=self.relation, node=self.node)


SelectedReceiver = VirtualReceiver | FocusMember


@dataclass(frozen=True)
class InputContext:
    foreground_surface: str | None = None
    keyboard_surface: str | None = None
    focused_node: str | None = None
    focus_ancestors: tuple[str, ...] = ()
    editor: str | None = None
    editor_owner: str | None = None
    selection_known: Knowledge = Knowledge.UNKNOWN
    geometry_current: Knowledge = Knowledge.UNKNOWN
    window_route: Knowledge = Knowledge.UNKNOWN
    pointer_route: Knowledge = Knowledge.UNKNOWN
    pointer_hit: str | None = None


@dataclass(frozen=True)
class Primitive:
    """A native primitive's declared contract, not a chosen fallback route."""
    id: str
    unit: str
    channel: Channel
    supported: Knowledge
    readiness: Knowledge = Knowledge.UNKNOWN
    pending: tuple[str, ...] = ()
    background_preparation: Knowledge = Knowledge.UNKNOWN
    action: str | None = None
    action_description: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self):
        from ..protocol.execution import unit_contracts
        if self.unit not in unit_contracts or not self.id:
            raise ValueError("Invalid native primitive contract")
        validate_primitive_contract(self.unit, self.channel, self.supported, self.readiness, self.pending, self.action)
        if self.unavailable_reason is not None:
            from ..protocol.execution import identity
            identity.parse(self.unavailable_reason)


@dataclass(frozen=True)
class Facts:
    binding: OperationTarget
    observed: str
    target_label: str
    identity: Knowledge
    within_scope: Knowledge
    enabled: Knowledge
    blocked_by: tuple[str, ...]
    primitives: tuple[Primitive, ...]
    input: InputContext = field(default_factory=InputContext)
    platform: str | None = None
    native_role: str | None = None
    actions_complete: bool = False
    actuator: SelectedActuator | None = None
    # Common-layer structural evidence; not another native wire permission.
    has_submenu: bool = False
    # None keeps legacy drivers on the conservative menu ancestry path.
    application_menu: bool | None = None
    receiver: SelectedReceiver | None = None

    def __post_init__(self):
        if len({p.id for p in self.primitives}) != len(self.primitives):
            raise ValueError("Duplicate primitive id")


@dataclass(frozen=True)
class HostPolicy:
    mode: str = "shared"
    foreground_assist: bool = False

    def __post_init__(self):
        if self.mode not in ("shared", "exclusive"):
            raise ValueError("Unknown desktop mode")

    @property
    def foreground_allowed(self):
        return self.mode == "exclusive" or self.foreground_assist


@dataclass(frozen=True)
class UnitCommand:
    primitive: Primitive
    target: OperationTarget
    intent: Intent
    actuator: SelectedActuator | None = None
    prepare: bool = False
    foreground: bool = False
    receiver: SelectedReceiver | None = None


@dataclass(frozen=True)
class StepResult:
    target: OperationTarget
    dispatch: Dispatch
    detail: str
    preparation: tuple[str, ...] = ()
    confirmation: Confirmation = Confirmation.NOT_REQUESTED
    cleanup: Cleanup = Cleanup.COMPLETE
    error_code: str | None = None
    # Only an explicit pre-dispatch native result permits another route.
    retry_reason: str | None = None
    context: Mapping[str, Any] | None = None
    application: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.retry_reason not in (None, "routeUnavailable", "preparationUnavailable", "preconditionChanged"):
            raise ValueError("Invalid replanning reason")
        if self.retry_reason is not None and (self.dispatch != Dispatch.NOT_DISPATCHED
                                              or self.cleanup != Cleanup.COMPLETE):
            raise ValueError("A possibly dispatched or unclean step cannot authorize retry")


class NativePort(Protocol):
    policy: HostPolicy
    calls: "CallLifecycle"

    @property
    def platform(self) -> str:
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        ...

    async def begin(self, intent: Intent, target: OperationTarget, timeout_ms: int) -> OpenedOperation:
        ...

    async def inspect(self, operation: str, target: OperationTarget, timeout_ms: int, *, within: tuple[str, ...] = (),
                      actuator: SelectedActuator | None = None, receiver: SelectedReceiver | None = None,
                      basis: tuple[SelectionDependency, ...] = ()) -> Facts:
        ...

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def nodes(self, operation: str, target: Binding, fields: list[str], max_nodes: int, max_depth: int,
                    timeout_ms: int, *, children_page: dict | None = None, relation_page: dict | None = None,
                    advertised_fields: list[str] | None = None, follow_relations: list[str] | None = None) -> dict:
        ...

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def relation(self, operation: str, target: Binding, relation_name: str, cursor: str | None, limit: int,
                       timeout_ms: int) -> dict:
        ...

    # Explicit native/observation contract fields stay aligned with their caller.
    # pylint: disable-next=huawei-too-many-arguments
    async def captured_text(self, operation: str, target: Binding, text_ref: str, offset: int, length: int,
                            timeout_ms: int) -> dict:
        ...

    async def capture(self, operation: str, target: Binding, max_dimension: int, timeout_ms: int, *,
                      kind: str = 'window') -> dict:
        ...

    async def execute(self, operation: str, step: int, command: UnitCommand, timeout_ms: int) -> StepResult:
        ...

    async def finish(self, operation: str) -> Cleanup:
        ...
