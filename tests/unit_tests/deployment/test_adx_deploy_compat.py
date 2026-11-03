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
            path.with_name(".env").write_text("MODEL_NAME='original'\n")
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
            self.assertEqual(value["sandbox"]["url"], "${JIUWENSWARM_ADX_NODE_INGRESS_URL}")
            self.assertIn("MODEL_NAME='original'", path.with_name(".env").read_text())
            self.assertIn("ADX_TOKEN=", path.with_name(".env").read_text())
            self.assertEqual(path.with_name(".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(sentinel.read_bytes(), b"retained-session-fixture")
        self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
        self.assertIn('JIUWENSWARM_ADX_NODE_INGRESS_URL="https://node.example:8443"', environment.read_text())

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



if __name__ == "__main__":
    unittest.main()
