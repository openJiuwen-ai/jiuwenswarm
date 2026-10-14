_handler = None


def setObservationHandler(handler):
    global _handler
    _handler = handler


def observationCompleted(observation):
    if _handler: _handler(dict(kind="observed", observation=observation))


def observationsInvalidated(source):
    if _handler: _handler(dict(kind="invalidate", source=source))


def pendingObservationsDiscarded(source):
    if _handler: _handler(dict(kind="discard-pending", source=source))
