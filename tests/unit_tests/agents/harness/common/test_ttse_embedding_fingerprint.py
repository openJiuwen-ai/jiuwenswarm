"""Team and single-agent embedding clients must share one fingerprint."""

from jiuwenswarm.agents.harness.common.memory.embeddings import (
    OpenAICompatibleEmbeddingProvider as SwarmEmbedding,
)
from openjiuwen.core.memory.lite.embeddings import (
    OpenAICompatibleEmbeddingProvider as CoreEmbedding,
)


def _fingerprint(provider):
    getter = provider.config_fingerprint
    if callable(getter):
        return getter()
    return getter


def test_swarm_embedding_fingerprint_matches_core_for_equivalent_urls():
    key = "test-key"
    model = "bge-m3"
    urls = [
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
        "https://api.example.com/v1/embeddings",
        "https://api.example.com/v1/embeddings/",
        "  https://api.example.com/v1/  ",
    ]
    expected = _fingerprint(
        CoreEmbedding(api_key=key, model=model, base_url="https://api.example.com/v1")
    )
    for url in urls:
        swarm = SwarmEmbedding(api_key=key, model=model, base_url=url)
        core = CoreEmbedding(api_key=key, model=model, base_url=url)
        assert swarm.base_url == core.base_url == "https://api.example.com/v1"
        assert _fingerprint(swarm) == _fingerprint(core) == expected


def test_embedding_fingerprint_changes_when_key_or_model_changes():
    left = SwarmEmbedding(api_key="a", model="bge-m3", base_url="https://api.example.com/v1/")
    right = SwarmEmbedding(api_key="b", model="bge-m3", base_url="https://api.example.com/v1")
    other_model = SwarmEmbedding(api_key="a", model="other", base_url="https://api.example.com/v1")
    assert _fingerprint(left) != _fingerprint(right)
    assert _fingerprint(left) != _fingerprint(other_model)
