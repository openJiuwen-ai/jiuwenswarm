"""Local observation gaps must not hide lost sessions or uncertain cleanup."""
from dataclasses import dataclass

from ..errors import ComputerUseError


@dataclass(frozen=True)
class ObservationGap:
    component: str
    code: str
    detail: str
    next: str


def observation_gap(component, error):
    return ObservationGap(component, error.code, error.detail, error.next)


async def collect_component(port, name, collect):
    generation = port.connection.session_identity
    try:
        return await collect(), None
    except ComputerUseError as error:
        unsafe_to_degrade = (port.connection.session_identity != generation or port.calls.retired
                             or error.effect != 'none'
                             or error.code not in ('observation_incomplete', 'execution_failed', 'stale_target',
                                                   'snapshot_expired', 'mode_unsupported'))
        if unsafe_to_degrade:
            raise
        return None, observation_gap(name, error)
