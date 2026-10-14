from .actions import setActionHandler
from .observations import setObservationHandler
from .preview import setPreviewHandler
from .diagnostics import setDiagnosticHandler, recordStructuredObservations
from .execution import setExecutionGuard
from .handles import setHandleHandler
from .result_presentation import setTextPageHandler


def computerHost(factory=None):
    """Own one fresh public Computer and its session lifecycle for a host task."""
    from .._identity import Record
    if factory is None:
        from .. import Computer
        factory = Computer
    computer = factory()
    async def close():
        transport = computer._controller.transport
        if hasattr(transport, "close"): transport.close()
        if hasattr(transport, "waitClosed"): await transport.waitClosed()
    return Record(computer=computer, close=close)
