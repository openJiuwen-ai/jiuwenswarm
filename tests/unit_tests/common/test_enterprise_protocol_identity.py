"""Exercise mixed Gateway / AgentServer protocol consumers after integration."""

import pytest
import e2a.message as protocol_message
from e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod

@pytest.mark.parametrize("name", [
    "CHAT_SEND", "A4P_CONFIG_GET", "A4P_CONFIG_UPDATE", "WORKSPACE_TREE",
    "FILE_READ_UPLOAD_CHUNK", "SKILLS_ENTERPRISE_INSTALL",
])
def test_swarm_rpc_members_cross_protocol_as_strings(name):
    local = getattr(ReqMethod, name)
    assert not hasattr(protocol_message, "ReqMethod")
    envelope = e2a_from_agent_fields(request_id="identity", req_method=local)
    assert envelope.method == local.value
    assert ReqMethod(envelope.method) is local


@pytest.mark.asyncio
async def test_application_plugin_awaits_host_attachment_persistence():
    from agent_gateway.extensions.sdk import ApplicationPluginServices
    from jiuwenswarm.extensions.sdk import ApplicationPluginServices as LegacyServices

    assert LegacyServices is ApplicationPluginServices
    params = {}

    async def persist(items, session_id):
        items["persisted_session"] = session_id

    service = ApplicationPluginServices(media_attachment_normalizer=persist)
    await service.normalize_media_attachments(params, "session-1030")
    assert params == {"persisted_session": "session-1030"}
