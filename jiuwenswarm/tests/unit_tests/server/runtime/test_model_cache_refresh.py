"""团队版请求未命中时，从当前配置重建模型缓存。"""

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_resolve_model_reloads_cache_for_newly_saved_model(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._global_index_to_cache_key = {}
    adapter._last_models_config_fingerprint = None
    adapter._model = None
    adapter._model_client_config = None
    adapter._model_request_config = None
    adapter._selected_model_context_window_tokens = None
    adapter._default_model_name = ""
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

    resolved = adapter._resolve_model_by_name("new-model")

    assert resolved is not None
    assert JiuWenSwarmDeepAdapter._model_looks_usable(resolved)
    assert adapter._has_valid_model_config("new-model")
