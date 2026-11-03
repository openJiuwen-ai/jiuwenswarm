"""Exercise mixed Gateway / AgentServer protocol consumers after integration."""

import pytest

from openjiuwen_protocol.message import ReqMethod as ProtocolReqMethod
from jiuwenswarm.common.schema.message import ReqMethod


@pytest.mark.parametrize("name", [
    "CHAT_SEND", "A4P_CONFIG_GET", "A4P_CONFIG_UPDATE", "WORKSPACE_TREE",
    "FILE_READ_UPLOAD_CHUNK", "SKILLS_ENTERPRISE_INSTALL",
])
def test_rpc_members_share_identity_across_protocol_consumers(name):
    local = getattr(ReqMethod, name)
    assert ReqMethod is ProtocolReqMethod
    assert ProtocolReqMethod(local.value) is local


@pytest.mark.asyncio
async def test_application_plugin_awaits_host_attachment_persistence():
    from gateway.extensions.sdk import ApplicationPluginServices
    from jiuwenswarm.extensions.sdk import ApplicationPluginServices as LegacyServices

    assert LegacyServices is ApplicationPluginServices
    params = {}

    async def persist(items, session_id):
        items["persisted_session"] = session_id

    service = ApplicationPluginServices(media_attachment_normalizer=persist)
    await service.normalize_media_attachments(params, "session-1030")
    assert params == {"persisted_session": "session-1030"}
