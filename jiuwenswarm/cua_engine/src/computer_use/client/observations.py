_handler = None


def set_observation_handler(handler):
    global _handler
    _handler = handler


def observation_completed(observation):
    if _handler:
        _handler(dict(kind="observed", observation=observation))


def observations_invalidated(source):
    if _handler:
        _handler(dict(kind="invalidate", source=source))


def pending_observations_discarded(source):
    if _handler:
        _handler(dict(kind="discard-pending", source=source))
