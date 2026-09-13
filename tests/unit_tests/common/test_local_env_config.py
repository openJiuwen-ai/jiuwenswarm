import os
from unittest.mock import MagicMock, patch
from jiuwenswarm.common.local_env_config import (
    BUSINESS_MIRROR_KEYS,
    get_local_config,
    is_sensitive_env_name,
    set_local_config,
    decrypt,
    encrypt,
    ENV_CONFIG_DICT,
    SPAWN_ENV_KEYS,
    bind_task_env_overlay,
    read_env,
    reset_task_env_overlay,
    set_os_environ,
)


class TestLocalEnvConfig:
    def setup_method(self):
        ENV_CONFIG_DICT.clear()

    def teardown_method(self):
        ENV_CONFIG_DICT.clear()

    def test_get_local_config_from_env_dict(self):
        ENV_CONFIG_DICT["TEST_KEY"] = "test_value"
        assert get_local_config("TEST_KEY") == "test_value"

    def test_get_local_config_from_os_environ(self):
        os.environ["TEST_KEY"] = "test_value"
        try:
            assert get_local_config("TEST_KEY") == "test_value"
        finally:
            del os.environ["TEST_KEY"]

    def test_get_local_config_default(self):
        assert get_local_config("NON_EXISTENT_KEY", "default") == "default"

    def test_set_local_config(self):
        set_local_config("TEST_KEY", "test_value")
        assert ENV_CONFIG_DICT["TEST_KEY"] == "test_value"

    def test_set_local_config_none_value(self):
        ENV_CONFIG_DICT["TEST_KEY"] = "test_value"
        set_local_config("TEST_KEY", None)
        assert "TEST_KEY" not in ENV_CONFIG_DICT

    def test_set_local_config_empty_string(self):
        ENV_CONFIG_DICT["TEST_KEY"] = "test_value"
        set_local_config("TEST_KEY", "")
        assert "TEST_KEY" not in ENV_CONFIG_DICT

    def test_set_local_config_zero_kept(self):
        set_local_config("TEST_KEY", 0)
        assert ENV_CONFIG_DICT["TEST_KEY"] == "0"

    def test_set_local_config_false_kept(self):
        set_local_config("TEST_KEY", False)
        assert ENV_CONFIG_DICT["TEST_KEY"] == "False"

    def test_decrypt_without_crypto_provider(self):
        result = decrypt("TEST_KEY", "cipher_text")
        assert result == "cipher_text"

    def test_decrypt_with_crypto_provider(self):
        mock_crypto = MagicMock()
        mock_crypto.decrypt.return_value = "decrypted_text"
        mock_registry = MagicMock()
        mock_registry.get_crypto_provider.return_value = mock_crypto
        
        with patch("jiuwenswarm.common.local_env_config.sys.modules") as mock_modules:
            mock_modules.get.return_value = MagicMock(ExtensionRegistry=MagicMock(get_instance=MagicMock(return_value=mock_registry)))
            result = decrypt("API_KEY", "cipher_text")
            assert result == "decrypted_text"
            mock_crypto.decrypt.assert_called_once_with("cipher_text")

    def test_decrypt_non_sensitive_key(self):
        mock_crypto = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_crypto_provider.return_value = mock_crypto
        
        with patch("jiuwenswarm.common.local_env_config.sys.modules") as mock_modules:
            mock_modules.get.return_value = MagicMock(ExtensionRegistry=MagicMock(get_instance=MagicMock(return_value=mock_registry)))
            result = decrypt("NON_SENSITIVE_KEY", "plain_text")
            assert result == "plain_text"
            mock_crypto.decrypt.assert_not_called()

    def test_encrypt_without_crypto_provider(self):
        result = encrypt("TEST_KEY", "plain_text")
        assert result == "plain_text"

    def test_encrypt_with_crypto_provider(self):
        mock_crypto = MagicMock()
        mock_crypto.encrypt.return_value = "encrypted_text"
        mock_registry = MagicMock()
        mock_registry.get_crypto_provider.return_value = mock_crypto
        
        with patch("jiuwenswarm.common.local_env_config.sys.modules") as mock_modules:
            mock_modules.get.return_value = MagicMock(ExtensionRegistry=MagicMock(get_instance=MagicMock(return_value=mock_registry)))
            result = encrypt("API_KEY", "plain_text")
            assert result == "encrypted_text"
            mock_crypto.encrypt.assert_called_once_with("plain_text")

    def test_encrypt_non_sensitive_key(self):
        mock_crypto = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_crypto_provider.return_value = mock_crypto
        
        with patch("jiuwenswarm.common.local_env_config.sys.modules") as mock_modules:
            mock_modules.get.return_value = MagicMock(ExtensionRegistry=MagicMock(get_instance=MagicMock(return_value=mock_registry)))
            result = encrypt("NON_SENSITIVE_KEY", "plain_text")
            assert result == "plain_text"
            mock_crypto.encrypt.assert_not_called()

    def test_deepresearch_python_executable_is_not_a_runtime_config_source(self):
        key = "DEEPRESEARCH_PYTHON_EXECUTABLE"
        assert key not in BUSINESS_MIRROR_KEYS
        assert key not in SPAWN_ENV_KEYS
        assert is_sensitive_env_name(key) is False

    def test_code_coauthor_header_switch_is_spawn_shared(self):
        assert "JIUWENSWARM_CODE_COAUTHOR_HEADER_ENABLED" in SPAWN_ENV_KEYS

    def test_set_os_environ_patches_bound_overlay_for_same_request_reads(self):
        """Enterprise vision apply writes tip under seal; readers must see it."""
        from jiuwenswarm.common.local_env_config import (
            bind_agent_env_ns,
            build_effective_env_overlay,
            clear_agent_env_ns,
            replace_active_env,
            reset_agent_env_ns,
        )
        from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
            apply_vision_model_config_from_yaml,
        )

        sid, aid = "overlay-svc", "overlay-agent"
        clear_agent_env_ns(sid, aid)
        replace_active_env({}, service_id=sid, agent_id=aid)
        ns = bind_agent_env_ns(sid, aid)
        overlay = build_effective_env_overlay(None, service_id=sid, agent_id=aid)
        assert "VISION_API_KEY" not in overlay
        ot = bind_task_env_overlay(overlay)
        try:
            apply_vision_model_config_from_yaml(
                {
                    "models": {
                        "vision": {
                            "model_client_config": {
                                "api_key": "vk-live",
                                "api_base": "http://vision.example/v1",
                                "model_name": "Qwen3.7-Plus",
                                "client_provider": "OpenAI",
                            }
                        }
                    }
                }
            )
            assert read_env("VISION_API_KEY") == "vk-live"
            assert read_env("VISION_API_BASE") == "http://vision.example/v1"
            assert read_env("VISION_MODEL_NAME") == "Qwen3.7-Plus"
        finally:
            reset_task_env_overlay(ot)
            reset_agent_env_ns(ns)
            clear_agent_env_ns(sid, aid)

    def test_set_os_environ_none_removes_from_bound_overlay(self):
        """Delete path must pop canonical (+ product legacy) keys from seal."""
        from jiuwenswarm.common.local_env_config import (
            bind_agent_env_ns,
            build_effective_env_overlay,
            clear_agent_env_ns,
            read_env_if_set,
            replace_active_env,
            reset_agent_env_ns,
        )

        sid, aid = "overlay-svc", "overlay-agent"
        clear_agent_env_ns(sid, aid)
        replace_active_env(
            {
                "VISION_API_KEY": "stale",
                "JIUWENSWARM_DISABLED_SKILLS": "skill-a",
            },
            service_id=sid,
            agent_id=aid,
        )
        ns = bind_agent_env_ns(sid, aid)
        overlay = build_effective_env_overlay(None, service_id=sid, agent_id=aid)
        overlay["VISION_API_KEY"] = "sealed-old"
        overlay["JIUWENSWARM_DISABLED_SKILLS"] = "sealed-canon"
        overlay["JIUWENCLAW_DISABLED_SKILLS"] = "sealed-legacy"
        ot = bind_task_env_overlay(overlay)
        try:
            set_os_environ("VISION_API_KEY", None)
            assert read_env("VISION_API_KEY") == ""
            assert read_env_if_set("VISION_API_KEY") is None

            set_os_environ("JIUWENSWARM_DISABLED_SKILLS", None)
            assert read_env("JIUWENSWARM_DISABLED_SKILLS") == ""
            assert read_env("JIUWENCLAW_DISABLED_SKILLS") == ""
            assert read_env_if_set("JIUWENSWARM_DISABLED_SKILLS") is None
        finally:
            reset_task_env_overlay(ot)
            reset_agent_env_ns(ns)
            clear_agent_env_ns(sid, aid)

    def test_set_os_environ_cross_ns_does_not_patch_bound_overlay(self):
        """Explicit foreign ns writes tip only; current seal must stay untouched."""
        from jiuwenswarm.common.local_env_config import (
            bind_agent_env_ns,
            build_effective_env_overlay,
            clear_agent_env_ns,
            effective_tip,
            replace_active_env,
            reset_agent_env_ns,
        )

        sid_b, aid_b = "svc-b", "agent-b"
        sid_a, aid_a = "svc-a", "agent-a"
        clear_agent_env_ns(sid_a, aid_a)
        clear_agent_env_ns(sid_b, aid_b)
        replace_active_env({}, service_id=sid_b, agent_id=aid_b)
        replace_active_env({}, service_id=sid_a, agent_id=aid_a)
        ns = bind_agent_env_ns(sid_b, aid_b)
        overlay = build_effective_env_overlay(None, service_id=sid_b, agent_id=aid_b)
        ot = bind_task_env_overlay(overlay)
        try:
            set_os_environ(
                "VISION_API_KEY",
                "key-of-a",
                service_id=sid_a,
                agent_id=aid_a,
            )
            assert "VISION_API_KEY" not in overlay
            assert read_env("VISION_API_KEY") == ""
            assert effective_tip(sid_a, aid_a).get("VISION_API_KEY") == "key-of-a"
        finally:
            reset_task_env_overlay(ot)
            reset_agent_env_ns(ns)
            clear_agent_env_ns(sid_a, aid_a)
            clear_agent_env_ns(sid_b, aid_b)

    def test_export_spawn_environ_keeps_process_path_without_tenant_credentials(self):
        from jiuwenswarm.common.local_env_config import export_spawn_environ

        with patch.dict(os.environ, {"PATH": "/process/bin"}, clear=True):
            set_os_environ(
                "LLM_API_KEY",
                "tenant-secret",
                service_id="svc",
                agent_id="agent",
            )
            set_os_environ(
                "API_KEY",
                "global-tenant-secret",
                service_id="svc",
                agent_id="agent",
            )
            set_os_environ(
                "PETAL_SEARCH_HEADERS",
                '{"Authorization":"tenant-secret"}',
                service_id="svc",
                agent_id="agent",
            )

            exported = export_spawn_environ()

        assert exported["PATH"] == "/process/bin"
        assert "LLM_API_KEY" not in exported
        assert "API_KEY" not in exported
        assert "PETAL_SEARCH_HEADERS" not in exported
        assert "tenant-secret" not in repr(exported)
