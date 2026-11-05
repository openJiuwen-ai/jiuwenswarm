"""团队版请求未命中时，从当前配置重建模型缓存。"""

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_resolve_model_reloads_cache_for_newly_saved_model(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    assert adapter.model_for_catalog_name("new-model") is None
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://example.test/v1",
                        "api_key": "sk-test",
                        "model_name": "new-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {},
                    "is_default": True,
                }
            ]
        },
        "react": {},
    }
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        lambda: config,
    )

    # Checking a warm session's requested profile goes through normal model resolution.
    adapter.skill_retrieval_model_profile_matches("new-model")
    resolved = adapter.model_for_catalog_name("new-model")

    assert resolved is not None
    assert resolved.model_config.model_name == "new-model"
    assert resolved.model_client_config.api_base == "https://example.test/v1"
    assert resolved.model_client_config.api_key == "sk-test"
