"""Session negotiation and structured failures for native protocol 5."""
import os

from ..errors import ComputerUseError, public_error_code
from .action_context import decode_action_context
from .primitives import finite, nonempty, record, safe_integer

PROTOCOL_VERSION = 5


def remember_failure_context(error, context=None):
    if context is not None:
        # SDK host/registry code shares state owned by these exact handle objects.
        # pylint: disable-next=protected-access
        error._failure_context = context


def failure_context(error):
    return getattr(error, "_failure_context", None) if isinstance(error, BaseException) else None


def desktop_mode():
    mode = os.environ.get("COMPUTER_USE_DESKTOP", "shared")
    if mode not in ("shared", "exclusive"):
        raise ComputerUseError(
            dict(code="invalid_request", effect="none", target="COMPUTER_USE_DESKTOP",
                 detail="COMPUTER_USE_DESKTOP must be shared or exclusive",
                 next="correct the host desktop mode configuration before connecting"))
    return mode


def decode_failure(value):
    has_context = isinstance(value, dict) and "context" in value
    error = record(value, ["code", "target", "effect", "detail", "next", *(["context"] if has_context else [])])
    for key in ("code", "target", "detail", "next"):
        nonempty(error[key], "protocol error " + key)
    if error["effect"] not in ("none", "possible", "partial"):
        raise ValueError("protocol: invalid error effect")
    if has_context:
        error["context"] = decode_action_context(error["context"])
    return error


def failure_error(method, failure):
    error = ComputerUseError({**failure, "code": public_error_code(failure["code"])}, method=method)
    remember_failure_context(error, failure.get("context"))
    return error


def decode_hello(value, mode):
    error_reply = isinstance(value, dict) and "error" in value
    reply = record(value, ["version", "session", "id", "error" if error_reply else "result"])
    incompatible_hello = (not finite(reply["version"]) or reply["version"] != PROTOCOL_VERSION
                          or not finite(reply["id"]) or reply["id"] != 0)
    if incompatible_hello:
        raise ValueError("protocol: incompatible hello version or id")
    nonempty(reply["session"], "protocol session")
    if error_reply:
        raise failure_error("hello", decode_failure(reply["error"]))
    result = record(reply["result"], ["platform", "desktopMode", "maxTimeoutMs", "capabilities"])
    nonempty(result["platform"], "protocol platform")
    if result["desktopMode"] != mode:
        raise ValueError("protocol: backend desktop mode does not match the requested mode")
    if not safe_integer(result["maxTimeoutMs"]) or not 1 <= result["maxTimeoutMs"] <= 120_000:
        raise ValueError("protocol: invalid maximum request timeout")
    capabilities = result["capabilities"]
    if not isinstance(capabilities, list) or any(not isinstance(v, str) or not v
                                                 for v in capabilities) or len(set(capabilities)) != len(capabilities):
        raise ValueError("protocol: invalid capabilities")
    required = ("cancel", "requestDedup", "nativeOperations")
    if 'nativeBoundContracts' in capabilities:
        required += ('desktopCoordination', )
    for capability in required:
        if capability not in capabilities:
            raise ValueError(f"protocol: backend is missing {capability}")
    return dict(session=reply["session"], platform=result["platform"], desktopMode=mode,
                maxTimeoutMs=result["maxTimeoutMs"], capabilities=tuple(capabilities))
