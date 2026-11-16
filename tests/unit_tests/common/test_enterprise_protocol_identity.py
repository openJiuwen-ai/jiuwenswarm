"""Exercise mixed Gateway / AgentServer protocol consumers after integration."""

import pytest

from jiuwenswarm.common.schema.req_method import ReqMethod as LocalReqMethod
from jiuwenswarm.common.schema.message import ReqMethod


@pytest.mark.parametrize("name", [
    "CHAT_SEND", "A4P_CONFIG_GET", "A4P_CONFIG_UPDATE", "WORKSPACE_TREE",
    "FILE_READ_UPLOAD_CHUNK", "SKILLS_ENTERPRISE_INSTALL",
])
def test_rpc_members_share_identity_across_local_consumers(name):
    local = getattr(ReqMethod, name)
    assert ReqMethod is LocalReqMethod
    assert LocalReqMethod(local.value) is local


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
