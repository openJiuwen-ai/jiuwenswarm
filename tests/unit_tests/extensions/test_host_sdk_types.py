from jiuwenswarm.agents.harness.common import memory
from jiuwenswarm.common.schema import agent, message
from jiuwenswarm.extensions import channel_contributions
from jiuwenswarm.extensions.sdk import host_types
from jiuwenswarm.extensions.sdk import memory as memory_sdk


def test_public_host_types_keep_runtime_identity():
    assert host_types.AgentRequest is agent.AgentRequest
    assert host_types.AgentResponse is agent.AgentResponse
    assert host_types.Message is message.Message
    assert host_types.ChannelSpec is channel_contributions.ChannelSpec


def test_public_memory_services_keep_runtime_identity():
    assert memory_sdk.bm25_rank_to_score is memory.bm25_rank_to_score


def test_public_memory_services_follow_host_hooks(monkeypatch):
    monkeypatch.setattr(memory, "get_embed_config", lambda: {"api_key": "probe"})
    assert memory_sdk.get_embed_config()["api_key"] == "probe"
