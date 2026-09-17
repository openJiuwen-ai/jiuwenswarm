from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    format_ws_diagnostics,
)


def test_format_ws_exception_diagnostics_includes_close_fields():
    exc = ConnectionClosedError(None, None)

    text = format_ws_diagnostics(describe_ws_exception(exc))

    assert "exc_type='ConnectionClosedError'" in text
    assert "message='no close frame received or sent'" in text
    assert "close_code=1006" in text
    assert "close_reason=''" in text
    assert "rcvd=None" in text
    assert "sent=None" in text
