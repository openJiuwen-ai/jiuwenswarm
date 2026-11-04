from datetime import datetime, timezone

from ..errors import ComputerUseError

SDK_VERSION = "0.0.1"
_handler = None


def set_diagnostic_handler(handler):
    global _handler
    _handler = handler


def diagnostic(event):
    try:
        if _handler:
            _handler({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                **event
            })
    except Exception:
        pass  # Diagnostic failure must not change or replay desktop actions.


def local_failure(message, next_step, code="invalid_request", target="request"):
    failure = dict(code=code, target=target, effect="none", detail=message, next=next_step)
    error = ComputerUseError(failure)
    diagnostic(dict(kind="local_error", stage="validation", error=str(error), failure=failure))
    return error
