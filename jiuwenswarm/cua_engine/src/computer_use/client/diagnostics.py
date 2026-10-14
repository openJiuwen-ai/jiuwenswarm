from datetime import datetime, timezone
from ..errors import ComputerUseError

sdkVersion = "0.0.1"
_handler = None
_structured = False


def setDiagnosticHandler(handler):
    global _handler
    _handler = handler


def recordStructuredObservations(enabled):
    global _structured
    _structured = enabled


def wantsStructuredObservations(): return _structured


def diagnostic(event):
    try:
        if _handler: _handler({"timestamp":datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), **event})
    except Exception:
        pass  # Diagnostic failure must not change or replay desktop actions.


def localFailure(message, next, code="invalid_request", target="request"):
    failure = dict(code=code, target=target, effect="none", detail=message, next=next)
    error = ComputerUseError(failure)
    diagnostic(dict(kind="local_error", stage="validation", error=str(error), failure=failure))
    return error
