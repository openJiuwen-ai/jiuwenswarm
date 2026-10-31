"""Web wire names and typed Harness RPC names reach the same handler."""
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers


@pytest.mark.parametrize(('wire_name', 'method'), [
    ('harness.packages', ReqMethod.HARNESS_PACKAGES_GET),
    ('harness.packages.scan', ReqMethod.HARNESS_PACKAGES_SCAN),
    ('harness.activate', ReqMethod.HARNESS_PACKAGES_ACTIVATE),
    ('harness.deactivate', ReqMethod.HARNESS_PACKAGES_DEACTIVATE),
    ('harness.delete', ReqMethod.HARNESS_PACKAGES_DELETE),
])
def test_harness_typed_method_is_registered_as_wire_alias(wire_name, method):
    methods = {}
    channel = SimpleNamespace(
        channel_id='web',
        register_method=lambda name, handler: methods.setdefault(name, handler),
        on_connect=lambda handler: None,
        on_disconnect=lambda handler: None,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    assert method.value in methods
    assert methods[method.value] is methods[wire_name]
