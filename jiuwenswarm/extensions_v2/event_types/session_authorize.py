# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude-shaped authorization values, validation and event registration."""

from collections.abc import Mapping
from typing import Literal, TypedDict

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

SESSION_AUTHORIZE_EVENT = "session.authorize"


class SessionAuthorization(TypedDict):
    """Only an opaque reference and its kind; never a credential."""

    handle: str
    kind: Literal["bearer", "api-key"]


class SessionAuthorizationError(RuntimeError):
    """A stable authorization failure with no credential-bearing diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"session authorization failed: {code}")


class SessionAuthorizationDenied(SessionAuthorizationError):
    """A deliberate policy refusal, distinct from absent credentials."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("DENIED")


def valid_authorize_input(value: object) -> bool:
    from jiuwenswarm.extensions_v2.session_authorization.dispatch import current_frame
    return current_frame() is not None and isinstance(value, Mapping) and not value


def valid_authorize_output(value: object) -> bool:
    from jiuwenswarm.extensions_v2.session_authorization.dispatch import current_frame
    frame = current_frame()
    if frame is None or not isinstance(value, Mapping):
        return False
    if set(value) == {"deny"}:
        return isinstance(value["deny"], str) and 1 <= len(value["deny"].strip()) <= 512
    if set(value) != {"value"}:
        return False
    result = value["value"]
    if result is None:
        return True
    if not isinstance(result, Mapping) or set(result) != {"handle", "kind"}:
        return False
    if not isinstance(result["handle"], str) or not result["handle"] or result["kind"] not in ("bearer", "api-key"):
        return False
    return frame.service.valid_result(frame, result)


def register_session_authorize_event(bus: ExtensionEventBus) -> None:
    from jiuwenswarm.extensions_v2.event_handlers.session_authorize import session_authorize_core
    bus.register_event_type(
        SESSION_AUTHORIZE_EVENT, match_pattern_subset, session_authorize_core,
        input_validator=valid_authorize_input, output_validator=valid_authorize_output,
    )
