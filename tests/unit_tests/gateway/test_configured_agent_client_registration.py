"""Gateway startup validates configuration without depending on a backend class."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.app_gateway import _validate_agent_client_registration


def extension(kind, client=object()):
    return SimpleNamespace(metadata=SimpleNamespace(id=kind), get_client=lambda: client)


@pytest.mark.parametrize('config', [None, {}, {'gateway': {'agent_client': {'type': 'websocket'}}}])
def test_default_websocket_keeps_implicit_registration(config):
    _validate_agent_client_registration(config, None)


@pytest.mark.parametrize('kind', ['agentos_router', 'custom_extension'])
def test_configured_extension_uses_the_registered_interface(kind):
    _validate_agent_client_registration({'gateway': {'agent_client': {'type': kind}}}, extension(kind))


@pytest.mark.parametrize('registered', [None, extension('other'), extension('agentos_router', None)])
def test_missing_or_wrong_configured_extension_stops_startup(registered):
    with pytest.raises(RuntimeError, match='not registered'):
        _validate_agent_client_registration({'gateway': {'agent_client': {'type': 'agentos_router'}}}, registered)


def test_retained_service_requirement_cannot_select_a_different_client():
    with pytest.raises(RuntimeError, match='not registered'):
        _validate_agent_client_registration({}, extension('agentos_router'), required_type='agentos_router')


def test_previous_service_argument_remains_parseable():
    result = subprocess.run([sys.executable, '-m', 'jiuwenswarm.gateway.app_gateway',
                             '--require-adx-router', '--help'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--require-adx-router' not in result.stdout
