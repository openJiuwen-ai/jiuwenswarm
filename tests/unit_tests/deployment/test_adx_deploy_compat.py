# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Package entrypoint, readiness and retained-user deployment contracts."""
import importlib.util
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("adx_integration_gateway", ROOT / "deploy/adx/configure.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = self.root / "tenant-key"
        self.key.write_text("test-only-fixture")
        self.settings = {
            "profile": "agentos", "shared_workspace_sqlite": True, "update_user_configs": True,
            "python": "/business/bin/python", "ca_file": str(self.root / "trust.pem"),
            "token_file": str(self.key), "tenant": "test", "namespace": "test",
            "ingress_url": "https://adx.example:8443", "node_ingress_url": "https://node.example:8443",
            "workspace_root": str(self.root / "users"), "shared_workspace_sqlite": False,
            "gateway": {"home": str(self.root / "home")},
            "runtime": {"user": pwd.getpwuid(os.getuid()).pw_name,
                        "agent_image": "agent:v1", "tool_image": "tool:v1"},
        }
        (self.root / "users").mkdir()

    def response(self):
        response = io.BytesIO(json.dumps({"items": []}).encode())
        response.status = 200
        return response

    def test_readiness_uses_tls_authenticated_adx_inventory(self):
        opener = Mock()
        opener.open.return_value = self.response()
        with patch.object(deploy.ssl, "create_default_context") as context, patch.object(deploy.urllib.request, "build_opener", return_value=opener):
            deploy.ready(self.settings, timeout=0)
        context.assert_called_once_with(cafile=self.settings["ca_file"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://adx.example:8443/api/instances?pageSize=1")
        self.assertIn("Authorization", request.headers)

    def test_readiness_auth_failure_is_terminal(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://adx.example", 403, "forbidden", {}, None)
        with patch.object(deploy.ssl, "create_default_context"), patch.object(deploy.urllib.request, "build_opener", return_value=opener), patch.object(deploy.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                deploy.ready(self.settings)
        sleep.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)

    def test_closed_adx_does_not_fall_back_to_frontend(self):
        opener = Mock()
        opener.open.side_effect = ConnectionRefusedError()
        with patch.object(deploy.ssl, "create_default_context"), patch.object(deploy.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "ADX API not ready"):
                deploy.ready(self.settings, timeout=0)
        self.assertNotIn("8888", opener.open.call_args.args[0].full_url)

    def test_redirect_cannot_forward_tenant_credential(self):
        self.assertIsNone(deploy.NoRedirect().redirect_request(None, None, 302, "found", {}, "https://other.example"))

    def test_plain_http_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            deploy.ready(dict(self.settings, ingress_url="http://adx.example"))

    def test_existing_model_iam_session_and_workspace_are_preserved(self):
        config = self.root / "home/.jiuwenswarm/config/config.yaml"
        user_config = self.root / "users/alice/.jiuwenswarm/config/config.yaml"
        original = {"models": [{"name": "original"}], "gateway": {"agentos": {"auth_enabled": True}}}
        for path in (config, user_config):
            deploy.write_yaml(path, original)
            path.with_name(".env").write_text(
                "MODEL_NAME='original'\nTOOL_SANDBOX_CPU=2000\nTOOL_SANDBOX_MEMORY=4096\n"
            )
        sentinel = self.root / "users/alice/session.db"
        sentinel.write_bytes(b"retained-session-fixture")
        with patch.object(deploy, "check_business"), patch.object(deploy.subprocess, "run") as run:
            environment = deploy.configure(self.settings)
            deploy.configure(self.settings)
        run.assert_not_called()  # Existing configuration must not run force init.
        for path in (config, user_config):
            value = deploy.read_yaml(path)
            self.assertEqual(value["models"], original["models"])
            self.assertTrue(value["gateway"]["agentos"]["auth_enabled"])
            self.assertEqual(value["sandbox"]["runtime"], "conch")
            self.assertEqual(value["sandbox"]["cpu"], 2000)
            self.assertEqual(value["sandbox"]["cpu_limit"], 2000)
            self.assertEqual(value["sandbox"]["memory"], 4096)
            self.assertEqual(value["sandbox"]["mem_limit"], 4096)
            self.assertNotIn("url", value["sandbox"])
            env_text = path.with_name(".env").read_text()
            self.assertIn("MODEL_NAME='original'", env_text)
            self.assertNotIn("TOOL_SANDBOX_CPU=", env_text)
            self.assertNotIn("TOOL_SANDBOX_MEMORY=", env_text)
            self.assertIn("ADX_TOKEN=", path.with_name(".env").read_text())
            self.assertEqual(path.with_name(".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(sentinel.read_bytes(), b"retained-session-fixture")
        self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("JIUWENSWARM_ADX_NODE_INGRESS_URL=", environment.read_text())

    def test_regeneration_preserves_supported_customization_and_removes_noops(self):
        self.settings["shared_workspace_sqlite"] = True
        original = {"sandbox": {"type": "adx-environment", "cpu": 1200, "workdir": "/chosen",
                                "mounts": [{"host_path": "/shared/data", "target": "/data", "options": ["ro"]}]},
                    "gateway": {"adx": {"ssh_known_hosts": "/trust/hosts", "connect_timeout_seconds": 42},
                                "agentos": {"ssh": {"user_template": "old-{user}", "port": 2222}}},
                    "checkpoint": {"sqlite_journal_mode": "DELETE", "db_type": "sqlite"},
                    "workspace_sqlite": {"journal_mode": "DELETE"},
                    "modes": {"team": {"code": {"storage": {"type": "sqlite", "params": {"path": "/keep"}}}}}}
        result = deploy.gateway_config(original, self.settings)
        self.assertEqual(result["sandbox"]["cpu"], 1200)
        self.assertEqual(result["sandbox"]["cpu_limit"], 1200)
        self.assertEqual(result["sandbox"]["mounts"], original["sandbox"]["mounts"])
        self.assertEqual(result["gateway"]["adx"]["ssh_known_hosts"], "/trust/hosts")
        self.assertEqual(result["gateway"]["adx"]["connect_timeout_seconds"], 42)
        self.assertEqual(result["gateway"]["agentos"]["ssh"], {"port": 2222})
        self.assertEqual(result["checkpoint"], {"db_type": "sqlite"})
        self.assertNotIn("workspace_sqlite", result)
        self.assertNotIn("startup_mode", result["sandbox"])
        self.assertEqual(result["modes"]["team"]["code"]["storage"]["params"],
                         {"path": "/keep", "db_enable_wal": False, "mmap_size_mb": 0})
        self.assertEqual(deploy.gateway_config(result, self.settings), result)


    def test_unsupported_user_policy_fails_before_changing_existing_files(self):
        config = self.root / "home/.jiuwenswarm/config/config.yaml"
        user = self.root / "users/alice/.jiuwenswarm/config/config.yaml"
        deploy.write_yaml(config, {"models": [{"name": "original"}]})
        deploy.write_yaml(user, {"sandbox": {"type": "jiuwenbox", "policy_file": "/old/policy"}})
        before = {path: path.read_bytes() for path in (config, user)}
        with patch.object(deploy, "check_business"), self.assertRaisesRegex(ValueError, "alice.*policy_file"):
            deploy.configure(self.settings)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertFalse(path.with_name(".env").exists())

    def test_legacy_env_migrates_effective_resources_and_removes_duplicate_inputs(self):
        from dotenv import dotenv_values

        config = self.root / "home/.jiuwenswarm/config/config.yaml"
        deploy.write_yaml(config, {"sandbox": {"type": "adx-environment", "cpu": 2000, "cpu_limit": 2000}})
        environment = config.with_name(".env")
        environment.write_text("TOOL_SANDBOX_CPU=3000\nTOOL_SANDBOX_MEMORY=8192\n"
                               "TOOL_SANDBOX_IDLE_TIMEOUT=0\nTOOL_SANDBOX_TYPE=yuanrong\n"
                               "TOOL_SANDBOX_IMAGE=old-image\nGATEWAY_PORT=1234\n"
                               "ADX_GATEWAY_TOKEN=old-fixture\nMODEL_NAME=keep\nCUSTOM_APP_VALUE=keep\n")
        with patch.object(deploy, "check_business"):
            deploy.configure(self.settings)
            before = config.read_bytes()
            deploy.configure(self.settings)
        self.assertEqual(config.read_bytes(), before)
        value = deploy.read_yaml(config)
        self.assertEqual(value["sandbox"]["cpu"], 3000)
        self.assertEqual(value["sandbox"]["cpu_limit"], 3000)
        self.assertEqual(value["sandbox"]["memory"], 8192)
        self.assertEqual(value["sandbox"]["mem_limit"], 8192)
        self.assertEqual(value["sandbox"]["idle_timeout"], 0)
        self.assertEqual(value["gateway"]["adx"]["token_env"], "ADX_TOKEN")
        actual = dotenv_values(environment)
        self.assertEqual(actual["MODEL_NAME"], "keep")
        self.assertEqual(actual["CUSTOM_APP_VALUE"], "keep")
        self.assertIn("ADX_TOKEN", actual)
        self.assertFalse(set(actual) & set(deploy._OBSOLETE_APPLICATION_ENV))
        self.assertFalse(any(name in actual for name, _, _ in deploy._TOOL_ENV_SETTINGS))

    def test_invalid_legacy_resource_env_does_not_modify_existing_configuration(self):
        config = self.root / "home/.jiuwenswarm/config/config.yaml"
        deploy.write_yaml(config, {"sandbox": {"type": "adx-environment"}})
        environment = config.with_name(".env")
        environment.write_text("TOOL_SANDBOX_CPU=invalid\n")
        before = {path: path.read_bytes() for path in (config, environment)}
        with patch.object(deploy, "check_business"), self.assertRaisesRegex(ValueError, "TOOL_SANDBOX_CPU"):
            deploy.configure(self.settings)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_missing_config_initialization_preserves_existing_workspace(self):
        sentinel = self.root / "home/.jiuwenswarm/history.db"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"retained-history")

        def initialize(command, **kwargs):
            self.assertNotIn("-f", command)
            self.assertNotIn("--force", command)
            deploy.write_yaml(sentinel.parent / "config/config.yaml", {})

        with patch.object(deploy, "check_business"), patch.object(
                deploy.subprocess, "run", side_effect=initialize) as run:
            deploy.configure(self.settings)
        run.assert_called_once()
        self.assertEqual(sentinel.read_bytes(), b"retained-history")

    def test_managed_profile_removes_persisted_node_addresses(self):
        from dotenv import dotenv_values

        # Old explicit True must not persist a node address into a shared user home.
        self.settings["persist_user_node_ingress_url"] = True
        config = self.root / "home/.jiuwenswarm/config/config.yaml"
        user_config = self.root / "users/alice/.jiuwenswarm/config/config.yaml"
        for path in (config, user_config):
            deploy.write_yaml(path, {})
        config.with_name(".env").write_text("JIUWENSWARM_ADX_NODE_INGRESS_URL=https://old-master:8443\n")
        self.settings.pop("node_ingress_url")
        user_env = user_config.with_name(".env")
        user_env.write_text("MODEL_NAME='original'\nJIUWENSWARM_ADX_NODE_INGRESS_URL='https://old:8443'\n")
        owner = user_env.stat()
        with patch.object(deploy, "check_business"):
            service_env = deploy.configure(self.settings)
            deploy.configure(self.settings)
        self.assertNotIn("JIUWENSWARM_ADX_NODE_INGRESS_URL", dotenv_values(user_env))
        self.assertEqual(dotenv_values(user_env)["MODEL_NAME"], "original")
        self.assertEqual((user_env.stat().st_uid, user_env.stat().st_gid), (owner.st_uid, owner.st_gid))
        self.assertEqual(user_env.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("JIUWENSWARM_ADX_NODE_INGRESS_URL", dotenv_values(service_env))



if __name__ == "__main__":
    unittest.main()
